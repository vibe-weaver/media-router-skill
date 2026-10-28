#!/usr/bin/env python3
"""策略收敛的单元测试。

背景：weight_only / fallback_chain 两种策略已下线，只保留
priority_then_weight（先按优先级分档、同档按权重分流）。
老配置里的写法要被平滑纠正、并且不能因此让整份配置打不开。

零依赖、不联网；运行：python tests/test_strategy.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["MEDIA_ROUTER_CONFIG_DIR"] = tempfile.mkdtemp(prefix="mr-strategy-")
sys.path.insert(0, str(ROOT / "scripts"))

from mrouter import config, selector  # noqa: E402

passed = 0
failed = 0


def check(cond: bool, message: str) -> None:
    global passed, failed
    if cond:
        passed += 1
        print("  [PASS] " + message)
    else:
        failed += 1
        print("  [FAIL] " + message)


print("路由策略测试")

# ---------------------------------------------------------------- 只剩一种
check(
    list(config.VALID_STRATEGIES) == ["priority_then_weight"],
    "只保留 priority_then_weight 一种策略",
)

_warnings: list[str] = []
check(
    config.normalize_strategy("weight_only", "image", _warnings)
    == "priority_then_weight",
    "老配置的 weight_only 被纠正为 priority_then_weight",
)
check(
    any("已不再支持" in w for w in _warnings),
    "纠正时留下告警，告诉用户该改配置",
)
check(
    config.normalize_strategy("fallback_chain", "video") == "priority_then_weight",
    "fallback_chain 同样被纠正（不传 warnings 也不报错）",
)
check(
    config.normalize_strategy(None, "image") == "priority_then_weight"
    and config.normalize_strategy("priority_then_weight", "image") == "priority_then_weight",
    "没写或写对时保持 priority_then_weight，且不产生告警",
)

# ---------------------------------------------------------------- 排序行为
def spec(model_id: str, priority: int, weight: float):
    return config.ModelSpec(
        id=model_id, provider="openai", kind="image",
        priority=priority, weight=weight, enabled=True,
    )


class _NoCooling:
    def is_cooling(self, _model_id, _now=None):
        return False


models = [spec("backup", 2, 100), spec("main-a", 1, 10), spec("main-b", 1, 30)]
for _ in range(20):
    ordered, notes = selector.rank_candidates(models, ["text2img"], _NoCooling())
    first_two = {m.id for m in ordered[:2]}
    if first_two != {"main-a", "main-b"}:
        check(False, "优先级 1 的两个模型应排在最前（实际 " + str([m.id for m in ordered]) + "）")
        break
else:
    check(True, "无论权重多大，优先级 1 的模型都排在优先级 2 前面")
check(notes.get("strategy") == "priority_then_weight", "排序输出里保留 strategy 字段")

# ---------------------------------------------------------------- 页面侧
page = (ROOT / "assets" / "config.html").read_text(encoding="utf-8")
check("weight_only" not in page and "fallback_chain" not in page,
      "页面里不再出现这两种策略的实现")
check("按优先级 · 同档加权" in page, "路由条用固定标签说明当前策略")

print(f"\n通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
