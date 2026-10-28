#!/usr/bin/env python3
"""语音合成通路的单元测试。

零依赖、不联网：把 adapters.request 换成桩，直接验证「请求体长什么样」和
「响应怎么解析」，不产生任何费用。

覆盖三条真实踩过的坑：
  1. 百炼两套 TTS 端点互不通用（qwen-audio 撞多模态端点会得到 url error）
  2. qwen-audio-*-tts-next 的入参字段是 text_prompt，不是文档写的 text
  3. /v1/audio/speech 直接回二进制；网关报错时也可能 200 + JSON，
     不认出来就会把一段 JSON 存成 .mp3

运行：python tests/test_audio.py
"""

import argparse
import io
import json
import os
import struct
import sys
import tempfile
import wave
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 末尾有一条 cli 级用例（语音克隆闸门）：把状态/产物目录指到临时目录，
# 避免它往仓库的 outputs/、state/ 里写东西。必须在 import config 之前设。
os.environ["MEDIA_ROUTER_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="mr-audio-state-")) / "state")
os.environ["MEDIA_ROUTER_OUTPUT_DIR"] = str(Path(tempfile.mkdtemp(prefix="mr-audio-out-")) / "outputs")

# 配置也指到临时目录：cli 级用例要能路由到一条语音模型。直接读用户真实的
# models.yaml 会让这个测试「配了语音模型才过、没配就崩」—— 出厂配置的语音池
# 是空的，于是这条测试在干净环境里必失败，而且报的是一长串 traceback。
_AUDIO_CFG = Path(tempfile.mkdtemp(prefix="mr-audio-cfg-")) / "config"
_AUDIO_CFG.mkdir(parents=True)
(_AUDIO_CFG / "models.yaml").write_text(
    "audio:\n"
    "  strategy: priority_then_weight\n"
    "  models:\n"
    "    - id: fake-tts\n"
    "      provider: openai\n"
    "      model: tts-1\n"
    "      priority: 1\n"
    "      weight: 100\n"
    "      supports: [text2audio]\n"
    "      api_key_env: OPENAI_API_KEY\n",
    encoding="utf-8",
)
os.environ["MEDIA_ROUTER_CONFIG_DIR"] = str(_AUDIO_CFG)

from mrouter import adapters  # noqa: E402
from mrouter.adapters import (  # noqa: E402
    Ctx,
    GenRequest,
    HttpError,
    _gen_dashscope_audio,
    _gen_openai_audio,
    _openai_speech_url,
    sniff_extension,
)
from mrouter.caption import audio_duration  # noqa: E402
from mrouter.config import ConfigError, ModelSpec  # noqa: E402

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


class FakeResp:
    def __init__(self, payload=None, body=b""):
        self._payload = payload
        self.body = body
        self.status = 200

    def json(self):
        return self._payload


def patch_request(fn):
    adapters.request = fn


CTX = Ctx(api_key="k", key_source="test", timeout=30, poll_interval=1, max_poll=30)
TMP = Path(tempfile.mkdtemp(prefix="mr-audio-"))


def dispatch_url_and_body(monkeypatch_payload):
    seen = {}

    def fake(method, url, **kw):
        seen["method"] = method
        seen["url"] = url
        seen["body"] = kw.get("json_body")
        return FakeResp(monkeypatch_payload)

    return seen, fake


print("语音合成通路测试")

# ---------------------------------------------------------------- 端点分派
dash = ModelSpec(id="tts-a", provider="dashscope", kind="audio", model="qwen-audio-3.1-tts-next")
seen, fake = dispatch_url_and_body({"output": {"audio": {"url": "http://x/a.wav", "duration": 1.5}}})
patch_request(fake)
res = _gen_dashscope_audio(dash, GenRequest(kind="audio", prompt="你好"), None, CTX)
check(seen["url"].endswith("/services/audio/tts/SpeechSynthesizer"), "qwen-audio 走 SpeechSynthesizer 端点")
check(seen["body"]["input"].get("text_prompt") == "你好", "入参带 text_prompt（文档写的是 text，实际要这个）")
check(seen["body"]["input"].get("text") == "你好", "同时带 text，兼容 cosyvoice 一类模型")
check(res.urls == ["http://x/a.wav"], "响应里的 audio.url 被取到")
check(res.meta.get("duration_seconds") == 1.5, "平台返回的 duration 写进 meta")

qwen3 = ModelSpec(id="tts-b", provider="dashscope", kind="audio", model="qwen3-tts-flash")
seen, fake = dispatch_url_and_body({"output": {"audio": {"url": "http://x/b.mp3"}}})
patch_request(fake)
_gen_dashscope_audio(qwen3, GenRequest(kind="audio", prompt="hi"), None, CTX)
check(seen["url"].endswith("/services/aigc/multimodal-generation/generation"), "qwen3-tts 走多模态端点")
check("text_prompt" not in seen["body"]["input"], "qwen3-tts 不塞 text_prompt")

# 自定义 endpoint 优先
custom = ModelSpec(
    id="tts-c", provider="dashscope", kind="audio", model="qwen-audio-3.1-tts-flash",
    endpoint="https://gw.example.com/tts",
)
seen, fake = dispatch_url_and_body({"output": {"audio": {"url": "http://x/c.wav"}}})
patch_request(fake)
_gen_dashscope_audio(custom, GenRequest(kind="audio", prompt="hi"), None, CTX)
check(seen["url"] == "https://gw.example.com/tts", "配置里的 endpoint 优先")

# params 透传
voiced = ModelSpec(
    id="tts-d", provider="dashscope", kind="audio", model="qwen-audio-3.1-tts-flash",
    params={"voice": "longanhuan_v3.1", "sample_rate": 24000},
)
seen, fake = dispatch_url_and_body({"output": {"audio": {"url": "http://x/d.wav"}}})
patch_request(fake)
_gen_dashscope_audio(voiced, GenRequest(kind="audio", prompt="hi"), None, CTX)
check(seen["body"]["input"]["voice"] == "longanhuan_v3.1" and seen["body"]["input"]["sample_rate"] == 24000,
      "params 透传进 input")

# base64 兜底
seen, fake = dispatch_url_and_body({"output": {"audio": {"url": "", "data": "QUJD"}}})
patch_request(fake)
res = _gen_dashscope_audio(voiced, GenRequest(kind="audio", prompt="hi"), None, CTX)
check(res.urls and res.urls[0].startswith("data:audio/wav;base64,"), "只有 base64 时转成 data URI")

# 没有音频字段要报错
seen, fake = dispatch_url_and_body({"output": {}})
patch_request(fake)
try:
    _gen_dashscope_audio(voiced, GenRequest(kind="audio", prompt="hi"), None, CTX)
    check(False, "缺 audio 字段应当抛错")
except HttpError as exc:
    check("没返回音频地址" in str(exc), "缺 audio 字段如实报错")

# ---------------------------------------------------------------- OpenAI 端点
check(_openai_speech_url(ModelSpec(id="o1", provider="openai", kind="audio")) ==
      "https://api.openai.com/v1/audio/speech", "缺省走官方 /audio/speech")
check(_openai_speech_url(ModelSpec(id="o2", provider="openai", kind="audio",
                                   endpoint="https://gw.example.com/v1/images/generations")) ==
      "https://gw.example.com/v1/audio/speech", "从 images/generations 推出 /audio/speech")
check(_openai_speech_url(ModelSpec(id="o3", provider="openai", kind="audio",
                                   endpoint="https://gw.example.com/v1")) ==
      "https://gw.example.com/v1/audio/speech", "从 /v1 推出 /audio/speech")
check(_openai_speech_url(ModelSpec(id="o4", provider="openai", kind="audio",
                                   endpoint="https://gw.example.com/v1/audio/speech")) ==
      "https://gw.example.com/v1/audio/speech", "已经是 /audio/speech 不重复拼")

# ---------------------------------------------------------------- OpenAI 二进制落盘
class _Binary:
    def __init__(self, body: bytes):
        self.body = body
        self.status = 200

    def json(self):
        return None


def fake_binary(data: bytes):
    def fake(method, url, **kw):
        return _Binary(data)

    return fake


wav_bytes = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 24
adapters.request = fake_binary(wav_bytes)
ospec = ModelSpec(id="o-tts", provider="openai", kind="audio", model="tts-1")
out_dir = TMP / "openai"
out_dir.mkdir(parents=True, exist_ok=True)
res = _gen_openai_audio(ospec, GenRequest(kind="audio", prompt="hi", output_dir=out_dir), None, CTX)
check(len(res.files) == 1 and res.files[0].endswith(".wav"), "二进制按真实文头存成 .wav")
check(Path(res.files[0]).stat().st_size == len(wav_bytes), "落盘大小与响应体一致")

adapters.request = fake_binary(json.dumps({"error": "boom"}).encode())
try:
    _gen_openai_audio(ospec, GenRequest(kind="audio", prompt="hi", output_dir=out_dir), None, CTX)
    check(False, "JSON 响应应当抛错")
except HttpError as exc:
    check("JSON" in str(exc), "200 + JSON 错误体被识破，不会存成音频")

adapters.request = fake_binary(wav_bytes)
try:
    _gen_openai_audio(ospec, GenRequest(kind="audio", prompt="hi", output_dir=None), None, CTX)
    check(False, "--no-download 应当报错")
except ConfigError as exc:
    # 用法错误 → ConfigError（不计熔断）。当成模型故障的话，连着点三次
    # --no-download 就会把这个模型打进冷却，而它其实一次请求都没发。
    check("没有可回传的 URL" in str(exc), "--no-download 明确报错（ConfigError，不计熔断）")

# ------------------------------------------------- 各家 /audio/speech 的默认参数
# 同一条协议，默认音色和格式并不通用，而且**参数校验发生在计费之前**：
# 实测智谱 glm-tts 传 voice=alloy 直接 400「音色不存在」，response_format=mp3
# 400「不支持当前 response_format 值」。默认值写死 OpenAI 那一套，智谱就永远出不了声。
def speech_call(endpoint, model="glm-tts", params=None, extra=None):
    sink = []
    adapters.request = lambda method, url, **kw: (
        sink.append({"url": url, "body": kw.get("json_body")}) or _Binary(wav_bytes)
    )
    spec = ModelSpec(id="voice-model", provider="openai", kind="audio",
                     model=model, endpoint=endpoint, params=params or {})
    _gen_openai_audio(
        spec,
        GenRequest(kind="audio", prompt="hi", output_dir=out_dir, extra=extra or {}),
        None, CTX,
    )
    return sink[0]["body"]


_zp = speech_call("https://open.bigmodel.cn/api/paas/v4/audio/speech")
check(_zp["voice"] == "tongtong", "智谱默认音色是 tongtong，不是 OpenAI 的 alloy")
check(_zp["response_format"] == "wav", "智谱默认格式是 wav（它不收 mp3）")

_zp2 = speech_call("https://open.bigmodel.cn/api/paas/v4/audio/speech",
                   params={"voice": "chuichui"})
check(_zp2["voice"] == "chuichui", "用户显式指定的音色优先于域名默认值")

_zp3 = speech_call("https://open.bigmodel.cn/api/paas/v4/audio/speech",
                   params={"response_format": "pcm"})
check(_zp3["response_format"] == "pcm", "用户显式指定的格式优先于域名默认值")

_oai = speech_call("", model="tts-1")
check(_oai["voice"] == "alloy" and _oai["response_format"] == "mp3",
      "没有端点时仍走 OpenAI 官方那套（alloy / mp3）")

_sf = speech_call("https://api.siliconflow.cn/v1/audio/speech",
                  model="FunAudioLLM/CosyVoice2-0.5B")
check(_sf["voice"] == "FunAudioLLM/CosyVoice2-0.5B:alex",
      "硅基流动的音色要带模型前缀，默认补 <model>:alex")

# response_format 和 format 同时给时，两个键都要从 params 里摘掉：
# 漏掉 format 就会把它原样发给接口，严格点的网关直接 400。
_both = speech_call("https://open.bigmodel.cn/api/paas/v4/audio/speech",
                    params={"response_format": "pcm", "format": "mp3"})
check(_both["response_format"] == "pcm", "response_format 优先于 format")
check("format" not in _both, "format 不会作为多余字段漏进请求体")

# ---------------------------------------------------------------- 时长解析
wav_path = TMP / "d.wav"
with wave.open(str(wav_path), "wb") as fh:
    fh.setnchannels(1)
    fh.setsampwidth(2)
    fh.setframerate(16000)
    fh.writeframes(b"\x00\x00" * 16000)
check(audio_duration(wav_path) == 1.0, "wav 时长精确解析（1.0s）")

# 流式 wav：data 长度是占位值，必须按文件实际长度钳制
stream_wav = TMP / "stream.wav"
frames = b"\x00\x00" * 16000
stream_wav.write_bytes(
    b"RIFF" + struct.pack("<I", 0x7FFFFFFF) + b"WAVEfmt "
    + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    + b"data" + struct.pack("<I", 0x7FFFFFFF) + frames
)
check(audio_duration(stream_wav) == 1.0, "data 长度是占位值（0x7FFFFFFF）时仍按实际长度算")

check(audio_duration(TMP / "nope.mp3") is None, "不存在的文件返回 None")
(TMP / "junk.bin").write_bytes(b"not audio")
check(audio_duration(TMP / "junk.bin") is None, "非音频文件返回 None")

# ---------------------------------------------------------------- 文件头嗅探
check(sniff_extension(b"RIFF\x00\x00\x00\x00WAVEfmt ") == ".wav", "WAVE 识别为 .wav")
check(sniff_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp", "WEBP 不被误判成 wav")
check(sniff_extension(b"fLaC\x00\x00") == ".flac", "FLAC 识别")
check(sniff_extension(b"OggS\x00\x02") == ".ogg", "OGG 识别")

# ---------------------------------------------------------------- 类目矩阵
check("audio" in adapters.SUPPORTED_KINDS["dashscope"], "dashscope 声明支持 audio")
check("audio" in adapters.SUPPORTED_KINDS["openai"], "openai 声明支持 audio")
check("audio" in adapters.SUPPORTED_KINDS["volcengine"], "volcengine 声明支持 audio")
check("audio" not in adapters.SUPPORTED_KINDS["replicate"], "replicate 未接 TTS，不谎报支持")
check("image" in adapters.SUPPORTED_KINDS["dashscope"], "加上 audio 没把 image 弄丢")

# 页面上的「支持 image / video / audio」和真实适配器能力必须一致 ——
# 这两处一份写在 catalog.kinds（给页面看），一份写在 SUPPORTED_KINDS（决定能不能跑）。
# 智谱和硅基流动都是 provider=openai，适配器早就认 audio 了，但 catalog 原先没写，
# 于是卡片说"不支持语音"，用户压根不会想到能配。这类"两处真相"最容易悄悄分叉。
from mrouter import catalog as _catalog  # noqa: E402

_drift = []
for _entry in _catalog.CATALOG:
    _provider = _entry.provider or _entry.key
    _adapter_audio = "audio" in adapters.SUPPORTED_KINDS.get(_provider, set())
    _page_audio = "audio" in (_entry.kinds or [])
    if _adapter_audio != _page_audio:
        _drift.append(f"{_entry.key}(页面={_page_audio}, 适配器={_adapter_audio})")
check(not _drift, "每个厂商的「页面类目」与「适配器能力」对得上：" + (", ".join(_drift) or "无分叉"))

# 声明了 audio 的厂商，必须给出音频端点 —— 否则新增语音模型时端点会留空，
# _openai_speech_url 会静默回落到 api.openai.com，拿别家的 Key 去打官方，401 还查不出原因。
_no_endpoint = [
    _entry.key for _entry in _catalog.CATALOG
    if "audio" in (_entry.kinds or []) and not (_entry.endpoints or {}).get("audio")
]
check(not _no_endpoint, "声明 audio 的厂商都配了音频端点：" + (", ".join(_no_endpoint) or "齐全"))

# ---------------------------------------------------------------- 多段（--count）
seen_urls = []


def counting_fake(method, url, **kw):
    seen_urls.append(url)
    return FakeResp({"output": {"audio": {"url": f"http://x/{len(seen_urls)}.wav", "duration": 1.0}}})


patch_request(counting_fake)
res = _gen_dashscope_audio(dash, GenRequest(kind="audio", prompt="你好", count=3), None, CTX)
check(len(res.urls) == 3, "--count 3 会真的发 3 次请求（之前固定只发 1 次）")
check(res.meta.get("count") == 3, "count 与实际产物数一致")

bin_calls = []


def binary_counting(method, url, **kw):
    bin_calls.append(url)
    return _Binary(wav_bytes)


adapters.request = binary_counting
count_dir = TMP / "openai-count"
count_dir.mkdir(parents=True, exist_ok=True)
res = _gen_openai_audio(
    ospec, GenRequest(kind="audio", prompt="hi", count=2, output_dir=count_dir), None, CTX
)
check(len(res.files) == 2 and len(bin_calls) == 2,
      f"openai 通路 --count 2 也落两段（files={len(res.files)} calls={len(bin_calls)}）")
check(res.files[0].endswith("_01.wav") and res.files[1].endswith("_02.wav"), "多段按 _01/_02 编号")

# ---------------------------------------------------------------- 可灵（异步任务）
from mrouter.adapters import _gen_kling_audio  # noqa: E402

kling_calls = []


def kling_fake(method, url, **kw):
    kling_calls.append({"method": method, "url": url, "body": kw.get("json_body"), "headers": kw.get("headers")})
    if method == "POST":
        return FakeResp({"code": 0, "message": "SUCCEED", "data": {"task_id": "task-1", "task_status": "submitted"}})
    if len([c for c in kling_calls if c["method"] == "GET"]) == 1:
        return FakeResp({"code": 0, "data": {"task_status": "processing"}})
    return FakeResp({
        "code": 0,
        "data": {
            "task_status": "succeed",
            "task_result": {"audios": [{"id": "aud-1", "url": "http://x/a.mp3", "duration": "2.232"}]},
        },
    })


spec_kling = ModelSpec(
    id="kling-tts", provider="kling", kind="audio", model="kling-tts",
    api_key_env="KLING_KEYS", params={"voice_id": "chuanmeizi_speech02", "voice_language": "zh"},
)
adapters.request = kling_fake
KLING_HEADERS = {"Authorization": "Bearer fake-jwt", "Content-Type": "application/json"}
res = _gen_kling_audio(spec_kling, GenRequest(kind="audio", prompt="你好"), None, CTX, KLING_HEADERS)
post = next(c for c in kling_calls if c["method"] == "POST")
check(post["url"].endswith("/audio/tts"), "可灵提交到 /audio/tts")
check(post["body"].get("text") == "你好", "请求体带 text")
check(post["body"].get("voice_id") == "chuanmeizi_speech02", "params.voice_id 透传（音色靠它选）")
check("model_name" not in post["body"], "TTS 请求体不带 model_name（官方样例里没有）")
check(str(post["headers"].get("Authorization", "")).startswith("Bearer "), "鉴权是 Bearer JWT")
check(kling_calls[1]["url"].endswith("/audio/tts/task-1"), "轮询地址是 /audio/tts/{task_id}")
check(res.urls == ["http://x/a.mp3"], "从 task_result.audios[].url 取到音频")
check(res.meta.get("duration_seconds") == 2.23, "duration 是字符串也解析成数字（2.232 → 2.23）")
check(res.meta.get("audio_id") == "aud-1", "audio_id 一并回传")

# 失败状态要如实报错
def kling_fail(method, url, **kw):
    if method == "POST":
        return FakeResp({"code": 0, "data": {"task_id": "t2", "task_status": "submitted"}})
    return FakeResp({"code": 0, "data": {"task_status": "failed", "task_status_msg": "余额不足"}})


adapters.request = kling_fail
try:
    _gen_kling_audio(spec_kling, GenRequest(kind="audio", prompt="你好"), None, CTX, KLING_HEADERS)
    check(False, "失败任务应当抛错")
except HttpError as exc:
    check("余额不足" in str(exc), "任务失败原因被带出来")

# 业务错误码
adapters.request = lambda method, url, **kw: FakeResp({"code": 1201, "message": "invalid token"})
try:
    _gen_kling_audio(spec_kling, GenRequest(kind="audio", prompt="你好"), None, CTX, KLING_HEADERS)
    check(False, "业务错误码应当抛错")
except HttpError as exc:
    check("1201" in str(exc) and "invalid token" in str(exc), "可灵业务错误码如实报出")

check("audio" in adapters.SUPPORTED_KINDS["kling"], "kling 声明支持 audio")
check("audio" in adapters.SUPPORTED_KINDS["keling"], "keling 别名也支持 audio")

# 音色是可灵 TTS 的必填项：缺了要**提前**报，而不是白跑一次拿 1201
def _explode(method, url, **kw):
    raise AssertionError("缺音色时不该发请求")


adapters.request = _explode
try:
    _gen_kling_audio(
        ModelSpec(id="kling-tts", provider="kling", kind="audio", model="kling-tts"),
        GenRequest(kind="audio", prompt="你好"), None, CTX, KLING_HEADERS,
    )
    check(False, "缺音色应当抛 ConfigError")
except ConfigError as exc:
    check("voice_id" in str(exc), "缺音色时提前报错（不花一次往返）")

# 只填音色时，语言默认 zh
sent = {}


def _capture(method, url, **kw):
    sent.update(kw.get("json_body") or {})
    return FakeResp({"code": 0, "data": {"task_id": "t9", "task_status": "succeed",
                                         "task_result": {"audios": [{"url": "http://x/9.mp3"}]}}})


adapters.request = _capture
_gen_kling_audio(
    ModelSpec(id="kling-tts", provider="kling", kind="audio", model="kling-tts",
              params={"voice_id": "chuanmeizi_speech02"}),
    GenRequest(kind="audio", prompt="你好"), None, CTX, KLING_HEADERS,
)
check(sent.get("voice_language") == "zh", "只填音色时语言默认 zh")

# 可灵两种鉴权都要支持：新版单个 API Key 直接 Bearer，旧版 AK:SK 签 JWT
from mrouter.adapters import _kling_authorization  # noqa: E402

_new_key = Ctx(api_key="api-key-kling-abcdefghij", key_source="t", timeout=5, poll_interval=1, max_poll=5)
check(_kling_authorization(ModelSpec(id="k", provider="kling", kind="audio"), _new_key, None)
      == "Bearer api-key-kling-abcdefghij", "新版单个 API Key 直接当 Bearer（不签名）")

_old_key = Ctx(api_key="ak-123:sk-456", key_source="t", timeout=5, poll_interval=1, max_poll=5)
_old_header = _kling_authorization(ModelSpec(id="k", provider="kling", kind="audio"), _old_key, None)
check(_old_header.startswith("Bearer ") and _old_header.count(".") == 2, "旧版 AK:SK 仍然签成 JWT")

try:
    _kling_authorization(
        ModelSpec(id="k", provider="kling", kind="audio"),
        Ctx(api_key="", key_source="t", timeout=5, poll_interval=1, max_poll=5), None,
    )
    check(False, "空密钥应当抛 ConfigError")
except ConfigError as exc:
    check("API Key" in str(exc), "空密钥报配置错误（不计熔断）")

# ---------------------------------------------------------------- 火山（豆包语音，流式 JSON）
import base64  # noqa: E402

from mrouter.adapters import _gen_volcengine_audio  # noqa: E402

volc_body = "\n".join([
    json.dumps({"code": 0, "data": base64.b64encode(b"ID3" + b"\x01" * 20).decode()}),
    json.dumps({"code": 0, "data": base64.b64encode(b"\x02" * 20).decode()}),
    json.dumps({"code": 20000000, "message": "OK", "data": None}),
]).encode()


class _Stream:
    def __init__(self, body: bytes):
        self.body = body
        self.status = 200

    def json(self):
        return None


volc_calls = []


def volc_fake(method, url, **kw):
    volc_calls.append({"url": url, "headers": dict(kw.get("headers") or {}), "body": kw.get("json_body")})
    return _Stream(volc_body)


spec_volc = ModelSpec(
    id="doubao-tts", provider="volcengine", kind="audio", model="doubao-tts",
    params={"speaker": "zh_female_tianmeixiaoyuan_moon_bigtts"},
)
adapters.request = volc_fake
volc_dir = TMP / "volc"
volc_dir.mkdir(parents=True, exist_ok=True)
res = _gen_volcengine_audio(
    spec_volc, GenRequest(kind="audio", prompt="你好", output_dir=volc_dir), None, CTX
)
first = volc_calls[0]
check(first["url"].endswith("/api/v3/tts/unidirectional"), "火山走豆包语音的 v3 单向流式接口")
check(first["headers"].get("X-Api-Key") == "k", "新版鉴权用 X-Api-Key")
check(first["headers"].get("X-Api-Resource-Id") == "seed-tts-2.0", "带 X-Api-Resource-Id（缺了直接 400）")
check(first["body"]["req_params"]["speaker"] == "zh_female_tianmeixiaoyuan_moon_bigtts", "音色放在 req_params.speaker")
check(first["body"]["req_params"]["audio_params"]["format"] == "mp3", "audio_params 默认 mp3")
check(len(res.files) == 1 and res.files[0].endswith(".mp3"), "多段 base64 片段拼接后落盘为 mp3")
check(Path(res.files[0]).read_bytes() == (b"ID3" + b"\x01" * 20) + b"\x02" * 20,
      "音频片段按顺序拼接，内容一致")
check(res.meta.get("resource_id") == "seed-tts-2.0", "meta 里回传 resource_id")

# 旧版鉴权：appid:token
volc_calls.clear()
adapters.request = volc_fake
_gen_volcengine_audio(
    spec_volc, GenRequest(kind="audio", prompt="你好", output_dir=volc_dir), None,
    Ctx(api_key="1234567:my-token", key_source="t", timeout=5, poll_interval=1, max_poll=5),
)
hdr = volc_calls[0]["headers"]
check(hdr.get("X-Api-App-Id") == "1234567" and hdr.get("X-Api-Access-Key") == "my-token",
      "旧版 appid:token 拆成 X-Api-App-Id / X-Api-Access-Key")
check("X-Api-Key" not in hdr, "旧版鉴权不会同时带 X-Api-Key")

# 缺音色：提前报错、不发请求
def _no_call(method, url, **kw):
    raise AssertionError("缺 speaker 时不该发请求")


adapters.request = _no_call
try:
    _gen_volcengine_audio(
        ModelSpec(id="t", provider="volcengine", kind="audio", model="doubao-tts"),
        GenRequest(kind="audio", prompt="你好", output_dir=volc_dir), None, CTX,
    )
    check(False, "缺音色应当抛 ConfigError")
except ConfigError as exc:
    check("speaker" in str(exc), "缺音色时提前报错（不花一次往返）")

# 只有错误码、没有音频
adapters.request = lambda method, url, **kw: _Stream(
    json.dumps({"code": 45000000, "message": "quota exceeded"}).encode()
)
try:
    _gen_volcengine_audio(spec_volc, GenRequest(kind="audio", prompt="你好", output_dir=volc_dir), None, CTX)
    check(False, "无音频应当抛错")
except HttpError as exc:
    check("45000000" in str(exc) and "quota" in str(exc), "错误码与原文一并带出")

check("audio" in adapters.SUPPORTED_KINDS["volcengine"], "volcengine 声明支持 audio")

# ogg_opus 不能直接当扩展名；model / voice_instruction 属于 req_params
_plain_chunk = base64.b64encode(b"\x00" * 20).decode()


def _ogg_fake(method, url, **kw):
    volc_calls.append({"url": url, "headers": dict(kw.get("headers") or {}), "body": kw.get("json_body")})
    return _Stream((json.dumps({"code": 0, "data": _plain_chunk}) + "\n").encode())


adapters.request = _ogg_fake
volc_calls.clear()
_spec_ogg = ModelSpec(
    id="doubao-tts", provider="volcengine", kind="audio", model="doubao-tts",
    params={"speaker": "s", "format": "ogg_opus",
            "model": "seed-tts-2.0-standard", "voice_instruction": "语速慢一点"},
)
_res = _gen_volcengine_audio(
    _spec_ogg, GenRequest(kind="audio", prompt="hi", output_dir=volc_dir), None, CTX
)
check(_res.files[0].endswith(".ogg"), "ogg_opus 落成 .ogg（不是 .ogg_opus）")
_rp = volc_calls[0]["body"]["req_params"]
check(_rp.get("voice_instruction") == "语速慢一点", "voice_instruction 进 req_params")
check(_rp.get("model") == "seed-tts-2.0-standard", "model 进 req_params")

# 多段请求各自带不同的 X-Api-Request-Id（复用同一个会让服务端日志串成一次）
adapters.request = volc_fake
volc_calls.clear()
_gen_volcengine_audio(
    spec_volc, GenRequest(kind="audio", prompt="hi", count=2, output_dir=volc_dir), None, CTX
)
_ids = [c["headers"].get("X-Api-Request-Id") for c in volc_calls]
check(len(_ids) == 2 and _ids[0] != _ids[1], "多段请求的 X-Api-Request-Id 各自不同")

# ---------------------------------------------------------------- 语音克隆闸门
# --image 走的是 audio2audio：没有适配器，必须明确拒绝，不能静默忽略输入音频
from mrouter import cli  # noqa: E402

_attempted = {"n": 0}


def _should_not_run(*a, **k):
    _attempted["n"] += 1
    raise AssertionError("语音克隆不该走到适配器")


cli.run_adapter = _should_not_run
args = argparse.Namespace(
    kind="audio", prompt="克隆我的声音", supports=None, image=[str(TMP / "voice.wav")],
    model=None, negative_prompt=None, size=None, aspect_ratio=None, duration=None,
    count=None, output_dir=None, max_attempts=2, no_download=False, param=None, pretty=False,
)
buf = io.StringIO()
with redirect_stdout(buf):
    rc = cli.cmd_generate(args)
out = json.loads(buf.getvalue())
check(rc == 1 and out["status"] == "error", "audio + --image 直接报错（退出码 1）")
check("语音克隆" in out["error"], "错误信息点名「语音克隆/变声」")
check(_attempted["n"] == 0, "被拦住时没有调用任何适配器（没花钱、没静默产出普通 TTS）")

# 火山的错误体把 code/message 包在 header 里，别家的解析要认得
_hdr_err = HttpError(
    "HTTP 401 x", 401, json.dumps({"header": {"code": 45000010, "message": "Invalid X-Api-Key"}})
)
check(
    "45000010" in cli._vendor_error_code(_hdr_err)
    and "Invalid X-Api-Key" in cli._vendor_error_detail(_hdr_err),
    "火山 header 嵌套的错误码/原文都能压出来",
)

print()
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
