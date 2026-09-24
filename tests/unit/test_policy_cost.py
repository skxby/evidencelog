"""Policy 与 Cost Controller 单测 —— 对应计划第 675 行验收四条。

  1. 预算不足拒绝创建
  2. 人为设置极小预算能触发 partial_success 且明确标注「不完整」
  3. 成本被正确累加
  4. 无法构造出无限调用的 Run
"""

from __future__ import annotations

import json

import pytest

from app.gateways.base import TierConfig
from app.policy.cost_controller import (
    CONTEXT_BUDGET_BY_TIER,
    OUTPUT_RATIO,
    CostController,
    StopExecution,
    estimate_run_cost,
)
from app.policy.policy import (
    DEFAULT_POLICY,
    SAFETY_MARGIN,
    STOP_BUDGET_EXCEEDED,
    STOP_CALL_LIMIT,
    STOP_RUNTIME_LIMIT,
    STOP_TOKEN_LIMIT,
    PolicyError,
    PolicyOverrides,
    RunPolicy,
)
from app.policy.store import ProjectPolicyStore, get_policy_store, reset_policy_store

TIERS = {
    "L1": TierConfig("L1", "m-small", "off", 1.0, 4.0),
    "L2": TierConfig("L2", "m-medium", "low", 1.0, 4.0),
    "L3": TierConfig("L3", "m-large", "high", 1.0, 4.0),
}


