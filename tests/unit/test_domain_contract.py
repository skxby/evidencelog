"""领域契约与注册表单测 —— 对应阶段 03 验收四项。

计划第 430 行的验收：
  1. 能注册并加载 computer_monitoring
  2. 不修改 Runtime 代码即可被替换
  3. 对样例行能产出标准事件、指标事件
  4. 命中异常时能取到对应 runbook

第 2 项用「另写一个假领域并替换」来证明，而不是嘴上说解耦。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domains.protocol import (
    AnalyzerFinding,
    DomainBase,
    MetricSpec,
    NormalizedEvent,
    Runbook,
)
from app.domains.registry import (
    DomainNotFoundError,
    DomainRegistry,
    DomainVersionMismatchError,
    get_domain_registry,
    reset_domain_registry,
)
from app.domains.wiring import build_default_registry

CM = "computer_monitoring"


@pytest.fixture()
def registry() -> DomainRegistry:
    return build_default_registry()


@pytest.fixture()
def domain(registry: DomainRegistry) -> DomainBase:
    return registry.load(CM, "1.0.0")


# ============================================================
# 验收 1：能注册并加载 computer_monitoring
# ============================================================


def test_computer_monitoring_is_registered(registry: DomainRegistry):
    assert CM in registry.ids()
    assert registry.versions_of(CM) == ["1.0.0"]
    assert len(registry) == 1


def test_load_by_id_and_version(domain: DomainBase):
    assert domain.domain_id == CM
    assert domain.version == "1.0.0"


def test_load_by_id_alone_works_when_single_version(registry: DomainRegistry):
    assert registry.load(CM).domain_id == CM


def test_load_unknown_domain_raises(registry: DomainRegistry):
    with pytest.raises(DomainNotFoundError):
        registry.load("no_such_domain")


def test_load_wrong_version_raises_instead_of_guessing(registry: DomainRegistry):
    """版本会进幂等键，绝不能"随便给一个版本"。"""
    with pytest.raises(DomainVersionMismatchError):
        registry.load(CM, "9.9.9")


def test_duplicate_registration_is_refused(registry: DomainRegistry):
    duplicate = registry.load(CM)
    with pytest.raises(ValueError, match="已注册"):
        registry.register(duplicate)


def test_ambiguous_versions_require_explicit_version():
    """同一 id 多版本时，不指定 version 必须报错而不是猜最新的。"""
    def make(version: str) -> FakeDomain:
        # domain_id / version 是类属性，按实例派生一个子类来造多版本
        return type("FakeDomainVersioned", (FakeDomain,), {"version": version})()

    reg = DomainRegistry()
    reg.register(make("1.0.0"))
    reg.register(make("2.0.0"))
    with pytest.raises(DomainVersionMismatchError, match="必须显式指定"):
        reg.load("fake")
    assert reg.load("fake", "2.0.0").version == "2.0.0"


# ============================================================
# 验收 2：不改 Runtime 即可替换领域
# ============================================================


class FakeDomain(DomainBase):
    """一个与 computer_monitoring 无关的领域，用来证明 Runtime 不写死。

    它只实现契约，**不 import 任何 computer_monitoring 的东西**。
    """

    domain_id = "fake"
    version = "1.0.0"

    def parse_line(self, line: str) -> dict | None:
        if not line.startswith("FAKE "):
            return None
        return {"ts": datetime(2026, 1, 1, tzinfo=timezone.utc), "msg": line[5:]}

    def normalize(self, raw: dict) -> NormalizedEvent:
        return NormalizedEvent(timestamp=raw["ts"], message=raw["msg"])

    def metric_specs(self) -> list[MetricSpec]:
        return [MetricSpec(metric_name="fake_metric", unit="x", source_key="value")]

    def analyzers(self) -> list:
        return []

    def runbooks(self) -> list[Runbook]:
        return [
            Runbook(
                id="rb_fake_001",
                title="假领域处置",
                applies={"analyzer": "FakeAnalyzer"},
                steps=["step one"],
            )
        ]


def test_a_foreign_domain_can_be_registered_without_touching_runtime():
    """把 CM 整个换掉，注册表照样工作 —— 这就是"不改内核即可替换"。"""
    reg = DomainRegistry()
    reg.register(FakeDomain())

    loaded = reg.load("fake", "1.0.0")
    assert loaded.parse_line("FAKE hello") == {
        "ts": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "msg": "hello",
    }
    assert loaded.parse_line("Jun 14 15:16:01 host proc[1]: x") is None
    assert [m.metric_name for m in loaded.metric_specs()] == ["fake_metric"]
    assert [r.id for r in loaded.runbooks()] == ["rb_fake_001"]


def _code_without_comments_or_docstrings(path) -> str:
    """去掉注释与字符串字面量后再做禁用 import 的静态扫描。

    直接 grep 源码会把**文档里举的反例**也算成违规——我自己就在
    registry.py 的 docstring 里写了「不要这样 import」的示例。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            is_docstring = (
                bool(body)
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            )
            if is_docstring:
                body.pop(0)  # 去掉 docstring
    return ast.unparse(tree)


def test_registry_has_no_concrete_domain_import():
    """注册表本体不得 import 任何具体 domain —— 否则解耦是假的。"""
    import app.domains.registry as registry_module

    code = _code_without_comments_or_docstrings(__import__("pathlib").Path(registry_module.__file__))
    assert "app.domains.computer_monitoring" not in code


def test_only_wiring_module_imports_a_concrete_domain():
    """全仓搜一遍：具体 domain 只该被组合根 import。"""
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        # 领域目录包自身、组合根不算违规（组合根就是要 import 具体领域的）
        if "domains/computer_monitoring" in path.as_posix():
            continue
        if path.name == "wiring.py":
            continue
        if "app.domains.computer_monitoring" in _code_without_comments_or_docstrings(path):
            offenders.append(path.name)
    assert offenders == [], f"这些模块不该 import 具体领域：{offenders}"


