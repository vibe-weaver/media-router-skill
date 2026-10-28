#!/usr/bin/env python3
"""`--param` 透传的回归测试（零依赖、不联网、不花钱）。

背景：`--param key=value` 的语义是「用户点名要发给这个接口的字段」。以前多数适配器
只认一份白名单，白名单外的键被静默丢掉 —— 平台收不到、日志里也没有任何交代。
这里逐个通路验证：请求体里真的出现了，而且日志里说明了透了什么。

运行：python tests/test_param_fixes.py
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mrouter import adapters  # noqa: E402
from mrouter.adapters import Ctx, GenRequest  # noqa: E402
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
    print(f"--- {title} " + "-" * max(0, 60 - len(title)))


class Captured(Exception):
    """截到请求体就中断，不依赖各家响应形状。"""


captured: dict[str, object] = {}
logs: list[str] = []


def capture(method, url, **kw):
    captured["method"] = method
    captured["url"] = url
    captured["json"] = kw.get("json_body")
    captured["form"] = kw.get("form")
    raise Captured()


adapters.request = capture


def run(adapter, spec, req, *extra_args):
    captured.clear()
    logs.clear()
    ctx = Ctx(api_key="ak:sk", key_source="t", timeout=5, poll_interval=1, max_poll=5,
              log=logs.append)
    try:
        adapter(spec, req, None, ctx, *extra_args)
    except Captured:
        pass
    return captured


def spec(provider, kind, model="m", **kw):
    return ModelSpec(id=kw.pop("id", "m"), provider=provider, kind=kind, model=model, **kw)


print("--param 透传回归测试")

# ---------------------------------------------------------------- openai
section("OpenAI 兼容")
cap = run(
    adapters.gen_openai,
    spec("openai", "image", "gpt-image-1"),
    GenRequest(kind="image", prompt="p", extra={"seed": 42}),
)
check((cap.get("json") or {}).get("seed") == 42, "图像 generations：--param seed 进了请求体")
check(any("透传" in line for line in logs), "并且日志里说明了透传的字段")

img = Path(tempfile.mkdtemp(prefix="mr-param-")) / "in.png"
img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
cap = run(
    adapters.gen_openai,
    spec("openai", "image", "gpt-image-1"),
    GenRequest(kind="image", prompt="p", images=[str(img)], extra={"seed": 7}),
)
check((cap.get("form") or {}).get("seed") == 7, "图像 edits（multipart）：--param seed 也生效")

cap = run(
    adapters.gen_openai,
    spec("openai", "video", "cogvideox-3"),
    GenRequest(kind="video", prompt="p", extra={"seed": 9}),
)
check((cap.get("json") or {}).get("seed") == 9, "视频：--param seed 进了请求体")

# ---------------------------------------------------------------- dashscope
section("百炼 / 通义万相")
cap = run(
    adapters.gen_dashscope,
    spec("dashscope", "image", "wanx2.1-t2i"),
    GenRequest(kind="image", prompt="p", extra={"seed": 3}),
)
params = (cap.get("json") or {}).get("parameters") or {}
check(params.get("seed") == 3, "wanx 图像：--param seed 进了 parameters")
check(params.get("n") == 1, "没传 --param n 时仍按 --count 填 n")

cap = run(
    adapters.gen_dashscope,
    spec("dashscope", "image", "wanx2.1-t2i"),
    GenRequest(kind="image", prompt="p", count=2, extra={"n": 4}),
)
params = (cap.get("json") or {}).get("parameters") or {}
check(params.get("n") == 4, "显式 --param n 覆盖 --count（用户点名的字段优先）")

cap = run(
    adapters.gen_dashscope,
    spec("dashscope", "image", "qwen-image-2.0"),
    GenRequest(kind="image", prompt="p", extra={"seed": 5}),
)
params = (cap.get("json") or {}).get("parameters") or {}
check(params.get("seed") == 5, "qwen-image：--param seed 进了 parameters")

# ---------------------------------------------------------------- volcengine
section("火山方舟")
cap = run(
    adapters.gen_volcengine,
    spec("volcengine", "image", "doubao-seedream"),
    GenRequest(kind="image", prompt="p", extra={"seed": 11}),
)
check((cap.get("json") or {}).get("seed") == 11, "即梦图像：--param seed 进了请求体")

cap = run(
    adapters.gen_volcengine,
    spec("volcengine", "video", "doubao-seedance"),
    GenRequest(kind="video", prompt="樱花", extra={"seed": 12, "watermark": False}),
)
text = ((cap.get("json") or {}).get("content") or [{}])[0].get("text", "")
check("--seed 12" in text, "Seedance 视频：--param seed 拼成提示词旗标")
check("--watermark false" in text, "布尔值按 true/false 拼（不是 Python 的 False）")

# ---------------------------------------------------------------- kling
section("可灵")
cap = run(
    adapters.gen_kling,
    spec("kling", "image", "kling-v2"),
    GenRequest(kind="image", prompt="p", extra={"seed": 21}),
)
check((cap.get("json") or {}).get("seed") == 21, "可灵图像：--param seed 进了请求体")

cap = run(
    adapters._gen_kling_audio,
    spec("kling", "audio", "kling-tts", params={"voice_id": "v1"}),
    GenRequest(kind="audio", prompt="你好", extra={"seed": 22}),
    {"Authorization": "Bearer x"},
)
check((cap.get("json") or {}).get("seed") == 22, "可灵语音：白名单外的键也进了请求体")

# ---------------------------------------------------------------- generic_http
section("自定义接口")
gcap = run(
    adapters.gen_generic_http,
    spec(
        "generic_http",
        "image",
        "custom",
        endpoint="https://gw.example.com/v1/images",
        options={"submit": {"url": "https://gw.example.com/v1/images"}, "sync": True,
                 "result_path": "data.0.url"},
    ),
    GenRequest(kind="image", prompt="p", extra={"seed": 31, "custom_flag": "x"}),
)
body = gcap.get("json") or {}
check(body.get("seed") == 31 and body.get("custom_flag") == "x",
      "模板里没有的键也会写进请求体")

# ---------------------------------------------------------------- 收口
section("不该被重复发送的键")
cap = run(
    adapters.gen_openai,
    spec("openai", "image", "gpt-image-1"),
    GenRequest(kind="image", prompt="p", extra={"prompt": "覆盖?"}),
)
check((cap.get("json") or {}).get("prompt") == "p", "--param prompt 不会顶掉 --prompt")

print()
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
