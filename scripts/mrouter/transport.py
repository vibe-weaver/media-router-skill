"""HTTP 传输层 —— 只用标准库 urllib，保证零依赖。

提供三件事：
  request()   发起一次请求（含重试、超时、multipart、JSON 解析）
  download()  把远程产物下载到本地
  poll()      异步任务的轮询等待
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from . import __version__

USER_AGENT = f"media-router/{__version__} (+skill)"

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

#: 这些方法在**网络层失败**时一律不重发。
#: 生成类接口（提交任务）几乎都是 POST：超时意味着"没收到响应"，而不是"没执行"——
#: 平台很可能已经收下并开始计费，重发一次就是两个任务、两笔钱，而第一个任务的结果
#: 因为没有 task_id 永远拿不回来。429 / 5xx 这类"服务端明确回了话"的情况仍然重试。
NON_REPLAYABLE_METHODS = {"POST", "PATCH"}

#: 显式代理开关。不设 = 沿用 HTTP(S)_PROXY 环境变量；设成 none/off/direct = 直连；
#: 设成 URL = 所有外部请求走这个代理。回环地址永远直连，不受这里影响。
PROXY_ENV = "MEDIA_ROUTER_PROXY"
_NO_PROXY_VALUES = {"none", "off", "0", "false", "direct"}

#: 查询串里看着像凭据的参数名 —— 有些自建接口用 ?key= 带密钥，
#: 一旦 URL 进了日志或错误信息，密钥就跟着漏了。
_SENSITIVE_PARAM = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|apikey|token|access[_-]?token|secret)=)([^&\s\"'<>]+)"
)

#: 对象存储签名。**只对日志/错误文本打码**：签名本身就是下载凭据，打掉之后
#: 那条 URL 就成了死链。生成结果里的 ``urls[]`` 是给调用方用的，必须保持可用。
_SIGNATURE_PARAM = re.compile(r"(?i)([?&](?:signature|sig)=)([^&\s\"'<>]+)")

#: 代理地址常写成 http://user:pass@host:port，打印前把凭据段掩掉。
_USERINFO = re.compile(r"(?i)(//)[^/@\s\"'<>]+@")


def redact(text: str, *, mask_signature: bool = True) -> str:
    """把 URL 里的凭据打码，日志/错误信息/JSON 输出都要先过它。

    两类都要处理：查询串里的 ``?key=`` / ``?token=``，以及代理地址里可能带上的
    ``user:pass@host`` —— `config` 子命令会把当前代理策略原样打出来。

    ``mask_signature=False`` 用于**要交给调用方使用的 URL**（结果 JSON 的
    ``urls[]``）：签名是对象存储的下载凭据，打码后链接直接失效，而这条 URL
    本身就是要返回给用户/agent 的产物地址，不属于"日志泄漏"。
    """
    cleaned = _USERINFO.sub(r"\1***@", str(text))
    cleaned = _SENSITIVE_PARAM.sub(r"\1***", cleaned)
    if mask_signature:
        cleaned = _SIGNATURE_PARAM.sub(r"\1***", cleaned)
    return cleaned


def _is_loopback_url(url: str) -> bool:
    host = (urllib.parse.urlparse(str(url)).hostname or "").lower()
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_OPENERS: dict[str, urllib.request.OpenerDirector] = {}


def _build_opener(spec: str) -> urllib.request.OpenerDirector:
    cached = _OPENERS.get(spec)
    if cached is not None:
        return cached
    if spec.lower() in _NO_PROXY_VALUES:
        handler = urllib.request.ProxyHandler({})
    elif spec:
        handler = urllib.request.ProxyHandler({"http": spec, "https": spec})
    else:
        handler = urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(handler)
    _OPENERS[spec] = opener
    return opener


def proxy_description() -> str:
    """当前代理策略的人话描述，给 `config` 子命令和排障用。"""
    spec = os.environ.get(PROXY_ENV, "").strip()
    if spec.lower() in _NO_PROXY_VALUES:
        return f"直连（{PROXY_ENV}={spec}）"
    if spec:
        return f"使用指定代理 {redact(spec)}"
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(name):
            return (
                f"沿用环境变量 {name}；密钥会经过该代理。"
                f"不想这样就把 {PROXY_ENV} 设成 none"
            )
    return "直连（未配置代理）"


def open_request(req: urllib.request.Request, timeout: float):
    """按代理策略打开请求。回环地址永远直连 —— 本机服务和测试不该被代理截走。"""
    url = getattr(req, "full_url", "")
    if _is_loopback_url(url):
        return _build_opener("none").open(req, timeout=timeout)
    spec = os.environ.get(PROXY_ENV, "").strip()
    return _build_opener(spec).open(req, timeout=timeout)


class HttpError(RuntimeError):
    """HTTP 层错误，带上状态码与响应片段方便定位。"""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = (body or "")[:800]


class PollTimeout(RuntimeError):
    """轮询超时。"""


class HttpResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise HttpError(f"响应不是合法 JSON：{self.text[:300]}") from exc


def _encode_multipart(
    fields: dict[str, Any], files: dict[str, tuple[str, bytes, str]]
) -> tuple[bytes, str]:
    boundary = "----media-router-" + uuid.uuid4().hex
    buf = bytearray()
    for key, value in (fields or {}).items():
        if value is None:
            continue
        buf += f"--{boundary}\r\n".encode()
        buf += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        buf += str(value).encode("utf-8") + b"\r\n"
    for key, (filename, content, ctype) in (files or {}).items():
        buf += f"--{boundary}\r\n".encode()
        buf += (
            f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
        ).encode()
        buf += f"Content-Type: {ctype}\r\n\r\n".encode()
        buf += content + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    form: dict[str, Any] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    timeout: float = 60.0,
    max_retries: int = 2,
    retry_backoff: float = 1.6,
) -> HttpResponse:
    """发起请求；对 429/5xx/网络抖动自动重试。非 2xx 抛 HttpError。

    重试分两种，不能一视同仁：

    * 服务端**回了响应**（429/5xx）：请求有没有被受理是可知的，重试安全；
    * **网络层失败**（超时 / 连接断）：请求可能已经被受理并计费。GET/HEAD/PUT/DELETE
      这类幂等方法重试没问题，POST/PATCH 提交一次就够 —— 见 NON_REPLAYABLE_METHODS。
    """
    if params:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}, doseq=True
        )
        url = f"{url}{'&' if '?' in url else '?'}{query}"

    final_headers = {"User-Agent": USER_AGENT}
    for key, value in (headers or {}).items():
        if value is not None:
            final_headers[key] = str(value)

    payload: bytes | None = None
    if files:
        payload, content_type = _encode_multipart(form or {}, files)
        final_headers["Content-Type"] = content_type
    elif json_body is not None:
        payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/json")
    elif form:
        payload = urllib.parse.urlencode(form).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    attempt = 0
    last_error: Exception | None = None
    #: 网络层失败能不能重来：POST / PATCH 提交过就不敢再提交一次（可能已计费）
    replayable = method.upper() not in NON_REPLAYABLE_METHODS
    while attempt <= max_retries:
        attempt += 1
        req = urllib.request.Request(
            url, data=payload, headers=final_headers, method=method.upper()
        )
        try:
            with open_request(req, timeout=timeout) as resp:
                return HttpResponse(
                    resp.status, dict(resp.headers.items()), resp.read()
                )
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            if exc.code in RETRY_STATUS and attempt <= max_retries:
                last_error = HttpError(f"HTTP {exc.code}", exc.code, body)
                time.sleep(retry_backoff ** attempt)
                continue
            raise HttpError(f"HTTP {exc.code} {redact(url)}", exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt <= max_retries and replayable:
                last_error = exc
                time.sleep(retry_backoff ** attempt)
                continue
            hint = (
                ""
                if replayable
                else "（提交类请求不会自动重发：服务端可能已经受理并计费，"
                "请先用平台控制台确认任务是否已创建，再决定是否重跑）"
            )
            raise HttpError(f"网络错误：{exc} ({redact(url)}){hint}") from exc

    raise HttpError(f"请求失败：{last_error}")


def guess_content_type(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


def read_upload(path_or_url: str) -> tuple[str, bytes, str]:
    """把本地路径或 http(s) URL 读成 (filename, bytes, content_type)，用于 multipart 上传。"""
    if str(path_or_url).startswith(("http://", "https://")):
        resp = request("GET", str(path_or_url), timeout=120)
        name = os.path.basename(urllib.parse.urlparse(str(path_or_url)).path) or "input.bin"
        ctype = resp.headers.get("Content-Type") or guess_content_type(Path(name))
        return name, resp.body, ctype
    local = Path(path_or_url).expanduser()
    if not local.exists():
        raise HttpError(f"输入文件不存在：{local}")
    return local.name, local.read_bytes(), guess_content_type(local)


def download(
    url: str,
    dest: Path,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 300.0,
    max_retries: int = 2,
    retry_backoff: float = 1.6,
) -> Path:
    """下载远程产物到本地文件。**先写 .part 再原子改名。**

    直接以最终文件名边下边写，一旦中途断流/被 Ctrl-C/磁盘满，磁盘上就会留下
    一个名字正常、内容截断的"产物"；而重试时 _unique_path 只会另起一个 -1 后缀，
    那半截文件就永远留在 outputs 里，用户拿它当生成结果引用，后面全错。

    下载**必须重试**：这一步失败时任务已经在平台侧生成并扣过费了，一次 CDN 抖动
    就把结果丢掉，等于白花钱重跑一遍。GET 是幂等的，重试没有副作用。
    403/404 不重试 —— 那多半是签名地址过期或文件被清，重试也还是同一句话。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    final_headers = {"User-Agent": USER_AGENT}
    for key, value in (headers or {}).items():
        if value is not None:
            final_headers[key] = str(value)

    tmp = dest.parent / f"{dest.name}.{os.getpid()}.part"

    def _drop() -> None:
        try:
            tmp.unlink()
        except OSError:
            pass

    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, headers=final_headers, method="GET")
        try:
            with open_request(req, timeout=timeout) as resp, tmp.open("wb") as fh:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
            break
        except urllib.error.HTTPError as exc:
            _drop()
            if exc.code in RETRY_STATUS and attempt <= max_retries:
                time.sleep(retry_backoff ** attempt)
                continue
            raise HttpError(f"下载失败 HTTP {exc.code}：{redact(url)}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            _drop()
            if attempt <= max_retries:
                time.sleep(retry_backoff ** attempt)
                continue
            raise HttpError(f"下载失败：{exc} ({redact(url)})") from exc

    if not tmp.exists() or tmp.stat().st_size == 0:
        _drop()
        raise HttpError(f"下载得到空文件：{redact(url)}")
    tmp.replace(dest)
    return dest


def poll(
    probe: Callable[[], Any],
    *,
    interval: float = 5.0,
    max_seconds: float = 900.0,
    on_tick: Callable[[float, Any], None] | None = None,
    log: Callable[[str], None] | None = None,
    max_interval: float = 15.0,
    backoff_after: float = 60.0,
) -> Any:
    """反复调用 probe 直到它返回非 None；超时抛 PollTimeout。

    probe 抛异常时向上传播（由适配器决定是否容忍）。

    间隔在 ``backoff_after``（默认 60 秒）之前保持原值，之后按 1.5 倍退避、
    ``max_interval`` 封顶。

    为什么不是一开始就退避：短视频通常 30~60 秒出结果，越早退避，检查点越稀，
    反而可能比固定间隔多等一个步长（5s 的片子实测会多等约 4 秒）。真正需要
    退避的是 2~3 分钟的长任务 —— 那时候每 5 秒打一次接口纯属浪费。

    ``log`` 给了就每轮打一行进度。以前这里是彻底静默的 —— 视频动辄 1~3 分钟，
    用户看到的只有提交那一行，然后什么都没有，分不清是在跑还是已经卡死。
    """
    started = time.time()
    interval = max(0.5, float(interval or 5.0))
    cap = max(interval, float(max_interval or interval))
    while True:
        value = probe()
        if value is not None:
            if log:
                log(f"轮询结束，用时 {time.time() - started:.0f} 秒")
            return value
        elapsed = time.time() - started
        if on_tick:
            on_tick(elapsed, None)
        if elapsed >= max_seconds:
            raise PollTimeout(f"轮询超过 {max_seconds:.0f} 秒仍未完成")
        if log:
            log(
                f"等待中：已用 {elapsed:.0f}s / 上限 {max_seconds:.0f}s，"
                f"{interval:.0f}s 后再查"
            )
        time.sleep(interval)
        if time.time() - started >= backoff_after:
            interval = min(interval * 1.5, cap)
