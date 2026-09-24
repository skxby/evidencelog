"""供应商凭据解析。

**凭据不硬编码、不进代码库**（红线：密钥只在环境变量 / 凭据文件里）。

解析顺序：
1. 显式传入的值（测试用）；
2. `Settings` —— 它同时覆盖**真实环境变量**与项目 `.env`
   （pydantic-settings 的优先级是 环境变量 > .env > 默认值）；
3. `DEEPSEEK_API_KEY` 这个别名（走 os.environ，Settings 里没有这个字段）；
4. DSH 的凭据文件 `$DSH_HOME/.credentials.yaml`。

第 4 条是为了复用「DSH 里已经配好的 Key」，避免同一把 Key 在两处各存一份。
找不到时抛 `ConfigurationError` 并说明去哪里配 —— 启动即失败，而不是
等第一次调用才报一个含糊的错误。

**为什么必须先读 `Settings`**：这里曾经只读 `os.environ`，而
`pydantic-settings` 载入 `.env` 时**不会**把值写进 `os.environ`。
后果是：README 让用户"填 `.env` 里的 MODEL_PROVIDER_API_KEY"，
而只在宿主机直跑时根本读不到它 —— 只有在 Docker 里才碰巧能用，
因为 compose 的 `env_file:` 会把 `.env` 变成**真正的环境变量**。
实测：把 `DSH_HOME` 指到空目录后，`.env` 里明明有 Key，
`resolve_api_key()` 依旧报"找不到 API Key"。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.gateways.base import ConfigurationError

#: 按优先级尝试的环境变量名
API_KEY_ENV_NAMES = ("MODEL_PROVIDER_API_KEY", "DEEPSEEK_API_KEY")


def dsh_credentials_path() -> Path:
    """DSH 凭据文件位置（`$DSH_HOME` 优先）。"""
    home = os.environ.get("DSH_HOME")
    base = Path(home) if home else Path.home() / ".dsh"
    return base / ".credentials.yaml"


def _read_credentials_file(path: Path) -> dict[str, str]:
    """读取 DSH 凭据文件里的 `refs` 段。读不到就返回空，不抛异常。

    该文件是 DSH 的私有格式（`refs: {NAME: value}`），故只做最小解析，
    不引入对 DSH 内部结构的依赖。
    """
    if not path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - 凭据文件损坏不该让整个进程起不来
        return {}

    refs = data.get("refs")
    if not isinstance(refs, dict):
        return {}
    return {str(k): str(v) for k, v in refs.items() if isinstance(v, str)}


def resolve_api_key(explicit: str | None = None) -> str:
    """按 显式 > Settings(环境变量/.env) > 别名环境变量 > DSH 凭据文件 的顺序解析 API Key。"""
    if explicit:
        return explicit

    # ① Settings：一条就覆盖了"真实环境变量"与"项目 .env"两种来源。
    #    不读它的话，宿主机上"只填了 .env"的用户会被判成没配 Key。
    try:
        from app.config import get_settings

        from_settings = (get_settings().model_provider_api_key or "").strip()
    except Exception:  # noqa: BLE001 - 配置读取失败不该让凭据解析直接崩
        from_settings = ""
    if from_settings:
        return from_settings

    # ② 别名（如 DEEPSEEK_API_KEY）：Settings 里没有这个字段，只能看环境变量
    for name in API_KEY_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return value.strip()

    # ③ DSH 凭据文件
    path = dsh_credentials_path()
    refs = _read_credentials_file(path)
    for name in API_KEY_ENV_NAMES:
        value = refs.get(name)
        if value:
            return value.strip()

    raise ConfigurationError(
        "找不到模型供应商 API Key。请任选其一：\n"
        f"  1) 在项目 .env 里设置 {API_KEY_ENV_NAMES[0]}=sk-...\n"
        f"  2) 设置环境变量 {API_KEY_ENV_NAMES[1]}\n"
        f"  3) 在 DSH 的 {path} 里配置 refs.{API_KEY_ENV_NAMES[0]}"
    )


def has_api_key() -> bool:
    """不抛异常的探测，供测试决定是否跳过真实调用。"""
    try:
        resolve_api_key()
    except ConfigurationError:
        return False
    return True
