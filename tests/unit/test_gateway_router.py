"""Gateway 与 Router 单测 —— **全部离线**（用 httpx.MockTransport，不联网、不花钱）。

覆盖计划第 621 行验收的三条里可离线验证的两条：
  2. 切换型号只改环境变量、不改业务代码；
  3. token 与成本被记录。
第 1 条（接通真实供应商）需要真实 API 调用，单独放在
`tests/integration/test_gateway_live.py`，默认跳过。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.gateways.base import (
    VALID_REASONING_EFFORTS,
    ConfigurationError,
    L0DoesNotCallModelError,
    ModelResult,
    ModelUnavailableError,
    StructuredOutputError,
)
from app.gateways.deepseek import (
    DeepSeekGateway,
    build_structured_prompt,
    parse_json_content,
)
from app.gateways.router import Router, read_tier_configs

# ============================================================
# 假的 Settings / 假网关
# ============================================================


class FakeSettings:
    """最小 Settings 替身，只带 Router 需要的字段。"""

    def __init__(self, **overrides: Any) -> None:
        defaults = {
            "model_l1": "test-model-small",
            "model_l2": "test-model-medium",
            "model_l3": "test-model-large",
            "model_l1_reasoning": "off",
            "model_l2_reasoning": "low",
            "model_l3_reasoning": "high",
            "model_l1_price_input_per_1m": 1.0,
            "model_l1_price_output_per_1m": 4.0,
            "model_l2_price_input_per_1m": 1.0,
            "model_l2_price_output_per_1m": 4.0,
            "model_l3_price_input_per_1m": 1.0,
            "model_l3_price_output_per_1m": 4.0,
            "model_provider_base_url": "https://example.invalid",
        }
        defaults.update(overrides)
        for key, value in defaults.items():
            setattr(self, key, value)


class RecordingGateway:
    """记录调用参数的假网关，用于验证 Router 传对了型号与档位。"""

    provider = "fake"

    def __init__(self, *, content: str = "ok", parsed: dict | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._content = content
        self._parsed = parsed

    def generate(self, prompt, schema, max_output_tokens, *, model, reasoning=None):
        self.calls.append(
            {
                "prompt": prompt,
                "schema": schema,
                "max_output_tokens": max_output_tokens,
                "model": model,
                "reasoning": reasoning,
            }
        )
        return ModelResult(
            content=self._content,
            parsed=self._parsed,
            tokens_input=1000,
            tokens_output=250,
            model=model,
            raw={"fake": True},
        )


class RecordingRunRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, dict]] = []

    def append_model_call(self, project_id: int, run_id: int, call: dict) -> bool:
        self.calls.append((project_id, run_id, call))
        return True


# ============================================================
# 等级解析
# ============================================================


def test_reads_all_three_tiers_from_settings():
    configs = read_tier_configs(FakeSettings())
    assert set(configs) == {"L1", "L2", "L3"}
    assert configs["L3"].model == "test-model-large"
    assert configs["L3"].reasoning == "high"
    assert configs["L1"].reasoning == "off"


def test_missing_model_raises_at_construction_not_first_call():
    """配置缺失要在启动期暴露，否则会伪装成"模型不可用"让人往网络方向排查。"""
    with pytest.raises(ConfigurationError, match="L2 未配置型号"):
        read_tier_configs(FakeSettings(model_l2=""))


def test_invalid_reasoning_is_refused_with_actionable_message():
    with pytest.raises(ConfigurationError, match="medium"):
        read_tier_configs(FakeSettings(model_l2_reasoning="medium"))


@pytest.mark.parametrize("effort", VALID_REASONING_EFFORTS)
def test_all_valid_reasoning_efforts_accepted(effort: str):
    read_tier_configs(FakeSettings(model_l3_reasoning=effort))


def test_empty_reasoning_is_allowed_and_means_no_effort_field():
    """档位留空是合法的（表示不指定），不该报错。"""
    configs = read_tier_configs(FakeSettings(model_l1_reasoning=""))
    assert configs["L1"].reasoning == ""


def test_l0_is_refused_and_does_not_fall_back_to_l1():
    """L0 是代码/规则等级；静默回退会让"本应零成本"的路径产生真实费用。"""
    gateway = RecordingGateway()
    router = Router(gateway, FakeSettings())
    with pytest.raises(L0DoesNotCallModelError, match="不调用模型"):
        router.generate(tier="L0", prompt="x", max_output_tokens=10)
    assert gateway.calls == []


def test_unknown_tier_is_refused():
    router = Router(RecordingGateway(), FakeSettings())
    with pytest.raises(ValueError, match="未知模型等级"):
        router.resolve("L9")


# ============================================================
# 验收 2：切换型号只改环境变量
# ============================================================


def test_switching_model_needs_only_a_config_change():
    """同一份 Router 代码，配置不同则路由到不同型号。"""
    calls = []
    for model_l3 in ("vendor-a-large", "vendor-b-large", "local-ollama:70b"):
        gateway = RecordingGateway()
        router = Router(gateway, FakeSettings(model_l3=model_l3))
        router.generate(tier="L3", prompt="p", max_output_tokens=100)
        calls.append(gateway.calls[0]["model"])

    assert calls == ["vendor-a-large", "vendor-b-large", "local-ollama:70b"]


def test_router_module_contains_no_model_literals():
    """红线 2：型号不进业务代码 —— Router 模块里一个都不能有。"""
    import ast
    import pathlib

    from app.gateways import router as router_module

    tree = ast.parse(
        pathlib.Path(router_module.__file__).read_text(encoding="utf-8")
    )
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            if any(token in lowered for token in ("deepseek", "gpt-", "claude", "qwen")):
                literals.append(node.value)
    assert literals == [], f"Router 里出现了型号字面量：{literals}"


def test_router_passes_configured_reasoning_through():
    gateway = RecordingGateway()
    router = Router(gateway, FakeSettings())
    router.generate(tier="L1", prompt="p", max_output_tokens=10)
    router.generate(tier="L3", prompt="p", max_output_tokens=10)
    assert [c["reasoning"] for c in gateway.calls] == ["off", "high"]


def test_fingerprint_records_the_tier_mapping():
    """型号可从环境变量切换，不把映射记下来就无法复现历史结果。"""
    router = Router(RecordingGateway(), FakeSettings())
    fingerprint = router.fingerprint()
    assert fingerprint == {
        "L1": "test-model-small",
        "L2": "test-model-medium",
        "L3": "test-model-large",
    }


# ============================================================
# 验收 3：token 与成本被记录
# ============================================================


def test_cost_is_computed_from_actual_token_usage():
    """输入 1000 × 1元/1M + 输出 250 × 4元/1M = 0.002 元。"""
    router = Router(RecordingGateway(), FakeSettings())
    result = router.generate(tier="L3", prompt="p", max_output_tokens=100, record=False)
    assert result.tokens_input == 1000
    assert result.tokens_output == 250
    assert result.cost == pytest.approx(0.002)


def test_call_is_recorded_to_run_with_promised_fields():
    """计划第 352 行约定的字段：call_id/tier/model/timestamp/tokens_*/cost/status。"""
    runs = RecordingRunRepository()
    router = Router(RecordingGateway(), FakeSettings(), run_repository=runs)
    router.generate(tier="L2", prompt="p", max_output_tokens=100, project_id=7, run_id=42)

    assert len(runs.calls) == 1
    project_id, run_id, entry = runs.calls[0]
    assert (project_id, run_id) == (7, 42)
    for field in (
        "call_id",
        "tier",
        "model",
        "timestamp",
        "tokens_input",
        "tokens_output",
        "cost",
        "status",
    ):
        assert field in entry, f"缺少约定字段 {field}"
    assert entry["tier"] == "L2"
    assert entry["model"] == "test-model-medium"
    assert entry["status"] == "ok"


def test_recorded_call_is_json_serializable():
    """要写进 JSONB 的 model_calls，必须天生可序列化（阶段 04/05 都栽过）。"""
    runs = RecordingRunRepository()
    router = Router(RecordingGateway(), FakeSettings(), run_repository=runs)
    router.generate(tier="L1", prompt="p", max_output_tokens=10, project_id=1, run_id=2)
    json.dumps(runs.calls[0][2])


def test_explicit_call_id_is_preserved():
    runs = RecordingRunRepository()
    router = Router(RecordingGateway(), FakeSettings(), run_repository=runs)
    router.generate(
        tier="L1", prompt="p", max_output_tokens=10, project_id=1, run_id=2, call_id="c_fixed"
    )
    assert runs.calls[0][2]["call_id"] == "c_fixed"


def test_recording_is_skipped_loudly_when_ids_missing():
    """记不了就要留痕说明，不能静默当作记过了。"""
    router = Router(RecordingGateway(), FakeSettings())
    result = router.generate(tier="L1", prompt="p", max_output_tokens=10)
    assert "_recording_skipped" in result.raw


def test_estimate_cost_is_available_for_pre_checks():
    """阶段 07 的 Pre-check 需要在不调用模型的前提下估成本。"""
    router = Router(RecordingGateway(), FakeSettings())
    assert router.estimate_cost("L3", 8000, 2000) == pytest.approx(0.016)


# ============================================================
# DeepSeek Gateway（离线：MockTransport）
# ============================================================


def _gateway_with(handler) -> DeepSeekGateway:
    client = httpx.Client(
        base_url="https://api.example.invalid",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-key"},
    )
    return DeepSeekGateway(
        base_url="https://api.example.invalid", api_key="test-key", client=client
    )


def _ok_handler(content: str = "hello", *, prompt_tokens=11, completion_tokens=5):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "responded-model",
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "prompt_cache_hit_tokens": 3,
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            },
        )

    return handler


def test_gateway_returns_content_and_usage():
    gateway = _gateway_with(_ok_handler("plain text"))
    result = gateway.generate("hi", None, 50, model="any-model")
    assert result.content == "plain text"
    assert result.parsed is None
    assert result.tokens_input == 11
    assert result.tokens_output == 5
    assert result.cached_tokens == 3
    assert result.reasoning_tokens == 2
    # 用服务端回报的型号，而不是请求里写的
    assert result.model == "responded-model"


def test_gateway_sends_off_as_thinking_disabled():
    """off 不是一个 reasoning_effort 取值，而是"关闭思考"。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "x"}}], "usage": {}})

    gateway = _gateway_with(handler)
    gateway.generate("hi", None, 10, model="m", reasoning="off")
    assert captured["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_gateway_passes_enabled_reasoning_effort(effort: str):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": "x"}}], "usage": {}})

    gateway = _gateway_with(handler)
    gateway.generate("hi", None, 10, model="m", reasoning=effort)
    assert captured["thinking"] == {"type": "enabled"}
    assert captured["reasoning_effort"] == effort


