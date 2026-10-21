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

#: 显式代理开关。不设 = 沿用 HTTP(S)_PROXY 环境变量；设成 none/off/direct = 直连；
#: 设成 URL = 所有外部请求走这个代理。回环地址永远直连，不受这里影响。
PROXY_ENV = "MEDIA_ROUTER_PROXY"
_NO_PROXY_VALUES = {"none", "off", "0", "false", "direct"}

#: 查询串里看着像凭据的参数名 —— 有些自建接口用 ?key= 带密钥，
#: 一旦 URL 进了日志或错误信息，密钥就跟着漏了。
_SENSITIVE_PARAM = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|apikey|token|access[_-]?token|secret|signature|sig)=)([^&\s\"'<>]+)"
)

#: 代理地址常写成 http://user:pass@host:port，打印前把凭据段掩掉。
_USERINFO = re.compile(r"(?i)(//)[^/@\s\"'<>]+@")


def redact(text: str) -> str:
    """把 URL 里的凭据打码，日志/错误信息/JSON 输出都要先过它。

    两类都要处理：查询串里的 ``?key=`` / ``?token=``，以及代理地址里可能带上的
    ``user:pass@host`` —— `config` 子命令会把当前代理策略原样打出来。
    """
    cleaned = _USERINFO.sub(r"\1***@", str(text))
    return _SENSITIVE_PARAM.sub(r"\1***", cleaned)


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
    """发起请求；对 429/5xx/网络抖动自动重试。非 2xx 抛 HttpError。"""
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
            if attempt <= max_retries:
                last_error = exc
                time.sleep(retry_backoff ** attempt)
                continue
            raise HttpError(f"网络错误：{exc} ({redact(url)})") from exc

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
) -> Path:
    """下载远程产物到本地文件。**先写 .part 再原子改名。**

    直接以最终文件名边下边写，一旦中途断流/被 Ctrl-C/磁盘满，磁盘上就会留下
    一个名字正常、内容截断的"产物"；而重试时 _unique_path 只会另起一个 -1 后缀，
    那半截文件就永远留在 outputs 里，用户拿它当生成结果引用，后面全错。
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

    req = urllib.request.Request(url, headers=final_headers, method="GET")
    try:
        with open_request(req, timeout=timeout) as resp, tmp.open("wb") as fh:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
    except urllib.error.HTTPError as exc:
        _drop()
        raise HttpError(f"下载失败 HTTP {exc.code}：{redact(url)}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _drop()
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
) -> Any:
    """反复调用 probe 直到它返回非 None；超时抛 PollTimeout。

    probe 抛异常时向上传播（由适配器决定是否容忍）。
    """
    started = time.time()
    interval = max(0.5, float(interval or 5.0))
    while True:
        value = probe()
        if value is not None:
            return value
        elapsed = time.time() - started
        if on_tick:
            on_tick(elapsed, None)
        if elapsed >= max_seconds:
            raise PollTimeout(f"轮询超过 {max_seconds:.0f} 秒仍未完成")
        time.sleep(interval)
