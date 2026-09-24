"""log-parser-eval —— 用 logs/ 下的真实日志量化「可解析率」与「脱敏误伤率」。

确定性脚本，**不调 LLM**（红线 1）。复用产品代码里的 Parser 与 Masker，
不另写一套正则 —— 另写一套测的就不是线上跑的东西了。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.domains.registry import get_domain_registry  # noqa: E402
from app.parsers import get_parser  # noqa: E402
from app.utils.masking import Masker  # noqa: E402

LOGS = pathlib.Path("logs")
JSONL_FILES = {"nginx_json_access.log", "nginxplus_status_json.log"}

#: 「排障字段」的原值形态。脱敏若把这些也换掉，就等于把排障信息删了 ——
#: 这类"误伤"不会报错，只会让报告变得无从下手，所以要单独量出来。
FORENSIC_PATTERNS = {
    "uid=/euid=": re.compile(r"\b(?:uid|euid|gid|egid)=\d+"),
    "pid=": re.compile(r"\bpid=\d+"),
    "http_status": re.compile(r'" (?:200|30\d|4\d\d|5\d\d) '),
    "req_path": re.compile(r'"(?:GET|POST|PUT|DELETE|HEAD) /[^"]*"'),
    "k8s/docker_id": re.compile(r"\b[0-9a-f]{12,64}\b"),
}


def detect_fmt(name: str) -> str:
    return "jsonl" if name in JSONL_FILES else "txt"


def count_forensic(text: str) -> dict[str, int]:
    return {label: len(rx.findall(text)) for label, rx in FORENSIC_PATTERNS.items()}


#: 被替换掉的**原值** → 次数。用来复核误伤：脱敏是"宁可多替换也不漏真密钥"，
#: 所以总会有个别非密钥被换掉，得让人看得见换的是什么。
#: 键名形态的（如 `token: xxx`）取冒号后面的值，值形态的（如 `sk-...`）取整段。
REPLACED_PATTERNS = (
    re.compile(r"(?i)\b(?:token|password|passwd|secret|api[_-]?key)\s*[:=]\s*(\S{6,})"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
)

replaced_counter: dict[str, int] = {}


def collect_replaced(raw: str, masked: str) -> None:
    """把"原文里有、脱敏后没了"的值记下来（只记值，不记密钥本身）。"""
    if raw == masked:
        return
    for pattern in REPLACED_PATTERNS:
        for match in pattern.finditer(raw):
            value = match.group(1) if match.groups() else match.group(0)
            # 只有确实从脱敏结果里消失了才算"被替换"
            if value and value not in masked:
                replaced_counter[value] = replaced_counter.get(value, 0) + 1


def main() -> int:
    domain = get_domain_registry().load("computer_monitoring")
    rows = []
    total_lines = total_ok = total_bad = 0
    total_masked = 0
    hurt_total = 0
    hurt_checked = 0

    for path in sorted(LOGS.glob("*.log")):
        raw = path.read_text(encoding="utf-8", errors="replace")
        fmt = detect_fmt(path.name)

        # ① 脱敏（与线上同一顺序：先脱敏，再解析）
        masker = Masker(mask_ip=True, enabled=True)
        masked = masker.mask(raw)
        collect_replaced(raw, masked)

        # ② 解析
        parser = get_parser(fmt)
        try:
            records = parser.parse(masked, domain)
            stats = parser.as_dict()
            error = ""
        except Exception as exc:  # noqa: BLE001 - 解析炸了也要如实报出来
            records, stats, error = [], {}, f"{type(exc).__name__}: {exc}"

        lines = max(len(raw.splitlines()), 1)
        bad = int(stats.get("bad_lines", 0))
        ok = int(stats.get("parsed", 0)) or len(records)
        masked_count = int(masker.stats.total)

        # ③ 误伤：统计"排障字段"在脱敏前后的存留
        before = count_forensic(raw)
        after = count_forensic(masked)
        hurt = {
            label: before[label] - after.get(label, 0)
            for label in before
            if before[label] and after.get(label, 0) < before[label]
        }
        hurt_total += sum(hurt.values())
        hurt_checked += sum(before.values())

        rows.append(
            {
                "file": path.name,
                "fmt": fmt,
                "lines": lines,
                "ok": ok,
                "bad": bad,
                "masked": masked_count,
                "by_rule": (masker.stats.as_dict() or {}).get("by_rule", {}),
                "hurt": hurt,
                "error": error,
                "sample": (stats.get("bad_line_samples") or [""])[0][:70],
            }
        )
        total_lines += lines
        total_ok += ok
        total_bad += bad
        total_masked += masked_count

    print("| 文件 | 总行 | 成功解析 | 坏行 | 脱敏替换 | 误伤样本 |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        hurt = ", ".join(f"{k}×{v}" for k, v in r["hurt"].items()) or "—"
        if r["error"]:
            hurt = f"解析异常! {r['error'][:40]}"
        print(
            f"| {r['file']} | {r['lines']} | {r['ok']} | {r['bad']} | "
            f"{r['masked']} | {hurt} |"
        )

    print()
    rate = total_ok / total_lines if total_lines else 0.0
    print(f"整体可解析率：{rate:.2%}（{total_ok}/{total_lines}）")
    print(f"坏行合计：{total_bad}")
    print(f"脱敏替换合计：{total_masked}")
    hurt_rate = hurt_total / hurt_checked if hurt_checked else 0.0
    print(f"排障字段误伤率：{hurt_rate:.4%}（伤 {hurt_total} / 共 {hurt_checked} 个排障字段）")
    print()
    print("规则命中分布（全部文件合计）：")
    merged: dict[str, int] = {}
    for r in rows:
        for k, v in r["by_rule"].items():
            merged[k] = merged.get(k, 0) + int(v)
    for k, v in sorted(merged.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")

    print()
    print("坏行样本（首个）：")
    for r in rows:
        if r["bad"] and r["sample"]:
            print(f"  {r['file']}: {r['sample']}")

    # 被替换掉的值长什么样 —— 这张表让"已知误伤"可复核，而不是靠人记得。
    # 脱敏是"宁可多替换也不漏真密钥"，所以总会有个别非密钥被换掉
    # （已知 1 例：`stream/token: com.apple.xpc.activity/4505` →
    #  `[SECRET_…]`，见 logs/README.md 的"已知误伤"一节）。
    # 把替换值按出现次数列出来，规则一改就能立刻看出"多换了什么、少换了什么"。
    print()
    print("被替换的值（Top 15，用于复核误伤）：")
    for value, count in sorted(replaced_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
        print(f"  ×{count:<4} {value[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
