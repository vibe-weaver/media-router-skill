"""Provider 适配器集合。

设计原则：
  - 每个适配器只负责「把一次生成请求翻译成某个 provider 的 HTTP 协议」，
    统一返回 GenResult，路由/熔断/回传由上层处理。
  - 尽量不新增适配器代码就能接新模型：``generic_http`` 是完全配置驱动的，
    绝大多数 REST 接口靠填 YAML 就能接上。
  - ``native`` 是特殊通道：它不调 HTTP，而是把「请调用内置 ImageGen / VideoGen」
    这条指令回给 agent，用来兜住那些把模型选择权收在工具内部的运行环境。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import ConfigError, ModelSpec, RouterConfig
from .transport import (
    HttpError,
    PollTimeout,
    download,
    poll,
    read_upload,
    request,
)

# ---------------------------------------------------------------- 数据结构


@dataclass
class GenRequest:
    kind: str
    prompt: str = ""
    requires: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    size: str = ""
    aspect_ratio: str = ""
    duration: int = 0
    negative_prompt: str = ""
    count: int = 1
    output_dir: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class GenResult:
    status: str  # ok | delegate | error
    model_id: str
    provider: str
    files: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    delegate: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class Ctx:
    api_key: str
    key_source: str
    timeout: float
    poll_interval: float
    max_poll: float
    log: Callable[[str], None] = lambda _msg: None


class DownloadError(HttpError):
    """产物已经生成（多半也已经计费），只是下载/落盘这一步失败。

    这类错误**不是模型故障**：换一个模型重跑等于再花一次钱，而上一单的结果
    还躺在平台侧。``urls`` 一定要带出去 —— 有它用户（或 agent）可以直接把产物
    取回来，不用重新生成。
    """

    def __init__(
        self,
        message: str,
        urls: list[str] | None = None,
        files: list[str] | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message, status)
        self.urls = list(urls or [])
        self.files = list(files or [])


# ---------------------------------------------------------------- 工具函数

_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
]


def sniff_extension(head: bytes) -> str | None:
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    if head[:4] == b"fLaC":
        return ".flac"
    if head[:4] == b"OggS":
        return ".ogg"
    if head[:3] == b"ID3" or head[:2] == b"\xff\xfb":
        return ".mp3"
    return None


def _safe(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", str(text)).strip("-") or "output"


def _ext_from_url(url: str, fallback: str) -> str:
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix and len(suffix) <= 6 and re.fullmatch(r"\.[0-9a-z]+", suffix):
        return suffix
    return fallback


def _unique_path(dest: Path) -> Path:
    """目标文件已存在时追加序号，确保永不覆盖既有产物。"""
    if not dest.exists():
        return dest
    for n in range(1, 10000):
        candidate = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        if not candidate.exists():
            return candidate
    return dest


def _fetch_to_local(
    url: str,
    out_dir: Path,
    stem: str,
    index: int,
    kind: str,
    headers: dict[str, str] | None = None,
) -> Path:
    """下载并纠正扩展名（以真实文件头为准）。"""
    default_ext = {"video": ".mp4", "audio": ".mp3"}.get(kind, ".png")
    text = str(url)
    if text.startswith("data:"):
        # 有些平台直接把产物以内联 base64 返回。urllib 不认 data: scheme，
        # 交给 download 只会得到 "unknown url type: data"，所以这里单独处理。
        return _write_b64_to_local(text, out_dir, stem, index, kind)
    if "://" not in text:
        return _write_inline_b64(text, out_dir, stem, index, kind)
    ext = _ext_from_url(url, default_ext)
    dest = _unique_path(out_dir / f"{stem}_{index:02d}{ext}")
    download(url, dest, headers=headers, timeout=300.0)
    head = dest.read_bytes()[:32]
    actual = sniff_extension(head)
    if actual and actual != ext:
        renamed = _unique_path(dest.with_suffix(actual))
        dest.replace(renamed)
        dest = renamed
    if dest.suffix.lower() == ".wav":
        _repair_wav_sizes(dest)
    return dest


#: 裸 base64 的形状。64 字起步 —— 任何媒体文件编码后都远长于此，
#: 而 task_id、model 名这类普通字符串几乎不会这么长，避免误判。
_B64_BODY = re.compile(r"^[A-Za-z0-9+/_-]{64,}={0,2}$")
_B64_TO_STD = str.maketrans("-_", "+/")


def _b64_decode_lenient(payload: str) -> bytes | None:
    """解 base64，容忍缺 padding、夹带换行、以及 urlsafe 字母表。解不出返回 None。"""
    body = re.sub(r"\s+", "", str(payload)).translate(_B64_TO_STD).rstrip("=")
    body += "=" * (-len(body) % 4)
    try:
        return base64.b64decode(body, validate=True)
    except ValueError:  # binascii.Error 是它的子类
        return None


def _repair_wav_sizes(path: Path) -> None:
    """把流式 WAV 头里的 RIFF / data 长度字段改成真实值。

    平台（百炼、火山等）返回的是「边生成边推」的 wav：头里的 data size 是占位值
    （常见 0x7FFFFFxx），按它算时长会得到几小时。我们自己的 audio_duration()
    会按文件实际长度钳制，所以 JSON 里的秒数是对的；但**文件本身**进了播放器/
    剪辑软件/素材库，就会显示荒谬时长、进度条拖不动，甚至被当成损坏文件。

    只改头里的两个 4 字节字段（RIFF size、data size），音频数据一个字节不动。
    用 ``r+b`` 定点读写，不把整个音频读进内存 —— TTS 生成几十分钟音频时，
    整块读入再写回会白占几百 MB。

    两个细节：
      * data size 已经是合理值（小于剩余字节）时**不覆盖** —— data 之后还可能
        跟着 LIST/INFO 之类的元数据 chunk，用"剩余全部字节"会把它们也算成音频；
      * 占位值（比剩余字节还大，或者 0）才用剩余长度兜底。这种情况下如果同一份
        文件里 data 后面又跟了元数据 chunk，就无法区分音频结束位置，时长会略微
        偏长 —— 但流式 wav 基本不会同时出现这两种特征，真实样本里没见到。
    """
    try:
        file_size = path.stat().st_size
        with path.open("r+b") as fh:
            header = fh.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return

            offset = 12
            data_offset = -1
            while offset + 8 <= file_size:
                fh.seek(offset)
                chunk = fh.read(8)
                if len(chunk) < 8:
                    return
                chunk_id = chunk[:4]
                chunk_size = struct.unpack("<I", chunk[4:8])[0]
                if chunk_id == b"data":
                    data_offset = offset
                    break
                if chunk_size > file_size:
                    # 头已经坏了，再按这个 size 跳只会越界
                    return
                offset += 8 + chunk_size + (chunk_size % 2)
            if data_offset < 0:
                return

            stored_riff = struct.unpack("<I", header[4:8])[0]
            fh.seek(data_offset + 4)
            stored_data = struct.unpack("<I", fh.read(4))[0]

            remaining = file_size - (data_offset + 8)
            if stored_data == 0 or stored_data > remaining:
                actual_data = remaining  # 占位值 / 空值：按实际长度兜底
            else:
                actual_data = stored_data  # 已经合理：别把尾随元数据算进音频

            if stored_riff == file_size - 8 and stored_data == actual_data:
                return
            fh.seek(4)
            fh.write(struct.pack("<I", file_size - 8))
            fh.seek(data_offset + 4)
            fh.write(struct.pack("<I", actual_data))
    except OSError:
        return


def _write_media_bytes(
    data: bytes, ext: str, out_dir: Path, stem: str, index: int
) -> Path:
    dest = _unique_path(out_dir / f"{stem}_{index:02d}{ext}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    if ext == ".wav":
        _repair_wav_sizes(dest)
    return dest


def _write_inline_b64(
    payload: str, out_dir: Path, stem: str, index: int, kind: str
) -> Path:
    """产物字段里放的是**不带 data: 前缀**的裸 base64。

    不少中转站把上游的 ``b64_json`` 原样塞进 ``url`` 字段，交给 download()
    只会得到 ``unknown url type: 'iVBOR…'`` —— 请求全对、图真的回来了，
    偏偏最后一步落盘失败，而那条报错完全看不出跟 base64 有关。

    解得出、且文件头认得出是图片/视频，才当产物收下。否则如实报错：
    更可能是 ``result_url_field`` / ``result_path`` 取错了字段。
    """
    data = _b64_decode_lenient(payload) if _B64_BODY.match(re.sub(r"\s+", "", payload)) else None
    ext = sniff_extension((data or b"")[:32])
    if data is None or ext is None:
        raise HttpError(
            "接口返回的产物字段既不是下载地址，也解不出可识别的图片/视频文件头"
            f"（内容开头：{str(payload)[:40]}）。\n"
            "多半是 result_url_field / result_path 取错了字段，"
            "把真正的下载地址或 base64 字段名对上即可。"
        )
    return _write_media_bytes(data, ext, out_dir, stem, index)


def _write_b64_to_local(
    payload: str, out_dir: Path, stem: str, index: int, kind: str
) -> Path:
    raw = str(payload)
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    data = _b64_decode_lenient(raw)
    if data is None:
        raise HttpError(f"内联 base64 产物解码失败（内容开头：{raw[:40]}）")
    ext = sniff_extension(data[:32]) or (".mp4" if kind == "video" else ".png")
    return _write_media_bytes(data, ext, out_dir, stem, index)


def _dig(data: Any, path: str | None, default: Any = None) -> Any:
    """按 ``a.b.0.c`` 取值，支持列表下标。"""
    if not path:
        return data
    cursor = data
    for part in str(path).split("."):
        if cursor is None:
            return default
        if isinstance(cursor, list):
            try:
                cursor = cursor[int(part)]
                continue
            except (ValueError, IndexError):
                return default
        if isinstance(cursor, dict):
            cursor = cursor.get(part, default)
            continue
        return default
    return cursor


def _render(node: Any, variables: dict[str, Any]) -> Any:
    """把配置里的 ``{{prompt}}`` 之类占位符替换成实际值。"""
    if isinstance(node, str):
        pattern = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

        def sub(match: re.Match[str]) -> str:
            return str(variables.get(match.group(1), "") or "")

        stripped = pattern.sub(sub, node)
        whole = node.strip()
        inner = whole[2:-2].strip() if whole.startswith("{{") and whole.endswith("}}") else None
        if inner and inner in variables:
            value = variables[inner]
            # 整串就是一个占位符时返回原始值（保留 int / bool 类型）。
            # None 要变成空串 —— 否则会被 str() 成字面量 "None" 拼进地址里。
            return "" if value is None else value
        return stripped
    if isinstance(node, dict):
        return {k: _render(v, variables) for k, v in node.items()}
    if isinstance(node, list):
        return [_render(v, variables) for v in node]
    return node


def _prune_empty(node: Any) -> Any:
    """把渲染后剩下的空串删掉。**只删空串，不碰 0 / False / None。**

    ``{{duration}}`` / ``{{aspect_ratio}}`` 这类占位符在"没指定"时会渲染成空串
    （以及修好之前的 ``{{duration}}`` 会渲染成数字 ``0``），原样发出去就是
    ``"duration": ""`` 或者干脆 ``"duration": 0`` —— 多数平台只会回一个 400，
    而用户完全看不出这是自己的模板里少填了一个变量。

    "没填就不发这个字段"才是对的语义，这也和各家适配器里 ``if ratio:`` 的写法一致。

    为什么**只**删空串：``seed: 0``、``camera_fixed: false`` 这类合法的假值必须
    原样留下 —— 它们和"没填"完全是两回事，删掉就是另一个 bug（就是 M3 那个）。
    """
    if isinstance(node, dict):
        out: dict[Any, Any] = {}
        for key, value in node.items():
            cleaned = _prune_empty(value)
            if isinstance(cleaned, str) and cleaned == "":
                continue
            out[key] = cleaned
        return out
    if isinstance(node, list):
        return [_prune_empty(item) for item in node]
    return node


def _collect_urls_from_items(
    items: Any, url_field: str = ""
) -> list[str]:
    """把各种形状的结果数组统一成 URL 列表。"""
    urls: list[str] = []
    if items is None:
        return urls
    if isinstance(items, str):
        return [items]
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return urls
    for item in items:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict):
            candidates = (
                [url_field]
                if url_field
                else ["url", "video_url", "image_url", "file_url", "download_url"]
            )
            for key in candidates:
                value = item.get(key)
                if isinstance(value, str) and value:
                    urls.append(value)
                    break
    return urls


#: 内联产物只认 ``data:`` 整体，不限图片 —— 平台照样可能内联返回
#: ``data:video/mp4;base64,…``，而 _fetch_to_local 对任意 data: URI 都能落盘。
_URL_SHAPED = re.compile(r"^(?:https?://|data:)", re.I)


def _find_urls_deep(node: Any, limit: int = 8) -> list[str]:
    """在响应里递归找"长得像地址"的字符串。

    给"只填了一个接口地址、没配 result_path"的用户兜底：只要响应里有一个
    形如 http(s)://… 或 data:… 的字段就取它。

    **只认地址形状的字符串**，不然会把 task_id、status 这类普通字符串也当成产物。
    """
    out: list[str] = []

    def walk(value: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(value, str):
            if _URL_SHAPED.match(value.strip()):
                out.append(value.strip())
            return
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)

    walk(node)
    # 去重但保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for url in out:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _finalize(
    spec: ModelSpec,
    req: GenRequest,
    urls: list[str],
    out_dir: Path | None,
    headers: dict[str, str] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> GenResult:
    """统一下载产物并组装结果。"""
    stem = f"{req.kind}_{_safe(spec.id)}_{time.strftime('%Y%m%d_%H%M%S')}"
    files: list[str] = []
    if out_dir is not None:
        for idx, url in enumerate(urls, start=1):
            try:
                files.append(str(_fetch_to_local(url, out_dir, stem, idx, req.kind, headers)))
            except HttpError as exc:
                # 关键区分：这里是「东西已经生成了、只是没下下来」。
                # 上层据此不熔断、不换模型重跑，并把 urls 原样交回去。
                raise DownloadError(
                    f"产物已生成但下载失败（{idx}/{len(urls)}）：{exc}",
                    urls=urls,
                    files=files,
                ) from exc
    meta = {
        "model": spec.model or spec.id,
        "count": len(urls),
        "requested_count": int(req.count or 1),
        "kind": req.kind,
    }
    # 平台侧水印是**加在成片上、去不掉**的（智谱 CogVideoX 的免费/标准档就是
    # 形如 ..._watermark.mp4 的地址）。这里只做标注，不碰文件本身：
    # 标注缺失时，用户往往要等下完片、打开看了才发现带水印。
    if any(
        "_watermark." in str(u).lower() or "watermark=1" in str(u).lower()
        for u in urls
    ):
        meta["watermark"] = True
    if extra_meta:
        meta.update(extra_meta)
    return GenResult(
        status="ok",
        model_id=spec.id,
        provider=spec.provider,
        files=files,
        urls=urls,
        meta=meta,
    )


def _param_map(spec: ModelSpec, req: GenRequest, keys: list[str]) -> dict[str, Any]:
    """从配置 params 与请求 extra 里挑出关心的字段，请求侧优先。"""
    out: dict[str, Any] = {}
    for key in keys:
        if key in spec.params:
            out[key] = spec.params[key]
    for key in keys:
        if key in req.extra and req.extra[key] not in (None, ""):
            out[key] = req.extra[key]
    return out


#: 由 CLI 专用旗标表达、不该再从 --param 重复发送的键
_CLI_FLAG_KEYS = {"prompt", "negative_prompt", "image", "images", "last_image", "count"}


def leftover_params(req: GenRequest, consumed: set[str] | None = None) -> dict[str, Any]:
    """``--param`` 里适配器还没消费掉的键。

    ``--param`` 的语义是「用户点名要发给这个接口的字段」，所以默认必须透传。
    以前白名单之外的键被静默丢掉：用户填了 seed / watermark / 自建网关的字段，
    平台收不到，日志里也没有任何交代，只能看到「平台没按我说的做」。
    """
    skip = set(_CLI_FLAG_KEYS) | set(consumed or ())
    out: dict[str, Any] = {}
    for key, value in (req.extra or {}).items():
        text = str(key)
        if text in skip or text == "inputs":
            continue
        if value is None or value == "":
            continue
        out[text] = value
    return out


def _flag_value(value: Any) -> str:
    """拼进提示词旗标里的值：布尔按 true/false 写（Python 的 True/False 平台不认）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _waiting(ctx: Ctx, label: str, hint: str = "同步接口，通常 10–60 秒") -> float:
    """同步接口「等平台」期间的进度提示，返回起始时间。

    同步出图/出音频时，提交之后到返回之间是纯等待（实测 30 秒上下），以前这段
    时间 stderr 一行都没有 —— 用户和 agent 都分不清是在跑还是已经卡死。
    视频那边有轮询进度，同步接口也需要一句「已提交，正在等」。
    """
    ctx.log(f"[{label}] 已提交，等待平台响应（{hint}）…")
    return time.time()


