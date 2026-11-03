#!/usr/bin/env python3
"""页面与技能文档的静态契约测试（零依赖、不启服务）。

运行：python tests/test_page_static.py
"""

from pathlib import Path
import re
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
check('id="test-fail-fast"' in PAGE and 'fail_fast:' in PAGE, "测试可选择临时首次失败即停策略")
check('不保证实际生成成功' in PAGE, "免费预检如实说明验证边界")
_test_model = PAGE[PAGE.index("async function testModel"):PAGE.index("/* ------------------------------------------------------------------ 事件 */")]
check('已保存且启用' in _test_model and '未保存改动不参与' in _test_model and '请先保存再测' in _test_model,
      "真实测试确认明确保存、启用与未保存改动的边界")
check(re.search(r'JSON\.stringify\(\{\s*id:\s*mid,\s*deep:\s*!!deep,\s*fail_fast:\s*failFast\s*\}\)', _test_model) is not None,
      "模型测试请求显式白名单，仅发送保存ID和两个临时布尔开关")
check('Object.assign({}, payload' not in _test_model and 'collectModelPayload(' not in _test_model and
      'saveModel(' not in _test_model, "真实测试不复制表单或自动保存未保存改动")

# 临时严格策略：原生 checkbox 维持状态，外观是立体按压按钮，不受满宽输入框样式干扰。
_fast_input = re.search(r'<input\b[^>]*\bid="test-fail-fast"[^>]*>', PAGE)
_fast_tag = _fast_input.group(0) if _fast_input else ""
check(PAGE.count('id="test-fail-fast"') == 1 and 'type="checkbox"' in _fast_tag,
      "严格测试保留唯一原生 checkbox，供请求读取 checked")
check(not re.search(r'\bchecked(?:\s|=|>)', _fast_tag), "严格测试默认关闭")
check('aria-labelledby="test-fail-fast-title"' in _fast_tag and 'id="test-fail-fast-title"' in PAGE,
      "立体按钮有明确的无障碍名称")
check('aria-describedby="test-fail-fast-help test-fail-fast-billing test-fail-fast-persistence"' in _fast_tag,
      "控件关联作用说明与费用提醒")
check('class="test-stop-panel"' in PAGE and 'class="test-stop-control"' in PAGE and
      'class="test-stop-button"' in PAGE, "立体按钮与旁边说明有独立局部布局")
check('已关闭 · 点击开启' in PAGE and '已开启 · 点击关闭' in PAGE,
      "开启与关闭都有文字状态，不只依赖颜色")
_css = PAGE.split('<style>', 1)[1].split('</style>', 1)[0]
check('.test-stop-control input:checked + .test-stop-button' in _css,
      "原生选中状态驱动立体按压外观")
check('.test-stop-control input:focus-visible + .test-stop-button' in _css,
      "键盘焦点显示在可见按钮上")
check('prefers-reduced-motion:reduce' in _css and 'forced-colors:active' in _css,
      "局部按钮兼容减少动态与系统高对比模式")
check(re.search(r'@media\s*\(max-width:620px\)\s*\{\s*\.test-stop-panel\s*\{[^}]*grid-template-columns:1fr', _css) is not None,
      "窄屏时按钮与说明纵向排列")
check('检测到失败就停止后续检查' in PAGE and '查询、下载错误不再自动重试' in PAGE and
      '排查密钥、接口或参数问题' in PAGE and '预检警告不一定阻止真实生成' in PAGE,
      "开启说明区分失败停止、重试与不拦截的预检警告")
check('查询、下载遇到临时网络问题' in PAGE and '生成提交本身仍不会自动重发' in PAGE,
      "关闭说明不误导用户认为生成提交会重发")
check('排队中 / 生成中' in PAGE and '仍会继续查询' in PAGE,
      "说明正常等待不会被当作错误中断")
check('这不是免费开关' in PAGE and '真实测试仍需确认并可能收费' in PAGE and
      '不能撤销已受理的任务' in PAGE, "费用与已受理任务边界明确")
check('仅影响随后发起的模型测试' in PAGE and '不修改模型配置' in PAGE,
      "说明偏好只影响后续测试而不改变模型配置")
check('首次接入平台' in PAGE and '更换密钥或接口' in PAGE and '调整模型参数后' in PAGE,
      "说明具体建议开启的排错场景")
check('日常验证' in PAGE and '临时网络波动' in PAGE,
      "说明日常测试可关闭以保留有界重试")
check('id="test-fail-fast-persistence"' in PAGE and 'role="status"' in PAGE and
      '同一地址刷新后保留' in PAGE and '更换浏览器或地址后需重新选择' in PAGE,
      "如实说明保存范围并可报告存储失败")
check('const modelTestsPending = new Set()' in PAGE, "同模型测试共享处理中锁")
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

# 模型授权：参数不能换模型，真实测试只用保存后的启用条目
check("不能用它换模型或计费资源" in SKILL and "allowed_params" in SKILL,
      "SKILL.md 说明 --param 不能换模型且自定义接口需显式放行")
check("已启用" in SKILL and "真实测试也只使用已保存且启用的条目" in SKILL,
      "SKILL.md 说明停用模型与真实测试的授权范围")
SETUP = (ROOT / "references" / "setup.md").read_text(encoding="utf-8")
check("只使用已保存且已启用的模型" in SETUP, "setup.md 说明真实测试只用已保存启用模型")
check("只读模式只拦住网页保存" in SETUP, "setup.md 不把网页只读模式说成沙箱")
PROVIDERS = (ROOT / "references" / "providers.md").read_text(encoding="utf-8")
check("allowed_params" in PROVIDERS and "model_fields" in PROVIDERS,
      "providers.md 记录自定义接口的运行参数白名单与身份字段声明")
check("只能用固定文本或 `{{model}}`" in PROVIDERS, "providers.md 限制提交地址占位符")

print(f"\n通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