# ============================================================
# 验收 3：样例行 → 标准事件 / 指标事件
# ============================================================


SYSLOG_SAMPLE = (
    "Jun 14 15:16:01 combo sshd(pam_unix)[19939]: "
    "authentication failure; logname= uid=0 euid=0 tty=NODEVssh"
)
LOG4J_SAMPLE = "2015-07-29 17:41:44,747 - INFO  [QuorumPeer[myid=1]/@774] - Notification time out: 3200"
APACHE_SAMPLE = "[Sun Dec 04 04:47:44 2005] [notice] workerEnv.init() ok /etc/httpd/conf/workers2.properties"


@pytest.mark.parametrize("sample", [SYSLOG_SAMPLE, LOG4J_SAMPLE, APACHE_SAMPLE])
def test_sample_lines_produce_standard_events(domain: DomainBase, sample: str):
    raw = domain.parse_line(sample)
    assert raw is not None, f"样例行解析失败：{sample}"
    event = domain.normalize(raw)

    assert isinstance(event, NormalizedEvent)
    assert event.timestamp is not None
    assert event.message
    assert event.event_type in ("log", "metric")
    assert event.severity in ("high", "medium", "low")


def test_auth_failure_is_high_severity(domain: DomainBase):
    """认证失败属于要盯的事件，severity 必须升上去。"""
    event = domain.normalize(domain.parse_line(SYSLOG_SAMPLE))
    assert event.event_type == "log"
    assert event.severity == "high"


def test_metric_raw_produces_metric_event(domain: DomainBase):
    """指标事件走 payload，且 payload 结构符合计划第 349 行。"""
    raw = {
        "ts": datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
        "msg": "cpu high",
        "metric_name": "cpu_used",
        "value": 92.4,
    }
    event = domain.normalize(raw)
    assert event.event_type == "metric"
    assert event.payload == {"metric_name": "cpu_used", "value": 92.4, "unit": "%"}


def test_normalize_without_timestamp_raises_instead_of_faking_now(domain: DomainBase):
    """静默补 now() 会让时间线建立在假数据上 —— 必须报错（红线 4）。"""
    with pytest.raises(ValueError, match="缺少 ts"):
        domain.normalize({"msg": "no timestamp"})


def test_non_metric_raw_does_not_become_a_metric_event(domain: DomainBase):
    raw = {
        "ts": datetime(2026, 9, 23, tzinfo=timezone.utc),
        "msg": "plain log",
        "metric_name": "cpu_used",  # 有名字但没数值
    }
    event = domain.normalize(raw)
    assert event.event_type == "log"
    assert event.payload is None


# ============================================================
# 验收 4：命中异常时能取到对应 runbook
# ============================================================


def test_runbooks_are_loaded_from_yaml(domain: DomainBase):
    books = domain.runbooks()
    assert len(books) == 5
    assert {b.id for b in books} == {
        "rb_oom_001",
        "rb_disk_001",
        "rb_crash_001",
        "rb_cpu_001",
        "rb_memgrowth_001",
    }
    for book in books:
        assert book.title and book.steps and book.applies


def test_oom_message_resolves_to_oom_runbook(domain: DomainBase):
    book = domain.find_runbook_for(
        "ProcessCrash", "kernel: Out of memory: Kill process 1234 (java)"
    )
    assert book is not None
    assert book.id == "rb_oom_001"


def test_segfault_resolves_to_crash_runbook(domain: DomainBase):
    book = domain.find_runbook_for("ProcessCrash", "nginx[9]: segfault at 0 ip 0 sp 0 error 4")
    assert book is not None
    assert book.id == "rb_crash_001"


def test_disk_analyzer_resolves_to_disk_runbook_and_respects_metric(domain: DomainBase):
    book = domain.find_runbook_for("DiskFull", metric_name="disk_used")
    assert book is not None and book.id == "rb_disk_001"
    # 指标不匹配时必须落空，否则会把磁盘告警指到 CPU 处置上
    assert domain.find_runbook_for("DiskFull", metric_name="cpu_used") is None


def test_cpu_spike_resolves_to_cpu_runbook(domain: DomainBase):
    book = domain.find_runbook_for("CPUSpike", metric_name="cpu_used")
    assert book is not None and book.id == "rb_cpu_001"


def test_memory_growth_resolves_to_growth_runbook(domain: DomainBase):
    book = domain.find_runbook_for("MemoryGrowth", metric_name="memory_used")
    assert book is not None and book.id == "rb_memgrowth_001"


def test_runbook_matching_is_deterministic(domain: DomainBase):
    """多次调用结果必须一致，不能依赖字典序偶然。"""
    first = domain.find_runbook_for("ProcessCrash", "out of memory")
    for _ in range(5):
        assert domain.find_runbook_for("ProcessCrash", "out of memory").id == first.id


def test_unknown_analyzer_returns_none(domain: DomainBase):
    assert domain.find_runbook_for("NoSuchAnalyzer") is None


# ============================================================
# 注册表单例
# ============================================================


def test_default_registry_singleton_is_reused():
    reset_domain_registry()
    first = get_domain_registry()
    second = get_domain_registry()
    assert first is second
    reset_domain_registry()


def test_analyzer_finding_is_a_candidate_not_a_conclusion():
    """措辞纪律：analyzer 产出的是候选异常，不是根因结论。"""
    finding = AnalyzerFinding(analyzer="X", severity="high", message="m")
    assert finding.metric_name is None
    assert finding.event_ids == []
