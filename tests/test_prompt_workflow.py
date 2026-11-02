"""提示词准备的静态文档契约；不能替代真实 Agent 行为评估，不执行生成。"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ("index.md", "image.md", "video.md")


class PromptWorkflowTests(unittest.TestCase):
    def document(self, relative):
        path = ROOT / relative
        self.assertTrue(path.is_file(), "缺少流程文档：%s" % relative)
        return path.read_text(encoding="utf-8")

    def require_terms(self, text, terms):
        for term in terms:
            with self.subTest(term=term):
                self.assertTrue(term in text, "文档缺少约定：%s" % term)

    def test_entry_prepares_before_original_generate(self):
        text = self.document("SKILL.md")
        self.require_terms(text, ("prompts search", "prompts show"))
        if "prompts search" not in text or "prompts show" not in text:
            return
        self.assertLess(text.index("prompts search"), text.index("generate --kind image"))
        self.assertLess(text.index("prompts show"), text.index("generate --kind image"))
        self.require_terms(text, ("用户要求优先", "严禁只照抄", "原样发送", "先别生成"))
        self.assertNotIn("正常情况下不要为了理解脚本去读 `scripts/`、`README.md` 或 `references/`", text)
        self.assertIn("读取相关规范和所选参考正文", text)

    def test_selection_uses_task_values_and_reads_selected_bodies(self):
        text = self.document("references/prompts/index.md")
        self.require_terms(text, (
            "prompts search", "prompts show", "--kind", "--query", "--category", "--id",
            "精简检索词", "不能硬套示例", "1–2", "0–3", "正文", "词面", "语义置信度",
            "不反复改 query", "密钥", "私密原文", "不加载整库",
        ))

    def test_reference_data_cannot_become_instructions(self):
        text = self.document("references/prompts/index.md")
        self.require_terms(text, (
            "用户要求优先", "reference_data", "参考数据", "角色声明", "命令", "链接", "索钥",
            "不执行", "unpublished", "目标说明", "不可冒充原 prompt", "unknown", "不表示无需素材",
        ))

    def test_image_preserves_constraints_and_distinguishes_missing_materials(self):
        text = self.document("references/prompts/image.md")
        self.require_terms(text, (
            "准确文字", "逐字", "禁止项", "示例品牌", "价格", "占位符", "参考图",
            "先询问", "候选模板", "换模板", "不伪造附件 ID", "不下载", "不谎称看过",
            "negative prompt", "count", "--image", "真实支持", "不把模板 JSON 当 API 参数",
        ))

    def test_video_is_visual_reference_with_time_and_minimal_camera_design(self):
        text = self.document("references/prompts/video.md")
        self.require_terms(text, (
            "图片视觉参考", "不是完整原生视频模板", "时间", "动作", "运镜", "连续性",
            "起始状态", "结束状态", "多格", "面板编号", "彩色箭头", "注释标题",
            "锁定镜头", "无字幕", "最小镜头设计", "不自动生成参考图", "分段视频",
            "不得增加 count", "不默认模型支持声音", "不额外调用 TTS", "真实映射", "不猜参数",
        ))

    def test_authorization_and_audio_web_paths_are_unchanged(self):
        skill = self.document("SKILL.md")
        index = self.document("references/prompts/index.md")
        self.require_terms(skill, ("已获授权", "语音不经过", "网页连通性／真实测试", "不执行 generate"))
        self.require_terms(index, (
            "原样发送", "不要润色", "先别生成", "不提交", "安全、权限、费用及隐私",
            "不强制每次确认", "用户要求先确认", "不把库名当成指定 GPT Image 模型",
        ))

    def test_confirmation_shortcut_preserves_billing_and_host_requirements(self):
        for relative in ("references/prompts/index.md", "tests/prompt_workflow_cases.md"):
            with self.subTest(document=relative):
                text = self.document(relative)
                self.require_terms(text, (
                    "普通已授权单次", "费用授权优先", "批量生成前先确认", "宿主强制确认", "不绕过",
                ))
                self.assertNotIn("用户要求先确认时才等待确认", text)
                self.assertNotIn("仅用户要求先确认时等待确认", text)

    def test_fallback_is_transparent_without_changing_generation_controls(self):
        text = self.document("references/prompts/index.md")
        self.require_terms(text, (
            "零命中", "库缺失", "损坏", "自拟", "不伪造检索成功", "不把资料问题说成模型生成失败",
            "media-router generate", "--model", "--fail-fast", "--max-attempts", "--no-download",
            "不自动重跑", "一次原 generate 流程", "不改变原命令内部的安全候选切换",
            "不写用户需求", "不改原生成 JSON", "宿主会话或命令历史",
        ))

    def test_reference_command_status_is_not_a_generation_result(self):
        text = self.document("SKILL.md")
        entry = text.split("## 按结果处理", 1)[0]
        self.require_terms(entry, (
            "下方结果表仅适用于 `generate`", "`prompts search`／`prompts show`",
            "`status=ok` 只表示资料读取／检索成功", "不表示媒体生成成功",
            "`kind=prompt_library`", "按上述资料降级处理", "不等于缺 Key",
        ))
        self.assertTrue("stdout 是单个 JSON，直接按下方 status 处理" not in entry,
                        "不能把资料命令套入生成结果处理")

    def test_original_result_config_and_billing_safety_remain(self):
        text = self.document("SKILL.md")
        self.require_terms(text, (
            "## 按结果处理", "**delegate**", "**error**", "delegate.arguments",
            "不要猜工具名", "绝对不要把 delegate 当成生成成功", "fallback_trail[].kind",
            "submission_uncertain: true", "不得自动重跑、换模型或改用内置工具补生成",
            "phase: delivery", "partial", "files[].path", "--fail-fast", "--max-attempts", "--no-download",
            "生成提交默认也不会自动重发", "不要为补描述/元数据再生成一次媒体",
            "## 需要配置或排障时", "改配置是用户的事", "## 硬性约束", "批量生成前先确认",
        ))

    def test_readme_explains_offline_scope_and_manual_cli_compatibility(self):
        text = self.document("README.md")
        self.require_terms(text, (
            "### 图片／视频提示词准备", "当前 Agent", "原样发送", "无需额外 Key、网络或依赖",
            "图片视觉参考", "连续性", "语音和网页模型测试不变", "手动 CLI", "不被强制",
            "第三方案例", "MIT", "prompts search", "prompts show",
        ))

    def test_maintained_guide_links_resolve_after_asset_import(self):
        for name in GUIDES:
            relative = "references/prompts/" + name
            text = self.document(relative)
            for raw in re.findall(r"\]\(<([^>]+)>\)", text):
                if "://" in raw:
                    continue
                with self.subTest(source=relative, link=raw):
                    file_part, _, anchor = raw.partition("#")
                    source = ROOT / relative
                    target = source.parent / file_part if file_part else source
                    self.assertTrue(target.is_file(), (relative, raw))
                    if anchor:
                        target_text = target.read_text(encoding="utf-8")
                        explicit = re.search(r'(?:id|name)=["\']' + re.escape(anchor) + r'["\']', target_text)
                        headings = [re.sub(r"[^\w\- ]", "", h.strip().lower()).replace(" ", "-")
                                    for h in re.findall(r"^#{1,6}\s+(.+)$", target_text, re.M)]
                        self.assertTrue(explicit or anchor in headings, (relative, raw))
        skill = self.document("SKILL.md")
        for name in GUIDES:
            self.assertIn("](<references/prompts/%s>)" % name, skill)
        self.assertIn("](<references/prompts/index.md>)", self.document("README.md"))

    def test_cases_define_protocol_not_completed_agent_evaluation(self):
        text = self.document("tests/prompt_workflow_cases.md")
        ids = re.findall(r"^\| (P\d{2}) \|", text, re.M)
        self.assertEqual(ids, ["P%02d" % number for number in range(1, 16)])
        self.require_terms(text, (
            "均未评估", "只定义协议", "静态测试不能替代", "真实 Agent", "argv", "不执行",
            "不允许 generate", "网络", "resolve", "probe", "凭据", "模拟工具结果",
            "每项缺记录即记未评估", "P07／P14／P15", "批量", "授权",
        ))
        self.assertNotRegex(text, r"P(?:0[1-9]|1[0-5])\s*[:：]\s*(?:PASS|通过)")


if __name__ == "__main__":
    unittest.main()