def test_gateway_sets_json_object_response_format_when_schema_given():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "m", "choices": [{"message": {"content": '{"a": 1}'}}], "usage": {}},
        )

    gateway = _gateway_with(handler)
    result = gateway.generate("hi", {"type": "object"}, 10, model="m")
    assert captured["response_format"] == {"type": "json_object"}
    assert result.parsed == {"a": 1}
    # schema 要求要并进 prompt（generate 没有单独的 system 参数）
    assert "JSON Schema" in captured["messages"][0]["content"]


def test_structured_request_raises_instead_of_degrading_to_text():
    """要求结构化却拿到散文，必须报错 —— 不能给下游一个"看着有结构"的字符串。"""
    gateway = _gateway_with(_ok_handler("I could not produce JSON, sorry."))
    with pytest.raises(StructuredOutputError):
        gateway.generate("hi", {"type": "object"}, 10, model="m")


def test_server_error_becomes_model_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    gateway = _gateway_with(handler)
    with pytest.raises(ModelUnavailableError, match="503"):
        gateway.generate("hi", None, 10, model="m")


def test_rate_limit_is_reported_as_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    gateway = _gateway_with(handler)
    with pytest.raises(ModelUnavailableError, match="429"):
        gateway.generate("hi", None, 10, model="m")


