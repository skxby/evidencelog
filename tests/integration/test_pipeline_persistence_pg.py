"""阶段 09 落库部分的集成测试（真实 PostgreSQL）。

补齐计划的最后两步：「Insight 落库；候选知识写入 staging」（计划第 751 行）。
这里也是红线 3 在**写入侧**的最后一道闸门。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.analysis.evidence import ValidatedInsight
from app.analysis.knowledge_staging import (
    candidate_id,
    load_candidates,
    staging_path,
    write_candidates,
)
from app.analysis.persistence import (
    EvidencePersistenceError,
    assert_no_evidence_violation,
    persist_grouping,
    persist_insights,
)
from app.db import SessionLocal
from app.models import AgentRun, DataSource, Project, User
from app.models.event import Event
from app.repositories import (
    AgentRunRepository,
    DataSourceRepository,
    EvidenceRepository,
    InsightRepository,
    ProjectRepository,
    UserRepository,
)
from app.repositories.event import EventRepository
from app.repositories.event_group import EventGroupRepository
from app.repositories.incident import IncidentRepository

pytestmark = pytest.mark.integration
UTC = timezone.utc
T0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session():
    s = SessionLocal()
    try:
        yield s
        s.rollback()
    finally:
        s.close()


@pytest.fixture()
def ctx(session):
    users = UserRepository(session)
    user = users.add(
        User(email=f"stage09-{datetime.now(UTC).timestamp()}@example.com", password_hash="x")
    )
    session.flush()
    project = ProjectRepository(session).add(
        Project(user_id=user.id, name="p09", budget_total=10, budget_used=0)
    )
    session.flush()
    source = DataSourceRepository(session).add(
        project.id,
        DataSource(project_id=project.id, type="file_upload", format="txt", location="a.log"),
    )
    session.flush()
    runs = AgentRunRepository(session)
    run = runs.add(project.id, AgentRun(project_id=project.id, source_id=source.id))
    session.flush()
    return project, source, run


def _insight(title: str, evidence: list[str], *, type_: str = "fact") -> ValidatedInsight:
    return ValidatedInsight(
        type=type_,
        severity="high",
        confidence=0.9,
        title=title,
        summary="摘要",
        evidence_ids=evidence,
    )


# ============================================================
# Insight 落库
# ============================================================


def test_insights_and_evidence_are_persisted(session, ctx):
    project, source, run = ctx
    insights = InsightRepository(session)
    evidences = EvidenceRepository(session)

    result = persist_insights(
        project_id=project.id,
        run_id=run.id,
        insights=[_insight("进程崩溃", ["1", "2"])],
        valid_event_ids={"1", "2", "3"},
        insight_repository=insights,
        evidence_repository=evidences,
        source_id=source.id,
    )
    session.flush()
    session.expire_all()

    assert len(result.insight_ids) == 1
    assert len(result.evidence_ids) == 1
    assert result.rejected == []

    stored = insights.list_for_run(project.id, run.id)
    assert len(stored) == 1
    assert stored[0].type == "fact"
    assert stored[0].confidence == 0.9

    evidence_rows = evidences.list_for_insight(project.id, stored[0].id)
    assert len(evidence_rows) == 1
    assert evidence_rows[0].event_ids == ["1", "2"]


def test_multiple_insights_each_get_their_own_evidence(session, ctx):
    project, source, run = ctx
    insights = InsightRepository(session)
    evidences = EvidenceRepository(session)

    result = persist_insights(
        project_id=project.id,
        run_id=run.id,
        insights=[_insight("A", ["1"]), _insight("B", ["2", "3"])],
        valid_event_ids={"1", "2", "3"},
        insight_repository=insights,
        evidence_repository=evidences,
        source_id=source.id,
    )
    session.flush()
    session.expire_all()

    assert len(result.insight_ids) == 2
    assert len(result.evidence_ids) == 2
    stored = insights.list_for_run(project.id, run.id)
    by_title = {i.title: i for i in stored}
    assert evidences.list_for_insight(project.id, by_title["A"].id)[0].event_ids == ["1"]
    assert evidences.list_for_insight(project.id, by_title["B"].id)[0].event_ids == ["2", "3"]


def test_possibility_without_evidence_is_persisted(session, ctx):
    """possibility 允许没有证据（它本来就是"无法确认"）。"""
    project, source, run = ctx
    insights = InsightRepository(session)
    evidences = EvidenceRepository(session)

    result = persist_insights(
        project_id=project.id,
        run_id=run.id,
        insights=[_insight("可能原因", [], type_="possibility")],
        valid_event_ids={"1"},
        insight_repository=insights,
        evidence_repository=evidences,
        source_id=source.id,
    )
    session.flush()

    assert len(result.insight_ids) == 1
    assert result.evidence_ids == []


# ============================================================
# 红线 3：写入侧的最后一道闸门
# ============================================================


def test_out_of_range_evidence_is_refused_at_the_write_gate(session, ctx):
    """到了落库这层还越界，说明上游漏检 —— 拒绝写入并留痕。"""
    project, source, run = ctx
    insights = InsightRepository(session)
    evidences = EvidenceRepository(session)

    result = persist_insights(
        project_id=project.id,
        run_id=run.id,
        insights=[_insight("伪造证据", ["9001"])],
        valid_event_ids={"1", "2"},
        insight_repository=insights,
        evidence_repository=evidences,
        source_id=source.id,
    )
    session.flush()

    assert result.insight_ids == []
    assert len(result.rejected) == 1
    assert "越界" in result.rejected[0]
    assert insights.list_for_run(project.id, run.id) == []

    with pytest.raises(EvidencePersistenceError, match="越界"):
        assert_no_evidence_violation(result)


def test_fact_without_evidence_is_refused_at_the_write_gate(session, ctx):
    """红线 3：fact 没有证据就不能进库。"""
    project, source, run = ctx
    insights = InsightRepository(session)
    evidences = EvidenceRepository(session)

    result = persist_insights(
        project_id=project.id,
        run_id=run.id,
        insights=[_insight("无证据的 fact", [])],
        valid_event_ids={"1"},
        insight_repository=insights,
        evidence_repository=evidences,
        source_id=source.id,
    )
    session.flush()

    assert result.insight_ids == []
    assert "fact 缺少有效证据" in result.rejected[0]


def test_assert_no_violation_passes_on_clean_result(session, ctx):
    project, source, run = ctx
    result = persist_insights(
        project_id=project.id,
        run_id=run.id,
        insights=[_insight("正常", ["1"])],
        valid_event_ids={"1"},
        insight_repository=InsightRepository(session),
        evidence_repository=EvidenceRepository(session),
        source_id=source.id,
    )
    assert_no_evidence_violation(result)  # 不抛异常


# ============================================================
# Project 隔离
# ============================================================


def test_insights_are_isolated_by_project(session, ctx):
    project, source, run = ctx
    insights = InsightRepository(session)
    evidences = EvidenceRepository(session)

    persist_insights(
        project_id=project.id,
        run_id=run.id,
        insights=[_insight("A 项目的结论", ["1"])],
        valid_event_ids={"1"},
        insight_repository=insights,
        evidence_repository=evidences,
        source_id=source.id,
    )
    session.flush()

    users = UserRepository(session)
    other_user = users.add(
        User(email=f"other09-{datetime.now(UTC).timestamp()}@example.com", password_hash="x")
    )
    session.flush()
    other_project = ProjectRepository(session).add(
        Project(user_id=other_user.id, name="other", budget_total=1, budget_used=0)
    )
    session.flush()

    assert insights.list_for_run(other_project.id, run.id) == []
    stored = insights.list_for_run(project.id, run.id)[0]
    assert evidences.list_for_insight(other_project.id, stored.id) == []


# ============================================================
# 候选知识写入 staging
# ============================================================


def test_candidates_written_to_data_dir_not_code_dir(work_tmp):
    """修订说明第 2 条：运行时数据写数据卷，**不写代码目录**。"""
    data_dir = work_tmp / "data"
    stats = write_candidates(
        data_dir=data_dir,
        domain_id="computer_monitoring",
        candidates=[
            {
                "kind": "error_pattern",
                "title": "OOM 反复出现",
                "description": "同一进程反复被 oom-killer 杀死",
                "match": {"event_type": "log"},
                "evidence_ids": ["1", "2"],
                "confidence": 0.7,
            }
        ],
        run_id=42,
        valid_event_ids={"1", "2", "3"},
    )

    assert stats["written"] == 1
    path = Path(stats["path"])
    assert path.is_file()
    # 路径必须在数据目录下，且没有落到代码目录里。
    # 注意 Windows 下 Path.parts 会把整条盘符路径拆开，故用 as_posix 比较。
    assert data_dir.as_posix() in path.as_posix()
    code_relative = path.as_posix().replace(data_dir.as_posix(), "")
    assert "/app/" not in code_relative
    assert path.as_posix().endswith("/staging/candidates.yaml")

    stored = load_candidates(path)
    assert len(stored) == 1
    assert stored[0]["status"] == "draft"
    assert stored[0]["evidence"]["event_ids"] == ["1", "2"]
    assert stored[0]["evidence"]["run_id"] == "42"


def test_candidate_without_valid_evidence_is_not_written(work_tmp):
    """计划第 482 行：候选必须绑定真实 event_id，不允许凭空总结。"""
    stats = write_candidates(
        data_dir=work_tmp / "data",
        domain_id="computer_monitoring",
        candidates=[
            {"kind": "error_pattern", "title": "凭空总结", "evidence_ids": ["9001"]},
            {"kind": "error_pattern", "title": "没有证据", "evidence_ids": []},
        ],
        run_id=1,
        valid_event_ids={"1"},
    )
    assert stats["written"] == 0
    assert stats["skipped"] == 2
    assert load_candidates(Path(stats["path"])) == []


def test_candidates_are_deduplicated_across_runs(work_tmp):
    """同一份异常重复分析不该越积越多候选。"""
    candidate = {"kind": "error_pattern", "title": "同一个问题", "evidence_ids": ["1"]}
    first = write_candidates(
        data_dir=work_tmp / "data", domain_id="d", candidates=[candidate],
        run_id=1, valid_event_ids={"1"},
    )
    second = write_candidates(
        data_dir=work_tmp / "data", domain_id="d", candidates=[candidate],
        run_id=2, valid_event_ids={"1"},
    )
    assert first["written"] == 1
    assert second["written"] == 0
    assert second["skipped"] == 1
    assert second["total"] == 1


def test_new_candidates_accumulate_alongside_existing(work_tmp):
    base = {"kind": "error_pattern", "evidence_ids": ["1"]}
    write_candidates(
        data_dir=work_tmp / "data", domain_id="d",
        candidates=[{**base, "title": "问题一"}], run_id=1, valid_event_ids={"1"},
    )
    stats = write_candidates(
        data_dir=work_tmp / "data", domain_id="d",
        candidates=[{**base, "title": "问题二"}], run_id=2, valid_event_ids={"1"},
    )
    assert stats["written"] == 1
    assert stats["total"] == 2


def test_candidate_id_is_stable_and_title_based():
    a = candidate_id("d", {"title": "同一个标题"})
    b = candidate_id("d", {"title": "同一个标题"})
    c = candidate_id("d", {"title": "另一个标题"})
    assert a == b
    assert a != c
    assert a.startswith("cand_")


def test_staging_path_layout_matches_revision_note():
    path = staging_path("/data", "computer_monitoring")
    assert path.as_posix().endswith(
        "knowledge/computer_monitoring/staging/candidates.yaml"
    )


def test_load_candidates_rejects_non_list_file(work_tmp):
    from app.analysis.knowledge_staging import KnowledgeStagingError

    path = work_tmp / "c.yaml"
    path.write_text("just: a mapping\n", encoding="utf-8")
    with pytest.raises(KnowledgeStagingError, match="必须是列表"):
        load_candidates(path)


# ============================================================
# 回归（2026-09-24 真机核验）：分组与事故必须落库
#
# 此前它们**只发生在内存里**：177 条 Run 跑完，event_groups 与 incidents
# 两张表始终是 0 行 —— "事故记忆库"永远是空的，"历史相似事故注入"也无从谈起。
# ============================================================


def _seed_events(session, project, source, count: int = 4) -> list[int]:
    ids: list[int] = []
    for i in range(count):
        row = Event(
            project_id=project.id,
            source_id=source.id,
            timestamp=T0,
            event_type="log",
            severity="high" if i % 2 == 0 else "low",
            message=f"event-{i}",
        )
        session.add(row)
        session.flush()
        ids.append(int(row.id))
    return ids


def _grouping(session, project, run, source, event_ids: list[int], *, reused_incident_id=None):
    incident = {
        "signature": "ProcessCrash",
        "severity": "high",
        "group_ids": [0, 1],
        "time_start": T0,
        "time_end": T0,
    }
    if reused_incident_id is not None:
        incident["reused_incident_id"] = reused_incident_id
    return persist_grouping(
        project_id=project.id,
        run_id=run.id,
        groups=[
            {
                "group_key": "k1",
                "source_id": source.id,
                "template": "t1",
                "event_ids": event_ids[:2],
                "event_count": 2,
                "time_start": T0,
                "time_end": T0,
            },
            {
                "group_key": "k2",
                "source_id": source.id,
                "template": "t2",
                "event_ids": event_ids[2:],
                "event_count": 2,
                "time_start": T0,
                "time_end": T0,
            },
        ],
        incidents=[
            incident
        ],
        group_repository=EventGroupRepository(session),
        incident_repository=IncidentRepository(session),
        event_repository=EventRepository(session),
    )


def test_grouping_and_incident_are_persisted_and_events_backfilled(session, ctx):
    """计划第 365 行验收：事件可归入 EventGroup、再归并为 Incident 并查回。"""
    project, source, run = ctx
    event_ids = _seed_events(session, project, source, 4)

    result = _grouping(session, project, run, source, event_ids)
    session.flush()
    session.expire_all()

    assert len(result.group_ids) == 2 and len(result.incident_ids) == 1
    assert result.groups_created == 2 and result.incidents_created == 1
    assert result.events_grouped == 4
    assert result.events_linked_to_incident == 4

    # 事件上要能查回分组与事故（真源是 Event.group_id / incident_id）
    stored = EventRepository(session).list_all(project.id)
    assert {e.group_id for e in stored} == set(result.group_ids)
    assert {e.incident_id for e in stored} == {result.incident_ids[0]}

    # Incidents.group_ids 是真源，成员事件由它派生（不双写）
    incidents = IncidentRepository(session)
    assert sorted(incidents.group_ids_of(project.id, result.incident_ids[0])) == sorted(
        result.group_ids
    )
    assert sorted(incidents.event_ids_of(project.id, result.incident_ids[0])) == sorted(
        event_ids
    )
    # 结论回填用的映射
    assert result.incident_by_event[str(event_ids[0])] == result.incident_ids[0]


def test_repeated_group_key_reuses_the_existing_row(session, ctx):
    """`(project, source, group_key)` 是唯一键：同一模板簇必须复用而不是再插一行。

    第二次还带上 `reused_incident_id`（`merge_into_incidents` 匹配到历史事故时就是这么给的）：
    事故要被**并入**而不是新开一条，`group_ids` 去重合并。
    """
    project, source, run = ctx
    event_ids = _seed_events(session, project, source, 2)

    first = _grouping(session, project, run, source, event_ids + event_ids)
    session.flush()
    second = _grouping(
        session,
        project,
        run,
        source,
        event_ids + event_ids,
        reused_incident_id=first.incident_ids[0],
    )
    session.flush()
    session.expire_all()

    assert first.groups_created == 2
    assert second.groups_created == 0, "同一 group_key 又插了一行，会撞唯一约束"
    assert second.group_ids == first.group_ids
    assert second.incidents_created == 0 and second.incidents_reused == 1
    assert second.incident_ids == first.incident_ids, "历史同类事故要并入，不是新开"


def test_insight_is_linked_to_the_incident_of_its_evidence(session, ctx):
    """结论与事故的关联**按证据事件**判定，不按标题字符串碰运气。"""
    project, source, run = ctx
    event_ids = _seed_events(session, project, source, 4)
    grouping = _grouping(session, project, run, source, event_ids)
    session.flush()

    insights = InsightRepository(session)
    persist_insights(
        project_id=project.id,
        run_id=run.id,
        insights=[_insight("崩溃相关结论", [str(event_ids[0])])],
        valid_event_ids={str(e) for e in event_ids},
        insight_repository=insights,
        evidence_repository=EvidenceRepository(session),
        source_id=source.id,
        incident_by_event=grouping.incident_by_event,
    )
    session.flush()
    session.expire_all()

    stored = insights.list_for_run(project.id, run.id)
    assert stored and stored[0].incident_id == grouping.incident_ids[0]


def test_recent_incidents_carry_source_and_signature_for_context(session, ctx):
    """历史事故注入 Context 需要 source_id 与 signature —— 二者都由落库派生。"""
    project, source, run = ctx
    event_ids = _seed_events(session, project, source, 2)
    _grouping(session, project, run, source, event_ids + event_ids)
    session.flush()
    session.expire_all()

    history = IncidentRepository(session).recent_for_context(project.id)
    assert len(history) == 1
    assert history[0]["signature"] == "ProcessCrash", "签名要从标题前缀取回"
    assert history[0]["source_id"] == source.id, "source_id 要从成员分组派生"
    assert history[0]["time_start"] == T0


# ============================================================
# 回归：候选知识写 staging + confirmed 知识参与分析
# ============================================================


def test_worker_stages_knowledge_candidates(monkeypatch, work_tmp):
    """计划第 751 行：候选知识写入 staging。

    不写的话报告页「待确认知识」区永远是空的 —— 8.1 知识闭环只剩"人工确认"这一半，
    没有候选可确认（真机核验：staging 文件始终不存在）。
    """
    from app.tasks import analysis as task_module

    class _Settings:
        data_dir = str(work_tmp)

    class _Domain:
        domain_id = "computer_monitoring"

    class _Result:
        knowledge_candidates = [
            {
                "kind": "error_pattern",
                "title": "磁盘写入延迟飙升",
                "description": "iostat await 持续 > 100ms",
                "match": {"metric_name": "disk_await"},
                "evidence_ids": ["11"],
                "status": "draft",
            }
        ]
        valid_event_ids = ["11"]

    monkeypatch.setattr(task_module, "_settings", lambda: _Settings())
    task_module._stage_knowledge_candidates(_Domain(), _Result(), run_id=7)

    path = staging_path(work_tmp, "computer_monitoring")
    assert path.is_file(), "候选没有落进 staging"
    stored = load_candidates(path)
    assert len(stored) == 1
    assert stored[0]["title"] == "磁盘写入延迟飙升"
    assert stored[0]["status"] == "draft", "候选一律 draft，人工确认前不参与结论"
    assert stored[0]["evidence"]["event_ids"] == ["11"]


def test_confirmed_knowledge_is_loaded_for_analysis(monkeypatch, work_tmp):
    """人工确认过的知识必须参与分析 —— 否则"确认后下次能命中"不成立。"""
    import yaml

    from app.domains.wiring import build_default_registry
    from app.tasks import analysis as task_module

    confirmed = work_tmp / "knowledge" / "computer_monitoring" / "confirmed" / "k.yaml"
    confirmed.parent.mkdir(parents=True, exist_ok=True)
    confirmed.write_text(
        yaml.safe_dump(
            [
                {
                    "id": "k_runtime_1",
                    "kind": "error_pattern",
                    "title": "CPU 持续高于 90%",
                    "match": {"metric_name": "cpu_used", "condition": "value > 90"},
                    "evidence": {"run_id": "1", "event_ids": ["11"]},
                    "confidence": 0.9,
                    "status": "confirmed",
                }
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    class _Settings:
        data_dir = str(work_tmp)

    monkeypatch.setattr(task_module, "_settings", lambda: _Settings())
    domain = build_default_registry().load("computer_monitoring")
    entries = task_module.load_confirmed_knowledge(domain)
    assert [e.id for e in entries] == ["k_runtime_1"]
    assert entries[0].status == "confirmed"
