#!/usr/bin/env python3
"""输入/输出模态（功能标签）的单元测试。

零依赖、不联网、不碰仓库里的真实配置：先把 MEDIA_ROUTER_CONFIG_DIR 指到临时
目录，再 import config / store —— 那两个模块在 import 期就定下了配置目录。

运行：python tests/test_modalities.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TMP = Path(tempfile.mkdtemp(prefix="mr-modalities-"))
os.environ["MEDIA_ROUTER_CONFIG_DIR"] = str(_TMP)
sys.path.insert(0, str(ROOT / "scripts"))

from mrouter import config, store  # noqa: E402

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


print("输入/输出模态测试")

# ---------------------------------------------------------------- 默认值
check(
    config.default_modalities("image", [], "input") == ["text"]
    and config.default_modalities("image", [], "output") == ["image"],
    "图像模型默认「文本 → 图片」",
)
check(
    config.default_modalities("image", ["text2img", "img2img"], "input")
    == ["text", "image"],
    "有 img2img 时输入默认带「图片」",
)
check(
    config.default_modalities("video", [], "output") == ["video"]
    and config.default_modalities("video", ["img2video"], "input") == ["text", "image"],
    "视频模型默认「文本 → 视频」，img2video 时带「图片」",
)
check(
    config.default_modalities("audio", [], "input") == ["text"]
    and config.default_modalities("audio", [], "output") == ["audio"],
    "语音模型默认「文本 → 音频」",
)
check(
    config.normalize_modalities("音频, 文本", "audio", [], "input") == ["text", "audio"],
    "语音模型接受「文本 / 音频」两种输入",
)
check(
    config.normalize_modalities(["video"], "audio", [], "output") == ["audio"],
    "语音模型上的「视频」被过滤，回退到默认值",
)

# ---------------------------------------------------------------- 归一化
check(
    config.normalize_modalities("图片, 文本", "image", [], "input") == ["text", "image"],
    "中文标签 + 逗号字符串能识别，并按固定顺序排",
)
check(
    config.normalize_modalities(["视频", "audio"], "video", [], "input")
    == ["video", "audio"],
    "视频模型接受「视频 / 音频」输入",
)
check(
    config.normalize_modalities(["video", "text"], "image", [], "input") == ["text"],
    "图像模型上的「视频」被过滤掉，合法值保留",
)
check(
    config.normalize_modalities(["video"], "image", [], "output") == ["image"],
    "全都不合法时回退到默认值，不存空列表",
)
check(
    config.normalize_modalities(None, "video", [], "output") == ["video"],
    "没写字段时走默认值",
)
check(
    config.invalid_modalities(["video", "text"], "image") == ["video"],
    "非法取值能被单独挑出来告警",
)

# 形状写错（数字/映射）：不炸配置，但要留下告警，不能默默当成没写
_shape_warnings: list[str] = []
_shape_spec = config._coerce_spec(  # noqa: SLF001 - 就是要测这层校验
    "image",
    {"id": "shape", "provider": "openai", "model": "m", "input_modalities": 5},
    0,
    _shape_warnings,
    {},
)
check(_shape_spec.input_modalities == ["text"], "形状写错时回退到默认模态")
check(
    any("input_modalities" in w for w in _shape_warnings),
    "形状写错时留下告警",
)

# 内置兜底（provider: native）不调接口，没有模态可言：不能按 supports 推默认值
_native_warnings: list[str] = []
_native_spec = config._coerce_spec(  # noqa: SLF001
    "image",
    {
        "id": "builtin-imagen",
        "provider": "native",
        "model": "builtin-imagen",
        "supports": ["text2img", "img2img"],
    },
    0,
    _native_warnings,
    {},
)
check(
    _native_spec.input_modalities == [] and _native_spec.output_modalities == [],
    "内置兜底不被贴上输入/输出模态",
)
check(_native_warnings == [], "内置兜底不会因此产生告警")

# 但如果手写层真的给它写了模态，要留一句话，不能静默丢掉
_explicit_warnings: list[str] = []
_explicit_spec = config._coerce_spec(  # noqa: SLF001
    "image",
    {
        "id": "builtin-imagen",
        "provider": "native",
        "model": "builtin-imagen",
        "input_modalities": ["text"],
    },
    0,
    _explicit_warnings,
    {},
)
check(
    _explicit_spec.input_modalities == []
    and any("内置兜底" in w for w in _explicit_warnings),
    "内置兜底显式写了模态时：忽略并告警",
)

# ---------------------------------------------------------------- 存盘往返
(_TMP / "models.yaml").write_text(
    """version: 1
