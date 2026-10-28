"""结果回传 —— 让纯文本模型也能"看见"产物。

两件事：
  1. 结构化元数据：文件路径、大小、图片宽高、视频时长。用纯标准库读文件头，
     不依赖 Pillow。
  2. 可选的文字描述（VLM caption）：把图片丢给一个 OpenAI 兼容的视觉模型，
     换回一句话描述。这才是让文本模型能接着往下推理的关键。

视频不做 VLM 描述（绝大多数视觉接口不收视频），只回元数据。
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Any

from .config import RouterConfig
from .transport import guess_content_type, request

DEFAULT_CAPTION_PROMPT = (
    "用一两句中文客观描述这张图片：主体内容、风格、构图与配色。"
    "不要客套，直接给描述，不要提到'这张图片'以外的元信息。"
)


# ---------------------------------------------------------------- 尺寸/时长


def _png_size(head: bytes) -> tuple[int, int] | None:
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if len(head) < 24:
        return None
    return struct.unpack(">II", head[16:24])


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    idx = 2
    total = len(data)
    while idx + 9 < total:
        if data[idx] != 0xFF:
            idx += 1
            continue
        marker = data[idx + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            idx += 2
            continue
        if idx + 4 > total:
            return None
        seg_len = struct.unpack(">H", data[idx + 2: idx + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            if idx + 9 > total:
                return None
            height, width = struct.unpack(">HH", data[idx + 5: idx + 9])
            return width, height
        idx += 2 + seg_len
    return None


def _gif_size(head: bytes) -> tuple[int, int] | None:
    if head[:6] not in (b"GIF87a", b"GIF89a") or len(head) < 10:
        return None
    width, height = struct.unpack("<HH", head[6:10])
    return width, height


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP" or len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def image_dimensions(path: Path) -> tuple[int, int] | None:
    """读图片文件头拿宽高，失败返回 None。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return None
    if not head:
        return None
    if head.startswith(b"\x89PNG"):
        return _png_size(head)
    if head.startswith(b"\xff\xd8"):
        return _jpeg_size(head)
    if head.startswith((b"GIF87a", b"GIF89a")):
        return _gif_size(head)
    if head[:4] == b"RIFF":
        return _webp_size(head)
    return None


def _moov_blob(fh: Any, file_size: int) -> bytes | None:
    """按顶层 box 走一遍，返回 moov 的**内容**（已跳过 box 头）。

    以前是「读文件头 1MB，再 find(b"mvhd")」。这在 faststart（moov 在前）的
    mp4 上能用，但 **moov 落在文件尾** 时就彻底找不到 —— 智谱 CogVideoX 返回的
    成片正是这种：2.1MB 的文件里 moov 在第 2185640 字节，`find` 直接落空，
    于是 caption.mode=metadata 承诺的「时长」永远是 None，而且不报任何错。

    顶层 box 只有 ftyp/free/mdat/moov 几个，按 size 跳着读，代价可以忽略，
    mdat 再大也不会被读进内存。

    返回的是内容而不是整块：调用方要遍历的是 moov 的**子 box**，
    带着这 8 字节头会让它们把 moov 自己当成第一个子 box，一个都匹配不上。
    """
    offset = 0
    while offset + 8 <= file_size:
        fh.seek(offset)
        header = fh.read(16)
        if len(header) < 8:
            return None
        size = int.from_bytes(header[0:4], "big")
        kind = header[4:8]
        head_len = 8
        if size == 1:  # 64 位扩展长度
            if len(header) < 16:
                return None
            size = int.from_bytes(header[8:16], "big")
            head_len = 16
        elif size == 0:  # 一直延伸到文件尾
            size = file_size - offset
        if size < head_len:
            return None
        if kind == b"moov":
            fh.seek(offset)
            raw = fh.read(min(size, 16 * 1024 * 1024))
            return raw[head_len:] if len(raw) > head_len else None
        offset += size
    return None


def _iter_boxes(blob: bytes, start: int = 0, end: int | None = None):
    """遍历一段字节里的 box，产出 (起始偏移, 类型, 头长度, 总长度)。

    mp4 的每个 box 都是「4 字节长度 + 4 字节类型 + 内容」，长度 1 表示后面
    再跟 8 字节的 64 位长度，长度 0 表示一直到结尾。

    为什么不用 ``blob.find(b"tkhd")``：那是靠 4 字节魔数猜位置，元数据
    （udta 里的标题、注释）恰好含这几个字符时就会误命中，读出一组
    莫名其妙的宽高。D2 那个 bug 的根因就是「位置假设」，这里不再重复。
    """
    limit = len(blob) if end is None else min(end, len(blob))
    pos = start
    while pos + 8 <= limit:
        size = int.from_bytes(blob[pos:pos + 4], "big")
        kind = blob[pos + 4:pos + 8]
        head = 8
        if size == 1:
            if pos + 16 > limit:
                return
            size = int.from_bytes(blob[pos + 8:pos + 16], "big")
            head = 16
        elif size == 0:
            size = limit - pos
        if size < head or pos + size > limit:
            return
        yield pos, kind, head, size
        pos += size


