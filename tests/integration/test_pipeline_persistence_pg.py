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
    persist_insights,
)
from app.db import SessionLocal
from app.models import AgentRun, DataSource, Project, User
from app.repositories import (
    AgentRunRepository,
    DataSourceRepository,
    EvidenceRepository,
    InsightRepository,
    ProjectRepository,
    UserRepository,
)

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
