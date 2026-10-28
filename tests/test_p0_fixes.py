#!/usr/bin/env python3
"""P0 修复的回归测试（零依赖、不联网、不花钱）。

覆盖这 9 个已复现缺陷的修复：
  1. 本地图片路径没转 data URI，多数图生图/图生视频通路把路径当 URL 发出去
  2. 页面编辑手写模型会丢掉模型级 endpoint / options
  3. 内联 api_key 的优先级高于环境变量（与 README 相反）
  4. 隐藏/停用手写模型会把内联 api_key 复制进 models.web.yaml
  5. 可灵 options.access_key/secret_key 被前置判键挡掉
  6. 厂商业务错误码（可灵 1102 等）丢响应体，被当成模型故障熔断
  7. --no-download 撞上内联 base64 响应：已计费却报错，并被计入熔断
  8. 提交类 POST 超时被自动重发（可能重复计费）
  9. 下载失败被当成模型故障、换模型重新生成，且 download 不重试

运行：python tests/test_p0_fixes.py
"""

import base64
import io
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 这几个环境变量在 mrouter.config 导入时读，必须先设
_WORK = Path(tempfile.mkdtemp(prefix="mr-p0-"))
os.environ["MEDIA_ROUTER_CONFIG_DIR"] = str(_WORK / "bootstrap-config")
os.environ["MEDIA_ROUTER_STATE_DIR"] = str(_WORK / "state")
os.environ["MEDIA_ROUTER_OUTPUT_DIR"] = str(_WORK / "outputs")
os.environ.pop("MEDIA_ROUTER_CONFIG", None)
os.environ.pop("MEDIA_ROUTER_SECRETS", None)

from mrouter import adapters, cli, config, health, selector, store, transport  # noqa: E402
from mrouter.adapters import Ctx, DownloadError, GenRequest, _finalize, _gen_kling_audio  # noqa: E402
from mrouter.config import ConfigError, ModelSpec  # noqa: E402
from mrouter.transport import HttpError  # noqa: E402

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
    print(f"--- {title} " + "-" * max(0, 66 - len(title)))


def use_config(files: dict[str, str], name: str = "cfg"):
    """新建一个临时配置目录并指过去，返回目录。"""
    root = Path(tempfile.mkdtemp(prefix=f"mr-p0-{name}-"))
    (root / "config").mkdir()
    for filename, text in files.items():
        (root / "config" / filename).write_text(text, encoding="utf-8")
    config.CONFIG_DIR = root / "config"
    config.STATE_DIR = root / "state"
    config.DEFAULT_OUTPUT_DIR = root / "outputs"
    return root


CTX = Ctx(api_key="k", key_source="t", timeout=5, poll_interval=1, max_poll=5)


class Resp:
    status = 200

    def __init__(self, payload=None, body: bytes = b""):
        self._payload = payload
        self.body = body

    def json(self):
        return self._payload


print("P0 修复回归测试")


# ---------------------------------------------------------------- P0-8
section("P0-8 提交类 POST 超时不自动重发")

hits: list[float] = []


