"""阶段 06 验收第 1 条：「至少接通一个真实供应商并返回结构化结果」。

这条**需要真实 API 调用，会产生真实费用**，所以默认跳过。要跑它：

```powershell
$env:RUN_LIVE_MODEL_TESTS = "1"
.venv\\Scripts\\python.exe -m pytest tests/integration/test_gateway_live.py -v -s
```

设计取舍：把它做成显式开关而不是默认的一部分，是因为「跑一次测试就花钱」
不是能悄悄发生的事 —— 单测已经用 MockTransport 覆盖了全部协议细节，
这里只验证「真实端点确实按我们以为的方式回话」。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

from app.gateways.base import ModelUnavailableError
from app.gateways.credentials import has_api_key, resolve_api_key
from app.gateways.deepseek import DeepSeekGateway
from app.gateways.router import Router

pytestmark = pytest.mark.integration

#: 结构化输出的最小 schema，用于验证 JSON 模式真的可用
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["answer", "confidence"],
}


def _live_enabled() -> bool:
    return os.environ.get("RUN_LIVE_MODEL_TESTS") == "1"


def _say(text: str) -> None:
    """打印真实调用的结果，**不因控制台编码而失败**。

    这条测试默认 Windows 控制台是 GBK，`¥` 直接 `print` 会抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\xa5'` ——
    最讽刺的是钱已经花了、模型也答对了，测试却"失败"在一个打印语句上，
    看上去像网关坏了。所以打印必须自己兜住编码，而不是让排障的人去猜。
    """
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:  # pragma: no cover - 被重定向到非二进制流时退回安全打印
        print(text.encode("ascii", "backslashreplace").decode("ascii"))
        return
    stream.write((text + "\n").encode("utf-8", "replace"))
    stream.flush()


@pytest.fixture()
def live_settings():
    if not _live_enabled():
        pytest.skip("真实模型调用未开启；设 RUN_LIVE_MODEL_TESTS=1 才运行（会产生费用）")
    if not has_api_key():
        pytest.skip("未找到 API Key（.env 的 MODEL_PROVIDER_API_KEY 或 DSH 凭据文件）")

    from app.config import get_settings

    return get_settings()


@pytest.fixture()
def live_gateway(live_settings) -> DeepSeekGateway:
    base_url = live_settings.model_provider_base_url
    if not base_url:
        pytest.skip("MODEL_PROVIDER_BASE_URL 未配置")
    return DeepSeekGateway(base_url=base_url, api_key=resolve_api_key())


def test_live_gateway_returns_structured_result(live_settings, live_gateway):
    """验收 1：真实供应商 + 结构化输出 + token 用量。"""
    router = Router(live_gateway, live_settings)

    result = router.generate(
        tier="L1",
        prompt=(
            "日志里出现一行：'kernel: Out of memory: Kill process 1234 (java)'。"
            "请判断这属于哪一类问题，并给出你的置信度。"
        ),
        schema=ANSWER_SCHEMA,
        max_output_tokens=200,
        record=False,
    )

    # 结构化结果
    assert result.parsed is not None, f"未拿到结构化输出，原文：{result.content[:300]}"
    assert "answer" in result.parsed
    assert "confidence" in result.parsed
    assert isinstance(result.parsed["confidence"], (int, float))

    # token 与成本确实被记下来了
    assert result.tokens_input > 0
    assert result.tokens_output > 0
    assert result.cost > 0
    assert result.tier == "L1"
    # 服务端回报的型号应该等于配置里的 L1 型号
    assert result.model == live_settings.model_l1

    _say(
        f"\n[live] model={result.model} tier={result.tier} "
        f"tokens_in={result.tokens_input} tokens_out={result.tokens_output} "
        f"cached={result.cached_tokens} reasoning={result.reasoning_tokens} "
        f"cost=¥{result.cost:.6f}"
    )
    _say(f"[live] parsed={result.parsed}")


def test_live_l0_still_refuses_to_call_the_model(live_settings, live_gateway):
    """L0 在任何情况下都不该发起真实调用 —— 这条不花钱，正好顺手验证。"""
    from app.gateways.base import L0DoesNotCallModelError

    router = Router(live_gateway, live_settings)
    with pytest.raises(L0DoesNotCallModelError):
        router.generate(tier="L0", prompt="x", max_output_tokens=10, record=False)


def test_live_bad_model_name_surfaces_as_gateway_error(live_gateway):
    """用一个不存在的型号，验证错误被明确报出而不是静默返回空内容。"""
    with pytest.raises(ModelUnavailableError):
        live_gateway.generate("ping", None, 5, model="definitely-not-a-real-model-xyz")
