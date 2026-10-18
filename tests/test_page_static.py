#!/usr/bin/env python3
"""页面与技能文档的静态契约测试（零依赖、不启服务）。

运行：python tests/test_page_static.py
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "assets" / "config.html").read_text(encoding="utf-8")
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")
CONFIG = (ROOT / "config" / "models.yaml").read_text(encoding="utf-8")

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


print("页面/技能文档静态契约测试")

# 阻塞式弹窗：厂商删除必须走页面内 uiConfirm，不能再出现原生 confirm 调用
check("if(!confirm(" not in PAGE, "没有原生 confirm 调用（if(!confirm）")
check("if(confirm(" not in PAGE and "if (confirm(" not in PAGE, "没有 if (confirm(...))")
check("= confirm(" not in PAGE, "没有赋值式 confirm(...)")
check("window.confirm(" not in PAGE, "没有 window.confirm")
check("await uiConfirm(" in PAGE, "删除操作使用页面内 uiConfirm")
check(PAGE.count("alert(") <= 1, "没有阻塞式 alert 调用（注释除外）")

# 页面结构：分页、策略、路由摘要、表单固定类型
check('id="kind-tabs"' in PAGE, "存在图像/视频分页容器")
check('id="strategy-select"' in PAGE, "存在当前策略下拉")
check('id="btn-refresh"' in PAGE, "存在刷新按钮")
check('class="model-head"' in PAGE, "页签与添加按钮在同一页头")
check('class="routing-bar"' in PAGE and 'id="prob-summary"' in PAGE, "存在路由摘要条")
check('data-kind="image"' not in PAGE, "添加表单已移除手动类型选择")
check("async function setActiveKind" in PAGE, "切页函数会处理未保存表单")
check("当前有未保存的模型表单" in PAGE, "切页前有未保存确认文案")
check("function renderModelCard" in PAGE and "function renderModelGroups" in PAGE, "模型卡/分页渲染函数存在")

# P2 文案与计数
check("初始状态模型池为空" not in CONFIG, "models.yaml 头注释已修正")
check("手写层模型只会隐藏" in PAGE, "删除厂商文案说明手写层只隐藏")
check("个启用 / 共" in PAGE, "计数区分启用数与总数")
check("/api/strategy" in PAGE, "页面通过 API 修改策略")

# 技能文档：快速路径 + 禁止强制 resolve + 按需阅读
check("## 快速路径" in SKILL, "SKILL.md 有快速路径")
check("不要先跑" in SKILL and "resolve" in SKILL, "SKILL.md 不再强制先 resolve")
check("这一步不能跳" not in SKILL, "SKILL.md 已移除旧的强制 resolve 措辞")
check((ROOT / "references" / "setup.md").is_file(), "references/setup.md 存在")

print(f"\n通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