class _Slow(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        hits.append(time.time())
        if len(hits) == 1:
            time.sleep(2.5)  # 第一次故意慢，触发客户端读超时
        data = b'{"ok": true}'
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def log_message(self, *args):  # noqa: D102
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

try:
    transport.request("POST", f"http://127.0.0.1:{port}/submit", json_body={"a": 1}, timeout=1.0)
    check(False, "超时应当抛错")
except HttpError as exc:
    check("网络错误" in str(exc), "超时结果如实报错，不伪装成别的错误")
    check("不会自动重发" in str(exc), "错误信息说明「提交类请求不会自动重发」")
time.sleep(0.3)
check(len(hits) == 1, f"服务端只收到 1 次 POST（实际 {len(hits)} 次）")

# GET 仍然是幂等的，应该重试
get_hits = [0]


class _Flaky(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        get_hits[0] += 1
        if get_hits[0] == 1:
            self.close_connection = True
            return
        data = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # noqa: D102
        pass


gsrv = ThreadingHTTPServer(("127.0.0.1", 0), _Flaky)
gport = gsrv.server_address[1]
threading.Thread(target=gsrv.serve_forever, daemon=True).start()
try:
    resp = transport.request("GET", f"http://127.0.0.1:{gport}/x", timeout=5.0)
    check(resp.status == 200 and get_hits[0] == 2, f"GET 遇到连接中断会重试（{get_hits[0]} 次）")
finally:
    gsrv.shutdown()
srv.shutdown()


# ---------------------------------------------------------------- P0-9
section("P0-9 下载重试 + 下载失败不熔断、不换模型")

dl_hits = [0]


class _Img(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        dl_hits[0] += 1
        if dl_hits[0] < 3:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # noqa: D102
        pass


isrv = ThreadingHTTPServer(("127.0.0.1", 0), _Img)
iport = isrv.server_address[1]
threading.Thread(target=isrv.serve_forever, daemon=True).start()
try:
    out = Path(tempfile.mkdtemp(prefix="mr-p0-dl-")) / "a.png"
    dest = transport.download(f"http://127.0.0.1:{iport}/img.png", out, retry_backoff=1.0)
    check(dest.exists() and dest.stat().st_size > 0, "下载在 503 之后自动重试并成功")
    check(dl_hits[0] == 3, f"共请求 3 次（实际 {dl_hits[0]}）")
finally:
    isrv.shutdown()


def _finalize_with_broken_download():
    spec = ModelSpec(id="m", provider="dashscope", kind="image")
    req = GenRequest(kind="image", prompt="p", output_dir=Path(tempfile.mkdtemp(prefix="mr-p0-fin-")))
    real = adapters.download
    adapters.download = lambda *a, **k: (_ for _ in ()).throw(HttpError("HTTP 403", 403))
    try:
        _finalize(spec, req, ["https://x/a.png", "https://x/b.png"], req.output_dir)
    finally:
        adapters.download = real


try:
    _finalize_with_broken_download()
    check(False, "下载失败应当抛错")
except DownloadError as exc:
    check(exc.urls == ["https://x/a.png", "https://x/b.png"], "DownloadError 带回原始 urls（不用重新生成）")


# CLI 层：下载失败 → 输出 JSON、不记健康度失败、不换模型
root = use_config(
    {
        "models.yaml": (
            "image:\n  models:\n"
            "    - id: m1\n      provider: openai\n      model: gpt-image-1\n"
            "      priority: 1\n      weight: 1\n      api_key_env: OPENAI_API_KEY\n"
        )
    },
    "dl-cli",
)
os.environ["OPENAI_API_KEY"] = "sk-test"
cfg = config.load_config()
health_path = cfg.health_path
if health_path.exists():
    health_path.unlink()

real_run = cli.run_adapter
cli.run_adapter = lambda *a, **k: (_ for _ in ()).throw(
    DownloadError("产物已生成但下载失败（1/1）：HTTP 403", urls=["https://x/a.png"])
)
args = cli.build_parser().parse_args(
    ["generate", "--kind", "image", "--prompt", "p", "--output-dir", str(root / "out")]
)
buf = io.StringIO()
try:
    with redirect_stdout(buf):
        rc = cli.cmd_generate(args)
finally:
    cli.run_adapter = real_run
payload = json.loads(buf.getvalue())
check(rc == 1 and payload["status"] == "error", "下载失败 → 退出码 1 + JSON")
check(payload.get("urls") == ["https://x/a.png"], "错误 JSON 里带 urls，产物地址没丢")
check(payload.get("hint", "").find("不要") >= 0, "提示里明确说不要再跑一次")
snapshot = health.HealthStore(health_path)
check(
    snapshot.state("m1").get("consecutive_failures", 0) == 0,
    "下载失败不计入熔断（consecutive_failures 仍为 0）",
)


# ---------------------------------------------------------------- P0-7
section("P0-7 --no-download + 内联 base64 → ConfigError（不计熔断）")

png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
adapters.request = lambda *a, **k: Resp({"data": [{"b64_json": png_b64}]})
spec = ModelSpec(id="gpt", provider="openai", kind="image", model="gpt-image-1")
try:
    adapters.gen_openai(spec, GenRequest(kind="image", prompt="p", output_dir=None), None, CTX)
    check(False, "b64 + --no-download 应当报错")
except ConfigError as exc:
    check("base64" in str(exc) and "--no-download" in str(exc), "b64 + --no-download 抛 ConfigError 并说明原因")

try:
    adapters.gen_volcengine(
        ModelSpec(id="ark", provider="volcengine", kind="image", model="seedream"),
        GenRequest(kind="image", prompt="p", output_dir=None),
        None,
        CTX,
    )
    check(False, "火山 b64 + --no-download 应当报错")
except ConfigError:
    check(True, "火山通路同样归 ConfigError")


# ---------------------------------------------------------------- P0-1
section("P0-1 本地图片路径转 data URI / 裸 base64")

local_img = Path(tempfile.mkdtemp(prefix="mr-p0-img-")) / "in.png"
local_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 40)
seen: dict[str, dict] = {}


def capture(method, url, **kw):
    seen["body"] = kw.get("json_body") or {}
    # 同步返回，避免进入轮询
    return Resp({"data": [{"url": "https://x/a.png"}], "id": "task-1", "status": "succeeded"})


adapters.request = capture
adapters.gen_volcengine(
    ModelSpec(id="ark", provider="volcengine", kind="image", model="seedream"),
    GenRequest(kind="image", prompt="p", images=[str(local_img)], output_dir=None),
    None,
    CTX,
)
value = str(seen["body"].get("image") or "")
check(value.startswith("data:image/png;base64,"), "火山图片：本地路径变成 data URI")

class _Captured(Exception):
    """截到请求体之后立刻中断，避免依赖各家的响应形状。"""


def capture_kling(method, url, **kw):
    seen["body"] = kw.get("json_body") or {}
    raise _Captured()


adapters.request = capture_kling
try:
    adapters.gen_kling(
        ModelSpec(id="kl", provider="kling", kind="image", model="kling-v2"),
        GenRequest(kind="image", prompt="p", images=[str(local_img)], output_dir=None),
        None,
        Ctx(api_key="ak:sk", key_source="t", timeout=5, poll_interval=1, max_poll=5),
    )
except _Captured:
    pass
kling_value = str(seen["body"].get("image") or "")
check(kling_value and not kling_value.startswith("data:"), "可灵：给裸 Base64（它不收 data: 前缀）")
check(base64.b64decode(kling_value)[:4] == b"\x89PNG", "可灵收到的 base64 能解回原图")

try:
    adapters._image_value(str(local_img.parent / "nope.png"))
    check(False, "缺文件应当报错")
except ConfigError as exc:
    check("不存在" in str(exc), "图片不存在时提前给出可读的 ConfigError")
check(adapters._image_value("https://x/a.png") == "https://x/a.png", "http URL 原样透传")


# ---------------------------------------------------------------- P0-3 / P0-5
section("P0-3/P0-5 密钥优先级与可灵 options 凭据")

use_config(
    {
        "models.yaml": (
            "image:\n  models:\n"
            "    - id: m1\n      provider: volcengine\n      model: x\n"
            "      api_key: inline-old\n      api_key_env: ARK_API_KEY\n"
        )
    },
    "key-order",
)
os.environ["ARK_API_KEY"] = "env-new"
spec = config.load_config().pool("image").models[0]
key, source = config.load_config().resolve_api_key(spec)
check(key == "env-new" and "环境变量" in source, f"环境变量优先于内联 api_key（拿到 {key!r}）")

os.environ.pop("ARK_API_KEY", None)
spec = config.load_config().pool("image").models[0]
key, source = config.load_config().resolve_api_key(spec)
check(key == "inline-old" and "内联" in source, "没有环境变量时才回落到内联字段")

use_config(
    {
        "models.yaml": (
            "image:\n  models:\n"
            "    - id: kling-v2\n      provider: kling\n      model: kling-v2\n"
            "      options:\n        access_key: ak-demo\n        secret_key: sk-demo\n"
        )
    },
    "kling-opts",
)
cfg = config.load_config()
spec = cfg.pool("image").models[0]
key, source = cfg.resolve_api_key(spec)
check(key == "ak-demo:sk-demo" and "access_key" in source, "可灵 options 里的 AK/SK 能被识别")
check(cli._requires_key(spec) and bool(key), "可灵模型不再被当成「缺 Key」跳过")

use_config(
    {
        "models.yaml": (
            "image:\n  models:\n"
            "    - id: kling-env\n      provider: kling\n      model: kling-v2\n"
            "      options:\n        access_key_env: KL_AK\n        secret_key_env: KL_SK\n"
        )
    },
    "kling-env",
)
os.environ["KL_AK"], os.environ["KL_SK"] = "ak-env", "sk-env"
key, _source = config.load_config().resolve_api_key(config.load_config().pool("image").models[0])
check(key == "ak-env:sk-env", "可灵 *_env 形式也能拿到凭据")


# ---------------------------------------------------------------- P0-6
section("P0-6 厂商业务错误码带 body，1102 归 config 类")

adapters.request = lambda *a, **k: Resp({"code": 1102, "message": "Account balance not enough"})
try:
    _gen_kling_audio(
        ModelSpec(id="k", provider="kling", kind="audio", model="kling-tts",
                  params={"voice_id": "v1"}),
        GenRequest(kind="audio", prompt="hi"),
        None,
        CTX,
        {"Authorization": "Bearer x"},
    )
    check(False, "业务错误码应当抛错")
except HttpError as exc:
    check("1102" in str(exc), "错误信息里有厂商 code")
    check("1102" in (exc.body or ""), "HttpError.body 里保留了原始响应体")
    code = cli._vendor_error_code(exc)
    check(code == "1102", f"CLI 能取出 code（实际 {code!r}）")
    check(code in cli.CONFIG_ERROR_CODES, "1102 在配置类错误码表里 → 不会打入冷却")


# ---------------------------------------------------------------- P0-2 / P0-4
section("P0-2/P0-4 叠加层字段级合并 + 启停不复制密钥")

root = use_config(
    {
        "models.yaml": (
            "vendors:\n"
            "  - id: v1\n    provider: openai\n    api_key_env: OPENAI_API_KEY\n"
            "image:\n  models:\n"
            "    - id: m1\n      vendor: v1\n      model: gpt-image-1\n"
            "      priority: 1\n      weight: 1\n"
            "      endpoint: https://gw.example.com/v1/images/generations\n"
            "      options:\n        custom_flag: 7\n"
            "      params:\n        seed: 42\n"
            "    - id: m2\n      provider: volcengine\n      model: seedream\n"
            "      api_key: sk-inline-secret\n      priority: 2\n      weight: 1\n"
        )
    },
    "overlay",
)
store.save_with(
    lambda ov: ov.upsert_model(
        {
            "id": "m1",
            "vendor": "v1",
            "model": "gpt-image-1",
            "kind": "image",
            "priority": 2,
            "weight": 5,
            "supports": ["text2img"],
            "input_modalities": ["text"],
            "output_modalities": ["image"],
        }
    )
)
cfg = config.load_config()
spec = next(m for m in cfg.pool("image").models if m.id == "m1")
check(spec.endpoint == "https://gw.example.com/v1/images/generations",
      "页面编辑后手写的 endpoint 还在（不再回落到别家默认地址）")
check(spec.options.get("custom_flag") == 7, "页面编辑后手写的 options 还在")
check((spec.params or {}).get("seed") == 42, "页面编辑后手写的 params 还在")
check(spec.priority == 2 and spec.weight == 5.0, "表单里改的优先级/权重确实生效了")

store.save_with(lambda ov: ov.set_enabled("m2", False))
overlay_text = (root / "config" / "models.web.yaml").read_text(encoding="utf-8")
check("sk-inline-secret" not in overlay_text, "隐藏手写模型不会把内联 api_key 复制进 models.web.yaml")
check("m2" in overlay_text, "叠加层里仍有 m2 的启停记录")
cfg = config.load_config()
spec = next(m for m in cfg.pool("image").models if m.id == "m2")
check(spec.enabled is False, "隐藏生效（路由不再选它）")
check(spec.provider == "volcengine", "隐藏后 provider 仍可从手写层继承")
check((spec.raw.get("api_key") or "") == "sk-inline-secret", "密钥仍只存在于手写层")

store.save_with(lambda ov: ov.set_enabled("m2", True))
cfg = config.load_config()
spec = next(m for m in cfg.pool("image").models if m.id == "m2")
check(spec.enabled is True, "再点一次能恢复显示")

# 隐藏 → 再编辑：params/endpoint 不能因为叠加层条目只有 id+enabled 而丢
store.save_with(lambda ov: ov.set_enabled("m1", False))
store.save_with(
    lambda ov: ov.upsert_model(
        {
            "id": "m1",
            "vendor": "v1",
            "model": "gpt-image-1",
            "kind": "image",
            "priority": 3,
            "weight": 1,
            "supports": ["text2img"],
        }
    )
)
cfg = config.load_config()
spec = next(m for m in cfg.pool("image").models if m.id == "m1")
check((spec.params or {}).get("seed") == 42, "先隐藏再编辑，params 依然保留")
check(spec.endpoint == "https://gw.example.com/v1/images/generations", "先隐藏再编辑，endpoint 依然保留")
check(spec.enabled is False, "编辑不会把停用的模型顺手开回来")


print()
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