def _mvhd_body(fh: Any, file_size: int) -> bytes | None:
    """moov 里 mvhd box 的内容（version/flags 起算）。"""
    blob = _moov_blob(fh, file_size)
    if not blob:
        return None
    for pos, kind, head, size in _iter_boxes(blob):
        if kind == b"mvhd":
            return blob[pos + head: pos + size]
    return None


def mp4_duration(path: Path) -> float | None:
    """从 mp4 的 mvhd box 读取时长（秒），失败返回 None。"""
    try:
        with path.open("rb") as fh:
            body = _mvhd_body(fh, path.stat().st_size)
    except OSError:
        return None
    if not body:
        return None
    version = body[0]
    if len(body) < (32 if version == 1 else 20):
        return None
    try:
        if version == 1:
            timescale = int.from_bytes(body[20:24], "big")
            duration = int.from_bytes(body[24:32], "big")
        else:
            timescale = int.from_bytes(body[12:16], "big")
            duration = int.from_bytes(body[16:20], "big")
    except (IndexError, ValueError):
        return None
    if not timescale:
        return None
    return round(duration / timescale, 2)


def mp4_dimensions(path: Path) -> tuple[int, int] | None:
    """从 moov → trak → tkhd 读分辨率。tkhd 里的宽高是 16.16 定点数。

    音轨的 tkhd 宽高恒为 0，所以要跳过它继续看下一条轨 —— 只取第一个 tkhd
    的话，音轨排在前面（很常见）就会得到 0×0。
    """
    try:
        with path.open("rb") as fh:
            blob = _moov_blob(fh, path.stat().st_size)
    except OSError:
        return None
    if not blob:
        return None
    for pos, kind, head, size in _iter_boxes(blob):
        if kind != b"trak":
            continue
        for cpos, ckind, chead, csize in _iter_boxes(blob, pos + head, pos + size):
            if ckind != b"tkhd":
                continue
            body = cpos + chead  # version+flags 的起点
            if body >= len(blob):
                continue
            # v1 的创建/修改时间是 64 位，后面所有字段整体后移 8 字节
            shift = 8 if blob[body] == 1 else 0
            base = body + 4  # 跳过 version(1) + flags(3)
            w_off = base + 72 + shift
            h_off = base + 76 + shift
            if h_off + 4 > len(blob):
                continue
            width = int.from_bytes(blob[w_off:w_off + 4], "big") / 65536
            height = int.from_bytes(blob[h_off:h_off + 4], "big") / 65536
            if width >= 1 and height >= 1:
                return int(round(width)), int(round(height))
    return None


def _wav_duration(data: bytes) -> float | None:
    """wav 精确时长：找到 fmt 里的 byte_rate 和 data 里的长度，直接除。"""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    byte_rate = 0
    data_size = 0
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        if chunk_id == b"fmt " and pos + 16 <= len(data):
            byte_rate = int.from_bytes(data[pos + 16:pos + 20], "little")
        elif chunk_id == b"data":
            # 有些编码器把 data 长度写成 0 或超出文件长度，取二者较小值
            data_size = min(size, len(data) - pos - 8) if size else len(data) - pos - 8
            break
        if size <= 0:
            return None
        pos += 8 + size + (size & 1)
    if not byte_rate or data_size <= 0:
        return None
    return round(data_size / byte_rate, 2)


_MP3_BITRATES_V1L3 = [
    0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0,
]