def test_network_error_becomes_model_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    gateway = _gateway_with(handler)
    with pytest.raises(ModelUnavailableError, match="ConnectError"):
        gateway.generate("hi", None, 10, model="m")


def test_no_choices_is_an_error_not_an_empty_string():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "m", "choices": []})

    gateway = _gateway_with(handler)
    with pytest.raises(ModelUnavailableError, match="choices"):
        gateway.generate("hi", None, 10, model="m")


def test_gateway_rejects_empty_model_and_bad_max_tokens():
    gateway = _gateway_with(_ok_handler())
    with pytest.raises(ValueError, match="model 不能为空"):
        gateway.generate("hi", None, 10, model="")
    with pytest.raises(ValueError, match="max_output_tokens"):
        gateway.generate("hi", None, 0, model="m")


def test_gateway_requires_base_url_and_key():
    with pytest.raises(ValueError, match="base_url"):
        DeepSeekGateway(base_url="", api_key="k")
    with pytest.raises(ValueError, match="api_key"):
        DeepSeekGateway(base_url="https://x.invalid", api_key="")


# ============================================================
# JSON 容错解析
# ============================================================


@pytest.mark.parametrize(
    "content",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'sure, here it is: {"a": 1} hope that helps',
    ],
)
def test_parse_json_content_tolerates_common_wrappers(content: str):
    """格式容错不是语义降级：拿到的仍是结构化对象。"""
    assert parse_json_content(content) == {"a": 1}


@pytest.mark.parametrize("content", ["", "   ", "no json here", "[1, 2, 3]", "{broken"])
def test_parse_json_content_returns_none_when_truly_unparseable(content: str):
    assert parse_json_content(content) is None


def test_build_structured_prompt_includes_schema_and_instruction():
    prompt = build_structured_prompt("do the thing", {"type": "object", "required": ["a"]})
    assert "do the thing" in prompt
    assert "JSON Schema" in prompt
    assert '"required"' in prompt
    assert "不要输出任何解释文字" in prompt
