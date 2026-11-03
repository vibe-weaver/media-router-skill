"""模型身份、接口地址与计费资源的授权边界。

本模块只做纯计算：不读配置、不发请求、不写文件。它实现的是**应用层**的
提交前检查，不是操作系统沙箱 —— 持有同一用户权限的进程仍然可以直接修改
配置或绕开本程序。这里能保证的是：本程序的正常入口（CLI / 网页 / 探测）
不会用请求参数改换配置批准的模型、地址、凭据或计费资源。

分工：
  * ``validate_request_params`` 在参数进入候选规划和取密钥之前，拒绝一切
    可能改变模型身份的字段（含嵌套结构与常见别名写法）。
  * ``generic_contract`` 为 ``generic_http`` 计算可信配置声明的身份槽；
    自定义接口无法自动推断任意字段的业务语义，因此只放行显式声明的运行参数。
  * ``SubmissionIdentity`` 在真正提交之前，按配置冻结的值复核实际请求。
"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterable

from .config import ConfigError, ModelSpec

#: 表示"配置批准这里没有这个字段"：实际请求里出现即视为越权新增。
MISSING = object()

#: 出现在字段名里就可能决定"调用哪个模型 / 打到哪个地址 / 用哪份计费资源"。
_IDENTITY_TOKENS = frozenset({
    "model", "models", "version", "versions", "resource", "resources",
    "endpoint", "endpoints", "provider", "catalog", "host", "hostname",
})

#: 凭据字段：即使值相同也不允许由请求参数注入。
_CREDENTIAL_TOKENS = frozenset({
    "key", "keys", "apikey", "authorization", "credential", "credentials",
    "password", "passwd", "secret", "secrets", "token", "auth", "bearer",
})

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


# ---------------------------------------------------------------- 字段分类


def _tokens(name: Any) -> list[str]:
    """把 ``API_KEY`` / ``apiKey`` / ``input[model]`` 统一拆成词元。"""
    text = _CAMEL_RE.sub("_", str(name))
    return [part for part in _SPLIT_RE.split(text.lower()) if part]


def is_identity_key(name: Any) -> bool:
    """这个字段名是否可能决定模型、地址或计费资源。"""
    return any(token in _IDENTITY_TOKENS for token in _tokens(name))


def is_credential_key(name: Any) -> bool:
    """这个字段名是否是凭据字段（密钥、令牌、鉴权头等）。"""
    return any(token in _CREDENTIAL_TOKENS for token in _tokens(name))


def is_sensitive_key(name: Any) -> bool:
    return is_identity_key(name) or is_credential_key(name)


def _blocked_message(path: Iterable[Any]) -> str:
    """报错只给字段路径，不回显用户填的值（值里可能有密钥）。"""
    text = ".".join(str(part) for part in path)
    return (
        f"请求参数 {text} 不能在生成时改换模型、接口地址、凭据或计费资源。"
        "要换模型请在配置里新增或启用条目，再用 --model 指定"
    )


def _reject_sensitive(node: Any, path: tuple = ()) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key)
            if is_sensitive_key(name):
                raise ConfigError(_blocked_message(path + (name,)))
            _reject_sensitive(value, path + (name,))
        return
    if isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _reject_sensitive(value, path + (index,))


def validate_request_params(extra: Any, spec: ModelSpec | None = None) -> None:
    """请求参数入口检查；不安全时抛 ConfigError（不静默丢弃）。

    传了 ``spec`` 且是 ``generic_http`` 时，额外执行自定义接口的显式白名单与
    身份模板检查；CLI 在还没确定候选模型时先做通用检查，适配器再按具体条目复核。
    """
    if extra is None:
        return
    if not isinstance(extra, dict):
        raise ConfigError("请求参数必须是 key=value 映射")
    _reject_sensitive(extra)
    if spec is not None and getattr(spec, "provider", "") == "generic_http":
        contract = generic_contract(spec)
        _validate_generic_params(extra, contract)


# ---------------------------------------------------------------- generic_http


class GenericContract:
    """``generic_http`` 的可信身份契约。

    ``fields`` 是 ``(location, 路径, 期望值)``；``MISSING`` 表示配置批准该字段
    不存在，实际请求出现即视为越权新增。``protected`` 是含模型身份的模板根键，
    不允许被请求参数整块替换。
    """

    __slots__ = ("method", "url", "fields", "allowed", "protected")

    def __init__(self, method: str, url: str, fields: tuple, allowed: frozenset,
                 protected: frozenset) -> None:
        self.method = method
        self.url = url
        self.fields = fields
        self.allowed = allowed
        self.protected = protected


def _generic_options(spec: ModelSpec) -> dict:
    options = getattr(spec, "options", None)
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ConfigError("自定义接口的 options 必须是映射")
    return options


def _allowed_params(options: dict) -> tuple:
    raw = options.get("allowed_params", [])
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("自定义接口的 options.allowed_params 必须是字段名列表")
    allowed: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError("自定义接口的 options.allowed_params 只能包含非空字段名")
        name = item.strip()
        if name in allowed:
            raise ConfigError(f"自定义接口的 options.allowed_params 重复声明了 {name}")
        if is_sensitive_key(name):
            raise ConfigError(
                f"自定义接口的 options.allowed_params 不能放行 {name}：它可能决定模型、地址或凭据"
            )
        allowed.append(name)
    return tuple(allowed)


def _model_fields(options: dict) -> tuple:
    raw = options.get("model_fields", [])
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("自定义接口的 options.model_fields 必须是点分路径列表")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigError("自定义接口的 options.model_fields 只能包含点分路径字符串")
        dotted = item.strip()
        parts = dotted.split(".")
        if not dotted or any(not part for part in parts):
            raise ConfigError("自定义接口的 options.model_fields 路径不合法")
        if dotted in out:
            raise ConfigError(f"自定义接口的 options.model_fields 重复声明：{dotted}")
        out.append(dotted)
    return tuple(out)


def _walk_leaves(node: Any, prefix: tuple = ()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_leaves(value, prefix + (key,))
        return
    if isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_leaves(value, prefix + (index,))
        return
    yield prefix, node


def _resolve_path(node: Any, path: tuple) -> Any:
    cursor = node
    for part in path:
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        elif isinstance(cursor, list) and isinstance(part, int) and 0 <= part < len(cursor):
            cursor = cursor[part]
        else:
            return MISSING
    return cursor


def _path_text(path: tuple) -> str:
    return ".".join(str(part) for part in path) or "<root>"


def _render_model(text: str, model: str) -> str:
    return _PLACEHOLDER_RE.sub(lambda _match: model, text)


def generic_contract(spec: ModelSpec) -> GenericContract:
    """解析自定义接口的身份契约；配置不自洽时 fail closed。"""
    options = _generic_options(spec)
    submit = options.get("submit") or {}
    if not isinstance(submit, dict):
        raise ConfigError("自定义接口的 options.submit 必须是映射")
    method = str(submit.get("method") or "POST").upper()
    allowed = _allowed_params(options)
    body = submit.get("body")
    if body is None:
        # 与适配器的默认模板保持一致：只发 prompt 和 model。
        body = {"prompt": "{{prompt}}", "model": "{{model}}"}
    model = str(getattr(spec, "model", "") or getattr(spec, "id", ""))
    fields: list[tuple] = []
    protected: set = set()

    for path, value in _walk_leaves(body):
        if not isinstance(value, str):
            continue
        tokens = set(_PLACEHOLDER_RE.findall(value))
        looks_identity = is_identity_key(path[-1]) if path else False
        if "model" in tokens:
            if tokens - {"model"}:
                raise ConfigError(
                    "自定义接口里含 {{model}} 的字段不能再混入 prompt/duration 等运行变量："
                    + _path_text(path)
                )
            expected = _render_model(value, model)
            if not expected:
                raise ConfigError("自定义接口的模型字段渲染为空：" + _path_text(path))
            fields.append(("json_body", tuple(path), expected))
            protected.add(path[0])
        elif looks_identity:
            if tokens:
                raise ConfigError(
                    "自定义接口的模型字段只能使用可信配置或 {{model}}：" + _path_text(path)
                )
            fields.append(("json_body", tuple(path), MISSING if value == "" else value))
            protected.add(path[0])

    for dotted in _model_fields(options):
        path = tuple(dotted.split("."))
        value = _resolve_path(body, path)
        if not isinstance(value, str):
            raise ConfigError(
                f"自定义接口的 options.model_fields 指向的字段不存在或不是字符串：{dotted}"
            )
        tokens = set(_PLACEHOLDER_RE.findall(value))
        if tokens - {"model"}:
            raise ConfigError(f"自定义接口的 options.model_fields 不能依赖运行变量：{dotted}")
        expected = _render_model(value, model)
        if not expected:
            raise ConfigError(f"自定义接口的 options.model_fields 渲染为空：{dotted}")
        fields.append(("json_body", path, expected))
        protected.add(path[0])

    # 可信 params 会在模板渲染之后覆盖顶层字段，身份值同样要按配置冻结。
    params = getattr(spec, "params", None) or {}
    if isinstance(params, dict):
        for key, value in params.items():
            if is_identity_key(key):
                fields.append(("json_body", (key,), copy.deepcopy(value)))
                protected.add(key)

    url = submit.get("url") or getattr(spec, "endpoint", "") or ""
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(
            "自定义接口缺少提交地址：请在模型里填 endpoint 或 options.submit.url"
        )
    if set(_PLACEHOLDER_RE.findall(url)) - {"model"}:
        raise ConfigError(
            "自定义接口的提交地址只能固定或引用 {{model}}，不能用 prompt/duration 等运行参数"
        )
    if method not in ("POST", "PUT", "PATCH"):
        # GET/HEAD 之类的提交不带请求体，模板里的身份字段本来就不会发出去。
        fields = []
    return GenericContract(method, _render_model(url, model), tuple(fields), frozenset(allowed),
                           frozenset(protected))


def _validate_generic_params(extra: dict, contract: GenericContract) -> None:
    for key, value in extra.items():
        name = str(key)
        if name not in contract.allowed:
            raise ConfigError(
                f"自定义接口不接受请求参数 {name}：可变的运行参数必须先在"
                " options.allowed_params 里声明（只支持标量）"
            )
        if name in contract.protected:
            raise ConfigError(f"请求参数 {name} 不能覆盖含模型身份的模板字段")
        if isinstance(value, (dict, list, tuple, set)):
            raise ConfigError(
                f"自定义接口的参数 {name} 只支持标量值，不能整块替换嵌套字段"
            )


# ---------------------------------------------------------------- 提交身份


def _collect_identity(payload: Any, prefix: tuple, location: str,
                      out: list[tuple]) -> None:
    if not isinstance(payload, dict):
        return
    for key, value in payload.items():
        path = prefix + (key,)
        if is_identity_key(key):
            out.append((location, path, copy.deepcopy(value)))
            continue
        if isinstance(value, dict):
            _collect_identity(value, path, location, out)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                _collect_identity(item, path + (index,), location, out)


def _same(actual: Any, expected: Any) -> bool:
    return type(actual) is type(expected) and actual == expected


class SubmissionIdentity:
    """一次提交的身份快照：只存方法、地址与身份槽，不存密钥。"""

    __slots__ = ("method", "url", "fields")

    def __init__(self, method: str, url: str, fields: Iterable[tuple]) -> None:
        self.method = str(method).upper()
        self.url = str(url)
        self.fields = tuple(fields)

    @classmethod
    def from_config(cls, method: str, url: str, *, json_body: Any = None,
                    form: Any = None, headers: Any = None,
                    literal: Iterable[tuple] = ()) -> "SubmissionIdentity":
        """用**配置侧**的值创建快照；绝不能拿合并后的实际请求反向创建。"""
        fields: list[tuple] = []
        for location, payload in (("json_body", json_body), ("form", form)):
            if isinstance(payload, dict):
                _collect_identity(payload, (), location, fields)
        if isinstance(headers, dict):
            for name, value in headers.items():
                if is_identity_key(name):
                    fields.append(("headers", (str(name).lower(),), copy.deepcopy(value)))
        for location, path, value in literal:
            fields.append((location, tuple(path), copy.deepcopy(value)))
        return cls(method, url, fields)

    def validate(self, method: str, url: str, kwargs: dict) -> None:
        """提交前复核；不一致时抛 ConfigError（早于任何生命周期/HTTP 副作用）。"""
        if str(method).upper() != self.method:
            raise ConfigError("提交前身份校验未通过：提交方法与配置批准的方法不一致")
        if str(url) != self.url:
            raise ConfigError("提交前身份校验未通过：提交地址与配置批准的地址不一致")
        actual: dict = {}
        for location in ("json_body", "form"):
            collected: list[tuple] = []
            _collect_identity(kwargs.get(location), (), location, collected)
            for loc, path, value in collected:
                actual[(loc, path)] = value
        headers = kwargs.get("headers")
        if isinstance(headers, dict):
            for name, value in headers.items():
                if is_identity_key(name):
                    actual[("headers", (str(name).lower(),))] = value
        expected = {(loc, path): value for loc, path, value in self.fields}
        collected_pairs = set(actual)
        for (location, path), wanted in expected.items():
            if location == "headers":
                found = actual.get((location, path), MISSING)
            else:
                found = _resolve_path(kwargs.get(location), path)
            if wanted is MISSING:
                if found is not MISSING or (location, path) in collected_pairs:
                    raise ConfigError(
                        "提交前身份校验未通过：配置没有批准该选模字段，实际请求却带上了"
                    )
                continue
            if found is MISSING:
                raise ConfigError(
                    "提交前身份校验未通过：配置批准的选模字段在实际请求里缺失或被替换"
                )
            if not _same(found, wanted):
                raise ConfigError(
                    "提交前身份校验未通过：实际请求的选模字段与配置批准的值不一致"
                )
        for key in collected_pairs:
            if key not in expected:
                raise ConfigError(
                    "提交前身份校验未通过：实际请求多出了配置未批准的选模字段"
                )


__all__ = [
    "MISSING",
    "GenericContract",
    "SubmissionIdentity",
    "generic_contract",
    "is_credential_key",
    "is_identity_key",
    "is_sensitive_key",
    "validate_request_params",
]
