"""集中配置：所有可变项来自环境变量 / .env，代码里不出现密钥与具体型号。

业务代码只认模型等级 L1 / L2 / L3；具体型号与端点只在 .env 里映射。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 相对项目根定位，避免「换个 cwd 就读不到配置」这种坑
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: str = "dev"

    # --- 数据库 / 队列 ---
    database_url: str = "postgresql+psycopg://logagent:logagent_dev_pw@127.0.0.1:5432/logagent"
    redis_url: str = "redis://127.0.0.1:6379/0"

    # --- 会话 ---
    secret_key: str = "change-me"

    # --- 模型等级 → 型号映射 ---
    model_l1: str = ""
    model_l2: str = ""
    model_l3: str = ""
    model_provider_base_url: str = ""
    model_provider_api_key: str = ""

    # --- 成本单价（元 / 每 1M token）---
    model_l1_price_input_per_1m: float = 0.0
    model_l1_price_output_per_1m: float = 0.0
    model_l2_price_input_per_1m: float = 0.0
    model_l2_price_output_per_1m: float = 0.0
    model_l3_price_input_per_1m: float = 0.0
    model_l3_price_output_per_1m: float = 0.0

    # --- 预算 ---
    monthly_budget: float = 10.0
    default_run_max_cost: float = 0.30

    # --- 时间 / 日志 ---
    default_timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"

    def tier_model(self, tier: str) -> str:
        """把等级（L1/L2/L3）翻译成具体型号。业务代码只该调这个。"""
        mapping = {"L1": self.model_l1, "L2": self.model_l2, "L3": self.model_l3}
        try:
            return mapping[tier.upper()]
        except KeyError:
            raise ValueError(f"未知模型等级: {tier!r}，只接受 L1 / L2 / L3") from None

    def tier_price(self, tier: str) -> tuple[float, float]:
        """返回 (输入单价, 输出单价)，单位：元 / 每 1M token。"""
        key = tier.upper()
        if key not in {"L1", "L2", "L3"}:
            raise ValueError(f"未知模型等级: {tier!r}，只接受 L1 / L2 / L3")
        prefix = f"model_{key.lower()}_price"
        return (
            getattr(self, f"{prefix}_input_per_1m"),
            getattr(self, f"{prefix}_output_per_1m"),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