class FakeClock:
    """可控时钟，让运行时长上限可被确定性地测试。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def controller(**policy_kwargs) -> CostController:
    return CostController(
        RunPolicy(**policy_kwargs), tier_configs=TIERS, clock=FakeClock()
    )


# ============================================================
# 策略本身的合法性
# ============================================================


def test_default_policy_matches_plan_defaults():
    """计划第 631–637 行的示例值。"""
    assert DEFAULT_POLICY.max_model_calls_per_run == 5
    assert DEFAULT_POLICY.max_tokens_per_run == 12_000
    assert DEFAULT_POLICY.max_runtime_seconds == 180
    assert DEFAULT_POLICY.allowed_tiers == ("L1", "L2", "L3")
    assert DEFAULT_POLICY.run_max_cost == 0.30


@pytest.mark.parametrize("field_name", ["max_model_calls_per_run", "max_tokens_per_run", "max_runtime_seconds"])
@pytest.mark.parametrize("bad", [0, -1])
def test_zero_or_negative_limits_are_refused(field_name: str, bad: int):
    """验收 4 的前提：上限本身不能是 0 或负数，否则等于没有上限。"""
    with pytest.raises(PolicyError, match=field_name):
        RunPolicy(**{field_name: bad})


def test_negative_cost_limit_is_refused():
    with pytest.raises(PolicyError, match="run_max_cost"):
        RunPolicy(run_max_cost=-0.1)


def test_empty_allowed_tiers_is_refused():
    with pytest.raises(PolicyError, match="allowed_tiers"):
        RunPolicy(allowed_tiers=())


def test_l0_cannot_be_listed_as_an_allowed_model_tier():
    """L0 不调用模型，把它列进"允许的模型等级"是概念混淆。"""
    with pytest.raises(PolicyError, match="L0"):
        RunPolicy(allowed_tiers=("L0", "L1"))


def test_snapshot_is_json_serializable():
    """快照要写进 run_metadata（计划第 356 行），必须可序列化。"""
    json.dumps(DEFAULT_POLICY.snapshot())
    assert DEFAULT_POLICY.snapshot()["allowed_tiers"] == ["L1", "L2", "L3"]


# ============================================================
# 成本估算（按 Context 预算，不按事件条数）
# ============================================================


def test_estimate_uses_context_budget_not_event_count():
    """计划第 643 行：按蒸馏后 Context 预算估，不是按原始事件数线性估。"""
    estimate = estimate_run_cost("L3", TIERS["L3"])
    assert estimate.estimated_input_tokens == CONTEXT_BUDGET_BY_TIER["L3"] == 8000
    assert estimate.estimated_output_tokens == int(8000 * OUTPUT_RATIO)


def test_estimate_includes_ten_percent_safety_margin():
    estimate = estimate_run_cost("L1", TIERS["L1"])
    assert estimate.estimated_cost_with_margin == pytest.approx(
        estimate.estimated_cost * (1 + SAFETY_MARGIN)
    )
    assert SAFETY_MARGIN == 0.10


def test_estimate_scales_with_planned_calls():
    one = estimate_run_cost("L2", TIERS["L2"], planned_calls=1)
    three = estimate_run_cost("L2", TIERS["L2"], planned_calls=3)
    assert three.estimated_input_tokens == one.estimated_input_tokens * 3
    assert three.estimated_cost == pytest.approx(one.estimated_cost * 3)


@pytest.mark.parametrize("bad_tier", ["L0", "L9", "l4"])
def test_estimate_refuses_non_model_tiers(bad_tier: str):
    with pytest.raises(ValueError, match="等级"):
        estimate_run_cost(bad_tier, TIERS["L1"])


def test_estimate_refuses_zero_planned_calls():
    with pytest.raises(ValueError, match="planned_calls"):
        estimate_run_cost("L1", TIERS["L1"], planned_calls=0)


# ============================================================
# 验收 1：预算不足拒绝创建
# ============================================================


def test_normal_budget_allows_creation():
    result = controller().pre_check("L1")
    assert result.allowed is True
    assert result.estimate is not None


def test_tiny_run_budget_refuses_creation_with_reason():
    result = controller(run_max_cost=0.0001).pre_check("L3")
    assert result.allowed is False
    assert result.reason == STOP_BUDGET_EXCEEDED
    assert "超过单次上限" in result.message


def test_insufficient_project_budget_refuses_creation():
    c = CostController(
        RunPolicy(), tier_configs=TIERS, project_budget_total=0.001, project_budget_used=0.0
    )
    result = c.pre_check("L3")
    assert result.allowed is False
    assert "剩余预算" in result.message


def test_exhausted_project_budget_refuses_creation():
    c = CostController(
        RunPolicy(), tier_configs=TIERS, project_budget_total=10.0, project_budget_used=10.0
    )
    assert c.pre_check("L1").allowed is False


def test_disallowed_tier_refuses_creation():
    result = controller(allowed_tiers=("L1",)).pre_check("L3")
    assert result.allowed is False
    assert "allowed_tiers" in result.message


def test_planned_calls_over_limit_refuses_creation():
    result = controller(max_model_calls_per_run=1).pre_check("L1", planned_calls=3)
    assert result.allowed is False
    assert result.reason == STOP_CALL_LIMIT


def test_token_limit_refuses_creation():
    result = controller(max_tokens_per_run=100).pre_check("L3")
    assert result.allowed is False
    assert result.reason == STOP_TOKEN_LIMIT


def test_precheck_result_is_serializable():
    json.dumps(controller().pre_check("L1").as_dict())
    json.dumps(controller(run_max_cost=0.0001).pre_check("L3").as_dict())


# ============================================================
# 验收 4：无法构造出无限调用的 Run
# ============================================================


def test_loop_is_stopped_exactly_at_the_call_limit():
    c = controller(max_model_calls_per_run=3)
    c.start()
    executed = 0
    with pytest.raises(StopExecution):
        for _ in range(100):  # 一个"想跑 100 次"的循环
            c.mid_check(tier="L1")
            c.record_call(tokens_input=10, tokens_output=5, cost=0.0001)
            executed += 1
    assert executed == 3, f"实际执行了 {executed} 次，应恰好被限制在 3 次"
    assert c.usage.calls == 3


def test_loop_is_stopped_by_token_limit():
    c = controller(max_tokens_per_run=100, max_model_calls_per_run=999)
    c.start()
    executed = 0
    with pytest.raises(StopExecution) as excinfo:
        for _ in range(100):
            c.mid_check(tier="L1")
            c.record_call(tokens_input=50, tokens_output=50, cost=0.0001)
            executed += 1
    assert excinfo.value.reason == STOP_TOKEN_LIMIT
    assert executed == 1


def test_loop_is_stopped_by_cost_limit():
    c = controller(run_max_cost=0.005, max_model_calls_per_run=999, max_tokens_per_run=10**9)
    c.start()
    executed = 0
    with pytest.raises(StopExecution) as excinfo:
        for _ in range(100):
            c.mid_check(tier="L1")
            c.record_call(tokens_input=1, tokens_output=1, cost=0.002)
            executed += 1
    assert excinfo.value.reason == STOP_BUDGET_EXCEEDED
    assert executed == 3  # 0.002×3 = 0.006 ≥ 0.005，第 4 次前被拦


def test_loop_is_stopped_by_runtime_limit():
    clock = FakeClock()
    c = CostController(
        RunPolicy(max_runtime_seconds=10, max_model_calls_per_run=999),
        tier_configs=TIERS,
        clock=clock,
    )
    c.start()
    executed = 0
    with pytest.raises(StopExecution) as excinfo:
        for _ in range(100):
            c.mid_check(tier="L1")
            c.record_call(tokens_input=1, tokens_output=1, cost=0.0)
            executed += 1
            clock.advance(4)  # 每次调用"耗时" 4 秒
    assert excinfo.value.reason == STOP_RUNTIME_LIMIT
    assert executed == 3


def test_runtime_limit_cannot_be_bypassed_by_never_starting():
    """没调 start() 时 elapsed 恒为 0，但那是调用方的责任；
    这里确认 start() 之后计时确实生效（否则时长上限形同虚设）。"""
    clock = FakeClock()
    c = CostController(RunPolicy(max_runtime_seconds=5), tier_configs=TIERS, clock=clock)
    assert c.elapsed_seconds() == 0.0
    c.start()
    clock.advance(6)
    assert c.elapsed_seconds() == 6.0
    with pytest.raises(StopExecution, match="上限"):
        c.mid_check(tier="L1")


def test_check_timeout_reports_runtime_limit():
    clock = FakeClock()
    c = CostController(RunPolicy(max_runtime_seconds=5), tier_configs=TIERS, clock=clock)
    c.start()
    clock.advance(10)
    with pytest.raises(StopExecution) as excinfo:
        c.check_timeout()
    assert excinfo.value.reason == STOP_RUNTIME_LIMIT


def test_mid_check_refuses_disallowed_tier_mid_run():
    """即使中途换等级，也要被 allowed_tiers 拦住。"""
    c = controller(allowed_tiers=("L1",))
    c.start()
    with pytest.raises(StopExecution, match="allowed_tiers"):
        c.mid_check(tier="L3")


# ============================================================
# 验收 2：极小预算触发 partial_success 且明确标注不完整
# ============================================================


def test_partial_success_metadata_marks_incompleteness():
    c = controller(max_model_calls_per_run=1)
    c.start()
    c.mid_check(tier="L1")
    c.record_call(tokens_input=100, tokens_output=20, cost=0.001)
    metadata = c.partial_success_metadata(STOP_CALL_LIMIT)

    assert metadata["stop_reason"] == STOP_CALL_LIMIT
    assert "不完整" in metadata["note"]
    assert "completed_phases" in metadata
    assert "skipped_phases" in metadata
    json.dumps(metadata)


def test_explicit_stop_reason_is_not_overwritten_by_usage():
    """踩过的坑：usage 里也有 stop_reason（初始 None），展开在后会覆盖显式值，
    让中断原因变成 null —— 结果看起来完整、其实没有原因。"""
    c = controller()
    c.start()
    metadata = c.partial_success_metadata(STOP_TOKEN_LIMIT)
    assert metadata["stop_reason"] == STOP_TOKEN_LIMIT


def test_partial_success_accepts_custom_note():
    c = controller()
    metadata = c.partial_success_metadata(STOP_BUDGET_EXCEEDED, note="预算耗尽，已返回前两阶段结果")
    assert metadata["note"] == "预算耗尽，已返回前两阶段结果"


# ============================================================
# 验收 3：成本被正确累加
# ============================================================


def test_cost_and_tokens_accumulate_across_calls():
    c = controller()
    c.start()
    for tokens_in, tokens_out, cost in ((1000, 250, 0.002), (2000, 500, 0.004)):
        c.record_call(tokens_input=tokens_in, tokens_output=tokens_out, cost=cost)

    assert c.usage.calls == 2
    assert c.usage.tokens_input == 3000
    assert c.usage.tokens_output == 750
    assert c.usage.tokens_total == 3750
    assert c.usage.cost == pytest.approx(0.006)


def test_post_check_returns_accumulated_usage():
    c = controller()
    c.start()
    c.record_call(tokens_input=100, tokens_output=20, cost=0.001)
    summary = c.post_check()
    assert summary["calls"] == 1
    assert summary["tokens_total"] == 120
    assert summary["cost"] == pytest.approx(0.001)
    json.dumps(summary)


def test_usage_dict_is_json_serializable_with_cost_rounded():
    c = controller()
    c.start()
    c.record_call(tokens_input=1, tokens_output=1, cost=0.123456789)
    assert c.usage.as_dict()["cost"] == 0.123457


# ============================================================
# 项目级策略覆盖
# ============================================================


def test_project_override_changes_only_specified_fields():
    store = ProjectPolicyStore()
    resolved = store.set_override(7, run_max_cost=1.5)
    assert resolved.run_max_cost == 1.5
    assert resolved.max_model_calls_per_run == DEFAULT_POLICY.max_model_calls_per_run
    assert store.policy_for(7).run_max_cost == 1.5
    assert store.policy_for(8).run_max_cost == DEFAULT_POLICY.run_max_cost


def test_override_rejects_unknown_field():
    store = ProjectPolicyStore()
    with pytest.raises(PolicyError, match="未知的策略字段"):
        store.set_override(1, not_a_field=1)


def test_override_of_invalid_value_is_refused_and_not_stored():
    store = ProjectPolicyStore()
    with pytest.raises(PolicyError):
        store.set_override(3, max_model_calls_per_run=0)
    assert store.policy_for(3).max_model_calls_per_run == DEFAULT_POLICY.max_model_calls_per_run


def test_clear_override_restores_default():
    store = ProjectPolicyStore()
    store.set_override(5, run_max_cost=9.9)
    store.clear_override(5)
    assert store.policy_for(5).run_max_cost == DEFAULT_POLICY.run_max_cost


def test_overrides_apply_converts_list_tiers_to_tuple():
    resolved = PolicyOverrides(values={"allowed_tiers": ["L1", "L2"]}).apply(DEFAULT_POLICY)
    assert resolved.allowed_tiers == ("L1", "L2")


def test_policy_store_singleton():
    reset_policy_store()
    assert get_policy_store() is get_policy_store()
    reset_policy_store()


# ============================================================
# 回归：声明了却没人读的配置（本轮扫出来的三处）
# ============================================================


def test_default_run_max_cost_env_actually_drives_the_policy(monkeypatch):
    """`DEFAULT_RUN_MAX_COST` 必须真的决定单次上限。

    此前它是个摆设：`get_policy_store()` 用的是 dataclass 里硬编码的 0.30，
    env 改成 0.05 也照样按 0.30 放行 —— 而它是一道**花钱的闸门**，
    失效方向恰好是"多花钱"。

    注意 `get_settings` 是 `lru_cache` 的：改了环境变量必须清缓存，
    否则读到的是进程启动时那份（这个坑本身也值得记住 —— 它会让
    "配置改了没生效"的调查白跑一轮）。
    """
    from app.config import get_settings
    from app.policy.store import policy_from_settings

    monkeypatch.setenv("DEFAULT_RUN_MAX_COST", "0.05")
    get_settings.cache_clear()
    try:
        assert policy_from_settings().run_max_cost == pytest.approx(0.05)

        monkeypatch.setenv("DEFAULT_RUN_MAX_COST", "-1")
        get_settings.cache_clear()
        with pytest.raises(PolicyError):
            policy_from_settings()
    finally:
        get_settings.cache_clear()  # 别把改动留给后面的测试


def test_cache_hit_tokens_are_cheaper_than_full_price():
    """缓存命中的输入 token 必须按命中价计，而不是全额输入价。

    `MODEL_CACHE_HIT_INPUT_PRICE_PER_1M` 同样一直是空头承诺：`cached_tokens`
    被记了下来、却从没进过成本公式，`TierConfig.estimate_cost` 的 docstring
    甚至写着"阶段 07 的成本控制会在此基础上区分命中/未命中"。
    高估的成本会喂给 Mid-check 的「已花成本 ≥ 上限」，让 Run 提前被砍。
    """
    config = TierConfig(
        tier="L1", model="m", reasoning="off",
        price_input_per_1m=0.5, price_output_per_1m=2.0,
        price_cache_hit_input_per_1m=0.02,
    )
    full = config.estimate_cost(1000, 500)
    half = config.estimate_cost(1000, 500, cached_tokens=500)
    all_hit = config.estimate_cost(1000, 500, cached_tokens=1000)

    assert all_hit < half < full, "命中越多应当越便宜"
    # 输出 token 的价钱与命中无关，不能被折扣掉
    output_only = 500 / 1_000_000 * 2.0
    assert all_hit == pytest.approx(1000 / 1_000_000 * 0.02 + output_only)


def test_unset_cache_price_does_not_silently_discount():
    """没配命中价时按正常输入价计 —— **不做静默折扣**。

    悄悄按更便宜的价算，会让账单看起来比实际低，方向正好是危险的
    （和"失败必须明确"同一条道理）。
    """
    config = TierConfig("L1", "m", "off", 0.5, 2.0)  # 没给命中价
    assert config.estimate_cost(1000, 500, cached_tokens=1000) == pytest.approx(
        config.estimate_cost(1000, 500)
    )


def test_cached_tokens_larger_than_input_is_clamped():
    """供应商若给出不合理的命中数，不能算出负数成本。"""
    config = TierConfig("L1", "m", "off", 0.5, 2.0, price_cache_hit_input_per_1m=0.02)
    assert config.estimate_cost(1000, 0, cached_tokens=99999) == pytest.approx(
        1000 / 1_000_000 * 0.02
    )


# ============================================================
# 峰谷价（MODEL_PEAK_PRICE_MULTIPLIER 以前没人读）
# ============================================================


def test_peak_multiplier_applies_only_when_asked():
    """配置里的单价是**闲时价**，高峰时段要乘倍数。

    不乘的话账单被**低估**，而低估的方向更糟：预算是"钱够不够"的闸门，
    低估会让它在该拦的时候不拦。这个倍数以前在 Settings 与 .env 里都有，
    代码里却没人读。
    """
    off_peak_config = TierConfig("L1", "m", "off", 0.5, 2.0)  # 默认倍数 1.0
    assert off_peak_config.estimate_cost(1000, 500, peak=True) == pytest.approx(
        off_peak_config.estimate_cost(1000, 500)
    )

    peak_config = TierConfig("L1", "m", "off", 0.5, 2.0, peak_price_multiplier=2.0)
    assert peak_config.estimate_cost(1000, 500, peak=True) == pytest.approx(
        2 * peak_config.estimate_cost(1000, 500)
    )


def test_peak_window_uses_local_timezone_and_weekdays():
    """峰谷窗口按**本地时区**的工作日判断。

    拿 UTC 直接比小时会把北京时间 10 点看成凌晨 2 点，峰谷整个错位。
    窗口取自 `app/config.py` 里 `model_peak_price_multiplier` 的注释：
    工作日 9:00–12:00、14:00–18:00（北京时间）。
    """
    from datetime import datetime, timezone

    from app.utils.timestamps import is_peak_time

    # 北京时间 = UTC+8；2026-09-24 是周四，2026-09-26 是周六
    assert is_peak_time(datetime(2026, 9, 24, 2, 0, tzinfo=timezone.utc)) is True  # 北京 10:00
    assert is_peak_time(datetime(2026, 9, 24, 7, 0, tzinfo=timezone.utc)) is True  # 北京 15:00
    assert is_peak_time(datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)) is False  # 北京 13:00
    assert is_peak_time(datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)) is False  # 北京 20:00
    assert is_peak_time(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc)) is False  # 周六


# ============================================================
# 月度预算（monthly_budget 以前没人读）
# ============================================================


def test_monthly_budget_refuses_when_the_month_is_used_up():
    """计划第 644 行：「月度预算或 Run 预算不足 → 拒绝创建」。

    `monthly_budget` 在 Settings 与 RunPolicy 里都声明了、README 也写成
    "预算上限"，却从来没有任何地方比过它 —— 又一个"配置项是摆设"。
    """
    controller = CostController(
        RunPolicy(monthly_budget=10.0), tier_configs=TIERS, month_spent=9.999
    )
    result = controller.pre_check("L3")

    assert result.allowed is False
    assert "月度预算" in (result.message or "")
    assert result.reason == STOP_BUDGET_EXCEEDED


def test_monthly_budget_leaves_room_when_not_exhausted():
    controller = CostController(
        RunPolicy(monthly_budget=10.0), tier_configs=TIERS, month_spent=1.0
    )
    assert controller.pre_check("L3").allowed is True


def test_monthly_budget_zero_means_no_monthly_cap():
    """0 表示"不设月度上限"，而不是"一分钱都不能花"。

    默认值若是 0 就被当成硬上限，所有项目都会在月初第一条分析就发不起来 ——
    那是把默认值当成了策略（与 `budget_total` 的 0 同一条道理）。
    """
    controller = CostController(
        RunPolicy(monthly_budget=0.0), tier_configs=TIERS, month_spent=1000.0
    )
    assert controller.pre_check("L3").allowed is True


def test_month_start_follows_configured_timezone():
    """月度起点按配置时区的自然月切，而不是 UTC。"""
    from datetime import datetime, timezone

    from app.policy.wiring import month_start

    class _Settings:
        default_timezone = "Asia/Shanghai"

    start = month_start(
        settings=_Settings(), now=datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)
    )
    # 北京 9/1 00:00 == UTC 8/31 16:00
    assert start == datetime(2026, 8, 31, 16, 0, tzinfo=timezone.utc)


def test_policy_from_settings_reads_both_money_knobs(monkeypatch):
    """两道花钱的闸门都必须从配置读，不能只搬其中一个。

    真机核验发现（2026-09-24）：`policy_from_settings()` 只把
    `DEFAULT_RUN_MAX_COST` 搬进策略，`MONTHLY_BUDGET` 仍取 dataclass 的默认 10.0 ——
    把月度预算改成 ¥0.02 后，真实花费 ¥0.0123 照样能继续发起分析。
    单次上限那半是接上的，所以肉眼与文本审计都看不出问题：
    `.dsh/settings_usage_audit.py` 把 cost_controller 里读**策略对象**属性的
    `.monthly_budget` 记成了"已接线"。**行为测试才是这类缺陷的终点。**
    """
    import app.config as config_module

    from app.policy.store import policy_from_settings

    class _Settings:
        default_run_max_cost = 0.05
        monthly_budget = 0.02

    monkeypatch.setattr(config_module, "get_settings", lambda: _Settings())
    policy = policy_from_settings()
    assert policy.run_max_cost == pytest.approx(0.05)
    assert policy.monthly_budget == pytest.approx(0.02), "月度预算没从配置读，改 .env 不生效"
