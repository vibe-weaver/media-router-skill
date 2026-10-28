#!/usr/bin/env python3
"""真实链路测试后修复项的回归测试（零依赖、不联网、不花钱）。

覆盖 2026-10-28 真跑中发现、并已修正的问题：
  1. 结果 JSON 里的 `urls[]` 签名被 CLI 打码成 `***`，链接变成死链
  2. 流式 WAV 的 RIFF/data 长度字段是占位值，播放器显示 11184 秒
  3. `caption` 在 metadata 模式下恒为空，与 SKILL「汇报用 caption」冲突
  4. 同步出图/出音频期间 stderr 没有任何进度，用户分不清卡没卡死
  5. 配置页缺少「单张约 2MB」的推荐尺寸预设

运行：python tests/test_real_fixes.py
"""

import io
import json
import struct
import sys
import tempfile
import wave
import zlib
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mrouter import adapters, caption, cli, config, transport  # noqa: E402
from mrouter.adapters import Ctx, GenRequest, _gen_dashscope_qwen_image, _repair_wav_sizes  # noqa: E402
from mrouter.config import ModelSpec  # noqa: E402

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


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 58 - len(title)))


print("真实链路修复回归测试")

# ---------------------------------------------------------------- 1. URL 打码
section("urls[] 保留签名，日志/错误文本照旧打码")

signed = "https://oss.example.com/a.png?Expires=1&OSSAccessKeyId=K&Signature=abc123"
check("Signature=***" in transport.redact(signed), "日志模式下签名会被打码")
check("Signature=abc123" in transport.redact(signed, mask_signature=False),
      "JSON 模式下签名原样保留")
check("key=***" in transport.redact("https://gw/x?key=secret", mask_signature=False),
      "JSON 模式下 ?key= 仍然打码（只放过签名）")

buf = io.StringIO()
with redirect_stdout(buf):
    cli._emit(
        {
            "status": "ok",
            "urls": [signed],
            "files": [{"path": "E:\\out\\a.png", "size_human": "5.2MB"}],
            "error": "请求 https://gw.example.com/x?key=secret 失败",
        },
        False,
    )
payload = json.loads(buf.getvalue())
check(payload["urls"][0] == signed, "结果 JSON 的 urls 可直接使用（签名未被破坏）")
check("secret" not in payload["error"] and "***" in payload["error"],
      "error 文本里的 ?key= 仍被打码")
check(payload["files"][0]["path"].endswith("a.png"), "files[].path 原样保留")

# ---------------------------------------------------------------- 2. WAV 头
section("流式 WAV 头修复")


def make_placeholder_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    frames = b"\x00\x00" * int(rate * seconds)
    fmt = struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return (
        b"RIFF"
        + struct.pack("<I", 0x7FFFFF00)  # 占位：真实平台常见形态
        + b"WAVE"
        + b"fmt "
        + fmt
        + b"data"
        + struct.pack("<I", 0x7FFFFF00)
        + frames
    )


wav_path = Path(tempfile.mkdtemp(prefix="mr-fix-wav-")) / "stream.wav"
wav_path.write_bytes(make_placeholder_wav())
_repair_wav_sizes(wav_path)
with wave.open(str(wav_path)) as fh:
    duration = fh.getnframes() / fh.getframerate()
check(abs(duration - 1.0) < 0.01, f"修复后通用库读出的时长是真实的 1.0 秒（实际 {duration:.2f}）")
check(caption.audio_duration(wav_path) == 1.0, "内部 audio_duration 也一致")

normal = Path(tempfile.mkdtemp(prefix="mr-fix-wav2-")) / "ok.wav"
with wave.open(str(normal), "wb") as fh:
    fh.setnchannels(1)
    fh.setsampwidth(2)
    fh.setframerate(16000)
    fh.writeframes(b"\x00\x00" * 1600)
before = normal.read_bytes()
_repair_wav_sizes(normal)
check(normal.read_bytes() == before, "本来就正确的 wav 不会被改写")