def _mp3_duration(data: bytes) -> float | None:
    """mp3 按首帧码率**估算**（CBR 准，VBR 会偏）。

    不逐帧扫描是有意的：一条 3 分钟的音频要解上万帧，只为拿一个时长不值。
    估不准时宁可不给 —— 反正上游一般同时回了 duration。
    """
    start = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        size_bytes = data[6:10]
        start = 10 + (
            (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
        )
    pos = start
    while pos + 4 <= len(data) and pos < start + 64 * 1024:
        if data[pos] == 0xFF and (data[pos + 1] & 0xE0) == 0xE0:
            version = (data[pos + 1] >> 3) & 0x03  # 3=MPEG1
            layer = (data[pos + 1] >> 1) & 0x03     # 1=Layer3
            bitrate_index = (data[pos + 2] >> 4) & 0x0F
            if version == 3 and layer == 1 and 0 < bitrate_index < 15:
                bitrate = _MP3_BITRATES_V1L3[bitrate_index] * 1000
                if bitrate:
                    return round((len(data) - pos) * 8 / bitrate, 2)
            return None
        pos += 1
    return None


def audio_duration(path: Path) -> float | None:
    """音频时长。wav 精确，mp3 估算，其余格式返回 None。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data[:4] == b"RIFF":
        return _wav_duration(data)
    if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return _mp3_duration(data)
    return None


def _extract_frame(video_path: Path) -> Path | None:
    """如果环境里有 ffmpeg，抽一帧给 VLM 用；没有就跳过描述。

    返回值是**临时文件**，调用方用完要删（见 vlm_caption 的 finally）。
    """
    import shutil
    import subprocess
    import tempfile
    import uuid

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        # 用随机名：原来用 video_path.stem 命名，不同目录下的同名视频会互相覆盖，
        # 而且那些文件从来没人清理，会在系统临时目录里一直攒着。
        out = Path(tempfile.gettempdir()) / f"mr_frame_{uuid.uuid4().hex}.jpg"
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(video_path), "-frames:v", "1", str(out)],
            check=True,
            timeout=60,
        )
        return out if out.exists() else None
    except Exception:  # noqa: BLE001 - 抽帧纯属增强，失败不影响主流程
        return None


# ---------------------------------------------------------------- 文件档案


def profile_files(files: list[str], kind: str) -> list[dict[str, Any]]:
    """给每个产物生成一份档案。"""
    out: list[dict[str, Any]] = []
    for raw in files:
        path = Path(raw)
        entry: dict[str, Any] = {"path": str(path)}
        try:
            stat = path.stat()
            entry["bytes"] = stat.st_size
            entry["size_human"] = _human_size(stat.st_size)
        except OSError:
            entry["bytes"] = 0
        entry["format"] = path.suffix.lstrip(".").lower()
        if kind == "image":
            dims = image_dimensions(path)
            if dims:
                entry["width"], entry["height"] = dims
        elif kind == "audio":
            duration = audio_duration(path)
            if duration:
                entry["duration_seconds"] = duration
        else:
            duration = mp4_duration(path)
            if duration:
                entry["duration_seconds"] = duration
            dims = mp4_dimensions(path)
            if dims:
                entry["width"], entry["height"] = dims
        out.append(entry)
    return out


def _human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GB"


# ---------------------------------------------------------------- VLM 描述


def caption_config(cfg: RouterConfig) -> dict[str, Any]:
    raw = cfg.defaults.get("caption") or {}
    return raw if isinstance(raw, dict) else {}


def caption_enabled(cfg: RouterConfig) -> str:
    """返回 none | metadata | vlm。"""
    raw = caption_config(cfg)
    mode = str(raw.get("mode") or "metadata").lower()
    if mode in ("off", "none", "false", "disabled"):
        return "none"
    if mode == "vlm":
        return "vlm"
    return "metadata"


def _caption_key(cfg: RouterConfig, raw: dict[str, Any]) -> str:
    import os

    env_name = str(raw.get("api_key_env") or "")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip()
    keys = (cfg.secrets or {}).get("keys") or {}
    if env_name and keys.get(env_name):
        return str(keys[env_name]).strip()
    if raw.get("api_key"):
        return str(raw["api_key"]).strip()
    return ""


#: 一次 VLM 请求里所有图片的原始字节上限。
#: 没有这个上限时，三张 4MB 的 PNG 会被 base64 成约 16MB 塞进一个请求里，
#: 网关基本都会拒（413），而失败信息只会是一句笼统的"视觉模型调用失败"。
MAX_CAPTION_BYTES = 8 * 1024 * 1024


def vlm_caption(
    cfg: RouterConfig,
    files: list[str],
    kind: str,
    *,
    goal: str = "",
    timeout: float = 120.0,
) -> tuple[str, str]:
    """调 VLM 产出文字描述。返回 (描述, 错误说明)。"""
    raw = caption_config(cfg)
    # 地址必须显式写。以前这里兜底到 api.openai.com，于是"把 caption 的密钥
    # 配成别家平台的、地址忘了改"就会把密钥发到 OpenAI 去 —— 宁可报错。
    endpoint = str(raw.get("endpoint") or "").strip()
    model = str(raw.get("model") or "")
    if not model:
        return "", "caption.model 未配置"
    if not endpoint:
        return "", (
            "caption.endpoint 未配置：视觉描述要请求哪个地址必须显式填写"
            "（例如 https://api.openai.com/v1/chat/completions，或你的网关地址）"
        )

    key = _caption_key(cfg, raw)
    if not key:
        env_name = raw.get("api_key_env") or "（未设置 api_key_env）"
        return "", f"未找到 caption 密钥：{env_name}"

    targets: list[Path] = []
    temp_frames: list[Path] = []
    for item in files[:3]:
        path = Path(item)
        if kind == "image":
            targets.append(path)
        else:
            frame = _extract_frame(path)
            if frame:
                targets.append(frame)
                temp_frames.append(frame)
    if not targets:
        return "", "没有可送入视觉模型的图像（视频需要 ffmpeg 抽帧）"

    try:
        total_bytes = 0
        for path in targets:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
        if total_bytes > MAX_CAPTION_BYTES:
            return "", (
                f"要送进视觉模型的图共 {_human_size(total_bytes)}，超过 "
                f"{_human_size(MAX_CAPTION_BYTES)} 的上限（base64 后还会再涨约三分之一），"
                f"大概率会被平台拒绝。请调小生成尺寸或减少数量，"
                f"也可以把 defaults.caption.mode 改成 metadata 只回元数据。"
            )

        prompt_text = str(raw.get("prompt") or DEFAULT_CAPTION_PROMPT)
        if goal:
            prompt_text += f"\n（背景：这张图服务于以下任务，描述时请侧重相关细节）{goal}"

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for path in targets:
            try:
                data = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError as exc:
                return "", f"读取图片失败：{exc}"
            mime = guess_content_type(path)
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
            )

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
        }
        try:
            resp = request(
                "POST",
                endpoint,
                headers={"Authorization": f"Bearer {key}"},
                json_body=payload,
                timeout=timeout,
                max_retries=1,
            )
        except Exception as exc:  # noqa: BLE001 - 描述失败不阻断主流程
            return "", f"视觉模型调用失败：{exc}"

        data = resp.json() or {}
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return "", f"视觉模型响应异常：{json.dumps(data, ensure_ascii=False)[:200]}"
        if isinstance(text, list):
            text = " ".join(
                part.get("text", "") for part in text if isinstance(part, dict)
            )
        return str(text).strip(), ""
    finally:
        # 抽出来的帧是临时文件，用完就删，别在系统临时目录里攒着
        for frame in temp_frames:
            try:
                frame.unlink()
            except OSError:
                pass


def _metadata_caption(
    files: list[str], kind: str, profiles: list[dict[str, Any]] | None = None
) -> str:
    """metadata 模式下的文字描述：把文件档案写成一句人话。

    以前 metadata 模式直接返回空串，而 SKILL 告诉 agent「汇报时用 caption」——
    于是文本模型拿到的是 ""，只好去翻 files[] 或者凭空描述。这里给一句结构化
    描述（尺寸/时长/格式/大小），既不用调用视觉模型，也够 agent 直接引用。

    ``profiles`` 由调用方（CLI）传进来时直接复用 —— 那份档案它已经算过一遍了，
    再算一次对音频意味着把整个文件重读一遍。
    """
    if not files:
        return ""
    entries = profiles if profiles is not None else profile_files(files, kind)
    pieces: list[str] = []
    for index, entry in enumerate(entries, start=1):
        bits: list[str] = []
        width, height = entry.get("width"), entry.get("height")
        if width and height:
            bits.append(f"{width}×{height}")
        duration = entry.get("duration_seconds")
        if duration:
            bits.append(f"{duration:.2f} 秒")
        fmt = str(entry.get("format") or "").upper()
        if fmt:
            bits.append(fmt)
        size = entry.get("size_human")
        if size:
            bits.append(str(size))
        if bits:
            prefix = f"第 {index} 个：" if len(entries) > 1 else ""
            pieces.append(prefix + "，".join(bits))
    if not pieces:
        return ""
    label = {"image": "图片", "video": "视频", "audio": "音频"}.get(kind, "产物")
    return f"{label}已生成：" + "；".join(pieces) + "。"


def build_caption(
    cfg: RouterConfig,
    files: list[str],
    kind: str,
    goal: str = "",
    profiles: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str]]:
    """按配置产出描述文本。返回 (描述, 提示信息)。"""
    mode = caption_enabled(cfg)
    notes: list[str] = []
    if mode == "none":
        return "", notes
    if mode == "metadata":
        return _metadata_caption(files, kind, profiles), notes
    text, error = vlm_caption(cfg, files, kind, goal=goal)
    if error:
        notes.append(f"文字描述未生成：{error}")
    return text, notes