def _waited(ctx: Ctx, label: str, started: float, note: str = "") -> None:
    spent = time.time() - started
    suffix = f"，{note}" if note else ""
    ctx.log(f"[{label}] 平台已返回（{spent:.1f}s{suffix}）")


def _merge_leftover(
    target: dict[str, Any],
    req: GenRequest,
    label: str,
    consumed: set[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """把未消费的 ``--param`` 合并进请求体，并留一行日志说明透了哪些字段。"""
    extra = leftover_params(req, consumed)
    if not extra:
        return target
    if log:
        log(f"[{label}] --param 透传：{', '.join(sorted(extra))}")
    target.update(extra)
    return target


def _passthrough_inputs(
    spec: ModelSpec,
    req: GenRequest,
    *,
    log: Callable[[str], None] | None = None,
    default_count_field: str = "",
) -> dict[str, Any]:
    """replicate / fal 这类"把一堆字段塞进同一个对象"的适配器的入参合并。

    这两个分支过去只读 ``req.extra["inputs"]``（一个嵌套字典），但 CLI 的
    ``--param`` 产出的是**扁平** key=value —— 没有任何代码会去造嵌套的 inputs，
    于是文档承诺的"``--param key=value`` 会合并进 input"根本没发生，参数被静默丢弃。
    这里把几种来源按 低 → 高 优先级收口：

        spec.params  <  options.inputs  <  --param（扁平 extra）  <  extra.inputs（嵌套）

    随后补 ``--duration`` / ``--count``。它们的字段名各家不同（replicate 上
    有 ``duration`` / ``num_frames`` / ``video_length`` 多种），所以：

      - 字段名可用 ``options.duration_field`` / ``options.count_field`` 覆盖；
      - 设成 ``none`` / ``false`` 表示显式关闭，此时会**留一行日志**说明被忽略，
        而不是继续静默吃掉用户传的旗标；
      - 优先级遵循 references/providers.md：
        点名字段（``--param duration=N``） > ``--duration N`` > ``params.duration``；
      - **按 kind 收紧**：``--duration`` 只对视频注入、``--count`` 只对图片注入。
        把 ``num_outputs`` / ``num_images`` 这类出图参数塞进视频请求，换来的
        只有 422，所以在 kind 不匹配时宁可不发（并留日志）。
    """
    opts = spec.options if isinstance(spec.options, dict) else {}

    merged: dict[str, Any] = {}
    merged.update(spec.params)

    declared = opts.get("inputs")
    if isinstance(declared, dict):
        merged.update(declared)

    # 扁平 --param，以及（理论上存在的）嵌套 extra.inputs
    flat = {k: v for k, v in req.extra.items() if k != "inputs"}
    merged.update(flat)

    nested = req.extra.get("inputs")
    if isinstance(nested, dict):
        merged.update(nested)

    # 用户"点名"过的字段：--param 或 extra.inputs 里出现过，一律最高优先，
    # 后续的 --duration / --count 兜底不得覆盖它们。
    named = set(flat)
    if isinstance(nested, dict):
        named.update(nested)

    def _resolve_field(key: str, fallback: str) -> str:
        raw = opts.get(key, fallback)
        if raw is None:
            raw = fallback
        if isinstance(raw, bool):
            raw = "" if raw is False else fallback
        text = str(raw).strip()
        if text.lower() in ("none", "false", "null", "-", "off"):
            return ""
        return text

    # ---- 时长（只对视频有意义）----
    # --duration 是视频旗标；把它注入图片请求只会在平台上换个 422。
    d_field = _resolve_field("duration_field", "duration")
    if req.duration and req.kind == "video":
        if not d_field:
            if log:
                log(
                    f"--duration {req.duration} 未生效：本条 options.duration_field 已被关闭。"
                    f"若该模型确实支持，请用 --param <字段名>={req.duration} 点名。"
                )
        elif d_field in named:
            if log:
                log(f"--duration {req.duration} 被 --param {d_field}=… 覆盖（点名字段优先）")
        else:
            merged[d_field] = req.duration
            if log:
                log(
                    f"--duration {req.duration} -> 入参 {d_field}；"
                    f"若该模型字段名与此不同（如 num_frames / video_length），"
                    f"改用 --param <字段名>={req.duration}"
                )
    elif req.duration:
        if log:
            log(f"--duration {req.duration} 未生效：kind={req.kind} 不是视频。")

    # ---- 数量（只对图片有意义）----
    # count 默认值是 1、几乎每次请求都带，所以只在用户明确要求 >1 时才考虑注入；
    # 而且没有任何一个视频接口认这个字段（num_outputs / num_images 都是出图参数），
    # 往视频请求里塞只会换来 422 —— 所以限死在 image。
    c_field = _resolve_field("count_field", default_count_field)
    if req.count and req.count > 1 and req.kind == "image":
        if not c_field:
            if log:
                log(
                    f"--count {req.count} 未生效：本条未声明 options.count_field。"
                    f"请用 --param <字段名>={req.count} 点名（replicate 常见 num_outputs，"
                    f"fal 常见 num_images）。"
                )
        elif c_field in named:
            if log:
                log(f"--count {req.count} 被 --param {c_field}=… 覆盖（点名字段优先）")
        else:
            merged[c_field] = req.count
            if log:
                log(
                    f"--count {req.count} -> 入参 {c_field}；"
                    f"若该模型字段名与此不同，改用 --param <字段名>={req.count}"
                )
    elif req.count and req.count > 1:
        if log:
            log(
                f"--count {req.count} 未生效：kind={req.kind} 不是图片。"
                f"确有多产物需求时请用 --param <字段名>={req.count} 点名。"
            )

    return merged


# ---------------------------------------------------------------- native


def gen_native(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    """内置工具通道：不调 HTTP，返回给 agent 的委托指令。"""
    tool = "ImageGen" if req.kind == "image" else "VideoGen"
    arguments: dict[str, Any] = {"prompt": req.prompt}
    if req.kind == "image":
        size = req.size or spec.params.get("size")
        if size:
            arguments["size"] = size
        if req.extra.get("quality"):
            arguments["quality"] = req.extra["quality"]
        if req.extra.get("style"):
            arguments["style"] = req.extra["style"]
        for idx, image in enumerate(req.images[:3], start=1):
            arguments[f"image{idx}"] = image
    else:
        if req.images:
            arguments["image"] = req.images[0]
        if req.extra.get("last_image"):
            arguments["last_image"] = req.extra["last_image"]
        if req.aspect_ratio:
            arguments["aspect_ratio"] = req.aspect_ratio
        if req.negative_prompt:
            arguments["negative_prompt"] = req.negative_prompt
        resolution = spec.params.get("resolution") or req.extra.get("resolution")
        if resolution:
            arguments["resolution"] = resolution

    note = (
        f"模型 {spec.id} 走内置工具通道，没有生成任何文件。"
        f"如果当前环境有内置的{'出图' if req.kind == 'image' else '出视频'}工具，"
        f"直接用上述 arguments 调用（工具名可能是 {tool}）；"
        f"如果没有，不要猜测工具名，直接告诉用户需要在配置页添加一个真实的第三方模型。"
    )
    if req.count and req.count > 1:
        note += (
            f"用户要求生成 {req.count} 个{ '图片' if req.kind == 'image' else '视频' }："
            f"内置工具支持批量就按数量生成，不支持就逐次调用 {req.count} 次，不要只出一份。"
        )

    return GenResult(
        status="delegate",
        model_id=spec.id,
        provider="native",
        delegate={
            "tool": tool,
            "arguments": arguments,
            "note": note,
        },
    )


# ---------------------------------------------------------------- openai 兼容


#: 智谱（CogView / CogVideoX）的 base。视频走异步任务：提交到
#: ``{base}/videos/generations``，再用 ``{base}/async-result/{id}`` 查结果。
#: 不少 OpenAI 兼容网关的视频接口照抄了这套协议，所以 provider=openai +
#: kind: video 统一按它走。
ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
OPENAI_VIDEO = f"{ZHIPU_BASE}/videos/generations"


def _openai_video_target(spec: ModelSpec) -> tuple[str, str]:
    """异步视频的 (提交地址, 任务查询前缀)。

    查询地址必须跟着提交地址走：提交是 ``{base}/videos/generations``、查询是
    ``{base}/async-result/{id}``，切掉最后一段就得到 base。给智谱配了中转/自建
    网关时提交走网关、查询却直连 open.bigmodel.cn —— 要么被拦、要么悄悄绕过
    网关，报错只剩一个连接超时，完全指不到原因。

    推不出来时（endpoint 里没有 ``/videos/generations`` 这一段）**宁可退回官方
    地址**，也不拼一个"看起来对、其实是错的"地址，后者只会得到更迷惑的 404。
    这种情形用 ``options.task_base`` 显式指定。
    """
    endpoint = str(spec.endpoint or "").strip().rstrip("/") or OPENAI_VIDEO
    explicit = str(spec.options.get("task_base") or "").strip()
    if explicit:
        return endpoint, explicit.rstrip("/")
    head = endpoint.split("/videos/generations", 1)[0].rstrip("/")
    if head and head != endpoint:
        return endpoint, head
    return endpoint, ZHIPU_BASE


def _gen_openai_video(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """OpenAI 风格的**异步**视频接口（智谱 CogVideoX，及照抄这套协议的网关）。

    没有这个分支时，provider=openai 的视频模型会落进下面的图像逻辑：请求发去
    /images/generations，拿回来的 VideoResult 里没有 ``data``，于是报一句
    "响应中没有图片数据" —— 用户看到的是"我明明配了视频模型"，却查不到原因。
    """
    url, task_base = _openai_video_target(spec)
    if spec.endpoint and task_base == ZHIPU_BASE and "/videos/generations" not in str(
        spec.endpoint
    ):
        ctx.log(
            "[openai] endpoint 是自定义地址但里面没有 /videos/generations 这一段，"
            "推不出任务查询地址，已回退到智谱官方地址。如果这里配的是中转/网关，"
            "请在模型的 options.task_base 里显式写上查询地址的前缀。"
        )

    headers = {"Authorization": f"Bearer {ctx.api_key}"}
    body: dict[str, Any] = {"model": spec.model or spec.id, "prompt": req.prompt}
    if req.negative_prompt:
        body["negative_prompt"] = req.negative_prompt
    if req.images:
        body["image_url"] = _image_value(req.images[0])
    body.update(spec.params)
    body.update(
        _param_map(
            spec, req, ["size", "quality", "with_audio", "fps", "watermark", "seed"]
        )
    )
    req_size = req.size or req.aspect_ratio
    if req_size and "size" not in body:
        body["size"] = req_size
    # 时长必须**单独**取：CLI 的 --duration 落在 req.duration 上，而 _param_map
    # 只认 spec.params / req.extra —— 光靠它会把 --duration 静默丢掉（与百炼
    # 视频分支同一个形状的坑）。优先级：--param duration= > --duration > params。
    duration = req.extra.get("duration")
    if duration in (None, ""):
        duration = req.duration
    # 用真值判断而不是 `not in (None, "")`：0 既不等于 None 也不等于 ""，
    # 那个写法会把 duration: 0 一路发出去。0 秒的视频没有意义，视为没指定。
    if duration:
        body["duration"] = duration
    # 白名单之外的 --param 也照样发：用户点名要的字段，静默丢掉只会让人
    # 以为"平台不支持"。真正不被接受时平台会回 400，信息比"没发出去"有用。
    _merge_leftover(
        body,
        req,
        "openai-video",
        consumed={"size", "quality", "with_audio", "fps", "watermark", "seed", "duration"},
        log=ctx.log,
    )

    ctx.log(f"[openai] video submit -> {url}")
    payload = (
        request("POST", url, headers=headers, json_body=body, timeout=ctx.timeout).json()
        or {}
    )
    if isinstance(payload, dict) and payload.get("error"):
        # OpenAI 风格的 error 是 {"message": …}，直接 str 出来是一坨 dict，
        # 用户得自己从花括号里找那句话。能取到 message 就只给 message。
        raise HttpError(
            f"provider 返回错误：{_dig(payload, 'error.message') or payload['error']}",
            None,
            json.dumps(payload, ensure_ascii=False),
        )

    # 有的网关是同步返回的：提交就把产物给了，没有 task_id 可轮询。
    task_id = _dig(payload, "id") or _dig(payload, "task_id") or _dig(payload, "output.task_id")
    if not task_id:
        found = _collect_urls_from_items(
            _dig(payload, "video_result") or _dig(payload, "data")
        )
        if found:
            return _finalize(spec, req, found, req.output_dir)
        raise HttpError(
            f"未拿到任务 id：{json.dumps(payload, ensure_ascii=False)[:300]}"
        )

    poll_url = f"{task_base}/async-result/{task_id}"

    def probe() -> list[str] | None:
        data = (
            request("GET", poll_url, headers=headers, timeout=60).json() or {}
        )
        status = str(
            data.get("task_status") or _dig(data, "output.task_status", "") or ""
        ).upper()
        if status in ("", "PROCESSING", "PENDING", "RUNNING", "QUEUING", "WAITING", "CREATED"):
            return None
        if status in ("FAILED", "FAILURE", "CANCELED", "CANCELLED", "ERROR"):
            reason = (
                _dig(data, "error.message")
                or _dig(data, "message")
                or _dig(data, "output.message")
                or json.dumps(data, ensure_ascii=False)[:200]
            )
            raise HttpError(f"任务失败：{reason}")
        found = _collect_urls_from_items(
            data.get("video_result") or _dig(data, "data")
        )
        if not found:
            single = (
                _dig(data, "output.video_url")
                or _dig(data, "video.url")
                or _dig(data, "url")
            )
            if single:
                found = [single]
        if not found:
            raise HttpError("任务成功但没解析出产物 URL")
        return found

    ctx.log(f"[openai] video task {task_id} 已提交，开始轮询")
    urls = poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll, log=ctx.log)
    return _finalize(spec, req, urls, req.output_dir, extra_meta={"task_id": task_id})


OPENAI_SPEECH = "https://api.openai.com/v1/audio/speech"

#: `/audio/speech` 这条协议各家的实现并不统一：默认音色、能收的格式都不一样，
#: 而且**参数校验发生在计费之前** —— 默认值一旦不对，这个模型永远出不了声，
#: 报的还是「音色不存在」「不支持当前 response_format 值」这种看着像配置错的 400。
#: 实测（2026-10，用真实密钥打 0 元探测）：
#:   * 智谱 glm-tts：voice 只认 tongtong/chuichui/xiaochen…，传 OpenAI 的 "alloy"
#:     直接 400 1214「音色不存在」；response_format 只收 wav / pcm，mp3 同样 400。
#: 所以按域名给默认值，认不出来的域名保持 OpenAI 那一套（官方和多数中转站都吃）。
_SPEECH_HOST_DEFAULTS: dict[str, dict[str, str]] = {
    "open.bigmodel.cn": {"voice": "tongtong", "format": "wav"},
}

#: 音色必须写成 "<模型名>:<音色名>" 的平台。文档里 CosyVoice2 / fish-speech /
#: MOSS-TTSD 都提供 alex，拿它当默认最稳；用户想换就写 params.voice。
_SPEECH_PREFIXED_VOICE_HOSTS = {"api.siliconflow.cn"}


def _speech_voice(url: str, spec: ModelSpec, params: dict[str, Any]) -> str:
    """音色的取值顺序：用户显式给了就用；否则按域名取默认；再否则 alloy。"""
    explicit = str(params.pop("voice", "") or "").strip()
    if explicit:
        return explicit
    host = urllib.parse.urlparse(url).netloc.lower()
    preset = (_SPEECH_HOST_DEFAULTS.get(host) or {}).get("voice")
    if preset:
        return preset
    if host in _SPEECH_PREFIXED_VOICE_HOSTS:
        return f"{spec.model or spec.id}:alex"
    return "alloy"


def _speech_format(url: str, params: dict[str, Any]) -> str:
    """格式同理：用户显式给了就用（response_format 优先于 format），否则按域名默认。

    两个键都要 pop：以前写成 `pop("response_format") or pop("format")`，
    只要前者有值，`format` 就留在 params 里，最后被 payload.update 原样发给接口 ——
    OpenAI 不认 `format` 这个字段，严格一点的网关会直接 400。
    """
    explicit = str(params.pop("response_format", "") or "").strip()
    fallback = str(params.pop("format", "") or "").strip()
    if explicit or fallback:
        return explicit or fallback
    host = urllib.parse.urlparse(url).netloc.lower()
    return (_SPEECH_HOST_DEFAULTS.get(host) or {}).get("format") or "mp3"


def _openai_speech_url(spec: ModelSpec) -> str:
    """从用户填的生成地址推 ``/audio/speech``；推不出来就用官方地址。

    和图像/视频同样的思路：网关地址各不相同，但路径尾巴是有规律的，
    把已知的生成后缀换成 /audio/speech 即可，不必让用户再填一个 endpoint。
    """
    endpoint = str(spec.endpoint or "").strip().rstrip("/")
    if not endpoint:
        return OPENAI_SPEECH
    if endpoint.endswith("/audio/speech"):
        return endpoint
    for suffix in (
        "/images/generations",
        "/images/edits",
        "/videos/generations",
        "/video/generations",
        "/chat/completions",
    ):
        if endpoint.endswith(suffix):
            return endpoint[: -len(suffix)] + "/audio/speech"
    return endpoint + "/audio/speech"


def _gen_openai_audio(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """OpenAI 兼容的语音合成：``/v1/audio/speech`` **直接吐二进制**，没有 URL 可下。

    这条通路不能复用 ``_finalize``（它按「URL 列表」下载），所以要自己落盘。
    另外它失败时也可能回 200 + JSON 错误体，这里显式认一下 ——
    否则会把一段 JSON 存成 .mp3，用户点开才发现是报错文本。
    """
    url = _openai_speech_url(spec)
    params: dict[str, Any] = {**spec.params, **req.extra}
    # 先算格式和音色：两者都要按**实际打到的域名**取默认值，不能写死 OpenAI 那一套。
    fmt = _speech_format(url, params)
    voice = _speech_voice(url, spec, params)
    payload: dict[str, Any] = {
        "model": spec.model or spec.id,
        "input": req.prompt,
        "voice": voice,
        "response_format": fmt,
    }
    payload.update({key: value for key, value in params.items() if value not in (None, "")})

    if req.output_dir is None:
        # --no-download 的语义是「只取远程 URL」，可这条通路根本没有 URL，
        # 不落盘就等于什么都没拿到。与其静默返回空结果，不如说清楚。
        # 放在发请求**之前**：别为了一个注定拿不到东西的调用去花额度。
        # 归 ConfigError（不是 HttpError）：参数用法错了，不算模型故障。
        raise ConfigError(
            "OpenAI 语音接口直接返回音频二进制、没有可回传的 URL，"
            "不能和 --no-download 一起用；去掉该参数即可正常落盘"
        )

    ctx.log(f"[openai] audio speech -> {url}（{fmt}）")
    stem = f"{req.kind}_{_safe(spec.id)}_{time.strftime('%Y%m%d_%H%M%S')}"
    files: list[str] = []
    started = _waiting(ctx, "openai-speech", hint="同步接口，返回音频二进制")
    for index in range(1, max(1, int(req.count or 1)) + 1):
        if (req.count or 1) > 1:
            ctx.log(f"[openai-speech] 第 {index}/{int(req.count)} 段…")
        resp = request(
            "POST", url,
            headers={"Authorization": f"Bearer {ctx.api_key}"},
            json_body=payload, timeout=ctx.timeout,
        )
        data = resp.body or b""
        if not data:
            raise HttpError("语音接口返回了空响应体")
        if data[:1] in (b"{", b"["):
            raise HttpError(
                "语音接口返回的是 JSON 而不是音频（多半是网关报错但状态码是 200）："
                + data[:200].decode("utf-8", "replace")
            )
        # 扩展名优先按真实文件头；认不出来时按声明的格式推，
        # 不能一律 .mp3 —— 智谱只收 wav/pcm，pcm 是裸采样、存成 .mp3 谁也不知道该怎么放。
        ext = sniff_extension(data[:32]) or {
            "wav": ".wav", "pcm": ".pcm", "opus": ".opus", "flac": ".flac",
        }.get(fmt, ".mp3")
        dest = _unique_path(Path(req.output_dir) / f"{stem}_{index:02d}{ext}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if ext == ".wav":
            _repair_wav_sizes(dest)
        files.append(str(dest))

    _waited(ctx, "openai-speech", started, f"{len(files)} 段")
    return GenResult(
        status="ok",
        model_id=spec.id,
        provider=spec.provider,
        files=files,
        urls=[],
        meta={
            "model": spec.model or spec.id,
            # 这个通路是循环发 count 次、每段独立落盘的，count 必须按实际产物数写；
            # 以前写死 1，`--count 2` 出了两段却报 count=1。
            "count": len(files),
            "requested_count": int(req.count or 1),
            "kind": req.kind,
            "sync": True,
            "response_format": fmt,
        },
    )


def gen_openai(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    """OpenAI 风格接口：图像走 ``/v1/images/generations`` 与 ``/v1/images/edits``，
    视频走异步任务（见 ``_gen_openai_video``），语音走 ``/v1/audio/speech``。

    SiliconFlow、Together、以及大多数自建网关都兼容这套协议，
    只要在配置里改 endpoint 就能复用。
    """
    if req.kind == "audio":
        return _gen_openai_audio(spec, req, cfg, ctx)
    if req.kind == "video":
        return _gen_openai_video(spec, req, cfg, ctx)
    headers = {"Authorization": f"Bearer {ctx.api_key}"}
    # 有输入图就走 /edits（改图）。原来的写法是
    # `bool(req.images) and ("img2img" in req.requires or … or bool(req.images))` ——
    # 括号里那串判断恒等于 bool(req.images)，纯属噪音，别让读者误以为
    # requires 参与了这个决定。
    edit_mode = bool(req.images)

    if edit_mode:
        url = spec.endpoint or "https://api.openai.com/v1/images/generations"
        if "/generations" in url:
            url = url.replace("/generations", "/edits")
        form: dict[str, Any] = {
            "model": spec.model or spec.id,
            "prompt": req.prompt,
            "n": req.count,
        }
        form.update(_param_map(spec, req, ["size", "quality", "background", "input_fidelity"]))
        _merge_leftover(
            form,
            req,
            "openai-edits",
            consumed={"size", "quality", "background", "input_fidelity"},
            log=ctx.log,
        )
        files = {}
        for idx, image in enumerate(req.images[:4]):
            name, data, ctype = read_upload(image)
            files["image" if idx == 0 else f"image[{idx}]"] = (name, data, ctype)
        ctx.log(f"[openai] edits -> {url}")
        started = _waiting(ctx, "openai-edits")
        resp = request(
            "POST", url, headers=headers, form=form, files=files, timeout=ctx.timeout
        )
        _waited(ctx, "openai-edits", started)
    else:
        url = spec.endpoint or "https://api.openai.com/v1/images/generations"
        body: dict[str, Any] = {
            "model": spec.model or spec.id,
            "prompt": req.prompt,
            "n": req.count,
        }
        body.update(spec.params)
        body.update(_param_map(spec, req, ["size", "quality", "style", "background"]))
        _merge_leftover(
            body,
            req,
            "openai-image",
            consumed={"size", "quality", "style", "background"},
            log=ctx.log,
        )
        ctx.log(f"[openai] generations -> {url}")
        started = _waiting(ctx, "openai-image")
        resp = request("POST", url, headers=headers, json_body=body, timeout=ctx.timeout)
        _waited(ctx, "openai-image", started)

    payload = resp.json() or {}
    if isinstance(payload, dict) and payload.get("error"):
        raise HttpError(
            f"provider 返回错误：{payload['error']}",
            None,
            json.dumps(payload, ensure_ascii=False),
        )

    items = payload.get("data") or []
    urls: list[str] = []
    local_from_b64: list[str] = []
    inline_without_dir = False
    out_dir = req.output_dir
    stem = f"{req.kind}_{_safe(spec.id)}_{time.strftime('%Y%m%d_%H%M%S')}"
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        if item.get("url"):
            urls.append(item["url"])
        elif item.get("b64_json"):
            if out_dir is not None:
                local_from_b64.append(
                    str(_write_b64_to_local(item["b64_json"], out_dir, stem, idx, req.kind))
                )
            else:
                inline_without_dir = True

    if local_from_b64 and not urls:
        return GenResult(
            status="ok",
            model_id=spec.id,
            provider=spec.provider,
            files=local_from_b64,
            urls=[],
            meta={"model": spec.model or spec.id, "count": len(local_from_b64), "kind": req.kind},
        )
    if not urls:
        if inline_without_dir:
            # 计费已经发生，但这条通路没有 URL 可回传。这是「用法问题」，
            # 归 ConfigError：不写健康度、不冷却模型 —— 下次去掉 --no-download 就好。
            raise ConfigError(
                "这个接口返回的是内联 base64 图片（b64_json），没有可回传的 URL，"
                "不能和 --no-download 一起用；去掉该参数即可正常落盘。"
                "（本次请求平台的计费通常已经发生）"
            )
        raise HttpError(f"响应中没有图片数据：{json.dumps(payload)[:300]}")
    return _finalize(spec, req, urls, out_dir, extra_meta={"usage": payload.get("usage")})


# ---------------------------------------------------------------- dashscope


DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/api/v1"
DASHSCOPE_IMAGE = f"{DASHSCOPE_BASE}/services/aigc/text2image/image-synthesis"
DASHSCOPE_VIDEO = f"{DASHSCOPE_BASE}/services/aigc/video-generation/video-synthesis"
#: qwen-image 系列（qwen-image / qwen-image-2.0 / qwen-image-edit 等）不走 wanx 异步接口，
#: 走多模态同步接口，请求体是 messages 格式。用错接口会得到 400 "url error"。
DASHSCOPE_QWEN_IMAGE = f"{DASHSCOPE_BASE}/services/aigc/multimodal-generation/generation"
#: 语音合成。**两套端点互不通用**（官方原话「端点不可混用」）：
#:   * qwen-audio-*-tts-* / cosyvoice-*   → /services/audio/tts/SpeechSynthesizer
#:   * qwen3-tts-*                        → /services/aigc/multimodal-generation/generation
#: 拿 qwen-audio 去撞多模态端点，得到的是 "url error, please check url" —— 看着像
#: 地址填错，其实是模型和端点不是一对。
DASHSCOPE_TTS = f"{DASHSCOPE_BASE}/services/audio/tts/SpeechSynthesizer"


def _is_qwen_image(spec: ModelSpec) -> bool:
    """qwen-image 系列的模型名以 qwen-image 开头（qwen-image、qwen-image-2.0、qwen-image-edit…）。"""
    name = (spec.model or spec.id).lower()
    return name.startswith("qwen-image") or name.startswith("qwen-vl") and "image" in name


def _is_qwen3_tts(spec: ModelSpec) -> bool:
    """qwen3-tts / qwen-tts 系列走多模态端点，其余 TTS（qwen-audio-*-tts、cosyvoice）走 SpeechSynthesizer。"""
    name = (spec.model or spec.id).lower()
    return name.startswith("qwen3-tts") or name.startswith("qwen-tts")


def gen_dashscope(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    """阿里云百炼 / 通义万相。qwen-image 系列走同步多模态接口，wanx 系列走异步任务。"""
    if req.kind == "audio":
        return _gen_dashscope_audio(spec, req, cfg, ctx)
    if req.kind == "image" and _is_qwen_image(spec):
        return _gen_dashscope_qwen_image(spec, req, cfg, ctx)
    return _gen_dashscope_wanx(spec, req, cfg, ctx)


def _gen_dashscope_audio(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """百炼语音合成：一次 POST 拿音频 URL，同步返回。

    ``text_prompt`` 这个字段名是**实测**出来的，不是文档抄的：官方文档写的是
    ``input.text``，但 qwen-audio-3.1-tts-next 收到 ``input.text`` 直接回
    ``InvalidParameter: text_prompt must not be empty.``。所以这里两个字段都送 ——
    对认 ``text`` 的模型（cosyvoice 等）和认 ``text_prompt`` 的模型都成立，
    平台会忽略多余的那个；只认一个的话，换模型就静默失效。

    voice / format / sample_rate 全部**可以不填**：实测裸最小体
    ``{"model":..., "input": {"text_prompt": "你好"}}`` 就返回 200，
    默认是 wav。所以这里不硬编码任何音色名 —— 硬编码一个音色，换模型就 400。
    """
    endpoint = str(spec.endpoint or "").strip()
    if not endpoint:
        endpoint = DASHSCOPE_QWEN_IMAGE if _is_qwen3_tts(spec) else DASHSCOPE_TTS

    payload_input: dict[str, Any] = (
        {"text": req.prompt} if _is_qwen3_tts(spec) else {"text": req.prompt, "text_prompt": req.prompt}
    )
    for key, value in {**spec.params, **req.extra}.items():
        if value not in (None, ""):
            payload_input[str(key)] = value

    ctx.log(f"[dashscope-tts] {endpoint}")
    urls: list[str] = []
    meta: dict[str, Any] = {"sync": True}
    # --count N 对语音同样要生效：之前固定只发一次请求，传了 --count 2 也只得到
    # 一段音频，而 JSON 里的 count 还是 1 —— 用户会以为是平台没返回。
    for _ in range(max(1, int(req.count or 1))):
        payload = request(
            "POST", endpoint,
            headers={
                "Authorization": f"Bearer {ctx.api_key}",
                "Content-Type": "application/json",
            },
            json_body={"model": spec.model or spec.id, "input": payload_input},
            timeout=ctx.timeout,
        ).json() or {}

        audio = _dig(payload, "output.audio") or {}
        url = str(audio.get("url") or "").strip()
        if not url:
            # 流式模式才会把音频塞进 data；非流式理论上一定有 url，
            # 但中转网关偶尔只回 base64，兜一下再报错
            b64 = str(audio.get("data") or "").strip()
            if b64:
                fmt = str(payload_input.get("format") or "wav")
                url = f"data:audio/{fmt};base64,{b64}"
        if not url:
            raise HttpError(
                f"语音接口没返回音频地址：{json.dumps(payload, ensure_ascii=False)[:300]}"
            )
        urls.append(url)
        if "duration_seconds" not in meta and audio.get("duration") not in (None, ""):
            try:
                meta["duration_seconds"] = round(float(audio["duration"]), 2)
            except (TypeError, ValueError):
                pass
        if audio.get("id") and "audio_id" not in meta:
            meta["audio_id"] = str(audio["id"])

    return _finalize(spec, req, urls, req.output_dir, extra_meta=meta)


def _dashscope_size(value: str) -> str:
    """qwen-image / wanx 的 size 都要求 width*height（星号分隔），把 WxH 归一化。

    用户侧习惯写 1536x1024（x），DashScope 只认 1536*1024（*）。不转的话部分端点
    报 400，部分端点静默忽略 —— 服务器不报错、直接给一张默认方图，比报错更坑。
    不匹配 WxH 的值（如 16:9 比例）原样透传。
    """
    v = str(value).strip()
    if re.fullmatch(r"\d{2,5}[xX]\d{2,5}", v):
        return v.replace("x", "*").replace("X", "*")
    return v


def _dashscope_task_base(spec: ModelSpec) -> tuple[str, bool]:
    """任务**查询**地址的 base，外加"是不是从配置推出来的"。

    查询地址必须跟着提交地址走。百炼的提交路径是 ``{base}/services/aigc/…``、
    查询是 ``{base}/tasks/{id}``，所以从提交地址里切掉 ``/services/…`` 就得到 base。

    为什么非做不可：给百炼配了中转/自建网关时，提交走网关、查询却直连
    dashscope.aliyuncs.com —— 要么被拦、要么悄悄绕过网关。而且报错只是一个
    连接超时，完全指不到原因。

    推不出来时（endpoint 里没有 ``/services/`` 这一段）**宁可退回官方地址**，
    也不要拼一个"看起来对、其实是错的"地址 —— 后者会得到一个更迷惑的 404。
    这种情形用 ``options.task_base`` 显式指定。
    """
    explicit = str(spec.options.get("task_base") or "").strip()
    if explicit:
        return explicit.rstrip("/"), True
    endpoint = str(spec.endpoint or "").strip()
    if endpoint:
        head = endpoint.split("/services/", 1)[0].rstrip("/")
        if head and head != endpoint.rstrip("/"):
            return head, True
    return DASHSCOPE_BASE, False


def _dashscope_qwen_url(spec: ModelSpec) -> str:
    """qwen-image 的提交地址。

    厂商的 ``endpoints.image`` 只能写一个，写的是 wanx 的异步接口（目录默认值就是它），
    而 qwen-image 走的是多模态同步接口。配置加载时会按 catalog 把厂商省略的地址补回
    ``spec.endpoint``，于是这里拿到的往往是 text2image 那个 —— 直接用会得到
    400 "url error"。

    所以按 ``/services/`` 之前的 base 重拼多模态路径，中转网关的路径前缀原样保留。
    ``endpoint`` 里没有 ``/services/`` 这一段时，说明用户指的就是多模态地址本身
    （或另一套网关），原样用，不做猜测。
    """
    endpoint = str(spec.endpoint or "").strip()
    if not endpoint:
        return DASHSCOPE_QWEN_IMAGE
    head = endpoint.split("/services/", 1)[0].rstrip("/")
    if not head or head == endpoint.rstrip("/"):
        return endpoint
    return f"{head}/services/aigc/multimodal-generation/generation"


def _gen_dashscope_qwen_image(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """qwen-image：同步多模态接口（messages 进、choices[].message.content[].image 出）。"""
    url = _dashscope_qwen_url(spec)
    content: list[dict[str, Any]] = []
    if req.prompt:
        content.append({"text": req.prompt})
    if req.negative_prompt:
        content.append({"negative_prompt": req.negative_prompt})
    if req.images:
        # 图生图/改图：本地路径转 base64，URL 直接传
        for img in req.images:
            content.append({"image": _image_value(img)})
    parameters: dict[str, Any] = {}
    parameters.update(spec.params)
    parameters.update(_param_map(spec, req, ["size"]))
    # CLI 的 --size 落在 req.size、--aspect-ratio 落在 req.aspect_ratio。
    # 优先级：本次请求 > 模型配置 params > 不指定（平台默认）。
    req_size = req.size or req.aspect_ratio
    if req_size:
        parameters["size"] = _dashscope_size(req_size)
    elif "size" in parameters:
        parameters["size"] = _dashscope_size(parameters["size"])
    if parameters.get("size") in ("", None):
        parameters.pop("size", None)
    _merge_leftover(parameters, req, "dashscope-qwen", consumed={"size"}, log=ctx.log)
    body: dict[str, Any] = {
        "model": spec.model or spec.id,
        "input": {"messages": [{"role": "user", "content": content}]},
    }
    if parameters:
        body["parameters"] = parameters

    ctx.log(f"[dashscope-qwen] {url}")
    urls: list[str] = []
    started = _waiting(ctx, "dashscope-qwen", hint=f"同步接口，共 {max(1, int(req.count or 1))} 次请求")
    # 多模态同步接口没有 n / num_images 这类批量参数，`--count N` 只能**逐个提交**。
    # 以前这里固定只发一次，用户要 3 张却只拿到 1 张，status 还是 ok。
    total = max(1, int(req.count or 1))
    for _index in range(total):
        if total > 1:
            ctx.log(f"[dashscope-qwen] 第 {_index + 1}/{total} 张…")
        payload = request(
            "POST", url,
            headers={
                "Authorization": f"Bearer {ctx.api_key}",
                "Content-Type": "application/json",
            },
            json_body=body, timeout=ctx.timeout,
        ).json() or {}

        found: list[str] = []
        for choice in _dig(payload, "output.choices") or []:
            for item in _dig(choice, "message.content") or []:
                if isinstance(item, dict):
                    u = item.get("image") or item.get("image_url")
                    if u:
                        found.append(str(u))
        if not found:
            raise HttpError(
                f"qwen-image 未返回图片：{json.dumps(payload, ensure_ascii=False)[:300]}"
            )
        urls.extend(found)
    _waited(ctx, "dashscope-qwen", started, f"{len(urls)} 张")
    return _finalize(
        spec, req, urls, req.output_dir, extra_meta={"sync": True, "requested_count": int(req.count or 1)}
    )


def _image_value(path_or_url: str, *, base64_only: bool = False) -> str:
    """把图片输入统一成接口能收的形式：http(s)/data URI 原样，本地文件转 base64。

    ``--image`` 的承诺是「绝对路径或 URL」，但把本地路径原样塞进请求体时，
    平台只会把它当成一个不存在的 URL（400/422「图片下载失败」）。这里统一转换。

    ``base64_only=True`` 给可灵那种「只收裸 Base64、不收 ``data:`` 前缀」的字段用；
    其余通路一律给 data URI —— OpenAI / Replicate / fal / 火山方舟都认这个形状。
    """
    text = str(path_or_url or "").strip()
    if text.lower().startswith(("http://", "https://")):
        return text
    if text.startswith("data:"):
        if not base64_only:
            return text
        # 可灵只收裸 Base64：用户直接给 data URI 时要把前缀去掉
        return text.split(",", 1)[-1]
    if not text:
        raise ConfigError("--image/参数里的图片是空的：请给图片的绝对路径或 http(s) URL")
    path = Path(text).expanduser()
    if not path.is_file():
        raise ConfigError(
            f"输入的图片不存在：{path}；--image 要的是图片的绝对路径或 http(s) URL"
        )
    import base64
    import mimetypes

    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode()
    return data if base64_only else f"data:{mime};base64,{data}"


def _gen_dashscope_wanx(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """wanx 系列：异步任务（提交拿 task_id，再轮询）。"""
    headers = {
        "Authorization": f"Bearer {ctx.api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }

    if req.kind == "video":
        url = spec.endpoint or DASHSCOPE_VIDEO
        inputs: dict[str, Any] = {"prompt": req.prompt}
        if req.negative_prompt:
            inputs["negative_prompt"] = req.negative_prompt
        if req.images:
            inputs["img_url"] = _image_value(req.images[0])
        parameters: dict[str, Any] = {}
        parameters.update(spec.params)
        parameters.update(_param_map(spec, req, ["size", "resolution", "prompt_extend", "watermark"]))
        # 时长必须**单独**取：CLI 的 --duration 落在 req.duration 上，而 _param_map
        # 只认 spec.params / req.extra —— 光靠它会把 --duration 静默丢掉（火山分支
        # 用 `req.duration or spec.params.get("duration")` 兜住了，这段漏了），
        # 于同一条 --duration 换个平台就失效，而且不报任何错。
        # 优先级按"越具体越优先"：--param duration=（点名了字段）
        #   > --duration（专用旗标）> 条目里的 params.duration（已在上面打底）。
        duration = req.extra.get("duration")
        if duration in (None, ""):
            duration = req.duration
        # 这里必须用**真值**判断，不能写 `if duration not in (None, "")`：
        # 0 既不等于 None 也不等于 ""，所以 `0 in (None, "")` 是 False ——
        # 那个写法会把 `duration: 0` 一路发出去，还会把条目里的
        # params.duration 覆盖成 0（就是 M3 那个坑的同一个形状，自检抓住了）。
        # 0 秒的视频没有意义，一律视为"没指定"。
        if duration:
            parameters["duration"] = duration
        req_size = req.size or req.aspect_ratio
        if req_size and "size" not in parameters:
            parameters["size"] = _dashscope_size(req_size)
        elif "size" in parameters:
            parameters["size"] = _dashscope_size(parameters["size"])
        _merge_leftover(
            parameters,
            req,
            "dashscope-video",
            consumed={"size", "resolution", "prompt_extend", "watermark", "duration"},
            log=ctx.log,
        )
        body = {"model": spec.model or spec.id, "input": inputs, "parameters": parameters}
    else:
        url = spec.endpoint or DASHSCOPE_IMAGE
        inputs = {"prompt": req.prompt}
        if req.negative_prompt:
            inputs["negative_prompt"] = req.negative_prompt
        parameters = {"n": req.count}
        parameters.update(spec.params)
        parameters.update(_param_map(spec, req, ["size", "style", "prompt_extend", "watermark"]))
        req_size = req.size or req.aspect_ratio
        if req_size:
            parameters["size"] = _dashscope_size(req_size)
        elif "size" in parameters:
            parameters["size"] = _dashscope_size(parameters["size"])
        _merge_leftover(
            parameters,
            req,
            "dashscope-image",
            consumed={"size", "style", "prompt_extend", "watermark"},
            log=ctx.log,
        )
        body = {"model": spec.model or spec.id, "input": inputs, "parameters": parameters}

    ctx.log(f"[dashscope] submit -> {url}")
    payload = request(
        "POST", url, headers=headers, json_body=body, timeout=ctx.timeout
    ).json() or {}
    task_id = _dig(payload, "output.task_id")
    if not task_id:
        raise HttpError(f"未拿到 task_id：{json.dumps(payload, ensure_ascii=False)[:300]}")

    task_base, derived = _dashscope_task_base(spec)
    if spec.endpoint and not derived:
        ctx.log(
            "[dashscope] endpoint 是自定义地址，但里面没有 /services/ 这一段，"
            "推不出任务查询地址，已回退到官方地址。如果这里配的是中转/网关，"
            "请在模型的 options.task_base 里显式写上查询地址的前缀。"
        )
    poll_url = f"{task_base}/tasks/{task_id}"

    def probe() -> list[str] | None:
        data = request(
            "GET", poll_url, headers={"Authorization": f"Bearer {ctx.api_key}"}, timeout=60
        ).json() or {}
        status = str(_dig(data, "output.task_status", "")).upper()
        if status in ("PENDING", "RUNNING", "UNKNOWN", ""):
            return None
        if status in ("FAILED", "CANCELED"):
            raise HttpError(
                f"任务失败：{_dig(data, 'output.message', '') or json.dumps(data)[:200]}"
            )
        found = _collect_urls_from_items(_dig(data, "output.results"))
        if not found:
            single = _dig(data, "output.video_url") or _dig(data, "output.url")
            found = [single] if single else []
        if not found:
            raise HttpError("任务成功但没解析出产物 URL")
        return found

    ctx.log(f"[dashscope] task {task_id} 已提交，开始轮询")
    urls = poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll, log=ctx.log)
    return _finalize(
        spec, req, urls, req.output_dir, extra_meta={"task_id": task_id}
    )


# ---------------------------------------------------------------- volcengine


ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
ARK_IMAGE = f"{ARK_BASE}/images/generations"
ARK_VIDEO_TASKS = f"{ARK_BASE}/contents/generations/tasks"


#: 火山「豆包语音」的 TTS 走的是**另一个服务**（openspeech.bytedance.com），
#: 不在方舟（ark.cn-beijing.volces.com）下面：域名、鉴权、请求体全都不一样。
#: V3 单向流式：一次性送文本，返回一串 JSON，每段 data 是 base64 音频。
VOLC_TTS = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
#: X-Api-Resource-Id 决定音色档位与计费方式，缺了直接 400。
VOLC_TTS_DEFAULT_RESOURCE = "seed-tts-2.0"
#: 允许直接透传进 audio_params 的键（语速/音量/比特率等）。
_VOLC_AUDIO_KEYS = ("format", "sample_rate", "bit_rate", "speech_rate", "loudness_rate")
#: 属于 req_params（而不是 audio_params）的可选键：模型版本、自然语言指令。
_VOLC_REQ_KEYS = ("model", "voice_instruction")
#: 火山支持的格式里只有 ogg_opus 不能直接当扩展名用。
_VOLC_EXT = {"mp3": ".mp3", "wav": ".wav", "pcm": ".pcm", "ogg_opus": ".ogg"}


def _gen_volcengine_audio(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """火山豆包语音合成（V3 HTTP 单向流式）。

    三个和图片/视频不一样的地方：

      1. **域名不同**：``openspeech.bytedance.com``，不是方舟那台；
      2. **鉴权不同**：新版 ``X-Api-Key``（单个 Key），旧版 ``X-Api-App-Id`` +
         ``X-Api-Access-Key``（写成 ``appid:token``）；两种都要带
         ``X-Api-Resource-Id``；
      3. **返回是流**：body 里是一串 JSON 行，每行 ``data`` 是 base64 音频片段，
         拼接解码后才是完整音频 —— 既不是 URL，也不是单个 JSON。

    音色是必填的（``speaker``），且它在请求体里而不是模型名里 —— 所以这里
    也做前置校验，别让用户白跑一次拿 ``45000000`` 之类的错误码。
    """
    url = str(spec.endpoint or "").strip() or VOLC_TTS
    params: dict[str, Any] = {**spec.params, **req.extra}

    speaker = str(params.get("speaker") or params.get("voice") or "").strip()
    if not speaker:
        raise ConfigError(
            "火山语音合成必须指定音色：请在模型配置里填 params.speaker"
            "（例如 zh_female_tianmeixiaoyuan_moon_bigtts），"
            "或在命令上加 --param speaker=..."
        )
    resource = str(params.get("resource_id") or spec.options.get("resource_id")
                   or VOLC_TTS_DEFAULT_RESOURCE)

    audio_params: dict[str, Any] = {}
    for key in _VOLC_AUDIO_KEYS:
        if params.get(key) not in (None, ""):
            audio_params[key] = params[key]
    audio_params.setdefault("format", "mp3")
    audio_params.setdefault("sample_rate", 24000)

    key = (ctx.api_key or "").strip()
    if not key:
        raise ConfigError("火山语音没有拿到密钥：请填「语音技术」的 API Key，或旧版 appid:token")
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": resource,
        "X-Api-Request-Id": f"mr-{int(time.time() * 1000)}",
    }
    if ":" in key:
        # 旧版控制台：APP ID + Access Token，一对
        app_id, token = key.split(":", 1)
        headers["X-Api-App-Id"] = app_id.strip()
        headers["X-Api-Access-Key"] = token.strip()
    else:
        headers["X-Api-Key"] = key

    stem = f"{req.kind}_{_safe(spec.id)}_{time.strftime('%Y%m%d_%H%M%S')}"
    fmt = str(audio_params.get("format") or "mp3")
    files: list[str] = []
    if req.output_dir is None:
        # 同上：用法问题，归 ConfigError，不计熔断
        raise ConfigError(
            "火山语音接口返回的是音频流（base64 片段），没有可回传的 URL，"
            "不能和 --no-download 一起用；去掉该参数即可正常落盘"
        )

    started = _waiting(ctx, "volcengine-tts", hint="流式接口，返回 base64 音频片段")
    for index in range(1, max(1, int(req.count or 1)) + 1):
        req_params: dict[str, Any] = {
            "text": req.prompt,
            "speaker": speaker,
            "audio_params": audio_params,
        }
        # model / voice_instruction 属于 req_params；丢掉的话用户填了也不生效，
        # 而且不会有任何报错 —— 静默失效比报错更难查
        for key in _VOLC_REQ_KEYS:
            if params.get(key) not in (None, ""):
                req_params[key] = params[key]
        # 剩下没被 _VOLC_REQ_KEYS / audio_params 收走的键也送进 req_params ——
        # 火山语音的字段经常按模型不同（比如 emotion / speed_ratio），白名单外
        # 的键以前直接丢掉，用户填了等于没填。
        extra_req = leftover_params(
            req, {"speaker", "voice", "resource_id", "format", "sample_rate", "rate"}
        )
        for key, value in extra_req.items():
            req_params.setdefault(key, value)
        if extra_req:
            ctx.log(f"[volcengine-tts] --param 透传：{', '.join(sorted(extra_req))}")
        # 每段一个 Request-Id：复用同一个 id 会让服务端日志里的 N 次调用串成一次
        headers["X-Api-Request-Id"] = f"mr-{int(time.time() * 1000)}-{index}"

        ctx.log(f"[volcengine] tts -> {url}（{resource}）")
        resp = request("POST", url, headers=headers, json_body={"req_params": req_params}, timeout=ctx.timeout)
        raw = (resp.body or b"").decode("utf-8", "replace")

        pieces: list[bytes] = []
        errors: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            chunk = obj.get("data")
            if chunk:
                decoded = _b64_decode_lenient(str(chunk))
                if decoded:
                    pieces.append(decoded)
            code = obj.get("code")
            # 0 / 20000000 都是正常；其它非空码收起来，没音频时用来报错
            if code not in (None, 0, 20000000) and not chunk:
                errors.append(f"{code} {obj.get('message') or ''}".strip())

        data = b"".join(pieces)
        if not data:
            detail = errors[0] if errors else raw[:200]
            raise HttpError(f"火山语音没有返回音频数据：{detail}")
        ext = sniff_extension(data[:32]) or _VOLC_EXT.get(fmt, f".{fmt}")
        files.append(str(_write_media_bytes(data, ext, req.output_dir, stem, index)))

    _waited(ctx, "volcengine-tts", started, f"{len(files)} 段")
    return GenResult(
        status="ok",
        model_id=spec.id,
        provider=spec.provider,
        files=files,
        urls=[],
        meta={
            "model": spec.model or spec.id,
            "count": len(files),
            "kind": req.kind,
            "sync": True,
            "speaker": speaker,
            "resource_id": resource,
            "response_format": fmt,
        },
    )


def gen_volcengine(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    """火山方舟：即梦图片走同步接口，Seedance 视频走异步任务；语音走豆包语音的另一套接口。"""
    if req.kind == "audio":
        return _gen_volcengine_audio(spec, req, cfg, ctx)
    headers = {"Authorization": f"Bearer {ctx.api_key}", "Content-Type": "application/json"}

    if req.kind == "video":
        url = spec.endpoint or ARK_VIDEO_TASKS
        text = req.prompt
        ratio = req.aspect_ratio or spec.params.get("ratio")
        duration = req.duration or spec.params.get("duration")
        if ratio:
            text += f" --ratio {ratio}"
        if duration:
            text += f" --dur {duration}"
        # Seedance 的参数以「提示词里的大括号旗标」形式传（--ratio / --dur 就是这样），
        # 所以 --param 的剩余键也按同一约定拼成 --key value。以前这里直接丢掉，
        # 用户传 --param seed=42 / watermark=false 完全没反应。
        extra_flags = leftover_params(req, {"ratio", "duration"})
        for key, value in extra_flags.items():
            text += f" --{key} {_flag_value(value)}"
        if extra_flags:
            ctx.log(
                f"[volcengine] --param 透传为提示词参数：{', '.join(sorted(extra_flags))}"
            )
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if req.images:
            content.append(
                {"type": "image_url", "image_url": {"url": _image_value(req.images[0])}}
            )
        ctx.log(f"[volcengine] video submit -> {url}")
        payload = request(
            "POST",
            url,
            headers=headers,
            json_body={"model": spec.model or spec.id, "content": content},
            timeout=ctx.timeout,
        ).json() or {}
        task_id = payload.get("id") or _dig(payload, "data.id")
        if not task_id:
            raise HttpError(f"未拿到任务 id：{json.dumps(payload, ensure_ascii=False)[:300]}")

        poll_url = f"{url.rstrip('/')}/{task_id}"

        def probe() -> list[str] | None:
            data = request("GET", poll_url, headers=headers, timeout=60).json() or {}
            status = str(data.get("status") or "").lower()
            if status in ("queued", "running", "pending", ""):
                return None
            if status in ("failed", "cancelled", "canceled"):
                raise HttpError(
                    f"任务失败：{data.get('error') or json.dumps(data, ensure_ascii=False)[:200]}"
                )
            found = _collect_urls_from_items(_dig(data, "content"))
            if not found:
                single = _dig(data, "content.video_url")
                found = [single] if single else []
            if not found:
                raise HttpError("任务成功但没解析出视频 URL")
            return found

        ctx.log(f"[volcengine] task {task_id} 已提交，开始轮询")
        urls = poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll, log=ctx.log)
        return _finalize(spec, req, urls, req.output_dir, extra_meta={"task_id": task_id})

    url = spec.endpoint or ARK_IMAGE
    if req.images:
        # 即梦的图生图/参考图接口与文生图不同，配置里可用 endpoint 覆盖
        ctx.log("[volcengine] 检测到输入图片，按 img2img 语义发送")
    body: dict[str, Any] = {"model": spec.model or spec.id, "prompt": req.prompt}
    body.update(spec.params)
    body.update(
        _param_map(spec, req, ["size", "response_format", "watermark", "guidance_scale", "seed"])
    )
    if req.images:
        body["image"] = _image_value(req.images[0])
    _merge_leftover(
        body,
        req,
        "volcengine-image",
        consumed={"size", "response_format", "watermark", "guidance_scale", "seed"},
        log=ctx.log,
    )
    ctx.log(f"[volcengine] image -> {url}")
    started = _waiting(ctx, "volcengine-image")
    payload = request(
        "POST", url, headers=headers, json_body=body, timeout=ctx.timeout
    ).json() or {}
    _waited(ctx, "volcengine-image", started)
    data_items = payload.get("data") or []
    urls = _collect_urls_from_items(data_items)
    if not urls:
        b64_items = [
            item.get("b64_json")
            for item in data_items
            if isinstance(item, dict) and item.get("b64_json")
        ]
        if b64_items and req.output_dir is not None:
            stem = f"{req.kind}_{_safe(spec.id)}_{time.strftime('%Y%m%d_%H%M%S')}"
            files = [
                str(_write_b64_to_local(v, req.output_dir, stem, i, req.kind))
                for i, v in enumerate(b64_items, start=1)
            ]
            return GenResult(
                status="ok",
                model_id=spec.id,
                provider=spec.provider,
                files=files,
                meta={"model": spec.model or spec.id, "count": len(files), "kind": req.kind},
            )
        if b64_items:
            # 同 openai 分支：计费已发生但没有 URL，属用法问题，不写健康度
            raise ConfigError(
                "这个接口返回的是内联 base64 图片（b64_json），没有可回传的 URL，"
                "不能和 --no-download 一起用；去掉该参数即可正常落盘。"
                "（本次请求平台的计费通常已经发生）"
            )
        raise HttpError(f"响应中没有图片数据：{json.dumps(payload, ensure_ascii=False)[:300]}")
    if body.get("response_format") == "b64_json":
        pass
    return _finalize(spec, req, urls, req.output_dir, extra_meta={"usage": payload.get("usage")})


# ---------------------------------------------------------------- kling


KLING_BASE = "https://api-beijing.klingai.com/v1"


def _kling_video_url(spec: ModelSpec, mode: str) -> str:
    """可灵视频的提交地址（``mode`` 为 ``text2video`` 或 ``image2video``）。

    可灵按模式分两个端点，而厂商的 ``endpoints.video`` 只能存一个（目录默认值是
    ``videos/text2video``）。配置加载会把厂商省略的地址按 catalog 补回 ``spec.endpoint``，
    于是带参考图的请求会打到文生视频接口上 —— 反过来也一样。

    只在末段**正好是另一个模式**时替换，别的路径（自建网关等）原样保留，
    不去猜用户没写的东西。
    """
    endpoint = str(spec.endpoint or "").strip().rstrip("/")
    if not endpoint:
        return f"{KLING_BASE}/videos/{mode}"
    other = "text2video" if mode == "image2video" else "image2video"
    if endpoint.endswith(f"/videos/{other}"):
        return f"{endpoint[: -len(other)]}{mode}"
    return endpoint


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def kling_jwt(access_key: str, secret_key: str, ttl: int = 1800) -> str:
    """可灵用 AK/SK 签 HS256 JWT，这里纯标准库实现，避免引入 PyJWT。"""
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({"iss": access_key, "exp": now + ttl, "nbf": now - 5}).encode()
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(secret_key.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(signature)}"


def _kling_authorization(spec: ModelSpec, ctx: Ctx, cfg: RouterConfig) -> str:
    """可灵的 Authorization 头。**两种鉴权并存**，都要支持：

      1. 新版（推荐）：控制台发一把 ``api-key-kling-…``，直接
         ``Authorization: Bearer <API Key>``，不需要签名。
      2. 旧版：AccessKey + SecretKey，每请求签一个 HS256 JWT。

    早期这里只认 ``AK:SK``，于是拿着一把 57 字符的新版 Key 会得到
    「需要两个值」——密钥本身没错，是鉴权方式换代了。判断依据很简单：
    带英文冒号才当 AK:SK 处理，否则整串当 API Key 用。

    显式配置优先于环境变量，与其它 provider 的口径一致：
    ``options.access_key/secret_key`` → ``options.*_env`` → 密钥文件/环境变量。
    """
    ak = spec.options.get("access_key") or ""
    sk = spec.options.get("secret_key") or ""
    if ak and sk:
        return f"Bearer {kling_jwt(str(ak), str(sk))}"

    env_ak = spec.options.get("access_key_env")
    env_sk = spec.options.get("secret_key_env")
    if env_ak and env_sk:
        import os

        env_ak_value = os.environ.get(str(env_ak), "")
        env_sk_value = os.environ.get(str(env_sk), "")
        if env_ak_value and env_sk_value:
            return f"Bearer {kling_jwt(env_ak_value, env_sk_value)}"

    key = (ctx.api_key or "").strip()
    if not key:
        raise ConfigError(
            "可灵没有拿到密钥：请在配置页填 API Key（新版单个）"
            "或 AccessKey:SecretKey（旧版一对，英文冒号连接）"
        )
    if ":" in key:
        pair_ak, pair_sk = key.split(":", 1)
        if pair_ak.strip() and pair_sk.strip():
            return f"Bearer {kling_jwt(pair_ak.strip(), pair_sk.strip())}"
        raise ConfigError(
            "可灵的 AK:SK 有一侧是空的：请在配置页把 AccessKey 和 SecretKey "
            "用英文冒号连成一行，例如 'your-ak:your-sk'"
        )
    # 新版单个 API Key：直接当 Bearer 用，不签名
    return f"Bearer {key}"


def _gen_kling_audio(
    spec: ModelSpec,
    req: GenRequest,
    cfg: RouterConfig,
    ctx: Ctx,
    headers: dict[str, str],
) -> GenResult:
    """可灵语音合成：异步任务（提交 → 轮询），一次任务出一条音频。

    与图片/视频不同，TTS 的请求体里**没有 model_name** —— 官方样例只有
    ``text`` / ``voice_id`` / ``voice_language`` / ``voice_speed``，音色靠 voice_id 选。
    个别网关会额外要 model_name，写进 ``params.model_name`` 即可（会原样透传）。

    结果在 ``data.task_result.audios[]``，每项形如
    ``{"id": "...", "url": "...mp3", "duration": "2.232"}`` —— 注意 duration 是**字符串**。
    """
    base = str(spec.endpoint or "").strip().rstrip("/") or f"{KLING_BASE}/audio/tts"
    body: dict[str, Any] = {"text": req.prompt}
    body.update(spec.params)
    body.update(_param_map(spec, req, ["voice_id", "voice_language", "voice_speed"]))
    _merge_leftover(
        body,
        req,
        "kling-audio",
        consumed={"voice_id", "voice_language", "voice_speed"},
        log=ctx.log,
    )
    # 可灵 TTS 的音色是**必填**的，缺了会得到一句 `1201 voiceId: must not be blank`
    # —— 那还是花了 200ms 换回来的一句话。这里提前拦下，直接告诉用户填哪两个键。
    if not str(body.get("voice_id") or body.get("voiceId") or "").strip():
        raise ConfigError(
            "可灵语音合成必须指定音色：请在模型配置里填 params.voice_id"
            "（例如 chuanmeizi_speech02）和 params.voice_language（例如 zh），"
            "或在命令上加 --param voice_id=... --param voice_language=zh"
        )
    # 语言也是必填。中文场景居多，没填时给 zh，并允许 params 覆盖。
    body.setdefault("voice_language", "zh")

    urls: list[str] = []
    extra: dict[str, Any] = {}
    for _ in range(max(1, int(req.count or 1))):
        ctx.log(f"[kling] tts submit -> {base}")
        payload = request(
            "POST", base, headers=headers, json_body=body, timeout=ctx.timeout
        ).json() or {}
        if payload.get("code") not in (0, None):
            raise HttpError(
                f"可灵返回错误 {payload.get('code')}: {payload.get('message')}",
                None,
                json.dumps(payload, ensure_ascii=False),
            )
        task_id = _dig(payload, "data.task_id")
        if not task_id:
            raise HttpError(f"未拿到 task_id：{json.dumps(payload, ensure_ascii=False)[:300]}")

        poll_url = f"{base}/{task_id}"
        info: dict[str, Any] = {}

        def probe() -> list[str] | None:
            data = request("GET", poll_url, headers=headers, timeout=60).json() or {}
            status = str(_dig(data, "data.task_status", "")).lower()
            if status in ("submitted", "processing", "queued", ""):
                return None
            if status == "failed":
                raise HttpError(
                    f"任务失败：{_dig(data, 'data.task_status_msg', '') or json.dumps(data)[:200]}"
                )
            audios = (_dig(data, "data.task_result") or {}).get("audios") or []
            found = _collect_urls_from_items(audios)
            if not found:
                raise HttpError("任务成功但没解析出音频地址")
            for item in audios if isinstance(audios, list) else []:
                if not isinstance(item, dict):
                    continue
                if item.get("duration") not in (None, ""):
                    try:
                        info.setdefault("duration_seconds", round(float(item["duration"]), 2))
                    except (TypeError, ValueError):
                        pass
                if item.get("id"):
                    info.setdefault("audio_id", str(item["id"]))
            return found

        ctx.log(f"[kling] tts task {task_id} 已提交，开始轮询")
        urls.extend(poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll, log=ctx.log))
        for key, value in info.items():
            extra.setdefault(key, value)
        extra.setdefault("task_id", task_id)

    return _finalize(spec, req, urls, req.output_dir, extra_meta={**extra, "count": len(urls)})


def gen_kling(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    headers = {
        "Authorization": _kling_authorization(spec, ctx, cfg),
        "Content-Type": "application/json",
    }
    if req.kind == "audio":
        return _gen_kling_audio(spec, req, cfg, ctx, headers)

    if req.kind == "video":
        mode = "image2video" if req.images else "text2video"
        url = _kling_video_url(spec, mode)
        body: dict[str, Any] = {"model_name": spec.model or spec.id}
        if req.prompt:
            body["prompt"] = req.prompt
        if req.negative_prompt:
            body["negative_prompt"] = req.negative_prompt
        if req.images:
            body["image"] = _image_value(req.images[0], base64_only=True)
        body.update(spec.params)
        body.update(_param_map(spec, req, ["aspect_ratio", "duration", "mode", "cfg_scale"]))
        if req.aspect_ratio and "aspect_ratio" not in body:
            body["aspect_ratio"] = req.aspect_ratio
        if req.duration and "duration" not in body:
            body["duration"] = str(req.duration)
        _merge_leftover(
            body,
            req,
            "kling-video",
            consumed={"aspect_ratio", "duration", "mode", "cfg_scale"},
            log=ctx.log,
        )
        poll_base = url
    else:
        url = spec.endpoint or f"{KLING_BASE}/images/generations"
        body = {"model_name": spec.model or spec.id, "prompt": req.prompt, "n": req.count}
        body.update(spec.params)
        body.update(_param_map(spec, req, ["aspect_ratio", "image_fidelity", "n"]))
        if req.negative_prompt:
            body["negative_prompt"] = req.negative_prompt
        if req.images:
            body["image"] = _image_value(req.images[0], base64_only=True)
            body["image_reference"] = body.get("image_reference", "subject")
        _merge_leftover(
            body,
            req,
            "kling-image",
            consumed={"aspect_ratio", "image_fidelity", "n"},
            log=ctx.log,
        )
        poll_base = url

    ctx.log(f"[kling] submit -> {url}")
    payload = request(
        "POST", url, headers=headers, json_body=body, timeout=ctx.timeout
    ).json() or {}
    if payload.get("code") not in (0, None):
        # 带上 body：CLI 靠 body 里的 code 判断「欠费 / 余额不足」这类配置错误
        # （可灵 1102），没有它就只能当模型故障打进冷却。
        raise HttpError(
            f"可灵返回错误 {payload.get('code')}: {payload.get('message')}",
            None,
            json.dumps(payload, ensure_ascii=False),
        )
    task_id = _dig(payload, "data.task_id")
    if not task_id:
        raise HttpError(f"未拿到 task_id：{json.dumps(payload, ensure_ascii=False)[:300]}")

    poll_url = f"{poll_base.rstrip('/')}/{task_id}"

    def probe() -> list[str] | None:
        data = request("GET", poll_url, headers=headers, timeout=60).json() or {}
        status = str(_dig(data, "data.task_status", "")).lower()
        if status in ("submitted", "processing", "queued", ""):
            return None
        if status == "failed":
            raise HttpError(
                f"任务失败：{_dig(data, 'data.task_status_msg', '') or json.dumps(data)[:200]}"
            )
        result = _dig(data, "data.task_result")
        found = _collect_urls_from_items(
            (result or {}).get("videos") or (result or {}).get("images")
        )
        if not found:
            raise HttpError("任务成功但没解析出产物 URL")
        return found

    ctx.log(f"[kling] task {task_id} 已提交，开始轮询")
    urls = poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll, log=ctx.log)
    return _finalize(spec, req, urls, req.output_dir, extra_meta={"task_id": task_id})


# ---------------------------------------------------------------- replicate


REPLICATE_BASE = "https://api.replicate.com/v1"


def gen_replicate(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    headers = {
        "Authorization": f"Bearer {ctx.api_key}",
        "Content-Type": "application/json",
    }
    # 入参来源与优先级统一走 _passthrough_inputs（修掉 --param / --duration / --count
    # 在这条链路上被静默丢弃的问题）。replicate 的批量出图字段是 num_outputs。
    inputs: dict[str, Any] = _passthrough_inputs(
        spec, req, log=ctx.log, default_count_field="num_outputs"
    )
    inputs.setdefault("prompt", req.prompt)
    if req.negative_prompt:
        inputs.setdefault("negative_prompt", req.negative_prompt)
    if req.aspect_ratio:
        inputs.setdefault("aspect_ratio", req.aspect_ratio)
    if req.images:
        inputs.setdefault("image", _image_value(req.images[0]))
        inputs.setdefault("input_image", _image_value(req.images[0]))

    version = spec.options.get("version") or spec.raw.get("version")
    if version:
        url = spec.endpoint or f"{REPLICATE_BASE}/predictions"
        body = {"version": version, "input": inputs}
    else:
        url = spec.endpoint or f"{REPLICATE_BASE}/models/{spec.model or spec.id}/predictions"
        body = {"input": inputs}

    ctx.log(f"[replicate] submit -> {url}")
    payload = request(
        "POST", url, headers=headers, json_body=body, timeout=ctx.timeout
    ).json() or {}
    status = str(payload.get("status") or "").lower()
    if status == "failed":
        raise HttpError(f"任务失败：{payload.get('error')}")

    if status not in ("succeeded", "failed", "canceled") and (payload.get("urls") or {}).get("get"):
        get_url = payload["urls"]["get"]

        def probe() -> list[str] | None:
            data = request("GET", get_url, headers=headers, timeout=60).json() or {}
            state = str(data.get("status") or "").lower()
            if state in ("starting", "processing", ""):
                return None
            if state in ("failed", "canceled"):
                raise HttpError(f"任务失败：{data.get('error')}")
            found = _collect_urls_from_items(data.get("output"))
            if not found:
                raise HttpError("任务成功但没有输出")
            return found

        ctx.log("[replicate] 开始轮询")
        urls = poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll, log=ctx.log)
    else:
        urls = _collect_urls_from_items(payload.get("output"))
        if not urls:
            raise HttpError(f"响应中没有输出：{json.dumps(payload, ensure_ascii=False)[:300]}")

    return _finalize(spec, req, urls, req.output_dir, extra_meta={"task_id": payload.get("id")})


# ---------------------------------------------------------------- fal


def gen_fal(spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx) -> GenResult:
    headers = {"Authorization": f"Key {ctx.api_key}", "Content-Type": "application/json"}
    url = spec.endpoint or f"https://queue.fal.run/{spec.model or spec.id}"
    # 同 replicate：统一走 _passthrough_inputs，fal 的批量出图字段是 num_images。
    body: dict[str, Any] = _passthrough_inputs(
        spec, req, log=ctx.log, default_count_field="num_images"
    )
    body.setdefault("prompt", req.prompt)
    if req.images:
        body.setdefault("image_url", _image_value(req.images[0]))
    if req.aspect_ratio:
        body.setdefault("aspect_ratio", req.aspect_ratio)
    if req.negative_prompt:
        body.setdefault("negative_prompt", req.negative_prompt)

    ctx.log(f"[fal] submit -> {url}")
    payload = request(
        "POST", url, headers=headers, json_body=body, timeout=ctx.timeout
    ).json() or {}
    status_url = payload.get("status_url")
    response_url = payload.get("response_url")
    if not status_url or not response_url:
        # 有些端点直接同步返回结果
        urls = _collect_urls_from_items(payload.get("images")) or _collect_urls_from_items(
            payload.get("video")
        )
        if urls:
            return _finalize(spec, req, urls, req.output_dir)
        raise HttpError(f"响应缺少 status_url/response_url：{json.dumps(payload)[:300]}")

    def probe() -> list[str] | None:
        data = request("GET", status_url, headers=headers, timeout=60).json() or {}
        state = str(data.get("status") or "").upper()
        if state in ("IN_QUEUE", "IN_PROGRESS", ""):
            return None
        if state not in ("COMPLETED",):
            raise HttpError(f"任务状态异常：{json.dumps(data, ensure_ascii=False)[:200]}")
        result = request("GET", response_url, headers=headers, timeout=120).json() or {}
        found = (
            _collect_urls_from_items(result.get("images"))
            or _collect_urls_from_items(result.get("video"))
            or _collect_urls_from_items(result.get("output"))
        )
        if not found:
            raise HttpError("任务成功但没解析出产物 URL")
        return found

    ctx.log("[fal] 开始轮询")
    urls = poll(probe, interval=ctx.poll_interval, max_seconds=ctx.max_poll, log=ctx.log)
    return _finalize(
        spec, req, urls, req.output_dir, extra_meta={"request_id": payload.get("request_id")}
    )


# ---------------------------------------------------------------- generic_http


def _apply_auth(
    spec: ModelSpec, ctx: Ctx, headers: dict[str, str], params: dict[str, Any]
) -> None:
    auth = spec.options.get("auth") or {}
    kind = str(auth.get("type") or ("bearer" if ctx.api_key else "none")).lower()
    if not ctx.api_key:
        return
    if kind == "bearer":
        headers["Authorization"] = f"Bearer {ctx.api_key}"
    elif kind == "key":
        headers["Authorization"] = f"Key {ctx.api_key}"
    elif kind == "token":
        headers["Authorization"] = f"Token {ctx.api_key}"
    elif kind == "header":
        headers[str(auth.get("header") or "X-API-Key")] = ctx.api_key
    elif kind == "query":
        params[str(auth.get("query") or "key")] = ctx.api_key
    elif kind == "none":
        return
    else:
        headers["Authorization"] = f"{kind} {ctx.api_key}"


def _generic_http_help(spec: ModelSpec, missing: str) -> str:
    """generic_http 的配置不完整时，给一份能照着抄的说明。

    这个适配器是完全配置驱动的，缺配置就是完全跑不了 —— 光说"缺少 xxx"
    用户没法自己修，所以把可用的键和最小示例一起给出来。
    """
    return (
        f"模型「{spec.label or spec.model or spec.id}」的自定义接口配置不完整：缺少 {missing}。\n"
        "自定义接口需要你自己描述请求长什么样，写在模型条目（或它所属厂商）的 options 下。"
        "最小可用示例：\n"
        "  options:\n"
        "    auth: {type: bearer}                 # 密钥怎么带：bearer | key | header | query | none\n"
        "    submit:\n"
        '      body: {prompt: "{{prompt}}", model: "{{model}}"}\n'
        "    result_path: data.images             # 从响应里找产物的路径（点号分隔）\n"
        "    result_url_field: url                # 产物是对象数组时取哪个字段\n"
        "  再加上提交地址：在模型条目里写 endpoint，或在 options.submit.url 里写。\n"
        "  异步接口（先拿 task_id 再轮询）还要：\n"
        "    submit: {task_id_path: data.task_id}   # 并进上面那个 submit，不是再来一个\n"
        '    poll: {url: "https://…/{{task_id}}", status_path: data.status, result_path: data.result}\n'
        "字段对照见 references/providers.md 的 generic_http 一节。"
    )


def gen_generic_http(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """完全配置驱动的适配器：填 YAML 就能接任意 REST 接口，不用改代码。

    关键配置项（写在模型条目的 options 下）：
      auth:   {type: bearer|key|token|header|query|none, header: X-API-Key, query: key}
      sync:   true 表示提交响应里直接就是结果
      submit: {url, method, headers, body, task_id_path}
      poll:   {url, method, interval, status_path, success: [...], failure: [...],
               result_path, result_url_field}

    为照顾"我只知道一个接口地址"的常见情况，做了两条兜底：
      * options.submit.url 没写时，用模型条目的 endpoint（配置页面填的那个地址就是它）
      * 既没写 sync 也没写 poll.url 时，按**同步**处理（提交响应里直接是结果）
    """
    options = spec.options
    # 本地图片要 base64 后重新编码，不能每个占位符各编一次（一张 4MB 的图会被
    # 编成三份字符串塞进变量表）。先算一次，后面的 image / image_url 共用。
    images_rendered = [_image_value(img) for img in req.images]
    first_image = images_rendered[0] if images_rendered else ""
    variables: dict[str, Any] = {
        "prompt": req.prompt,
        "negative_prompt": req.negative_prompt,
        "model": spec.model or spec.id,
        "size": req.size or spec.params.get("size") or "",
        "aspect_ratio": req.aspect_ratio,
        # 没指定时长时给空串而不是 0：空串会被 _prune_empty 剔掉（"没填就不发"），
        # 而 0 会原样发出去，平台只会回 400。
        "duration": req.duration or "",
        "count": req.count,
        "image": first_image,
        "image_url": first_image,
    }
    variables.update({f"image{i + 1}": img for i, img in enumerate(images_rendered)})

    submit = options.get("submit") or {}
    # endpoint 兜底：配置页面里那个"图像/视频接口地址"字段落到的就是它。
    # 少了这条，界面上填了地址也依然报"缺少 options.submit.url"，等于白填。
    submit_url = submit.get("url") or spec.endpoint
    if not submit_url:
        # 缺的是**配置**，不是接口：归 ConfigError，不计熔断、不冷却模型。
        raise ConfigError(_generic_http_help(spec, "提交地址"))

    submit_headers = {str(k): str(v) for k, v in (submit.get("headers") or {}).items()}
    submit_params: dict[str, Any] = dict(submit.get("params") or {})
    _apply_auth(spec, ctx, submit_headers, submit_params)

    body_template = submit.get("body")
    if body_template is None:
        body_template = {"prompt": "{{prompt}}", "model": "{{model}}"}
        if req.kind == "video":
            body_template["aspect_ratio"] = "{{aspect_ratio}}"
    merged_body = _prune_empty(_render(body_template, variables))
    if isinstance(merged_body, dict):
        merged_body.update({k: v for k, v in spec.params.items()})
        # 模板里没写的键也照加：自定义接口经常需要平台特有字段，
        # 只覆盖"模板里已有键"等于逼用户先改模板才能传参。
        _merge_leftover(merged_body, req, "generic_http", log=ctx.log)

    method = str(submit.get("method") or "POST").upper()
    submit_url = str(_render(submit_url, variables))
    ctx.log(f"[generic_http] {method} {submit_url}")
    payload = request(
        method,
        submit_url,
        headers=submit_headers,
        params=submit_params or None,
        json_body=merged_body if method in ("POST", "PUT", "PATCH") else None,
        timeout=ctx.timeout,
    ).json() or {}

    pl = options.get("poll") or {}
    has_poll = bool(pl.get("url"))
    # 没显式写 sync、也没给 poll.url → 按同步处理。这是"只填了一个接口地址"时
    # 唯一合理的猜测：提交响应里直接带结果。
    sync_mode = bool(options.get("sync")) or (options.get("sync") is None and not has_poll)
    if sync_mode:
        if options.get("sync") is None:
            ctx.log("[generic_http] 未配置 sync/poll，按同步接口处理（响应里应直接带产物）")
        result_path = options.get("result_path")
        urls = _collect_urls_from_items(
            _dig(payload, result_path), options.get("result_url_field") or ""
        )
        if not urls and not result_path:
            # 没告诉我从哪儿取 → 自己在响应里找地址形状的字段
            urls = _find_urls_deep(payload)
            if urls:
                ctx.log(f"[generic_http] 未配置 result_path，自动从响应里取到 {len(urls)} 个地址")
        if not urls:
            raise ConfigError(
                f"接口有响应，但从里面找不到产物地址。"
                f"（响应：{json.dumps(payload, ensure_ascii=False)[:300]}）\n"
                + _generic_http_help(spec, "options.result_path / options.result_url_field")
            )
        return _finalize(spec, req, urls, req.output_dir)

    # task_id_path 必须先有，再去取值。
    # 少了这一步，_dig(payload, None) 会把**整个响应**当成 task_id 返回
    # （它的约定是"没有路径就返回原值"），于是 task_id 变成一个巨大的字典、
    # 被 str() 拼进轮询地址，实际发出去的是 https://…/{'data': {'status': …}}
    # 这种垃圾请求，最后以超时收场 —— 用户完全查不到真正的原因。
    task_id_path = submit.get("task_id_path")
    if not task_id_path:
        # 配置缺项，不是模型/接口故障
        raise ConfigError(_generic_http_help(spec, "options.submit.task_id_path"))
    task_id = _dig(payload, task_id_path)
    if task_id in (None, ""):
        raise HttpError(
            f"提交响应里没有 {task_id_path}，拿不到任务 id。"
            f"响应前 200 字：{json.dumps(payload, ensure_ascii=False)[:200]}"
        )
    poll_url_template = pl.get("url")
    if not poll_url_template:
        raise ConfigError(_generic_http_help(spec, "options.poll.url"))
    poll_url = str(_render(poll_url_template, {**variables, "task_id": task_id}))
    poll_method = str(pl.get("method") or "GET").upper()
    poll_headers = {str(k): str(v) for k, v in (pl.get("headers") or {}).items()}
    poll_params: dict[str, Any] = dict(pl.get("params") or {})
    _apply_auth(spec, ctx, poll_headers, poll_params)

    success = [str(s).lower() for s in (pl.get("success") or ["succeeded", "success", "done", "completed"])]
    failure = [str(s).lower() for s in (pl.get("failure") or ["failed", "error", "canceled"])]
    status_path = pl.get("status_path")
    result_path = pl.get("result_path")
    url_field = pl.get("result_url_field") or ""

    def probe() -> list[str] | None:
        data = request(
            poll_method,
            poll_url,
            headers=poll_headers,
            params=poll_params or None,
            timeout=60,
        ).json() or {}
        state = str(_dig(data, status_path, "") or "").lower()
        if state in failure:
            raise HttpError(f"任务失败：{json.dumps(data, ensure_ascii=False)[:200]}")
        if status_path and state not in success:
            return None
        found = _collect_urls_from_items(_dig(data, result_path), url_field)
        if not found and not result_path:
            found = _find_urls_deep(data)
        if not found:
            if status_path:
                raise ConfigError("任务成功但没解析出产物 URL，请检查 options.poll.result_path")
            return None
        return found

    ctx.log(f"[generic_http] 轮询 {poll_url}")
    urls = poll(
        probe,
        interval=float(pl.get("interval") or ctx.poll_interval),
        max_seconds=ctx.max_poll,
        log=ctx.log,
    )
    return _finalize(spec, req, urls, req.output_dir, extra_meta={"task_id": task_id})


# ---------------------------------------------------------------- 注册与派发


ADAPTERS: dict[str, Callable[[ModelSpec, GenRequest, RouterConfig, Ctx], GenResult]] = {
    "native": gen_native,
    "openai": gen_openai,
    "openai_image": gen_openai,
    "dashscope": gen_dashscope,
    "aliyun": gen_dashscope,
    "bailian": gen_dashscope,
    "volcengine": gen_volcengine,
    "ark": gen_volcengine,
    "doubao": gen_volcengine,
    "kling": gen_kling,
    "keling": gen_kling,
    "replicate": gen_replicate,
    "fal": gen_fal,
    "falai": gen_fal,
    "generic_http": gen_generic_http,
    "generic": gen_generic_http,
    "http": gen_generic_http,
}

#: 不需要配置密钥的 provider
KEYLESS_PROVIDERS = {"native", "generic_http_nokey"}


def known_providers() -> list[str]:
    return sorted(ADAPTERS)


class KindNotSupported(HttpError):
    """该类目还没有生成适配器（目前是 audio）。

    单独一个类型，是为了让调用方能区分「模型/接口失败」和「能力还没做」：
    后者不该计入熔断，也不该提示用户去检查密钥。
    """

    def __init__(self, message: str, kind: str = ""):
        super().__init__(message)
        self.kind = kind


#: 每个 provider 已经接通的类目。音频（TTS/ASR）还没有适配器 —— 不在这里挡一道，
#: 一个语音模型会被当成图像模型去 POST /images/generations，用户只会看到一堆
#: 看不懂的 400。配置可以照常保存，只是生成时明确拒绝。
SUPPORTED_KINDS: dict[str, set[str]] = {name: {"image", "video"} for name in ADAPTERS}
#: 语音一共四条通路，协议各不相同：
#:   * dashscope —— qwen-audio-*-tts-* / cosyvoice-* / qwen3-tts-*，同步，一次 POST 拿音频 URL
#:   * openai    —— /v1/audio/speech，同步，一次 POST 直接拿二进制
#:                  （智谱 glm-tts、硅基流动 CosyVoice 都是这套，所以它们跟着可用）
#:   * kling     —— /audio/tts，**异步**，提交后轮询 task_result.audios[]
#:   * volcengine—— 豆包语音 /api/v3/tts/unidirectional，流式 JSON 行，逐段 base64 拼装
#: 其余 provider（replicate、fal、generic_http）没有对应的 TTS 协议，保持不放开，
#: 免得「配上去能存、一跑就报错」。
for _provider in ("dashscope", "aliyun", "bailian"):
    SUPPORTED_KINDS[_provider] = {"image", "video", "audio"}
SUPPORTED_KINDS["openai"] = {"image", "video", "audio"}
#: 可灵：TTS 走 /audio/tts，异步任务（提交 → 轮询），鉴权仍是 AK/SK 签 JWT，
#: 和视频共用同一套凭据，所以接入成本最低。
for _provider in ("kling", "keling"):
    SUPPORTED_KINDS[_provider] = {"image", "video", "audio"}
#: 火山：TTS 是**豆包语音**的另一个服务（openspeech.bytedance.com），
#: 和方舟不是一套鉴权/请求体，但既然都叫 volcengine，就按 provider 放开。
for _provider in ("volcengine", "ark", "doubao"):
    SUPPORTED_KINDS[_provider] = {"image", "video", "audio"}


def run_adapter(
    spec: ModelSpec, req: GenRequest, cfg: RouterConfig, ctx: Ctx
) -> GenResult:
    """按 provider 派发；未知 provider 直接报错，避免静默走错通道。"""
    adapter = ADAPTERS.get(spec.provider)
    if adapter is None:
        raise HttpError(
            f"未知 provider {spec.provider!r}（模型 {spec.id}）。"
            f"可用值：{', '.join(known_providers())}；"
            f"或用 generic_http 自行配置任意 REST 接口。"
        )
    kinds = SUPPORTED_KINDS.get(spec.provider, set())
    if req.kind not in kinds:
        if spec.provider == "native":
            # 内置兜底只认 agent 自己的出图/出视频工具，没有语音这一路
            raise KindNotSupported(
                f"内置兜底只覆盖 image / video，{req.kind} 没有可委托的内置工具"
                f"（模型 {spec.id}）。",
                kind=req.kind,
            )
        raise KindNotSupported(
            f"{req.kind} 类目的生成还没有接入适配器（模型 {spec.id}，"
            f"provider {spec.provider}）。模型配置会正常保存，"
            f"但 generate --kind {req.kind} 暂时不可用。",
            kind=req.kind,
        )
    return adapter(spec, req, cfg, ctx)


__all__ = [
    "GenRequest",
    "GenResult",
    "Ctx",
    "ADAPTERS",
    "KEYLESS_PROVIDERS",
    "known_providers",
    "run_adapter",
    "PollTimeout",
    "HttpError",
    "KindNotSupported",
]
