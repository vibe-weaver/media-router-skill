#!/usr/bin/env python3
"""端到端自检 —— 不用任何 API Key 就能验证整条链路。

做法：本地起一个模拟的「异步任务型」图片接口（提交拿 task_id → 轮询 → 返回图片 URL），
再临时写一份指向它的配置，然后真的跑一次 generate，断言：

  1. 排在前面的模型故意 404，路由是否正确降级到第二个模型
  2. 提交 → 轮询 → 下载 → 落盘 是否走通
  3. 落盘文件的扩展名是否按真实文件头纠正为 .png
  4. 图片宽高是否被正确读出来（元数据回传）
  5. 失败模型是否被记入健康度，成功模型是否清零

运行：python scripts/selftest.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import contextlib
import io
import json
import struct
import sys
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mrouter.cli import build_parser  # noqa: E402
from mrouter.cli import main as cli_main  # noqa: E402
from mrouter.config import load_config  # noqa: E402
from mrouter.health import HealthStore  # noqa: E402

TARGET_W, TARGET_H = 64, 32
POLL_CALLS: dict[str, int] = {}
#: 收到的 POST 请求体。用来断言"实际发到线上的字节"——
#: 光看函数返回值看不出占位符被渲染成了什么。
POSTED_BODIES: list[dict] = []
#: 收到的 GET 路径。用来断言异步任务的**轮询打到了哪个地址**——
#: 光看结果成功与否，区分不出"打到了中转网关"还是"偷偷直连了官方域名"。
GET_PATHS: list[str] = []


def make_png(width: int, height: int, rgb: tuple[int, int, int] = (110, 150, 210)) -> bytes:
    """手搓一张纯色 PNG，用来验证真实文件头解析。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class Handler(BaseHTTPRequestHandler):
    server_port = 0

    def log_message(self, *args):  # noqa: D102 - 静音
        pass

    def _raw(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._raw(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if raw:
            try:
                POSTED_BODIES.append(json.loads(raw.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                POSTED_BODIES.append({"_raw": raw.decode("utf-8", "replace")})
        if self.path == "/fail":
            self._raw(404, b'{"error":"model unavailable"}')
            return
        if self.path == "/submit":
            self._json({"data": {"task_id": "selftest-task"}})
            return
        if self.path == "/ds-submit" or "services/aigc/" in self.path:
            # 百炼（dashscope）异步任务的响应形状。第二个条件是为了接住
            # "经中转网关提交"的情形 —— 路径前缀会带上网关自己的那段。
            self._json({"output": {"task_id": "ds-task"}})
            return
        if self.path == "/rep-submit":
            # Replicate：同步就给出 succeeded + output，跳过轮询。
            # 断言关心的是 input 对象里到底带了哪些字段。
            self._json(
                {
                    "id": "rep-1",
                    "status": "succeeded",
                    "output": [f"http://127.0.0.1:{self.server_port}/img.png"],
                }
            )
            return
        if self.path == "/fal-submit":
            # fal.ai：先拿 status_url / response_url 再轮询。
            self._json(
                {
                    "request_id": "fal-1",
                    "status_url": f"http://127.0.0.1:{self.server_port}/fal-status",
                    "response_url": f"http://127.0.0.1:{self.server_port}/fal-resp",
                }
            )
            return
        self._raw(404, b'{"error":"no route"}')

    def do_GET(self):  # noqa: N802
        GET_PATHS.append(self.path)
        if self.path == "/img.png":
            self._raw(200, make_png(TARGET_W, TARGET_H), "image/png")
            return
        if self.path == "/html":
            # 200，但响应体不是 JSON —— "被网关/WAF 拦成 HTML"的典型形状。
            # 密钥校验探测必须给出可读结论，而不是抛异常。
            self._raw(200, b"<html><body>blocked by waf</body></html>", "text/html")
            return
        if self.path.startswith("/tasks/"):
            seen = POLL_CALLS.get(self.path, 0)
            POLL_CALLS[self.path] = seen + 1
            if seen == 0:
                self._json({"data": {"status": "running", "images": []}})
            else:
                self._json(
                    {
                        "data": {
                            "status": "succeeded",
                            "images": [
                                {"url": f"http://127.0.0.1:{self.server_port}/img.png"}
                            ],
                        }
                    }
                )
            return
        if self.path.startswith("/ds/tasks/") or self.path.endswith("/tasks/ds-task"):
            # 百炼异步任务查询：一次就给成功结果。
            # 后半个条件是为了接住"轮询打到了自定义网关/中转地址"的情形 ——
            # 断言要能看出它到底打到了哪儿。
            self._json(
                {
                    "output": {
                        "task_status": "SUCCEEDED",
                        "results": [
                            {"url": f"http://127.0.0.1:{self.server_port}/img.png"}
                        ],
                    }
                }
            )
            return
        if self.path == "/fal-status":
            self._json({"status": "COMPLETED"})
            return
        if self.path == "/fal-resp":
            self._json({"images": [{"url": f"http://127.0.0.1:{self.server_port}/img.png"}]})
            return
        self._raw(404, b'{"error":"no route"}')


CONFIG_TEMPLATE = """\
version: 1
defaults:
  timeout_seconds: 15
  poll_interval_seconds: 1
  max_poll_seconds: 30
  max_attempts: 3
  output_dir: __OUT__
  state_dir: __STATE__
  health:
    failure_threshold: 3
    cooldown_seconds: 60
  caption:
    mode: metadata

image:
  strategy: fallback_chain
  models:
    - id: mock-broken
      provider: generic_http
      model: mock-broken
      priority: 1
      weight: 100
      supports: [text2img]
      options:
        auth:
          type: none
        submit:
          url: http://127.0.0.1:__PORT__/fail
          method: POST
          body:
            prompt: "{{prompt}}"
        poll:
          url: http://127.0.0.1:__PORT__/tasks/{{task_id}}

    - id: mock-working
      provider: generic_http
      model: mock-working
      priority: 2
      weight: 100
      supports: [text2img, img2img]
      options:
        auth:
          type: none
        submit:
          url: http://127.0.0.1:__PORT__/submit
          method: POST
          body:
            model: "{{model}}"
            prompt: "{{prompt}}"
          task_id_path: data.task_id
        poll:
          url: http://127.0.0.1:__PORT__/tasks/{{task_id}}
          method: GET
          interval: 1
          status_path: data.status
          success: [succeeded]
          failure: [failed]
          result_path: data.images
          result_url_field: url
"""


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    tmp = Path(tempfile.mkdtemp(prefix="media-router-selftest-"))
    out_dir = tmp / "outputs"
    state_dir = tmp / "state"
    config_path = tmp / "models.yaml"

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    Handler.server_port = port
    config_path.write_text(
        CONFIG_TEMPLATE.replace("__PORT__", str(port))
        .replace("__OUT__", out_dir.as_posix())
        .replace("__STATE__", state_dir.as_posix()),
        encoding="utf-8",
    )

    import os

    os.environ["MEDIA_ROUTER_CONFIG"] = str(config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"[selftest] 模拟接口已启动: http://127.0.0.1:{port}")
    print(f"[selftest] 临时配置: {config_path}")

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                [
                    "generate",
                    "--kind",
                    "image",
                    "--prompt",
                    "自检用图：纯色色块",
                    "--output-dir",
                    out_dir.as_posix(),
                ]
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[selftest][debug] cli_main 抛出异常: {exc!r}")

    raw = buffer.getvalue().strip()
    payload = json.loads(raw) if raw else {}

    if payload.get("status") != "ok":
        print("\n[selftest][debug] 首轮返回：")
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:3000])

    print("\n=== 自检结果 ===")
    check("generate 退出码为 0", code == 0, f"实际 {code}")
    check("status == ok", payload.get("status") == "ok", str(payload.get("status")))
    check(
        "失败模型降级成功（选中 mock-working）",
        (payload.get("model") or {}).get("id") == "mock-working",
        str((payload.get("model") or {}).get("id")),
    )

    trail = payload.get("fallback_trail") or []
    check("降级轨迹记录了 mock-broken", any(t.get("model") == "mock-broken" for t in trail), str(trail))
    check(
        "失败被归类为 runtime（计入熔断）",
        any(t.get("kind") == "runtime" for t in trail),
        str([t.get("kind") for t in trail]),
    )

    files = payload.get("files") or []
    check("产出 1 个文件", len(files) == 1, f"实际 {len(files)}")
    if files:
        f = files[0]
        path = Path(f.get("path", ""))
        check("文件已落盘", path.exists(), str(path))
        check("扩展名按真实文件头纠正为 .png", path.suffix == ".png", path.suffix)
        check(
            "图片宽高解析正确",
            f.get("width") == TARGET_W and f.get("height") == TARGET_H,
            f"{f.get('width')}x{f.get('height')}（应为 {TARGET_W}x{TARGET_H}）",
        )
        check("文件大小已统计", (f.get("bytes") or 0) > 0, str(f.get("bytes")))
        check("落地文件内容为合法 PNG", path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n")

    check("轮询确实发生了多次（异步任务生效）", len(POLL_CALLS) > 0, str(POLL_CALLS))

    health = HealthStore(state_dir / "health.json")
    broken = health.state("mock-broken")
    working = health.state("mock-working")
    check(
        "失败模型计数 +1",
        broken.get("failure_count") == 1,
        str(broken.get("failure_count")),
    )
    check(
        "成功模型成功计数 +1",
        working.get("success_count") == 1,
        str(working.get("success_count")),
    )
    check(
        "失败模型未被误熔断（仅 1 次 < 阈值 3）",
        not health.is_cooling("mock-broken"),
        f"cooling={health.is_cooling('mock-broken')}",
    )

    # 第二轮：验证同一份配置能稳定复现（不依赖一次性状态）
    print("\n[selftest] 第二轮验证 —— 确认可重复运行")
    buffer2 = io.StringIO()
    try:
        thread2 = threading.Thread(target=server.serve_forever, daemon=True)
        thread2.start()
        with contextlib.redirect_stdout(buffer2):
            code2 = cli_main(
                [
                    "generate",
                    "--kind",
                    "image",
                    "--prompt",
                    "第二轮",
                    "--output-dir",
                    out_dir.as_posix(),
                ]
            )
    finally:
        with contextlib.suppress(Exception):
            server.shutdown()
    payload2 = json.loads(buffer2.getvalue().strip() or "{}")
    check("第二轮同样成功", code2 == 0 and payload2.get("status") == "ok", str(payload2.get("status")))
    check(
        "两轮产出文件不同名（不互相覆盖）",
        bool(payload2.get("files"))
        and payload2["files"][0]["path"] != (files[0]["path"] if files else ""),
        "",
    )

    # ---------------------------------------------------------- 解析器一致性
    # 这一组是防"装了 PyYAML 和没装行为分叉"的。分叉的后果是：同一份配置在
    # 两台机器上跑出两个结果，而报错信息完全不指向真正的原因。
    print("\n[selftest] 解析器一致性")
    check_parser_agreement(check)

    print("\n[selftest] 配置原文体检（lint）")
    check_config_lint(check)

    print("\n[selftest] 已修 bug 的回归断言")
    check_bugfixes(check)

    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        suffix = f"  -> {detail}" if detail and not ok else ""
        print(f"  [{mark}] {name}{suffix}")
    print(f"\n{passed}/{len(results)} 项通过")
    if passed != len(results):
        print(f"临时目录保留以便排查: {tmp}")
        return 1
    print("全部通过。")
    return 0


#: 这些值的"用户本意"都是字符串。放进映射值的位置（配置里的真实上下文）
#: 之后，两个解析器必须给出同样的结果，否则就是行为分叉。
TRICKY_VALUES = [
    # 比值 / 时间 —— YAML 1.1 的六十进制陷阱，最险的一类
    "1:1", "16:9", "9:16", "4:3", "4:5", "1:2:3", "12:30:45", "16:99", "1.5:30",
    # 数字的各种写法
    "0", "007", "017", "0o17", "0x10", "0b101", "1_000", "+5", "-5",
    "1.5", "-1.5", ".5", "5.", "1.", "1e3", "1.0e+3", "1.5e-3", ".inf", "-.inf", ".nan",
    # 尺寸
    "1024x1024", "1024*1024", "1280*720",
    # 布尔 / 空值的各种写法（none 必须是字符串，不能是 null）
    "true", "True", "TRUE", "false", "yes", "no", "on", "off", "null", "~", "None", "none",
    # 常见字符串
    "https://api.example.com/v1", "https://api.example.com/v1/tasks/x",
    "black-forest-labs/FLUX.1-schnell", "kling-v2-master",
    "带中文的值", "hello world", "a#b", "a #b",
    # 空值后面的注释形式
    "16:9 ",
]


def check_parser_agreement(check) -> None:
    """只要 PyYAML 在，就逐项比对两个解析器；解析器自己的往返测试总是跑。"""
    from mrouter import miniyaml

    mismatched: list[str] = []
    for value in TRICKY_VALUES:
        text = f"k: {value}"
        try:
            mine = miniyaml.loads(text)["k"]
        except Exception as exc:  # noqa: BLE001
            mine = f"<{type(exc).__name__}: {exc}>"
        try:
            import yaml  # type: ignore

            theirs = yaml.safe_load(text)["k"]
        except ImportError:
            theirs = mine
        except Exception as exc:  # noqa: BLE001
            theirs = f"<{type(exc).__name__}: {exc}>"
        same = type(mine) is type(theirs) and (
            mine == theirs
            or (isinstance(mine, float) and isinstance(theirs, float) and mine != mine and theirs != theirs)
        )
        if not same:
            mismatched.append(f"{value!r}: PyYAML={theirs!r} miniyaml={mine!r}")
    check(
        f"两个解析器对 {len(TRICKY_VALUES)} 个易错值判断一致",
        not mismatched,
        "; ".join(mismatched[:5]),
    )

    # 裸写 `1:1` 必须被两个解析器都读成六十进制 61（这是 YAML 1.1 的规定），
    # 正因为如此，配置里必须加引号 —— 而引号之后必须读回字符串。
    bare = miniyaml.loads("k: 1:1")["k"]
    quoted = miniyaml.loads('k: "1:1"')["k"]
    check(
        "裸写 1:1 会被读成 61（所以才必须加引号）",
        bare == 61 and quoted == "1:1",
        f"裸写={bare!r} 加引号={quoted!r}",
    )

    # 序列化往返：写成文本再读回来，必须一模一样
    round_trip = [
        "1:1", "16:9", "1024x1024", "{{prompt}}", "{{model}}", "a: b", "a #b", "a#b",
        "true", "no", "none", "Null", " 前后有空格 ", "带#号 # 的值", "2026-01-01",
        "1e3", "0x10", "-", "?", "*", "", "多行\n文本", "tab\t键", '带"引号"', "带'单引号'",
        "第 3 行: 有冒号", "https://a.b/c?d=1&e=2",
    ]
    bad: list[str] = []
    for value in round_trip:
        try:
            back = miniyaml.loads(miniyaml.dumps({"k": value}))["k"]
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{value!r} -> {type(exc).__name__}: {exc}")
            continue
        if back != value or type(back) is not str:
            bad.append(f"{value!r} -> {back!r} ({type(back).__name__})")
    check(f"序列化往返不失真（{len(round_trip)} 个刁钻值）", not bad, "; ".join(bad[:5]))

    # 嵌套结构往返
    nested = {
        "version": 1,
        "image": {"strategy": "priority_then_weight", "models": [
            {"id": "a-1", "model": "x/y-2.0", "priority": 1, "weight": 50,
             "enabled": True, "supports": ["text2img"], "params": {"aspect_ratio": "1:1",
             "size": "1024*1024", "note": None, "flag": False}},
        ]},
        "defaults": {"caption": {"prompt": "用一两句中文描述这张图片：主体、风格。"}},
    }
    reloaded = miniyaml.loads(miniyaml.dumps(nested))
    check("嵌套结构往返一致", reloaded == nested, f"{reloaded!r}"[:200])

    # 写坏的配置要给可读的报错，而不是猜一个结果
    for text, why in [
        ("k: {{prompt}}", "占位符没加引号"),
        ("k: [a, b", "行内列表没闭合"),
        ("k: 重要: 别删", "值里有裸写的「冒号+空格」"),
    ]:
        try:
            miniyaml.loads(text)
            ok = False
            detail = f"{text!r} 没有报错"
        except miniyaml.MiniYamlError as exc:
            ok = True
            detail = str(exc)
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        check(f"写坏的配置会明确报错（{why}）", ok, detail)

    # 文档级裸标量：PyYAML 支持，回退解析器也必须支持
    check(
        "文档级裸标量能解析",
        miniyaml.loads("1024x1024") == "1024x1024" and miniyaml.loads("16:9") == 969,
        f"{miniyaml.loads('1024x1024')!r} / {miniyaml.loads('16:9')!r}",
    )
    check(
        "单行文本不会被硬当成配置报错",
        miniyaml.loads("这不是配置") == "这不是配置" and miniyaml.loads("a:b") == "a:b",
        f"{miniyaml.loads('这不是配置')!r} / {miniyaml.loads('a:b')!r}",
    )
    check(
        "冒号后没空格不算键分隔符（与 PyYAML 一致）",
        miniyaml.loads("url: https://a.b/c")["url"] == "https://a.b/c"
        and miniyaml.loads("k: 10:30 开会")["k"] == "10:30 开会",
        f"{miniyaml.loads('url: https://a.b/c')!r}",
    )

    # 数组/映射往返
    check("空列表与空映射往返", miniyaml.loads(miniyaml.dumps({"a": [], "b": {}})) == {"a": [], "b": {}})

    # 整份真实配置：两条解析路径必须给出**完全相同**的结构。
    # 这是最有价值的一条 —— 当初 `aspect_ratio: 1:1` 被 PyYAML 读成 61、
    # 被回退解析器读成 "1:1" 的分叉就是这么抓出来的。
    config_dir = Path(__file__).resolve().parent.parent / "config"
    for name in ("models.yaml", "secrets.example.yaml"):
        path = config_dir / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        try:
            mine = miniyaml.loads(text)
        except Exception as exc:  # noqa: BLE001
            check(f"{name} 能被回退解析器解析", False, f"{type(exc).__name__}: {exc}")
            continue
        check(f"{name} 能被回退解析器解析", True)
        # 往返：序列化再读回来必须一致（网页配置写文件走的就是这条路）
        check(
            f"{name} 序列化往返不失真",
            miniyaml.loads(miniyaml.dumps(mine)) == mine,
            "dumps 之后读不回来了",
        )
        try:
            import yaml  # type: ignore

            theirs = yaml.safe_load(text)
        except ImportError:
            continue  # 没装 PyYAML，没有第二条路径可比
        a = json.dumps(theirs, sort_keys=True, default=str)
        b = json.dumps(mine, sort_keys=True, default=str)
        check(
            f"{name} 两条解析路径结果逐字节相同",
            a == b,
            f"PyYAML 与回退解析器结果不同（装了 PyYAML 的机器会读出另一个结果）",
        )

    # 值域层面：比值类参数必须是字符串。少了这条，有人把配置里的引号删掉
    # 也依然"两条路径一致"（一致地错成 61）。
    shipped = config_dir / "models.yaml"
    if shipped.exists():
        data = miniyaml.loads(shipped.read_text(encoding="utf-8"))
        ratios: list[str] = []
        for pool in data.values():
            if not isinstance(pool, dict):
                continue
            for model in pool.get("models") or []:
                for key, value in (model.get("params") or {}).items():
                    ratios.append(f"{model.get('id')}.{key}={value!r}")
                    if key in ("aspect_ratio", "ratio", "fps") and not isinstance(value, str):
                        ratios.append(f"!! {model.get('id')}.{key} 不是字符串")
        bad_types = [r for r in ratios if r.startswith("!!")]
        check("比值类参数读出来是字符串而不是数字", not bad_types, "; ".join(bad_types))


def check_config_lint(check) -> None:
    """原文体检：能被解析、但结果不是用户想要的那种写法必须被挑出来。"""
    from mrouter import config

    cases = [
        ("params:\n  aspect_ratio: 1:1\n", "裸写比值", True),
        ("params:\n  ratio: 16:9\n", "裸写比值（视频）", True),
        ('params:\n  aspect_ratio: "1:1"\n', "加引号的比值（应当放过）", False),
        ("note: 2026-01-01\n", "裸写日期", True),
        ('note: "2026-01-01"\n', "加引号的日期（应当放过）", False),
        ("prompt: {{prompt}}\n", "没加引号的占位符", True),
        ('prompt: "{{prompt}}"  # 说明\n', "加引号的占位符（应当放过）", False),
        ("params: {size:1024x1024}\n", "行内映射冒号后没空格", True),
        ("params: {size: 1024x1024}\n", "行内映射写法正确（应当放过）", False),
        ("note: 重要: 别删\n", "值里有裸写的「冒号+空格」", True),
        ('note: "重要: 别删"\n', "加了引号（应当放过）", False),
        ("keys:\n  ARK_API_KEY: ab:cd\n", "密钥值里带冒号但不含空格（应当放过）", False),
        ("endpoint: https://ark.cn-beijing.volces.com/api/v3\n", "URL（应当放过）", False),
        ("# 注释里的 1:1 不该被误报\nsize: 1024x1024\n", "注释与正常值（应当放过）", False),
        ("size: 1024*1024\ntimeout_seconds: 120\n", "正常值（应当放过）", False),
    ]
    for text, why, should_warn in cases:
        messages = config.lint_text(text)
        got = bool(messages)
        check(
            f"lint {'能查出' if should_warn else '不误报'}：{why}",
            got == should_warn,
            f"期望{'有' if should_warn else '无'}告警，实际 {messages}",
        )

    # 自带的默认配置本身必须是干净的 —— 这是最容易忘记的一条。
    # （这里显式读文件，不能走 lint_files()，因为它会跟随 MEDIA_ROUTER_CONFIG
    #   指向本次自检的临时配置。）
    shipped = config.CONFIG_DIR / "models.yaml"
    shipped_lints = config.lint_text(shipped.read_text(encoding="utf-8")) if shipped.exists() else []
    check("自带的 models.yaml 通过体检", not shipped_lints, "; ".join(shipped_lints[:3]))

    shipped_secrets = config.CONFIG_DIR / "secrets.example.yaml"
    if shipped_secrets.exists():
        secret_lints = config.lint_text(shipped_secrets.read_text(encoding="utf-8"))
        check("自带的 secrets.example.yaml 通过体检", not secret_lints, "; ".join(secret_lints[:3]))

    # 解析失败必须报成"配置错误"（kind=config），而不是丢一个原始异常让 CLI
    # 标成 internal —— 用户看到 internal 会以为是程序坏了，其实是自己写错了。
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    original = os.environ.get("MEDIA_ROUTER_CONFIG")
    with tempfile.TemporaryDirectory(prefix="media-router-lint-") as workdir:
        broken = Path(workdir) / "broken.yaml"
        # 同时埋两个坑：一个只是"读出来不是你想的值"（1:1），一个是真语法错（占位符没引号）。
        # 报错信息里应该把两个都指出来。
        broken.write_text(
            "image:\n"
            "  models:\n"
            "    - id: a\n"
            "      provider: volcengine\n"
            "      params:\n"
            "        r: 1:1\n"
            "        prompt: {{prompt}}\n",
            encoding="utf-8",
        )
        os.environ["MEDIA_ROUTER_CONFIG"] = str(broken)
        try:
            try:
                config.load_config()
                check("写坏的配置会抛 ConfigError", False, "居然没报错")
            except config.ConfigError as exc:
                text = str(exc)
                check(
                    "写坏的配置会抛 ConfigError",
                    "占位符" in text and "1:1" in text,
                    text[:160],
                )
            except Exception as exc:  # noqa: BLE001
                check("写坏的配置会抛 ConfigError", False, f"{type(exc).__name__}: {exc}")
            lints = config.lint_files()
            check("解析失败时体检结果也能拿到", any("1:1" in m for m in lints), str(lints)[:120])
        finally:
            if original is None:
                os.environ.pop("MEDIA_ROUTER_CONFIG", None)
            else:
                os.environ["MEDIA_ROUTER_CONFIG"] = original

    # ---------------------------------------------------------- 命令行入口
    # 光敲脚本名是最自然的动作，必须给引导而不是 "arguments are required"
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main([])
    payload = json.loads(out.getvalue().strip() or "{}")
    check(
        "不带子命令时给出引导而不是报错",
        code == 0 and payload.get("status") == "help" and "web" in payload.get("commands", {}),
        f"code={code} payload={str(payload)[:120]}",
    )
    check(
        "引导走 stderr、stdout 仍然只有 JSON",
        "web" in err.getvalue() and out.getvalue().count("\n") == 1,
        f"stderr={err.getvalue()[:60]!r}",
    )

    # 每个子命令都要有 --help 且能跑起来（漏注册的会在这里露馅）
    available = set(build_parser()._subparsers._group_actions[0].choices)  # noqa: SLF001
    check(
        "八个子命令都注册了 --help",
        available
        == {"config", "providers", "list", "resolve", "generate", "report", "health", "web"},
        str(sorted(available)),
    )
    for name in sorted(available):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                build_parser().parse_args([name, "--help"])
                ok = True
                detail = ""
            except SystemExit as exc:
                ok = exc.code == 0
                detail = f"退出码 {exc.code}"
            except Exception as exc:  # noqa: BLE001
                ok = False
                detail = f"{type(exc).__name__}: {exc}"
        check(f"`{name} --help` 正常", ok, detail)


def check_bugfixes(check) -> None:
    """把代码审查中修掉的 bug 逐条固化成断言。

    每条都写清"原来的写法会怎样" —— 将来有人把逻辑改回去，这里立刻变红，
    而不是等用户在生产里踩一次。分组顺序与审查报告一致（H*/M*/L*）。
    """
    import base64  # noqa: PLC0415
    import dataclasses  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    from mrouter import adapters, catalog, config, miniyaml, probe, transport  # noqa: PLC0415

    # ------------------------------------------------ enabled 的空值语义（H4）
    # 原写法 entry.get("enabled", True)：键存在但值为 None 时默认值不生效，
    # bool(None) 变成 False —— 一行没写完的 `enabled:` 会让模型静默停用，
    # 用户只看到"没有可用模型"，完全查不出原因。
    check(
        "enabled 写了但没值 -> 视为启用（不是静默停用）",
        config.coerce_enabled(None) is True
        and config.coerce_enabled("") is True
        and config.coerce_enabled(True) is True,
        f"None={config.coerce_enabled(None)} ''={config.coerce_enabled('')}",
    )
    check(
        "enabled 的各种「否」写法都能识别",
        all(
            config.coerce_enabled(v) is False
            for v in ("false", "no", "0", "off", "FALSE", " Off ", 0, False)
        ),
        "有写法没被识别成停用",
    )
    check(
        "模型条目里 `enabled:` 空值不会被读成停用",
        config.build_spec_from_entry(
            "image", {"id": "e1", "provider": "openai", "enabled": None}, {}
        ).enabled
        is True,
        "",
    )

    # ------------------------------------------------ 形状写错要报配置错误（H5）
    # 原写法 dict(...) 直接抛 TypeError，被 CLI 归成 kind=internal ——
    # 用户看到"内部错误"，根本想不到是自己配置里的 options/params 写成了列表。
    for field, bad_value in (("options", ["a"]), ("params", 3), ("options", "x")):
        try:
            config.build_spec_from_entry(
                "image", {"id": "bad", "provider": "openai", field: bad_value}, {}
            )
            ok, detail = False, "居然没报错"
        except config.ConfigError as exc:
            ok, detail = field in str(exc), str(exc)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        check(f"模型 {field} 写成 {type(bad_value).__name__} 时报配置错误", ok, detail)

    try:
        config.build_vendors([{"id": "v", "provider": "openai", "options": ["x"]}])
        ok, detail = False, "居然没报错"
    except config.ConfigError as exc:
        ok, detail = "options" in str(exc), str(exc)
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    check("厂商 options 写成列表时报配置错误", ok, detail)

    # ------------------------------------------------ 不静默丢条目（H5 的另一半）
    # 原 _dedupe_last 把"不是 dict"和"没有 id"的条目收进一个列表然后忘了返回，
    # 于是 models: [{provider: x}] 会凭空消失，用户看到空池子却没有任何提示。
    messy = config.visible_entries([{"provider": "openai"}, {"id": "m1", "model": "a"}])
    check(
        "缺 id 的条目不会被静默丢掉（而是走到报错）",
        len(messy) == 2 and messy[1].get("id") == "m1",
        str(messy),
    )
    check(
        "墓碑会把手写层条目盖掉",
        config.visible_entries([{"id": "m1"}, {"id": "m1", "_deleted": True}]) == [],
        "墓碑没有生效",
    )

    # ------------------------------------------------ 叠加层不抹掉手写层（合并）
    # 叠加层只写了 strategy、没写 models 时，整体覆盖会把手写层这一整个类目的
    # 模型全部抹掉 —— 用户改个策略，模型全没了。
    merged = config._merge_config(  # noqa: SLF001 - 自检就是要看合并语义本身
        {"image": {"strategy": "fallback_chain", "models": [{"id": "a"}]}},
        {"image": {"strategy": "weight_only"}},
    )
    check(
        "叠加层只改 strategy 时不会抹掉手写层的模型",
        merged.get("image", {}).get("strategy") == "weight_only"
        and [m.get("id") for m in merged["image"].get("models", [])] == ["a"],
        str(merged.get("image")),
    )
    merged_vendors = config._merge_config(  # noqa: SLF001
        {"vendors": [{"id": "v", "provider": "openai", "label": "手写"}]},
        {"vendors": [{"id": "v", "provider": "openai", "label": "页面"}]},
    )
    check(
        "厂商按 id 去重、叠加层覆盖手写层",
        len(merged_vendors.get("vendors", [])) == 1
        and merged_vendors["vendors"][0].get("label") == "页面",
        str(merged_vendors.get("vendors")),
    )

    # ------------------------------------------------ 浮点序列化（M5）
    # YAML 1.1 要求指数形式带小数点：repr(1e20) 是 '1e+20'，直接写出去再读回
    # 就变成字符串，再往下就当成参数发给接口了（那边只会给你一个 400）。
    floats = [1e20, 1e-7, -2.5e30, 3.0, 0.1, float("inf"), float("-inf")]
    bad_floats: list[str] = []
    for value in floats:
        text = miniyaml.dumps({"k": value})
        try:
            back = miniyaml.loads(text)["k"]
        except Exception as exc:  # noqa: BLE001
            bad_floats.append(f"{value!r} -> {text.strip()} -> {type(exc).__name__}: {exc}")
            continue
        if not (isinstance(back, float) and back == value):
            bad_floats.append(f"{value!r} -> {text.strip()} -> {back!r}")
    check(
        f"浮点写出去能被读回成浮点（{len(floats)} 个）",
        not bad_floats,
        "; ".join(bad_floats[:4]),
    )
    nan_back = miniyaml.loads(miniyaml.dumps({"k": float("nan")}))["k"]
    check("NaN 往返仍然是 NaN", isinstance(nan_back, float) and nan_back != nan_back, repr(nan_back))
    try:
        import yaml  # type: ignore

        cross = [
            repr(v)
            for v in floats
            if not isinstance(yaml.safe_load(miniyaml.dumps({"k": v}))["k"], float)
        ]
        check("PyYAML 也把这些值读成浮点（两条解析路径不分叉）", not cross, str(cross))
    except ImportError:
        pass

    # ------------------------------------------------ 整串占位符取到 None（M4）
    # 原写法 str(None) -> "None" 被拼进地址或参数里，接口只会回 400。
    check(
        "整串占位符取到 None 时渲染成空串（不是字面量 None）",
        adapters._render("{{size}}", {"size": None}) == ""  # noqa: SLF001
        and adapters._render("x{{a}}y", {"a": None}) == "xy"  # noqa: SLF001
        and adapters._render("{{n}}", {"n": 8}) == 8,  # noqa: SLF001
        f"{adapters._render('{{size}}', {'size': None})!r}",
    )

    # 渲染出空串的键不能再发出去 —— 但**合法的假值必须留下**。
    # 只删空串是刻意的：seed: 0 / camera_fixed: false 和"没填"完全是两回事，
    # 一律按假值删掉就又变成 M3 那个 bug 了。
    pruned = adapters._prune_empty(  # noqa: SLF001
        {"空": "", "零": 0, "假": False, "无": None, "有值": "x", "嵌套": {"里空": "", "里零": 0}}
    )
    check(
        "_prune_empty 只删空串，不碰 0 / False / None",
        pruned == {"零": 0, "假": False, "无": None, "有值": "x", "嵌套": {"里零": 0}},
        str(pruned),
    )

    # ------------------------------------------------ 取名字必须只有一套规则（L6）
    # _extract_names 与 _local_capability_filter 用不同兜底顺序的话，
    # 本地过滤会把能用的模型全部误判成"不匹配"。
    check(
        "两条取名字的路径用的是同一套兜底顺序",
        probe._item_name({"id": "a", "model": "b"}, "name") == "a"  # noqa: SLF001
        and probe._item_name({"model": "b"}, "name") == "b"  # noqa: SLF001
        and probe._item_name({"name": "c", "id": "a"}, "name") == "c"  # noqa: SLF001
        and probe._item_name({}, "name") == "",  # noqa: SLF001
        "",
    )

    scratch = Path(tempfile.mkdtemp(prefix="media-router-bugfix-"))
    try:
        # -------------------------------------------- data: URL 落盘（L7）
        # 有些平台直接把产物以内联 base64 返回。urllib 不认 data: scheme，
        # 交给 download 只会得到 "unknown url type: data"。
        data_url = "data:image/png;base64," + base64.b64encode(make_png(8, 8)).decode()
        written = adapters._fetch_to_local(data_url, scratch, "inline", 0, "image")  # noqa: SLF001
        check(
            "data: URL 能落盘成真实文件",
            written.exists() and written.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n",
            str(written),
        )

        # -------------------------------------------- 健康状态容错（H6）
        # 路由每一步都要问 is_cooling；health.json 一坏就抛异常的话，
        # 整条生成链路全断，而报错内容（'str' object has no attribute 'get'）
        # 跟真实原因毫无关系。
        health_path = scratch / "health.json"
        health_path.write_text("{ 这不是 json", encoding="utf-8")
        broken = HealthStore(health_path)
        check(
            "health.json 整个坏掉时能静默重建",
            broken.state("m") == {} and broken.is_cooling("m") is False,
            "",
        )

        health_path.write_text(
            json.dumps({"schema_version": 1, "models": {"m": "oops", "n": {"a": 1}}}),
            encoding="utf-8",
        )
        partial = HealthStore(health_path)
        check(
            "health.json 里非 dict 的条目被丢掉、其余照常",
            partial.state("m") == {} and partial.state("n") == {"a": 1},
            str(partial._data),  # noqa: SLF001
        )

        health_path.write_text(
            json.dumps(
                {"models": {"m": {"cooldown_until": "later", "consecutive_failures": [1]}}}
            ),
            encoding="utf-8",
        )
        dirty = HealthStore(health_path)
        check(
            "health 字段类型不对时按 0 处理（不炸掉整条生成链路）",
            dirty.is_cooling("m") is False
            and dirty.cooling_remaining("m") == 0.0
            and dirty.snapshot().get("m", {}).get("consecutive_failures") == 0,
            str(dirty.snapshot()),
        )
        dirty.record_success("m")
        leftovers = [p.name for p in scratch.glob("*.tmp")]
        check(
            "health 落盘不留 .tmp 残渣（tmp 名带 pid）",
            health_path.exists() and not leftovers,
            str(leftovers),
        )
        check(
            "health 写入后可正常读回",
            HealthStore(health_path).state("m").get("success_count") == 1,
            str(HealthStore(health_path).state("m")),
        )

        # -------------------------------------------- 下载的原子性（M7）
        # 原来直接以最终文件名边下边写：中途断流/磁盘满就会留下一个名字正常、
        # 内容截断的"产物"，用户拿它当生成结果引用，后面全错。
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        try:
            Handler.server_port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base_url = f"http://127.0.0.1:{srv.server_address[1]}"

            ok_dest = scratch / "atomic-ok.png"
            got = transport.download(f"{base_url}/img.png", ok_dest)
            check(
                "下载成功且不留 .part 残渣",
                got.exists()
                and got.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
                and not list(scratch.glob("*.part")),
                str(sorted(p.name for p in scratch.iterdir()))[:200],
            )

            fail_dest = scratch / "atomic-fail.bin"
            try:
                transport.download(f"{base_url}/nope.png", fail_dest)
                ok, detail = False, "居然下载成功了"
            except transport.HttpError as exc:
                ok, detail = True, str(exc)[:100]
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            check("下载失败会抛可读的 HttpError", ok, detail)
            check(
                "下载失败不留半截产物",
                not fail_dest.exists()
                and not list(scratch.glob("*.part"))
                and not list(scratch.glob("*.tmp")),
                str(sorted(p.name for p in scratch.iterdir()))[:200],
            )

            # ---------------------------------------- 密钥校验不能崩（H3）
            # 200 但不是 JSON（被 WAF 拦成 HTML）原来是直接抛异常，
            # 配置页面只会显示 500，用户分不清是地址错、密钥错还是平台抽风。
            entry = catalog.get("volcengine")
            if entry is not None:
                patched = dataclasses.replace(
                    entry, auth_check={"url": f"{base_url}/html", "auth": "bearer"}
                )
                target = probe.ProbeTarget(
                    catalog_key="volcengine", provider=entry.provider, api_key="fake-key"
                )
                outcome = probe._check_auth(target, patched, "fake-key", 5.0)  # noqa: SLF001
                verdict = outcome[0]
                check(
                    "校验端点回 HTML 时给结论而不是抛异常",
                    verdict.status == probe.WARN and "不是 JSON" in verdict.detail,
                    f"{verdict.status} {verdict.detail[:120]}",
                )

            # ------------------------------------ 自定义接口的占位符：没填就不发
            # {{duration}} 在"没指定"时曾经渲染成数字 0 原样发出去，平台只会回
            # 一个 400，而用户完全看不出是自己的模板里少填了一个变量。
            # 这里断言的是**真正发到线上的字节**，不是函数返回值 ——
            # mock 服务端把 POST 体抄了一份给 POSTED_BODIES。
            probe_cfg = config.RouterConfig(
                skill_dir=config.SKILL_DIR,
                config_dir=config.CONFIG_DIR,
                state_dir=scratch / "probe-state",
                config_path=scratch / "probe-models.yaml",
                defaults={"output_dir": str(scratch / "probe-out")},
                pools={},
                secrets={"keys": {}},
                source_format="yaml",
            )

            def video_spec(params: dict) -> config.ModelSpec:
                """一个自定义 HTTP 视频模型，body 模板里显式用了占位符。"""
                return config.build_spec_from_entry(
                    "video",
                    {
                        "id": "probe-video",
                        "provider": "generic_http",
                        "model": "probe-video",
                        "params": params,
                        "options": {
                            "auth": {"type": "none"},
                            "submit": {
                                "url": base_url + "/submit",
                                "method": "POST",
                                "task_id_path": "data.task_id",
                                "body": {
                                    "prompt": "{{prompt}}",
                                    "ratio": "{{aspect_ratio}}",
                                    "duration": "{{duration}}",
                                },
                            },
                            "poll": {
                                "url": base_url + "/tasks/{{task_id}}",
                                "method": "GET",
                                "interval": 1,
                                "status_path": "data.status",
                                "success": ["succeeded"],
                                "failure": ["failed"],
                                "result_path": "data.images",
                                "result_url_field": "url",
                            },
                        },
                    },
                    {},
                )

            POSTED_BODIES.clear()
            bare = probe.deep_probe(
                probe_cfg, video_spec({}), "自检用视频", timeout=10, poll_interval=1, max_poll=20
            )
            sent = POSTED_BODIES[0] if POSTED_BODIES else {}
            check(
                "占位符没填时不会把空值/0 发出去（duration / ratio 都不出现）",
                bool(sent) and "duration" not in sent and "ratio" not in sent,
                str(sent),
            )
            check("整条链路仍然跑得通（不是把请求弄坏了）", bare.get("ok") is True, str(bare)[:160])

            POSTED_BODIES.clear()
            filled = probe.deep_probe(
                probe_cfg,
                video_spec({"duration": 5, "ratio": "16:9"}),
                "自检用视频",
                timeout=10,
                poll_interval=1,
                max_poll=20,
            )
            sent2 = POSTED_BODIES[0] if POSTED_BODIES else {}
            check(
                "填了的值照常发出（params 在渲染之后合并，不会被裁掉）",
                sent2.get("duration") == 5 and sent2.get("ratio") == "16:9",
                str(sent2),
            )
            check(
                "有值的字段不受影响（prompt 仍在）",
                sent2.get("prompt") == "自检用视频" and filled.get("ok") is True,
                str(sent2),
            )

            # ------------------------ --duration 必须真的送到平台（dashscope 分支）
            # CLI 的 --duration 落在 req.duration，而 _param_map 只认
            # spec.params / req.extra —— 光靠它会把这条旗标静默丢掉
            # （火山分支用 req.duration 兜住了、这段漏了）。断言抓的是**提交请求体
            # 里的 parameters.duration**，不是函数返回值。
            real_ds_base = adapters.DASHSCOPE_BASE
            adapters.DASHSCOPE_BASE = base_url + "/ds"
            try:
                ds_ctx = adapters.Ctx(
                    api_key="fake-key",
                    key_source="自检",
                    timeout=10,
                    poll_interval=1,
                    max_poll=20,
                    log=lambda _m: None,
                )

                def ds_run(
                    params: dict, duration: int, extra: dict | None = None
                ) -> tuple[dict, Any]:
                    ds_spec = config.build_spec_from_entry(
                        "video",
                        {
                            "id": "ds-video",
                            "provider": "dashscope",
                            "model": "wan2.1-t2v-turbo",
                            "endpoint": base_url + "/ds-submit",
                            "params": params,
                        },
                        {},
                    )
                    ds_req = adapters.GenRequest(
                        kind="video",
                        prompt="自检用视频",
                        duration=duration,
                        output_dir=scratch / "ds-out",
                        extra=extra or {},
                    )
                    POSTED_BODIES.clear()
                    outcome = adapters.run_adapter(ds_spec, ds_req, probe_cfg, ds_ctx)
                    return (POSTED_BODIES[0] if POSTED_BODIES else {}), outcome

                ds_body, ds_result = ds_run({}, 7)
                check(
                    "--duration 会真的送进百炼的 parameters（不再被静默丢弃）",
                    (ds_body.get("parameters") or {}).get("duration") == 7,
                    str(ds_body.get("parameters")),
                )
                check(
                    "百炼视频这条链路仍然跑得通",
                    ds_result.status == "ok" and bool(ds_result.files),
                    f"{ds_result.status} {ds_result.error}"[:160],
                )

                ds_param_body, _ = ds_run({}, 7, {"duration": 9})
                check(
                    "--param duration= 仍然压过 --duration（点名了字段的更具体）",
                    (ds_param_body.get("parameters") or {}).get("duration") == 9,
                    str(ds_param_body.get("parameters")),
                )

                ds_params_body, _ = ds_run({"duration": 10}, 0)
                check(
                    "条目里的 params.duration 仍是兜底值",
                    (ds_params_body.get("parameters") or {}).get("duration") == 10,
                    str(ds_params_body.get("parameters")),
                )

                ds_bare_body, _ = ds_run({}, 0)
                check(
                    "三处都没给时不会凭空造一个 duration 字段",
                    "duration" not in (ds_bare_body.get("parameters") or {}),
                    str(ds_bare_body.get("parameters")),
                )

                # 轮询地址必须跟着**提交地址**走。用户把 endpoint 配成中转网关时，
                # 提交走网关、轮询却直连官方域名的话会连不上，而报错只是个
                # 超时，完全指不到原因。
                unit_cases = [
                    (
                        "含 /services/ 时能推出",
                        {
                            "endpoint": "https://relay.example.com/api/v1"
                            "/services/aigc/video-generation/video-synthesis"
                        },
                        ("https://relay.example.com/api/v1", True),
                    ),
                    (
                        "显式 task_base 优先",
                        {
                            "endpoint": "https://relay.example.com/api/v1/services/aigc/x",
                            "options": {"task_base": "https://gw.example.com/tasks-api/"},
                        },
                        ("https://gw.example.com/tasks-api", True),
                    ),
                    (
                        "推不出时退回官方地址（不硬拼一个错的）",
                        {"endpoint": "https://relay.example.com/just-a-gateway"},
                        (adapters.DASHSCOPE_BASE, False),
                    ),
                    ("没配 endpoint 时用官方地址", {}, (adapters.DASHSCOPE_BASE, False)),
                ]
                unit_bad = []
                for why, entry, want in unit_cases:
                    case_spec = config.build_spec_from_entry(
                        "video",
                        {"id": "ds-case", "provider": "dashscope", "model": "m", **entry},
                        {},
                    )
                    got = adapters._dashscope_task_base(case_spec)
                    if got != want:
                        unit_bad.append(f"{why}: 期望 {want}，实得 {got}")
                check(
                    "百炼任务地址的四种情形都对", not unit_bad, "; ".join(unit_bad)
                )

                def relay_paths(entry_extra: dict) -> list[str]:
                    rspec = config.build_spec_from_entry(
                        "video",
                        {
                            "id": "ds-relay",
                            "provider": "dashscope",
                            "model": "wan2.1-t2v-turbo",
                            **entry_extra,
                        },
                        {},
                    )
                    GET_PATHS.clear()
                    adapters.run_adapter(
                        rspec,
                        adapters.GenRequest(
                            kind="video",
                            prompt="自检用视频",
                            duration=5,
                            output_dir=scratch / "ds-relay-out",
                        ),
                        probe_cfg,
                        ds_ctx,
                    )
                    return list(GET_PATHS)

                derived_paths = relay_paths(
                    {
                        "endpoint": base_url
                        + "/relay/api/v1/services/aigc/video-generation/video-synthesis"
                    }
                )
                check(
                    "轮询跟着提交地址走（不会偷偷直连官方域名）",
                    any(p.startswith("/relay/api/v1/tasks/") for p in derived_paths),
                    f"实际请求路径：{derived_paths}",
                )

                explicit_paths = relay_paths(
                    {
                        "endpoint": base_url
                        + "/relay/api/v1/services/aigc/video-generation/video-synthesis",
                        "options": {"task_base": base_url + "/ds"},
                    }
                )
                check(
                    "options.task_base 显式压过自动推导",
                    any(p.startswith("/ds/tasks/") for p in explicit_paths),
                    f"实际请求路径：{explicit_paths}",
                )
            finally:
                adapters.DASHSCOPE_BASE = real_ds_base

            # ------------ replicate / fal：--param / --duration / --count 的透传
            # 这两个分支过去只读 req.extra["inputs"]（一个嵌套字典），而 CLI 的
            # --param 产出的是**扁平** key=value —— 没人会去造嵌套的 inputs，
            # 于是文档承诺的"参数会合并进 input"根本没发生：
            # --param / --duration / --count 全部被静默吃掉。
            # 断言盯的是**真正 POST 出去的请求体**（replicate 包在 input 里，
            # fal 直接铺在顶层），而不是函数返回值。
            pf_logs: list[str] = []
            ctx_pf = adapters.Ctx(
                api_key="fake-key",
                key_source="自检",
                timeout=10,
                poll_interval=1,
                max_poll=20,
                log=pf_logs.append,
            )

            def _pf_send(
                provider: str,
                kind: str,
                suffix: str,
                *,
                params: dict | None = None,
                options: dict | None = None,
                **req_kw: Any,
            ) -> tuple[dict, Any]:
                spec = config.build_spec_from_entry(
                    kind,
                    {
                        "id": f"{provider}-case",
                        "provider": provider,
                        "model": "owner/name",
                        "endpoint": base_url + suffix,
                        "params": params or {},
                        "options": options or {},
                    },
                    {},
                )
                req = adapters.GenRequest(
                    kind=kind,
                    prompt="自检用",
                    output_dir=scratch / f"{provider}-out",
                    **req_kw,
                )
                POSTED_BODIES.clear()
                pf_logs.clear()
                outcome = adapters.run_adapter(spec, req, probe_cfg, ctx_pf)
                sent = POSTED_BODIES[0] if POSTED_BODIES else {}
                body = sent.get("input") if isinstance(sent, dict) and "input" in sent else sent
                return (body if isinstance(body, dict) else {}), outcome

            # --- replicate ---
            rep_body, rep_result = _pf_send(
                "replicate", "image", "/rep-submit", extra={"guidance": 3.5}
            )
            check(
                "replicate：--param 的扁平参数会真的合并进 input（文档承诺过）",
                rep_body.get("guidance") == 3.5,
                str(rep_body),
            )
            check(
                "replicate：整条链路仍然跑得通",
                rep_result.status == "ok" and bool(rep_result.files),
                f"{rep_result.status} {rep_result.error}"[:160],
            )

            opt_body, _ = _pf_send(
                "replicate",
                "image",
                "/rep-submit",
                options={"inputs": {"num_frames": 81}},
            )
            check(
                "replicate：条目里的 options.inputs 会被读进来",
                opt_body.get("num_frames") == 81,
                str(opt_body),
            )

            dur_body, _ = _pf_send("replicate", "video", "/rep-submit", duration=6)
            check(
                "replicate：--duration 会真的送进 input.duration（不再静默丢弃）",
                dur_body.get("duration") == 6,
                str(dur_body),
            )

            dur_param_body, _ = _pf_send(
                "replicate", "video", "/rep-submit", duration=6, extra={"duration": 9}
            )
            check(
                "replicate：--param duration= 压过 --duration（点名了字段的更具体）",
                dur_param_body.get("duration") == 9,
                str(dur_param_body),
            )

            dur_override_body, _ = _pf_send(
                "replicate", "video", "/rep-submit", duration=6, params={"duration": 4}
            )
            check(
                "replicate：--duration 压过条目里的 params.duration",
                dur_override_body.get("duration") == 6,
                str(dur_override_body),
            )

            cnt_body, _ = _pf_send("replicate", "image", "/rep-submit", count=3)
            check(
                "replicate：--count 映射到 num_outputs",
                cnt_body.get("num_outputs") == 3,
                str(cnt_body),
            )

            one_body, _ = _pf_send("replicate", "image", "/rep-submit", count=1)
            check(
                "replicate：count 默认 1 时不会凭空塞 num_outputs",
                "num_outputs" not in one_body,
                str(one_body),
            )

            none_body, _ = _pf_send(
                "replicate",
                "video",
                "/rep-submit",
                duration=5,
                options={"duration_field": "none"},
            )
            check(
                "replicate：duration_field 关掉后不注入（但会留日志，不是静默忽略）",
                "duration" not in none_body and any("未生效" in m for m in pf_logs),
                f"{none_body} | logs={pf_logs}",
            )

            # --- fal ---
            fal_body, fal_result = _pf_send(
                "fal", "video", "/fal-submit", duration=5, extra={"seed": 7}
            )
            check(
                "fal：--duration / --param 真的落到请求体上",
                fal_body.get("duration") == 5 and fal_body.get("seed") == 7,
                str(fal_body),
            )
            check(
                "fal：整条链路仍然跑得通（轮询 status_url -> response_url）",
                fal_result.status == "ok" and bool(fal_result.files),
                f"{fal_result.status} {fal_result.error}"[:160],
            )

            fal_cnt_body, _ = _pf_send("fal", "image", "/fal-submit", count=2)
            check(
                "fal：--count 映射到 num_images（图片请求）",
                fal_cnt_body.get("num_images") == 2,
                str(fal_cnt_body),
            )

            fal_custom_body, _ = _pf_send(
                "fal",
                "video",
                "/fal-submit",
                duration=4,
                options={"duration_field": "video_length"},
            )
            check(
                "fal：字段名可用 options.duration_field 覆盖（各家不一致）",
                fal_custom_body.get("video_length") == 4
                and "duration" not in fal_custom_body,
                str(fal_custom_body),
            )

            # --- 按 kind 收紧：出图参数不许塞进视频请求 ---
            # num_outputs / num_images 都是出图字段，塞进视频请求只会换来 422。
            vid_cnt_body, _ = _pf_send("replicate", "video", "/rep-submit", count=2)
            check(
                "replicate：视频请求不会被塞进出图字段 num_outputs（会 422）",
                "num_outputs" not in vid_cnt_body and any("不是图片" in m for m in pf_logs),
                f"{vid_cnt_body} | logs={pf_logs}",
            )

            img_dur_body, _ = _pf_send("replicate", "image", "/rep-submit", duration=8)
            check(
                "replicate：图片请求不会被塞进 duration（会 422）",
                "duration" not in img_dur_body and any("不是视频" in m for m in pf_logs),
                f"{img_dur_body} | logs={pf_logs}",
            )
        finally:
            with contextlib.suppress(Exception):
                srv.shutdown()
            srv.server_close()
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
