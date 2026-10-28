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
check('class="strategy-badge"' in PAGE, "路由条用固定标签说明当前策略")
check("忽略优先级" not in PAGE and "按序不随机" not in PAGE,
      "「全局权重 / 严格顺位」两种策略已从页面移除")
check('id="btn-refresh"' in PAGE, "存在刷新按钮")
check('class="model-head"' in PAGE, "页签与添加按钮在同一页头")
check('class="routing-bar"' in PAGE and 'id="prob-summary"' in PAGE, "存在路由摘要条")
check('data-kind="image"' not in PAGE, "添加表单已移除手动类型选择")
check("async function setActiveKind" in PAGE, "切页函数会处理未保存表单")
check("当前有未保存的模型表单" in PAGE, "切页前有未保存确认文案")
check("function renderModelCard" in PAGE and "function renderModelGroups" in PAGE, "模型卡/分页渲染函数存在")

# 输入/输出模态：表单里是可多选的功能标签，字段名要与后端一致
check('id="mf-in-mods"' in PAGE and 'id="mf-out-mods"' in PAGE,
      "模型表单有输入/输出模态多选项")
check("input_modalities" in PAGE and "output_modalities" in PAGE, "页面提交/回填模态字段")
check("MODALITY_OPTIONS" in PAGE and "MODALITY_LABELS" in PAGE, "模态可选值与中文标签集中在常量里")
check('id="mf-extra"' not in PAGE, "已移除与「输入含图片」重复的附加能力开关")
check("input_modalities" in CONFIG and "output_modalities" in CONFIG,
      "models.yaml 注释说明了模态字段")
# 语音模型：第三个类目页签 + 表单里的语音规格 + 厂商的语音接口地址
check('id="mf-spec-audio"' in PAGE and 'id="mf-voice"' in PAGE and 'id="mf-speed"' in PAGE,
      "模型表单有语音生成规格（音色/语速/格式）")
check('id="vf-ep-audio"' in PAGE, "厂商表单有语音接口地址")
check("语音模型" in PAGE and "audio:{input:" in PAGE, "页面认识 audio（语音）类目")

# 模型表单的信息架构：显示名称紧跟模型名称、优先级在权重之后、关键字段有绿色提示
check(
    PAGE.index('id="mf-model"') < PAGE.index('id="mf-label"') < PAGE.index('id="mf-in-mods"'),
    "「显示名称」紧跟在「模型名称」下面",
)
check(
    PAGE.index('id="mf-weight"') < PAGE.index('id="mf-priority"'),
    "「优先级」排在「权重」下面",
)
_between = PAGE[PAGE.index('id="mf-weight"'):PAGE.index('id="mf-priority"')]
check("<details" not in _between, "权重和优先级之间不再有折叠的高级选项")
check(PAGE.count("高级选项") == 1, "模型表单的高级选项已删除（只剩厂商表单那个）")
check('class="tip"' in PAGE and PAGE.count('class="tip"') >= 3, "关键字段用绿色提示块标出")

# 从卡片点「编辑」时，表单在列表下方 —— 不滚过去就会像"点了没反应"
check('el("model-form").scrollIntoView' in PAGE, "打开模型表单后会滚入视野")
check('el("vendor-form").scrollIntoView' in PAGE, "打开厂商表单后会滚入视野")

# 保存成功要回到列表，不能把用户留在表单里
_save_model = PAGE[PAGE.index("async function saveModel"):PAGE.index("async function testModel")]
check(
    "closeModelForm()" in _save_model and "showModelForm(null)" not in _save_model,
    "保存模型后收起表单回到列表",
)
_save_vendor = PAGE[PAGE.index("async function saveVendor"):PAGE.index("function payloadFromVendor")]
check(
    "closeVendorForm()" in _save_vendor and "showVendorForm(vendorById" not in _save_vendor,
    "保存厂商后同样回到列表",
)
check("function closeModelForm(" in PAGE and "function closeVendorForm(" in PAGE,
      "两个表单共用同一套收起逻辑")

# 测试入口在模型卡片上，不在表单里（测试要针对已保存的模型）
check('id="btn-test-model"' not in PAGE and 'id="btn-real-test-model"' not in PAGE,
      "表单里不再有测试按钮")
check('data-act="test-model"' in PAGE and 'data-act="real-test-model"' in PAGE,
      "卡片的操作区提供「测试连通性」和「真实测试」")
check('class="actions-row"' in PAGE, "卡片按钮收在独立容器里（窄屏整体换行）")
check("\naudio:" in CONFIG or CONFIG.startswith("audio:"), "models.yaml 有语音模型池")

# P2 文案与计数
check("初始状态模型池为空" not in CONFIG, "models.yaml 头注释已修正")
check("手写层模型只会隐藏" in PAGE, "删除厂商文案说明手写层只隐藏")
check("个启用 / 共" in PAGE, "计数区分启用数与总数")
check("/api/strategy" not in PAGE and "strategy-select" not in PAGE,
      "页面不再调用策略修改接口")

# 技能文档：快速路径 + 禁止强制 resolve + 按需阅读
check("## 快速路径" in SKILL, "SKILL.md 有快速路径")
check("不要先跑" in SKILL and "resolve" in SKILL, "SKILL.md 不再强制先 resolve")
check("这一步不能跳" not in SKILL, "SKILL.md 已移除旧的强制 resolve 措辞")
check((ROOT / "references" / "setup.md").is_file(), "references/setup.md 存在")

print(f"\n通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
