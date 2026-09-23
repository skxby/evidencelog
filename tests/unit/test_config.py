"""配置层单测：等级→型号映射必须只在配置层发生。"""

import pytest

from app.config import Settings, get_settings


def test_settings_loads_from_env_file():
    settings = get_settings()
    assert settings.environment
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.redis_url.startswith("redis://")


def test_tier_model_returns_configured_model():
    settings = Settings(model_l1="small-x", model_l2="medium-y", model_l3="large-z")
    assert settings.tier_model("L1") == "small-x"
    assert settings.tier_model("l2") == "medium-y"
    assert settings.tier_model("L3") == "large-z"


def test_tier_model_rejects_unknown_tier():
    settings = Settings()
    for bad in ("L0", "L4", "gpt", ""):
        with pytest.raises(ValueError):
            settings.tier_model(bad)


def test_tier_price_returns_input_output_pair():
    settings = Settings(
        model_l3_price_input_per_1m=1.5,
        model_l3_price_output_per_1m=6.0,
    )
    assert settings.tier_price("L3") == (1.5, 6.0)


def test_tier_price_rejects_unknown_tier():
    with pytest.raises(ValueError):
        Settings().tier_price("L9")
