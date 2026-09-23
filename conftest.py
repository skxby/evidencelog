"""pytest 全局配置。

把项目根塞进 sys.path，保证在任意 cwd 下 `import app` 都成立。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
