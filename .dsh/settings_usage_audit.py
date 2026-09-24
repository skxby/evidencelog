"""settings-usage-audit —— 逐项确认每个配置项**真的被读过**。

背景：`DEFAULT_RUN_MAX_COST`、`MODEL_CACHE_HIT_INPUT_PRICE_PER_1M`、`MONTHLY_BUDGET`
都属于同一形态 —— 在 `.env.example` 与 README 里写得好好的，
代码里却没有任何地方读它。这类"配置项是摆设"最阴险的地方在于：
**用户改了它、以为生效了**，而失效方向往往正好是多花钱。

判据：
  ① `.字段名` 在 `app/` 里（config.py 之外）出现过 → 已接线；
  ② 只以字符串形式出现（`getattr(settings, "model_l1")` 这种动态访问）→ 标注 DYN；
  ③ 两者都没有 → **没人读**，必须处理（接线或删掉并改文档）。

另：`.env.example` 里声明、但 Settings 里没有对应字段的键也是问题
（用户配了它，pydantic 会忽略未知键而不报错）。

用法：python .dsh/settings_usage_audit.py
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402


def _corpus() -> dict[pathlib.Path, str]:
    return {
        p: p.read_text(encoding="utf-8", errors="replace")
        for p in ROOT.joinpath("app").rglob("*.py")
    }


def _env_example_keys() -> list[str]:
    keys: list[str] = []
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)\s*=", line.strip())
        if match:
            keys.append(match.group(1))
    return keys


def main() -> int:
    corpus = _corpus()
    outside = {p: t for p, t in corpus.items() if p.name != "config.py"}
    joined = "\n".join(outside.values())

    #: 这些字段是通过 `Settings.tier_model()` / `tier_price()` / `tier_reasoning()`
    #: 里的 **f-string 动态属性访问**读到的（`getattr(self, f"model_{tier}_price...")`），
    #: 字面量搜不到。不把它们排除掉，工具就会对每个模型字段报一次假警 ——
    #: 而假警多了，真警就没人看了。
    dynamic_by_accessor = {
        "model_l1", "model_l2", "model_l3",
        "model_l1_reasoning", "model_l2_reasoning", "model_l3_reasoning",
        "model_l1_price_input_per_1m", "model_l1_price_output_per_1m",
        "model_l2_price_input_per_1m", "model_l2_price_output_per_1m",
        "model_l3_price_input_per_1m", "model_l3_price_output_per_1m",
    }

    print("| Settings 字段 | 属性访问次数 | 判读 |")
    print("|---|---|---|")
    dead: list[str] = []
    for name in sorted(Settings.model_fields):
        direct = len(re.findall(r"\." + re.escape(name) + r"\b", joined))
        dynamic = bool(re.search('"' + re.escape(name) + '"', joined))
        if direct:
            verdict = "已接线"
        elif dynamic:
            verdict = "动态访问（getattr）"
        elif name in dynamic_by_accessor:
            verdict = "经 tier_model/tier_price 读取"
        else:
            verdict = "**没人读**"
            dead.append(name)
        print(f"| `{name}` | {direct} | {verdict} |")

    print()
    env_keys = _env_example_keys()
    aliases = {
        (getattr(field, "alias", None) or name.upper()): name
        for name, field in Settings.model_fields.items()
    }
    unknown = [k for k in env_keys if k not in aliases]
    print(f".env.example 里声明但 Settings 没有的键：{unknown or '无'}")
    print(f"声明了却没人读的字段：{dead or '无'}")
    return 1 if (dead or unknown) else 0


if __name__ == "__main__":
    raise SystemExit(main())
