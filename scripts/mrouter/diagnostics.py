"""供 CLI 与网页复用的厂商错误解析和脱敏诊断。"""

from __future__ import annotations

import json
import re
import urllib.error
from http.client import IncompleteRead
from typing import Any

from .config import ConfigError
from .transport import gateway_html_hint, html_label, html_summary, redact

# 顶层 InvalidParameter 本身不是确定的配置证据，须结合具体字段校验信息。
CONFIG_ERROR_CODES = {
    "arrearage", "insufficientbalance", "invalidapikey", "modelnotopen",
    "modelnotfound", "unpurchased", "accessdenied", "1102", "1113",
}


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {redact(str(key)): _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def error_payload(exc: Exception) -> dict[str, Any] | None:
    """解析 JSON 对象并脱敏；诊断中的 URL 也不例外保留签名。"""
    body = getattr(exc, "body", "") or ""
    if not isinstance(body, (str, bytes, bytearray)) or not body.strip():
        return None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    return _safe(payload) if isinstance(payload, dict) else None


def vendor_error_code(exc: Exception) -> str:
    payload = error_payload(exc)
    if not payload:
        return ""
    code = payload.get("code") or payload.get("error_code") or ""
    for holder in ("error", "header"):
        nested = payload.get(holder)
        if not code and isinstance(nested, dict):
            code = nested.get("code") or ""
    return redact(str(code).strip())


def _html_plain(body: str, limit: int = 120) -> str:
    """把 HTML 压成一行可读文本，供诊断展示；不执行脚本/样式内容。

    诊断拿到的 body 常被截断在 800 字符，``</script>`` 可能已经丢了；
    这里允许“脚本块一直延伸到结尾”，否则 JS 源码会被当成可见文字展示。
    """
    text = re.sub(r"(?is)<(?:script|style)[^>]*>.*?(?:</(?:script|style)>|$)", " ", body)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return " ".join(text.split())[:limit]


def vendor_error_detail(exc: Exception) -> str:
    """沿用 code/message、error、header 结构与 200 字符单行片段。"""
    body = str(getattr(exc, "body", "") or "").strip()
    if not body:
        return ""
    page = html_summary(body)
    if page:
        # HTML 页面通常是网关/登录页，整段标签对排查没有价值；只保留标题和可见文字。
        plain = _html_plain(body)
        if plain.startswith(page):
            plain = plain[len(page):].strip()
        suffix = f"：{plain}" if plain else ""
        return redact(f"{html_label(body)}{suffix}")[:200]
    payload = error_payload(exc)
    if payload:
        code = payload.get("code") or payload.get("error_code") or ""
        message = payload.get("message") or payload.get("msg") or ""
        for holder in ("error", "header"):
            nested = payload.get(holder)
            if isinstance(nested, dict):
                code = code or nested.get("code") or ""
                message = message or nested.get("message") or ""
            elif isinstance(nested, str):
                message = message or nested
        parts = [str(part).strip() for part in (code, message) if str(part).strip()]
        if parts:
            return redact(" ".join(" ".join(parts).split()))[:200]
    return redact(" ".join(body.split()))[:200]


_ENGINE_ERROR = re.compile(
    r"\bengine[\s_-]*(?:error|failure|failed)\b|引擎.{0,10}(?:错误|失败|异常)", re.I,
)
_MODEL_ERROR = re.compile(
    r"\bmodel\b.{0,100}\b(?:does not exist|not found)\b"
    r"|模型.{0,60}(?:不存在|未开通|未找到)", re.I,
)
_PARAMETER_ERROR = re.compile(
    r"\b(?:parameter|field)\s+[`'\"]?(?!(?:is|was|must|invalid|required)\b)[\w.-]+[`'\"]?"
    r"\s+(?:is\s+)?(?:invalid|not valid|required|missing|must\b|should\b)"
    r"|\b(?:input\.)?(?:text|text_prompt|prompt|size|width|height|duration|duration_seconds|voice|voice_id|"
    r"format|speed|sample_rate|count|n)\b[`'\"]?\s+(?:is\s+)?"
    r"(?:required|missing|invalid|not valid|must\b|should\b)"
    r"|(?:参数|字段)\s*[`'\"]?[A-Za-z_][\w.-]*[`'\"]?\s*(?:不合法|无效|必须|应为|不能为空|缺失)",
    re.I,
)
_QUOTA_CODES = {"arrearage", "insufficientbalance", "unpurchased", "1102", "1113"}
_MODEL_CODES = {"modelnotfound", "modelnotopen"}


def diagnose_error(exc: Exception) -> dict[str, Any]:
    """只按已确认的证据分类；受理不明始终优先，避免重复付费生成。"""
    code = vendor_error_code(exc)
    normalized_code = code.lower()
    detail = vendor_error_detail(exc) or redact(str(exc))
    body_text = str(getattr(exc, "body", "") or "")
    page = html_summary(body_text)
    label = html_label(body_text)
    gateway = gateway_html_hint(body_text)
    evidence = redact(
        f"{json.dumps(error_payload(exc), ensure_ascii=False)} "
        f"{body_text} {exc}"
    )
    status = getattr(exc, "status", None)
    uncertain = bool(getattr(exc, "submission_uncertain", False))
    kind, category = "runtime", "unknown"
    summary = "请求失败，根因待确认"
    hint = "请保留厂商错误码和原文，根因待确认；结合平台记录或接口文档排查。"

    if uncertain:
        kind, category = "uncertain", "submission_uncertain"
        if page:
            summary = f"提交结果不确定：响应是 {label}"
            hint = (
                "接口地址可能填成了网页地址，或请求被登录页、代理、防火墙拦截。"
                "HTTP 2xx 仍不能证明平台未受理：请先核对平台任务与计费，确认前不要重跑或切换模型。"
            )
            if gateway:
                hint += gateway + "。"
        else:
            summary = "提交结果不确定，平台可能已经受理并计费"
            hint = "请先到平台控制台确认任务是否已创建及计费状态；确认前不要再次生成或切换候选重跑。"
    elif getattr(exc, "stop_generation", False):
        kind, category = "delivery", "generated_result"
        summary = "已有生成结果，交付或批次尚未完成"
        hint = getattr(exc, "hint", "") or "不要再次生成；请先从已有文件、产物地址或平台任务记录取回结果。"
    elif isinstance(exc, ConfigError):
        kind, category = "config", "configuration"
        summary = "本地配置或参数有误"
        hint = "请按错误原文检查当前配置与调用参数，修正后再执行。"
    elif page:
        kind, category = "runtime", "html_response"
        summary = f"接口返回 {label} 而不是 JSON"
        hint = (
            "接口地址可能填成了网页地址，或请求被登录页、代理、防火墙拦截；"
            "请核对 endpoint、代理与网络登录状态后再试。"
        )
        if gateway:
            hint += gateway + "。"
    elif _ENGINE_ERROR.search(evidence):
        category = "engine"
        summary = "厂商引擎返回错误，根因待确认"
        hint = "根因待确认，不能仅凭顶层错误码断言用户配置有误；请保留厂商原文并核对平台任务记录或咨询厂商。"
    elif normalized_code in _QUOTA_CODES or status == 402:
        kind, category = "config", "quota"
        summary = "账户额度或资源包不足，或尚未购买服务"
        hint = "请到平台控制台核对余额、额度及适用于当前模型的资源包或购买状态；确认后再执行。"
    elif normalized_code in _MODEL_CODES or _MODEL_ERROR.search(evidence):
        kind, category = "config", "model"
        summary = "模型不存在或尚未开通"
        hint = "请核对实际模型名、接口地址及区域，并在平台控制台确认已开通该模型。"
    elif normalized_code == "invalidapikey" or status == 401:
        kind, category = "config", "authentication"
        summary = "密钥无效、过期或未被接受"
        hint = "请在本机配置与平台控制台核对密钥是否有效且属于当前接口；不要把密钥写入日志或反馈内容。"
    elif normalized_code == "accessdenied" or status == 403:
        kind, category = "config", "permission"
        summary = "当前账户或密钥无权访问该服务"
        hint = "请核对服务开通状态、模型权限及所用接口与区域。"
    elif _PARAMETER_ERROR.search(evidence):
        kind, category = "config", "parameter"
        summary = "请求参数未通过厂商校验"
        hint = "请按厂商原文和模型接口契约修正具体字段；不要猜测或注入未经确认的默认参数。"
    elif status == 429:
        category = "rate_limit"
        summary = "请求被平台限流"
        hint = "请核对平台并发与速率限制，等待限流解除后再决定是否重试。"
    elif (isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead))
          or "网络错误" in evidence):
        category = "network"
        summary = "网络连接或响应接收失败"
        hint = "请检查网络、超时与平台状态；如涉及生成提交，先核对平台任务记录，不要盲目重跑。"
    elif status is not None:
        category = "http"
        summary = f"平台返回 HTTP {status}，根因待确认"

    return _safe({
        "kind": kind,
        "category": category,
        "summary": summary,
        "detail": detail,
        "hint": hint,
        "error_code": code,
        "submission_uncertain": uncertain,
    })