out_dir = Path(tempfile.mkdtemp(prefix="mr-fix-wav3-"))
dest = adapters._write_media_bytes(make_placeholder_wav(), ".wav", out_dir, "audio_x", 1)
check(caption.audio_duration(dest) == 1.0, "_write_media_bytes 落盘时也会顺手修头")

# ---------------------------------------------------------------- 3. caption
section("metadata 模式给出可读描述")


def tiny_png(width: int = 2, height: int = 2) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    body = b"\x00" * (width * 3 + 1) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
        + struct.pack(">I", len(body))
        + b"IDAT"
        + body
        + struct.pack(">I", zlib.crc32(b"IDAT" + body) & 0xFFFFFFFF)
        + struct.pack(">I", 0)
        + b"IEND"
        + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    )


cfg_root = Path(tempfile.mkdtemp(prefix="mr-fix-cfg-"))
(cfg_root / "config").mkdir()
(cfg_root / "config" / "models.yaml").write_text(
    "defaults:\n  caption:\n    mode: metadata\nimage:\n  models: []\n", encoding="utf-8"
)
config.CONFIG_DIR = cfg_root / "config"
config.STATE_DIR = cfg_root / "state"
cfg = config.load_config()

png = Path(tempfile.mkdtemp(prefix="mr-fix-png-")) / "a.png"
png.write_bytes(tiny_png())
text, _notes = caption.build_caption(cfg, [str(png)], "image")
check("图片已生成" in text and "2×2" in text and "PNG" in text,
      f"图像 caption 带尺寸/格式：{text!r}")

wav2 = Path(tempfile.mkdtemp(prefix="mr-fix-wav4-")) / "a.wav"
with wave.open(str(wav2), "wb") as fh:
    fh.setnchannels(1)
    fh.setsampwidth(2)
    fh.setframerate(16000)
    fh.writeframes(b"\x00\x00" * 8000)
text, _notes = caption.build_caption(cfg, [str(wav2)], "audio")
check("音频已生成" in text and "0.50 秒" in text, f"音频 caption 带时长：{text!r}")

# ---------------------------------------------------------------- 4. 同步进度
section("同步接口等待期间有进度日志")

logs: list[str] = []


class Resp:
    status = 200
    body = b""

    def json(self):
        return {"output": {"choices": [{"message": {"content": [{"image": "https://x/a.png"}]}}]}}


real_request = adapters.request
adapters.request = lambda *a, **k: Resp()
real_download = adapters.download
adapters.download = lambda url, dest, **kw: (Path(dest).write_bytes(tiny_png()), Path(dest))[1]
try:
    _gen_dashscope_qwen_image(
        ModelSpec(id="qwen", provider="dashscope", kind="image", model="qwen-image-2.0"),
        GenRequest(kind="image", prompt="p", output_dir=Path(tempfile.mkdtemp(prefix="mr-fix-q-"))),
        None,
        Ctx(api_key="k", key_source="t", timeout=5, poll_interval=1, max_poll=5, log=logs.append),
    )
finally:
    adapters.request = real_request
    adapters.download = real_download

check(any("等待平台响应" in line for line in logs), "提交后有「等待平台响应」提示")
check(any("平台已返回" in line for line in logs), "返回后有耗时提示")

# ---------------------------------------------------------------- 5. 页面预设
section("配置页尺寸预设")
html = (ROOT / "assets" / "config.html").read_text(encoding="utf-8")
check('value="1328x1328"' in html, "新增 1328×1328 推荐尺寸")
check("单张约 2MB" in html, "选项文案说明了体积")
check("单张 5MB 以上" in html, "提示里说明了「不指定」的平台默认可能很大")

# ---------------------------------------------------------------- 复核补充
section("复核：边界条件（自查发现的问题）")