vendors:
  - id: v1
    provider: openai
image:
  strategy: priority_then_weight
  models: []
video:
  strategy: priority_then_weight
  models: []
""",
    encoding="utf-8",
)

overlay = store.Overlay(data={"version": 1, "vendors": []}, base=config.base_layer())
overlay.upsert_model(
    {
        "vendor": "v1",
        "model": "sora-like",
        "kind": "video",
        "priority": 1,
        "weight": 50,
        "supports": ["text2video", "img2video"],
        "input_modalities": ["audio", "text", "image"],
        "output_modalities": ["video"],
    }
)
store.write_structured(store.overlay_path(), overlay.data, store.HEADER)

loaded = config.load_config()
spec = loaded.pools["video"].models[0]
check(
    spec.input_modalities == ["text", "image", "audio"],
    "模态写入叠加层后能原样读回（并按固定顺序）",
)
check(spec.output_modalities == ["video"], "输出模态往返一致")

as_dict = spec.to_dict()
check(
    as_dict.get("input_modalities_text") == ["文本", "图片", "音频"],
    "to_dict 给出中文标签，agent 不用自己维护映射",
)

# 页面每次都提交完整数组：少提交 = 沿用旧值，不能把标签清空
overlay.upsert_model(
    {
        "id": spec.id,
        "vendor": "v1",
        "model": "sora-like",
        "kind": "video",
        "priority": 1,
        "weight": 50,
        "supports": ["text2video"],
    }
)
kept = overlay.locate_model(spec.id)[1]
check(
    kept.get("input_modalities") == ["text", "image", "audio"],
    "再次保存但没提交模态时，原有标签被保留",
)

# ---------------------------------------------------------------- 生成闸门
# 语音不再「全平台未接」了：dashscope / openai / kling / volcengine 都已能真跑
# （见 tests/test_audio.py）。但其余 provider 的 TTS 协议各不相同，仍必须在入口拦下，
# 且用专门的异常类型（调用方据此不计熔断、不给"检查密钥"的误导提示）。
#
# 这里刻意**从 SUPPORTED_KINDS 里挑一个确实还没接 audio 的 provider**，而不是写死
# 某个名字 —— 写死的话，哪天真把它接上了，这条测试就会突然去打网络（本轮就踩了
# 两次：先是 openai，后是 volcengine）。
from mrouter import adapters  # noqa: E402

_uncovered = [
    p for p in ("replicate", "fal", "generic_http")
    if "audio" not in adapters.SUPPORTED_KINDS.get(p, set())
]
_audio_spec = config.ModelSpec(id="tts", provider=_uncovered[0], kind="audio")
_audio_req = adapters.GenRequest(kind="audio", prompt="你好")
_ctx = adapters.Ctx(api_key="k", key_source="test", timeout=1, poll_interval=1, max_poll=1)
try:
    adapters.run_adapter(_audio_spec, _audio_req, None, _ctx)
    _gate = "没拦住"
except adapters.KindNotSupported as exc:
    _gate = exc.kind
check(_gate == "audio", "未接 TTS 的 provider 在适配器入口被 KindNotSupported 拦下")
check(
    issubclass(adapters.KindNotSupported, adapters.HttpError),
    "KindNotSupported 属于 HttpError 家族（旧的错误处理不会漏）",
)

print(f"\n通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
