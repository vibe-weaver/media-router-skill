"""生成结果的只读规格评估；生成成功不等于规格与播放验收通过。"""

from __future__ import annotations

import math
import re
from typing import Any


_SIZE_FIELDS = ("size", "image_size", "width", "height", "aspect_ratio", "ratio", "resolution")
_SOURCE_FIELDS = set(_SIZE_FIELDS) | {"duration", "duration_seconds", "seconds", "num_frames", "video_length"}
_SIZE_NAMES = {
    "auto", "default", "small", "medium", "large", "square", "square_hd",
    "portrait_4_3", "portrait_16_9", "landscape_4_3", "landscape_16_9",
    "1k", "2k", "4k", "8k",
}


def _positive_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and not re.fullmatch(r"\d{1,12}(?:\.\d{1,12})?", value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number) if number.is_integer() else number


def _safe_size(value: Any) -> Any:
    if isinstance(value, dict):
        dims = {key: _positive_number(value.get(key)) for key in ("width", "height")}
        return dims if all(dims.values()) else None
    if not isinstance(value, str):
        return _positive_number(value)
    if value.lower() in _SIZE_NAMES or re.fullmatch(r"\d{1,5}[xX*]\d{1,5}|\d{1,4}(?:\.\d{1,4})?:\d{1,4}(?:\.\d{1,4})?|\d{3,4}[pPiI]", value):
        return value
    return None