# 5.1 data 之后还有元数据 chunk 时，不能把尾随字节算进音频
fmt_payload = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
frames = b"\x00\x00" * 1600  # 3200 字节 = 0.1 秒
trailing = b"LIST" + struct.pack("<I", 8) + b"INFOxxxx"
body = (
    b"WAVE"
    + b"fmt " + struct.pack("<I", len(fmt_payload)) + fmt_payload
    + b"data" + struct.pack("<I", len(frames)) + frames
    + trailing
)
trail_wav = Path(tempfile.mkdtemp(prefix="mr-fix-trail-")) / "t.wav"
trail_wav.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
_repair_wav_sizes(trail_wav)
raw = trail_wav.read_bytes()
data_at = raw.find(b"data")
check(struct.unpack("<I", raw[data_at + 4:data_at + 8])[0] == len(frames),
      "data 后的 LIST/INFO 不会被算进音频长度")
check(caption.audio_duration(trail_wav) == 0.1, "此时时长仍是 0.1 秒")

# 5.2 caption 复用调用方已经算好的 profile，不再二次读文件
reads = {"n": 0}
real_profile = caption.profile_files


def counting_profile(files, kind):
    reads["n"] += 1
    return real_profile(files, kind)


caption.profile_files = counting_profile
try:
    caption.build_caption(cfg, [str(png)], "image", profiles=[{"format": "png", "size_human": "1B"}])
finally:
    caption.profile_files = real_profile
check(reads["n"] == 0, "传了 profiles 时 caption 不再重复读文件")

# 5.3 可灵 base64_only 遇到 data URI 要剥掉前缀
data_uri = "data:image/png;base64,iVBORw0KGgo="
check(adapters._image_value(data_uri, base64_only=True) == "iVBORw0KGgo=",
      "可灵的裸 Base64 字段不再带 data: 前缀")
check(adapters._image_value(data_uri) == data_uri, "其它通路仍然保留 data URI")

# 5.4 页面清空自定义接口地址/参数后，手写层的旧值不能复活
cfg_root = Path(tempfile.mkdtemp(prefix="mr-fix-vendor-"))
(cfg_root / "config").mkdir()
(cfg_root / "config" / "models.yaml").write_text(
    "vendors:\n"
    "  - id: v1\n    provider: openai\n    api_key_env: OPENAI_API_KEY\n"
    "    endpoints:\n      image: https://old-gw.example.com/v1/images/generations\n"
    "    options:\n      custom: 1\n"
    "image:\n  models: []\n",
    encoding="utf-8",
)
config.CONFIG_DIR = cfg_root / "config"
config.STATE_DIR = cfg_root / "state"
from mrouter import store  # noqa: E402

store.save_with(
    lambda ov: ov.upsert_vendor(
        {
            "id": "v1",
            "catalog_key": "openai",
            "label": "OpenAI",
            "api_key_env": "OPENAI_API_KEY",
            "endpoints": {},
            "options": "",
        }
    )
)
reloaded = config.load_config()
from mrouter import catalog  # noqa: E402

default_image = catalog.get("openai").endpoints["image"]
check(
    reloaded.vendors["v1"].endpoints.get("image") == default_image,
    "清空接口地址后回落到厂商默认地址（旧网关地址不复活）",
)
check(
    "old-gw.example.com" not in str(reloaded.vendors["v1"].endpoints.get("image")),
    "旧网关地址确实不在了",
)
check(not reloaded.vendors["v1"].options, "清空接口参数后也不会复活")

# 5.5 错误文本里的签名仍然要打码（只有地址字段保留签名）
buf = io.StringIO()
with redirect_stdout(buf):
    cli._emit(
        {
            "status": "error",
            "error": f"下载失败：https://oss.example.com/a?Signature=abc123",
            "urls": ["https://oss.example.com/a?Signature=abc123"],
        },
        False,
    )
payload = json.loads(buf.getvalue())
check("abc123" not in payload["error"], "error 文本里的签名被打码")
check(payload["urls"][0].endswith("Signature=abc123"), "同一份结果里的 urls 仍保留签名")

print()
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
