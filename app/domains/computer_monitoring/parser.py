r"""computer_monitoring 的行解析（原始行 → 原始字段）。

只用标准库 `re` + `datetime`，**不调 LLM、不猜**（红线 1）。覆盖 `logs/` 里
真实存在的四类时间戳形态：

    Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication failure; ...
    Jun 15 04:06:20 combo logrotate: ALERT exited abnormally with [1]
    Jul  1 09:29:02 host sandboxd[129] ([31211]): com.apple...deny network-outbound
    2015-07-29 17:41:44,747 - INFO  [QuorumPeer[myid=1]/...@774] - msg
    [Sun Dec 04 04:47:44 2005] [notice] ...            (Apache)

解析不了就返回 `None`，**不抛异常、不硬凑**——坏行要能被如实统计为坏行，
而不是被塞进一个看似正常的结构里（红线 4）。

两个曾经踩过的坑（都靠真实日志实测暴露，不是推测）：
- log4j 的 `[thread]` 里**可以嵌套方括号**（`[QuorumPeer[myid=1]/...]`），
  用 `[^\]]*` 会在内层 `]` 处截断，导致这些行整条解析失败；
- syslog 的 `[pid]` **不是必需的**（`logrotate: ALERT ...` 就没有），
  把它写成强制项会让这类行全部落空。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# `Jun 14 15:16:01`（`Jul  1` 是双空格）—— syslog 缺年份
_SYSLOG_TS = r"[A-Z][a-z]{2} {1,2}\d{1,2} \d{2}:\d{2}:\d{2}"
# `2015-07-29 17:41:44,747` —— Zookeeper log4j 风格，带毫秒
_LOG4J_TS = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}"
# `[Sun Dec 04 04:47:44 2005]` —— Apache error log
_APACHE_TS = r"\[[A-Z][a-z]{2} [A-Z][a-z]{2} {1,2}\d{1,2} \d{2}:\d{2}:\d{2} \d{4}\]"

# --- syslog：先切出 ts / host / 其余，再把「其余」拆成 proc / pid / msg ---
_RE_SYSLOG = re.compile(
    rf"^(?P<ts>{_SYSLOG_TS})\s+(?P<host>\S+)\s+(?P<rest>.*)$"
)
# 进程名字符集必须容得下真实日志里出现过的东西：
#   `sshd(pam_unix)`        带括号限定
#   `Microsoft Word`        含空格的多词名
#   `BezelServices 1.19`    名字里带空格与版本号
#   ` -- root`              syslog 守护进程用 ` -- ` 标记（如 cron/login）
# pid 可有可无；后面可能还有一个 `([31211])` 这样的附注组。
_RE_SYSLOG_REST = re.compile(
    r"^\s*(?P<proc>[^\[\]:]*?)"
    r"(?:\s*\[(?P<pid>\d+)\])?"
    r"(?:\s*\(\[(?P<extra>\d+)\]\))?"
    r"\s*:\s?(?P<msg>.*)$"
)

# --- log4j：`[thread]` 允许嵌套方括号 ---
_RE_LOG4J = re.compile(
    rf"^(?P<ts>{_LOG4J_TS})\s+-\s+(?P<level>[A-Z]+)\s+"
    rf"\[(?P<thread>(?:[^\[\]]|\[[^\]]*\])*)\]\s+-\s+(?P<msg>.*)$"
)

# --- Apache error log ---
_RE_APACHE = re.compile(
    rf"^(?P<ts>{_APACHE_TS})\s+\[(?P<level>[a-z]+)\]\s+(?P<msg>.*)$"
)

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

#: 日志等级词 → 本项目三档 severity
_LEVEL_TO_SEVERITY = {
    "fatal": "high",
    "critical": "high",
    "crit": "high",
    "error": "high",
    "err": "high",
    "emerg": "high",
    "alert": "high",
    "warn": "medium",
    "warning": "medium",
    "notice": "low",
    "info": "low",
    "debug": "low",
    "trace": "low",
}

#: message 出现这些词就升级为 high —— 它们几乎总是真问题
_HIGH_SEVERITY_HINTS = re.compile(
    r"\b(crash|segfault|segmentation fault|oom[-_ ]?kill|out of memory|panic|"
    r"core dumped|fatal|failed|failure|denied|timeout|refused|corrupt)\b",
    re.IGNORECASE,
)


def parse_syslog_line(
    line: str, *, assume_year: int | None = None
) -> dict[str, Any] | None:
    """解析 syslog 风格行。

    syslog 时间戳**不含年份**。`assume_year=None` 时取当前 UTC 年——这是**有损假设**，
    故返回值里带 `ts_has_year=False` 让上层知道；跨年推断属阶段 04 归一化职责。
    """
    match = _RE_SYSLOG.match(line)
    if match is None:
        return None

    parts = match.group("ts").split()
    month = _MONTHS.get(parts[0])
    if month is None:
        return None
    day = int(parts[1])
    hour, minute, second = (int(x) for x in parts[2].split(":"))

    year = assume_year if assume_year is not None else datetime.now(timezone.utc).year
    try:
        ts = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None  # 非法日期（如 2 月 30 日）明确判为坏行

    rest = _RE_SYSLOG_REST.match(match.group("rest"))
    if rest is None:
        return None

    pid_raw = rest.group("pid")
    return {
        "ts": ts,
        "ts_has_year": False,
        "host": match.group("host"),
        "proc": rest.group("proc"),
        "pid": int(pid_raw) if pid_raw is not None else None,
        "extra": rest.group("extra"),
        "msg": rest.group("msg").strip(),
        "format": "syslog",
    }


def parse_log4j_line(line: str) -> dict[str, Any] | None:
    """解析 `2015-07-29 17:41:44,747 - INFO  [thread] - msg`。"""
    match = _RE_LOG4J.match(line)
    if match is None:
        return None
    try:
        ts = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S,%f").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return {
        "ts": ts,
        "ts_has_year": True,
        "level": match.group("level"),
        "thread": match.group("thread"),
        "msg": match.group("msg").strip(),
        "format": "log4j",
    }


def parse_apache_line(line: str) -> dict[str, Any] | None:
    """解析 `[Sun Dec 04 04:47:44 2005] [notice] msg`。"""
    match = _RE_APACHE.match(line)
    if match is None:
        return None
    try:
        ts = datetime.strptime(
            match.group("ts").strip("[]"), "%a %b %d %H:%M:%S %Y"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return {
        "ts": ts,
        "ts_has_year": True,
        "level": match.group("level"),
        "msg": match.group("msg").strip(),
        "format": "apache",
    }


#: 按顺序尝试；先匹配到的赢。顺序即优先级，故意显式写出来便于审阅。
PARSERS = (parse_log4j_line, parse_syslog_line, parse_apache_line)


def parse_line(line: str) -> dict[str, Any] | None:
    """依次尝试各格式；全部失败返回 None。"""
    stripped = line.strip()
    if not stripped:
        return None
    for parser in PARSERS:
        raw = parser(stripped)
        if raw is not None:
            return raw
    return None


def infer_severity(raw: dict[str, Any]) -> str:
    """从显式等级词或 message 关键词推断严重度。显式等级词优先。"""
    level = str(raw.get("level", "")).lower()
    if level in _LEVEL_TO_SEVERITY:
        return _LEVEL_TO_SEVERITY[level]
    message = str(raw.get("msg", ""))
    if _HIGH_SEVERITY_HINTS.search(message):
        return "high"
    return "low"
