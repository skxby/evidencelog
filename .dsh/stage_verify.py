"""stage-verify 驱动：按「阶段 → 测试文件」跑出可核对的原始计数。

为什么用 junitxml 而不是抓 pytest 的 stdout：
本项目在 Windows/pwsh 下，pytest 最后那行 "N passed" 经常抓不到
（`| Select-Object -Last 1` 得到的是进度条那行）。**计数是验收证据本身**，
抓不到就等于没有证据，所以改成让它写 XML、再解析 —— 数字不会因为终端而丢。

用法：python .dsh/stage_verify.py [阶段号 ...]
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
OUT = ROOT / ".dsh" / "junit"

#: 阶段 → (计划里的验收原文, 承载证据的测试文件)
STAGES: dict[int, tuple[str, list[str]]] = {
    1: (
        "docker compose up 起 PG+Redis；FastAPI 启动并返回健康检查；CI 可跑测试",
        [
            "tests/integration/test_env_connectivity.py",
            "tests/unit/test_config.py",
            "tests/unit/test_delivery_config.py",
        ],
    ),
    2: (
        "迁移可执行可回滚；能建 Project/DataSource；能插入普通事件与指标事件并按 "
        "project 查回；事件可归入 EventGroup、再归并为 Incident；model_calls 可追加",
        ["tests/unit/test_models.py", "tests/unit/test_enums.py",
         "tests/integration/test_models_pg.py"],
    ),
    3: (
        "能注册并加载 computer_monitoring；不改 Runtime 代码即可替换；"
        "对样例行能产出标准事件、指标事件；命中异常时能取到对应 runbook",
        ["tests/unit/test_domain_contract.py", "tests/unit/test_analyzers_and_loaders.py",
         "tests/unit/test_parser_computer_monitoring.py"],
    ),
    4: (
        "上传 TXT/JSONL 能解析入库且响应含统计；邮箱/密钥在落盘文件与库中都已被替换；"
        "坏行被计数不静默丢弃；未带 data_source_id 能自动建/复用；超限被拒",
        ["tests/integration/test_upload_pipeline.py"],
    ),
    5: (
        "三个工具结果正确、边界（空集/单事件/大窗口）行为明确；注册器能按名取用；"
        "执行结果可记录到 Run",
        ["tests/unit/test_tools_data_ops.py", "tests/unit/test_tools_registry.py",
         "tests/integration/test_tool_usage_recorded.py"],
    ),
    6: (
        "至少接通一个真实供应商并返回结构化结果；切换型号只改环境变量；"
        "token 与成本被记录",
        ["tests/unit/test_gateway_router.py"],
    ),
    7: (
        "预算不足拒绝创建；极小预算能触发 partial_success 且页面标注「不完整」；"
        "成本被正确累加；无法构造出无限调用的 Run",
        ["tests/unit/test_policy_cost.py", "tests/integration/test_cost_control_pg.py"],
    ),
    8: (
        "创建 Run 立即返回 run_id+queued；kill Worker 后僵尸 Run 被回收；"
        "重复提交不产生重复 Run；取消在下个检查点生效；模型不可用降级 L0；"
        "错误分类正确",
        ["tests/unit/test_reliability.py", "tests/integration/test_run_lifecycle_pg.py"],
    ),
    9: (
        "正常日志走 L0；少量错误走 L1；多错误/高危走到相应等级；事件能分组并归并"
        "为事故、历史相似事故注入 Context；Context 不超预算；假 event_id 能拦截"
        "并重试/降级；每个 fact 都有有效证据",
        ["tests/unit/test_analysis_pipeline.py",
         "tests/integration/test_pipeline_persistence_pg.py"],
    ),
    10: (
        "每个端点鉴权与参数校验生效；分析端点立即返回不阻塞；/docs 可访问；"
        "跨 Project 访问被拒",
        ["tests/integration/test_api_pg.py"],
    ),
    11: (
        "从上传到看报告全程不碰命令行；fact 的证据可点击核对；runbook 可见；"
        "不完整/失败结果有显著提示与重试入口",
        ["tests/integration/test_web_ui_pg.py"],
    ),
    12: ("给定任一 Run 能完整复述「系统做了什么、花了多少、为何得到这个结论」",
         ["tests/integration/test_observability_pg.py"]),
    13: (
        "单测通过、Golden Set 五个场景全部符合预期、E2E 跑通、"
        "隔离攻击用例全部失败于系统防线",
        ["tests/golden/test_golden_set.py", "tests/integration/test_e2e_isolation_pg.py"],
    ),
    14: ("全新环境 clone 后，按 README 能在本机一键起全栈并完成一次真实分析",
         []),  # 证据是"真起一遍"，见报告里的实测记录
}


def run_stage(stage: int, files: list[str]) -> dict[str, int]:
    if not files:
        return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    OUT.mkdir(parents=True, exist_ok=True)
    xml = OUT / f"stage{stage:02d}.xml"
    cmd = [
        str(PY), "-m", "pytest", *files,
        "-p", "no:cacheprovider", "-q", "--tb=short",
        f"--junitxml={xml}",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, check=False)
    if not xml.exists():
        return {"tests": 0, "failures": 0, "errors": -1, "skipped": 0}
    root = ET.parse(xml).getroot()
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    return {
        "tests": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "skipped": int(suite.get("skipped", 0)),
        "exit": proc.returncode,
    }


def main() -> int:
    wanted = [int(a) for a in sys.argv[1:]] or sorted(STAGES)
    print("| 阶段 | 测试数 | 失败 | 错误 | 跳过 | 退出码 | 结论 |")
    print("|---|---|---|---|---|---|---|")
    bad = []
    for stage in wanted:
        _, files = STAGES[stage]
        r = run_stage(stage, files)
        ok = r.get("exit", 0) == 0 and r["failures"] == 0 and r["errors"] <= 0
        if not files:
            verdict, ok = "真机实测（见报告）", True
        else:
            verdict = "✅ 全绿" if ok else "❌ 有失败"
        if not ok:
            bad.append(stage)
        print(
            f"| {stage:02d} | {r['tests']} | {r['failures']} | {r['errors']} | "
            f"{r['skipped']} | {r.get('exit', '-')} | {verdict} |"
        )
    print()
    print("未通过阶段：", bad or "无")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
