"""候选模型排序 —— 权重与优先级的唯一一套算法。

规则只有一条（priority_then_weight）：
  先按 priority 分档，只在最小 priority 那一档里做加权随机。
  priority=1 的模型永远压过 priority=2；同档多个模型按 weight 分摊流量。

排序结果同时被用作「失败后的降级序列」：第 1 个失败就试第 2 个，以此类推。
（早期还有 weight_only / fallback_chain 两种策略，已下线：
  前者忽略优先级，等于让备用模型跟主用模型抢流量，和 priority 的语义打架；
  后者不随机，和"同档按权重分流"是两套心智模型。留一套就够，页面也只讲这一套。）
"""

from __future__ import annotations

import random
from typing import Any, Iterable

from .config import ModelSpec


def weighted_shuffle(models: Iterable[ModelSpec]) -> list[ModelSpec]:
    """不放回的加权随机抽样，得到一个带优先级的随机序列。"""
    items = list(models)
    ordered: list[ModelSpec] = []
    while items:
        total = sum(max(m.weight, 0.0) for m in items)
        if total <= 0:
            ordered.extend(items)
            break
        point = random.uniform(0.0, total)
        acc = 0.0
        picked = len(items) - 1
        for idx, item in enumerate(items):
            acc += max(item.weight, 0.0)
            if point <= acc:
                picked = idx
                break
        ordered.append(items.pop(picked))
    return ordered


def rank_candidates(
    models: list[ModelSpec],
    requires: list[str],
    health: Any,
    now: float | None = None,
) -> tuple[list[ModelSpec], dict[str, Any]]:
    """返回 (按优先级排列的候选模型, 说明信息)。"""
    notes: dict[str, Any] = {
        "capability_relaxed": False,
        "health_relaxed": False,
        "excluded_cooling": [],
    }

    enabled = [m for m in models if m.enabled]
    if not enabled:
        return [], notes

    capable = [m for m in enabled if m.describes(requires)]
    if not capable:
        # 没有模型声明支持所需能力时，放开能力过滤（宁可用可能不匹配的，也不要直接失败）
        capable = enabled
        notes["capability_relaxed"] = True

    cooling = [m for m in capable if health.is_cooling(m.id, now)]
    cooling_ids = {m.id for m in cooling}
    healthy = [m for m in capable if m.id not in cooling_ids]
    relaxed = False
    if not healthy:
        # 全部在冷却中：与其失败，不如放开熔断、**按最早恢复的先后**去试。
        # 这里刻意不再按 priority 分档：档位只决定"平时谁先上"，
        # 而全部被熔断时，用户要的是"最快能出结果的那个"，
        # 否则 A 还要等 4 分钟、B 5 秒后就恢复，却会先去撞 A。
        healthy = sorted(
            capable,
            key=lambda m: (health.cooling_remaining(m.id, now), m.priority),
        )
        relaxed = True
        notes["health_relaxed"] = True
        # 这些模型接下来都会被尝试，不该再列进"被排除"里
        notes["excluded_cooling"] = []
    else:
        notes["excluded_cooling"] = [m.id for m in cooling]

    ordered: list[ModelSpec] = []
    if relaxed:
        ordered.extend(healthy)
    else:
        # 先分档，再在档内做加权随机：分档保证"优先级 1 永远压过 2"，
        # 档内随机保证同优先级的多个模型能按 weight 分摊流量。
        for priority in sorted({m.priority for m in healthy}):
            tier = [m for m in healthy if m.priority == priority]
            ordered.extend(weighted_shuffle(tier))

    # 输出里保留 strategy 字段（值恒定），CLI/自检按老结构读取不受影响
    notes["strategy"] = "priority_then_weight"
    notes["requires"] = list(requires)
    notes["candidate_count"] = len(ordered)
    return ordered, notes


def plan_attempts(
    ordered: list[ModelSpec], max_attempts: int
) -> list[ModelSpec]:
    """按 max_attempts 截取实际要尝试的模型序列。"""
    limit = max(1, int(max_attempts or 1))
    return ordered[:limit]