def _safe_source(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value in ("platform_default", "disabled", "unverified"):
        return value
    prefix, _, field = value.rpartition(".")
    if prefix in ("params", "request", "extra", "options.inputs", "extra.inputs", "template") and field in _SOURCE_FIELDS:
        return value
    return None


def _safe_effective_spec(raw: Any) -> dict[str, Any]:
    """字段与值双白名单：元数据不可成为原始 body/URL/凭据的旁路。"""
    if not isinstance(raw, dict):
        return {}
    safe: dict[str, Any] = {}
    if "duration_seconds" in raw:
        safe["duration_seconds"] = _positive_number(raw["duration_seconds"])
    source = _safe_source(raw.get("duration_source"))
    if source:
        safe["duration_source"] = source
    if raw.get("duration_field") in ("duration", "duration_seconds", "seconds", "num_frames", "video_length", "--dur"):
        safe["duration_field"] = raw["duration_field"]
    for field in _SIZE_FIELDS:
        if field not in raw:
            continue
        value = _positive_number(raw[field]) if field in ("width", "height") else _safe_size(raw[field])
        if value is not None:
            safe[field] = value
            source = _safe_source(raw.get(field + "_source"))
            if source:
                safe[field + "_source"] = source
    status = raw.get("size_status")
    if status in ("explicit", "platform_default", "unverified"):
        safe["size_status"] = "unverified" if status == "explicit" and not any(k in safe for k in _SIZE_FIELDS) else status
    return safe


def _requested_dimensions(effective: dict[str, Any]) -> list[tuple[Any, Any]]:
    """收集不同的精确宽高；字段顺序不是平台覆盖证据。"""
    groups: list[tuple[Any, Any]] = []
    width, height = effective.get("width"), effective.get("height")
    if width and height:
        groups.append((width, height))
    for key in ("size", "image_size"):
        value = effective.get(key)
        if isinstance(value, dict):
            groups.append((value["width"], value["height"]))
        if isinstance(value, str):
            match = re.fullmatch(r"(\d{1,5})[xX*](\d{1,5})", value)
            if match:
                groups.append((int(match[1]), int(match[2])))
    return list(dict.fromkeys(groups))


def assess_result(spec, req, result, profiles) -> tuple[dict[str, Any], list[str]]:
    """评估现有 profile_files 档案；不联网、不读写媒体，不更改传入对象。

    checked 仅表示可读本地档案及有证据的规格已比较，并非播放、听感或无水印验收。
    profiles 必须来自已有 metadata helpers；没有本地文件或档案不能声称完成核验。
    """
    meta = result.meta if isinstance(result.meta, dict) else {}
    effective = _safe_effective_spec(meta.get("effective_spec"))
    files = [str(path) for path in result.files]
    by_path = {str(p.get("path")): p for p in profiles if isinstance(p, dict)}
    readable = {path: by_path[path] for path in files if path in by_path and
                _positive_number(by_path[path].get("bytes")) is not None}
    local_checked = bool(files) and len(readable) == len(files)
    watermark = "present" if meta.get("watermark") is True else "unknown"
    assessment: dict[str, Any] = {
        "status": "unverified",
        "duration": [],
        "watermark": watermark,
        "local_files_checked": local_checked,
        "effective_spec": effective,
    }
    notes: list[str] = []
    mismatch = watermark == "present"
    unverified = not local_checked
    if watermark == "present":
        notes.append("平台结果有水印标记：本次产物带水印。")
    elif req.kind in ("image", "video"):
        notes.append("水印状态未知：没有足够证据确认是否带水印。")
    if not files:
        notes.append("没有本地文件，媒体元数据与规格未核验。")
    elif not local_checked:
        notes.append("部分本地文件缺少可读档案，媒体规格未核验。")

    if req.kind in ("image", "video"):
        size_status = effective.get("size_status")
        if size_status == "platform_default" or (not any(k in effective for k in _SIZE_FIELDS) and
            not req.size and not req.aspect_ratio and not any(req.extra.get(k) for k in _SIZE_FIELDS) and
            not any(spec.params.get(k) for k in _SIZE_FIELDS) and size_status != "unverified"):
            notes.append("本次未指定有效尺寸，使用平台默认；生成前具体分辨率和文件大小不确定。")
        elif size_status == "unverified" or not any(k in effective for k in _SIZE_FIELDS):
            notes.append("无法确认实际生效的尺寸参数，所请求尺寸未核验。")
            unverified = True
        if (any(key in effective for key in ("aspect_ratio", "ratio", "resolution")) or
            any(key in effective and not _requested_dimensions({key: effective[key]})
                for key in ("size", "image_size")) or
            (("width" in effective) != ("height" in effective))):
            notes.append("比例、命名尺寸、分辨率或不完整宽高约束未核验；仅精确宽高可直接比较。")
            unverified = True

    requested_duration = _positive_number(effective.get("duration_seconds"))
    dimension_groups = _requested_dimensions(effective)
    dimensions = dimension_groups[0] if len(dimension_groups) == 1 else None
    if req.kind in ("image", "video") and len(dimension_groups) > 1:
        notes.append("同时发送的精确尺寸参数存在冲突，缺少平台覆盖证据，尺寸未核验。")
        unverified = True
    for path in files:
        profile = readable.get(path, {})
        actual_duration = _positive_number(profile.get("duration_seconds"))
        if req.kind == "video":
            status = "unverified"
            if requested_duration is not None and actual_duration is not None:
                # profile_files 已提供现有精度，直接比较，不能暗设容差或再次舍入。
                status = "checked" if requested_duration == actual_duration else "mismatch"
            assessment["duration"].append({"path": path, "requested": requested_duration,
                                            "actual": actual_duration, "status": status})
            if status == "mismatch":
                mismatch = True
                notes.append(f"{path}：时长不一致，请求 {requested_duration} 秒，实际 {actual_duration} 秒。")
            elif status == "unverified":
                unverified = True
                notes.append(f"{path}：时长未核验（生效请求秒数或实际时长未知）。")
        if req.kind in ("image", "video") and profile:
            width, height = profile.get("width"), profile.get("height")
            resolution = f"{width}×{height}" if width and height else "分辨率未知"
            notes.append(f"{path}：{resolution}，{profile['bytes']} bytes，格式 {profile.get('format') or '未知'}。")
            if not width or not height:
                unverified = True
                notes.append(f"{path}：缺少实际分辨率，尺寸未核验。")
            elif dimensions and (width, height) != dimensions:
                mismatch = True
                notes.append(f"{path}：尺寸不一致，请求 {dimensions[0]}×{dimensions[1]}，实际 {width}×{height}。")
    assessment["status"] = "mismatch" if mismatch else "unverified" if unverified else "checked"
    return assessment, notes


__all__ = ["assess_result"]
