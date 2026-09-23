r"""pytest 全局配置。

1. 把项目根塞进 sys.path，保证在任意 cwd 下 `import app` 都成立。
2. 提供**工作区内的**临时目录夹具，替代 pytest 自带的 `tmp_path`。

为什么不用 `tmp_path`：它建在系统临时目录（Windows 上通常是
`%LOCALAPPDATA%\Temp`）。在受限沙箱下该目录不可枚举也不可写，会直接抛
`PermissionError: [WinError 5]` —— 那是环境约束，不是代码问题。把探针数据
放在工作区内既避开限制，也让产物可见、可清理（`.pytest-tmp/` 已进 .gitignore）。
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_TMP_ROOT = PROJECT_ROOT / ".pytest-tmp"


@pytest.fixture()
def work_tmp(request: pytest.FixtureRequest) -> Iterator[Path]:
    """每个测试一个干净的工作区临时目录，用完即删。

    取名为 `work_tmp` 而不是覆盖 `tmp_path`：覆盖内置夹具会让别人读代码时
    以为这是标准行为，反而更迷惑。
    """
    safe_name = request.node.name.replace("/", "_").replace("\\", "_").replace(":", "_")
    path = _TMP_ROOT / safe_name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)