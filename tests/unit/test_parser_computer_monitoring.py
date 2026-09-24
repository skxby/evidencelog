"""解析器单测 —— 用 `logs/` 下的**真实日志**，不用合成数据。

计划第 590 行与阶段 04 验收都要求用真实日志验证；这里先立住本 domain 的解析基线，
它也是「坏行要被如实统计」这条（红线 4）的第一道保障。

实测基线（2026-09-23，`logs/` 全量）：
    Apache_2k.log       2000/2000  100.0%
    Linux_2k.log        2000/2000  100.0%
    OpenSSH_2k.log      2000/2000  100.0%
    Zookeeper_2k.log    2000/2000  100.0%
    Mac_2k.log          1981/2000   99.0%    ← 余下为 launchd 转义服务名
    合计                9981/10000  99.8%
阈值定在 0.98 而非 1.0：不把"必须 100%"写死，否则将来新增样本会
逼着后人去放宽断言（stage-verify 技能明令禁止那种做法）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domains.computer_monitoring.parser import (
    infer_severity,
    parse_apache_line,
    parse_line,
    parse_log4j_line,
    parse_syslog_line,
)

LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"

SYSLOG_FILES = ("Apache_2k.log", "Linux_2k.log", "Mac_2k.log", "OpenSSH_2k.log", "Zookeeper_2k.log")

#: web access 日志不属本 domain（阶段 04 的 JsonlParser / 其他），故意不纳入基线
WEB_FILES = ("nginx_json_access.log", "nginx_plain.log", "nginxplus_status_json.log")


def _lines(name: str) -> list[str]:
    path = LOGS_DIR / name
    if not path.is_file():
        pytest.skip(f"缺少样本 {name}")
    return [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]


# ============================================================
# 逐格式
# ============================================================


def test_parse_syslog_line_extracts_all_fields():
    raw = parse_syslog_line(
        "Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication failure; logname= uid=0"
    )
    assert raw is not None
    assert raw["host"] == "combo"
    assert raw["proc"] == "sshd(pam_unix)"
    assert raw["pid"] == 19939
    assert raw["msg"].startswith("authentication failure")
    assert raw["format"] == "syslog"
    # syslog 无年份，必须如实标注，不能假装知道
    assert raw["ts_has_year"] is False


def test_syslog_without_pid_is_still_parsed():
    """`logrotate: ALERT ...` 没有 [pid] —— 曾因把 pid 写成强制项而整类落空。"""
    raw = parse_syslog_line("Jun 15 04:06:20 combo logrotate: ALERT exited abnormally with [1]")
    assert raw is not None
    assert raw["proc"] == "logrotate"
    assert raw["pid"] is None
    assert raw["msg"].startswith("ALERT")


def test_syslog_with_extra_bracket_group_is_parsed():
    """macOS sandboxd 形如 `sandboxd[129] ([31211]): ...`。"""
    raw = parse_syslog_line(
        "Jul  1 09:29:02 host sandboxd[129] ([31211]): com.apple.Addres deny network-outbound"
    )
    assert raw is not None
    assert raw["proc"] == "sandboxd"
    assert raw["pid"] == 129
    assert raw["extra"] == "31211"


def test_syslog_with_dotted_parenthetical_group_is_parsed():
    """macOS launchd 的附注组**不带方括号**，且是点分域名：

    `com.apple.xpc.launchd[1] (com.apple.xpc.launchd.domain.pid.WebContent.32502): ...`

    真实日志里这类行有 19/2000（log-parser-eval 实测），早先全部判成坏行。
    它们恰恰指明了"哪个进程崩溃/被拒"，是排障最需要的那几行。
    """
    raw = parse_syslog_line(
        "Jul  2 16:55:53 host com.apple.xpc.launchd[1] "
        "(com.apple.xpc.launchd.domain.pid.WebContent.32502): "
        "Path not allowed in target domain"
    )
    assert raw is not None
    assert raw["proc"] == "com.apple.xpc.launchd"
    assert raw["pid"] == 1
    assert raw["extra"] == "com.apple.xpc.launchd.domain.pid.WebContent.32502"
    assert raw["msg"].startswith("Path not allowed")


def test_parenthetical_inside_message_is_not_eaten_as_extra():
    """message 自己以括号开头时不能被当成附注组。

    `proc[1]: (note) 出事了` —— 冒号紧跟 pid，附注组分支匹配不到，
    msg 必须完整保留 `(note) 出事了`。加附注分支最容易踩的就是这里。
    """
    raw = parse_syslog_line("Jul  2 12:00:00 host kernel[1]: (note) 出事了")
    assert raw is not None
    assert raw["extra"] is None
    assert raw["msg"] == "(note) 出事了"


def test_syslog_multi_word_process_name_is_parsed():
    """`Microsoft Word[912]: ...` 这类多词进程名。"""
    raw = parse_syslog_line(
        "Jul  2 12:00:00 host Microsoft Word[912]: Cocoa scripting error"
    )
    assert raw is not None
    assert raw["proc"] == "Microsoft Word"
    assert raw["pid"] == 912


def test_parse_log4j_line_handles_nested_brackets_in_thread():
    """`[QuorumPeer[myid=1]/...@774]` 线程名内含方括号 —— 曾整类解析失败。"""
    raw = parse_log4j_line(
        "2015-07-29 17:41:44,747 - INFO  "
        "[QuorumPeer[myid=1]/0:0:0:0:0:0:0:0:2181:FastLeaderElection@774] - Notification time out: 3200"
    )
    assert raw is not None
    assert raw["level"] == "INFO"
    assert raw["thread"].startswith("QuorumPeer[myid=1]")
    assert "Notification time out" in raw["msg"]
    # 刻意是 naive：syslog 时间戳不含时区，无权假定 UTC。
    # 时区由上层按 DEFAULT_TIMEZONE 解释后转 UTC（计划第 539 行）。
    assert raw["ts"].tzinfo is None
    assert raw["ts_has_tz"] is False


def test_parse_apache_line():
    raw = parse_apache_line(
        "[Sun Dec 04 04:47:44 2005] [notice] workerEnv.init() ok /etc/httpd/conf/workers2.properties"
    )
    assert raw is not None
    assert raw["level"] == "notice"
    assert raw["ts"].year == 2005


def test_invalid_calendar_date_is_rejected_not_coerced():
    """`Feb 30` 这种非法日期要判坏行，不能悄悄挪到 3 月 2 日。"""
    assert parse_syslog_line("Feb 30 10:00:00 host proc[1]: x") is None


# ============================================================
# 坏行要能被如实识别
# ============================================================


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "not a log line at all",
        "93.180.71.3 - - [17/May/2015:08:05:32 +0000] \"GET / HTTP/1.1\" 304 0",
        '{"json": "line"}',
    ],
)
def test_unparseable_lines_return_none(line: str):
    assert parse_line(line) is None


# ============================================================
# severity 推断
# ============================================================


@pytest.mark.parametrize(
    ("level", "expected"),
    [("ERROR", "high"), ("FATAL", "high"), ("WARN", "medium"), ("INFO", "low")],
)
def test_explicit_level_maps_to_severity(level: str, expected: str):
    assert infer_severity({"level": level, "msg": ""}) == expected


@pytest.mark.parametrize(
    "message",
    [
        "kernel: Out of memory: Kill process 1234",
        "nginx[9]: segfault at 0",
        "sshd: authentication failure",
        "systemd: Failed to start service",
    ],
)
def test_high_severity_keywords_upgrade_severity(message: str):
    assert infer_severity({"msg": message}) == "high"


def test_explicit_level_wins_over_message_keywords():
    """显式 INFO 不该被 message 里的普通词覆盖成 high。"""
    assert infer_severity({"level": "INFO", "msg": "connection established"}) == "low"


# ============================================================
# 真实日志整体基线
# ============================================================


def test_real_system_logs_parse_rate_meets_baseline():
    """系统日志合计解析率必须 ≥ 98%。"""
    total = parsed = 0
    for name in SYSLOG_FILES:
        lines = _lines(name)
        total += len(lines)
        parsed += sum(1 for ln in lines if parse_line(ln) is not None)

    rate = parsed / total
    assert rate >= 0.98, f"系统日志解析率 {rate:.4f} 低于基线 0.98（{parsed}/{total}）"


@pytest.mark.parametrize("name", ["Apache_2k.log", "Linux_2k.log", "OpenSSH_2k.log", "Zookeeper_2k.log"])
def test_four_samples_parse_perfectly(name: str):
    """这四份必须 100% —— 它们代表的是本 domain 的核心格式。"""
    lines = _lines(name)
    bad = [ln for ln in lines if parse_line(ln) is None]
    assert bad == [], f"{name} 有 {len(bad)} 行解析失败，例如：{bad[:2]}"


def test_domain_parser_returns_naive_timestamps_and_flags_missing_tz():
    """领域层如实报告「缺时区」，不擅自假定 UTC。

    若这里硬编码 UTC，`DEFAULT_TIMEZONE` 就成了死配置，且所有日志时间会
    整体偏移一个时区——而偏移后的报告看起来完全正常，属最危险的一类错误。
    """
    for name in SYSLOG_FILES:
        for line in _lines(name)[:200]:
            raw = parse_line(line)
            if raw is not None:
                assert raw["ts"].tzinfo is None, f"{name}: {line[:60]}"
                assert raw["ts_has_tz"] is False, f"{name}: {line[:60]}"


def test_web_access_logs_are_not_claimed_by_this_domain():
    """nginx access 日志不属本 domain —— 如实不认领，而不是硬解析成 syslog。"""
    for name in WEB_FILES:
        lines = _lines(name)
        assert all(parse_line(ln) is None for ln in lines[:200]), name
