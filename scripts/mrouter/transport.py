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
from contextlib import contextmanager
from contextvars import ContextVar
from http.client import BadStatusLine, IncompleteRead, LineTooLong
from pathlib import Path
from typing import Any, Callable

from . import __version__

USER_AGENT = f"media-router/{__version__} (+skill)"

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

#: 提交类方法遇到任何 HTTP / 网络失败都不重发；错误响应也不能证明没有执行。
NON_REPLAYABLE_METHODS = {"POST", "PATCH"}

_FAIL_FAST = ContextVar("media_router_fail_fast", default=False)
_SUBMISSION_HINT = (
    "（提交类请求不会自动重发：提交结果不确定，服务端可能已经受理并计费，"
    "请先用平台控制台确认任务是否已创建，再决定是否重跑）"
)


@contextmanager
def execution_policy(*, fail_fast: bool = False):
    """本次执行的局部策略；内层只能提升严格度，退出时恢复原策略。"""
    token = _FAIL_FAST.set(_FAIL_FAST.get() or bool(fail_fast))
    try:
        yield
    finally:
        _FAIL_FAST.reset(token)


def is_fail_fast() -> bool:
    return _FAIL_FAST.get()

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


_HTML_START = re.compile(r"(?is)^[\s\ufeff]*(?:<!doctype\s+html\b|<html\b)")
_HTML_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_HTML_TAG = re.compile(r"(?s)<[^>]+>")


def html_summary(body: str) -> str:
    """识别 HTML 响应并给出简短标题；不是 HTML 时返回空串。

    只用于错误信息与诊断，不执行页面里的任何内容。返回空串即"不像 HTML"，
    调用方应继续按普通非 JSON 响应处理。
    """
    text = str(body or "")[:4000]
    if not _HTML_START.match(text):
        return ""
    match = _HTML_TITLE.search(text)
    if match:
        title = " ".join(_HTML_TAG.sub(" ", match.group(1)).split())
        title = redact(title)[:80]
        if title:
            return title
    return "HTML 页面"


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

    def __init__(
        self, message: str, status: int | None = None, body: str | bytes = "", *,
        submission_uncertain: bool = False,
    ):
        super().__init__(redact(message))
        self.status = status
        if isinstance(body, (bytes, bytearray)):
            # 调用方传原始字节时先解码：str(b"...") 会留下 b'' 外壳，
            # 既不利于展示，也会让 HTML 页面识别失效。
            body = bytes(body).decode("utf-8", errors="replace")
        self.body = redact(body or "")[:800]
        self.submission_uncertain = bool(submission_uncertain)


class PollTimeout(RuntimeError):
    """轮询超时。"""


class HttpResponse:
    def __init__(
        self, status: int, headers: dict[str, str], body: bytes, *, method: str = "",
        generation_submission: bool = False,
    ):
        self.status = status
        self.headers = headers
        self.body = body
        self.method = method.upper()
        self.generation_submission = bool(generation_submission)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        uncertain = self.generation_submission or self.method in NON_REPLAYABLE_METHODS
        hint = f"\n{_SUBMISSION_HINT}" if uncertain else ""
        if not self.body:
            if not uncertain:
                return None
            raise HttpError(
                f"响应为空，无法解析 JSON{hint}", self.status, self.text,
                submission_uncertain=True,
            )
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            page = html_summary(self.text)
            if page:
                message = (
                    f"响应不是 JSON，而是 HTML 页面（{page}）："
                    "接口地址可能填成了网页地址，或请求被登录页、代理、防火墙拦截"
                )
                if uncertain:
                    message += f"\n{_SUBMISSION_HINT}"
                raise HttpError(
                    message, self.status, self.text, submission_uncertain=uncertain
                ) from exc
            raise HttpError(
                f"响应不是合法 JSON：{redact(self.text[:300])}{hint}", self.status, self.text,
                submission_uncertain=uncertain,
            ) from exc


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
    generation_submission: bool = False,
) -> HttpResponse:
    """发起请求；非 2xx 抛 HttpError，提交类请求永不自动重放。

    默认保留幂等请求的有界重试；严格模式连查询也只发一次。
    POST/PATCH 的 408/425/5xx 或网络、接收失败不能证明未受理，须先查平台任务。
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
    #: HTTP 和网络失败共用此门控，避免错误响应触发重复提交。
    replayable = not generation_submission and method.upper() not in NON_REPLAYABLE_METHODS
    while attempt <= max_retries:
        attempt += 1
        req = urllib.request.Request(
            url, data=payload, headers=final_headers, method=method.upper()
        )
        try:
            with open_request(req, timeout=timeout) as resp:
                return HttpResponse(
                    resp.status, dict(resp.headers.items()), resp.read(), method=method.upper(),
                    generation_submission=generation_submission,
                )
        except urllib.error.HTTPError as exc:
            body = ""
            receive_failed = False
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except (
                urllib.error.URLError, TimeoutError, OSError,
                IncompleteRead, BadStatusLine, LineTooLong,
            ) as read_exc:
                receive_failed = True
                partial = getattr(read_exc, "partial", b"")
                if isinstance(partial, bytes):
                    body = partial.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            if (replayable and not is_fail_fast()
                    and exc.code in RETRY_STATUS and attempt <= max_retries):
                last_error = HttpError(f"HTTP {exc.code}", exc.code, body)
                time.sleep(retry_backoff ** attempt)
                continue
            uncertain = not replayable and (
                receive_failed or exc.code in (408, 425) or 500 <= exc.code < 600
            )
            hint = f"\n{_SUBMISSION_HINT}" if uncertain else ""
            raise HttpError(
                f"HTTP {exc.code} {redact(url)}{hint}", exc.code, body,
                submission_uncertain=uncertain,
            ) from exc
        except (
            urllib.error.URLError, TimeoutError, OSError,
            IncompleteRead, BadStatusLine, LineTooLong,
        ) as exc:
            if attempt <= max_retries and replayable and not is_fail_fast():
                last_error = exc
                time.sleep(retry_backoff ** attempt)
                continue
            hint = "" if replayable else f"\n{_SUBMISSION_HINT}"
            raise HttpError(
                f"网络错误：{exc} ({redact(url)}){hint}",
                submission_uncertain=not replayable,
            ) from exc

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

    默认保留有界重试：这一步失败时任务已经在平台侧生成并扣过费了，一次 CDN
    抖动不应丢掉结果。GET 是幂等的，重试没有生成副作用；严格模式只下载一次。
    403/404 不重试 —— 那多半是签名地址过期或文件被清，重试也还是同一句话。
    """
    dest = Path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HttpError(f"下载目录不可写：{exc} ({redact(url)})") from exc
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
            if not is_fail_fast() and exc.code in RETRY_STATUS and attempt <= max_retries:
                time.sleep(retry_backoff ** attempt)
                continue
            raise HttpError(f"下载失败 HTTP {exc.code}：{redact(url)}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError, IncompleteRead, BadStatusLine, LineTooLong) as exc:
            _drop()
            if not is_fail_fast() and attempt <= max_retries:
                time.sleep(retry_backoff ** attempt)
                continue
            raise HttpError(f"下载失败：{exc} ({redact(url)})") from exc

    try:
        if not tmp.exists() or tmp.stat().st_size == 0:
            _drop()
            raise HttpError(f"下载得到空文件：{redact(url)}")
        tmp.replace(dest)
    except OSError as exc:
        _drop()
        raise HttpError(f"下载完成但落盘失败：{exc} ({redact(url)})") from exc
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
