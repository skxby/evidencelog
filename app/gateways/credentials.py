"""供应商凭据解析。

**凭据不硬编码、不进代码库**（红线：密钥只在环境变量 / 凭据文件里）。

解析顺序：
1. 显式传入的值（测试用）；
2. 环境变量（`.env` 由 `pydantic-settings` 载入 `Settings`）；
3. DSH 的凭据文件 `$DSH_HOME/.credentials.yaml`（或 `~/.dsh/.credentials.yaml`）。

第 3 条是为了复用「DSH 里已经配好的 Key」，避免同一把 Key 在两处各存一份。
找不到时抛 `ConfigurationError` 并说明去哪里配 —— 启动即失败，而不是
等第一次调用才报一个含糊的错误。
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
    """按 显式 > 环境变量 > DSH 凭据文件 的顺序解析 API Key。"""
    if explicit:
        return explicit

    for name in API_KEY_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return value.strip()

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
