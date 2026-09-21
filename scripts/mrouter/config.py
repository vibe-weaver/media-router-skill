"""配置加载与解析。

职责：
  1. 定位 skill 自身的目录（全部路径相对 skill 解析，绝对不硬编码机器路径）
  2. 读取 config/models.yaml（或 .json），解析成 ModelSpec / Pool / RouterConfig
  3. 解析 API Key：环境变量 → config/secrets.yaml → 模型内联字段，三条路兜底
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import catalog

# mrouter/config.py -> mrouter -> scripts -> <skill_dir>
SKILL_DIR = Path(__file__).resolve().parents[2]


def _dir_from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


#: 配置目录。允许用 MEDIA_ROUTER_CONFIG_DIR 指到别处 ——
#: 自检、试验、多环境隔离时就不会碰到用户真实的 models.yaml / secrets.yaml。
CONFIG_DIR = _dir_from_env("MEDIA_ROUTER_CONFIG_DIR", SKILL_DIR / "config")
STATE_DIR = _dir_from_env("MEDIA_ROUTER_STATE_DIR", SKILL_DIR / "state")
#: 产物默认目录。两级选择（前者优先）：
#:   1. 环境变量 MEDIA_ROUTER_OUTPUT_DIR —— 部署/测试隔离用
#:   2. 当前工作目录 ./outputs —— **默认值**。很多 agent 沙箱把 skill 目录设为只读，
#:      落盘到 cwd（= 用户的工作区）产物才能被用户看到、后续步骤才能引用。
#: 目录**不在 import 时创建**（见 _resolve_default_output_dir），真正落盘时再建。
#: 另外 <skill>/outputs 仍被 /api/file 列为可预览目录，用来兼容早期版本的产物。


def _resolve_default_output_dir() -> Path:
    """只**计算**产物目录，不创建它。

    以前这里顺手 `mkdir` 了，于是"只是 import 一下模块"也会在当前工作目录里
    凭空多出一个 outputs/ —— 副作用不该发生在 import 期。真正需要目录的
    地方（download / _write_b64_to_local）自己会 mkdir，那里失败也能如实报错。
    """
    raw = os.environ.get("MEDIA_ROUTER_OUTPUT_DIR")
    if raw:
        return Path(raw).expanduser()
    return Path.cwd() / "outputs"


DEFAULT_OUTPUT_DIR = _resolve_default_output_dir()

VALID_STRATEGIES = ("priority_then_weight", "weight_only", "fallback_chain")


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def _load_structured(path: Path) -> Any:
    """按扩展名选择解析器：优先用 PyYAML，缺失时回退到内置 miniyaml。

    解析失败一律转成 ConfigError，并且**顺手把原文体检的结果一起带上** ——
    用户打错一个缩进，最有用的是"这个文件还犯了哪些同类错误"，而不是一句
    JSONDecodeError。转成 ConfigError 还有个作用：CLI 会把它标成 kind=config
    而不是 kind=internal，用户一眼就知道是自己的配置问题。
    """
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        # 空文件 = "什么都没配"，不是解析错误。三条路原来的答案各不相同：
        # json.loads("") 抛异常、PyYAML 给 None、miniyaml 给 {} ——
        # 前两种都会让上层报"配置文件顶层必须是映射"，于是"新建一个空的
        # models.web.yaml"在装了 PyYAML 的机器上直接把配置页面和 CLI 一起弄挂。
        return {}
    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path.name} 不是合法的 JSON：{exc}") from exc
    try:
        import yaml  # type: ignore

        parser = yaml.safe_load
    except ImportError:
        from . import miniyaml

        parser = miniyaml.loads
    try:
        parsed = parser(text)
    except Exception as exc:  # noqa: BLE001 - 任何解析错误都要变成可读提示
        hints = lint_text(text)
        detail = "\n".join(f"    - {h}" for h in hints)
        raise ConfigError(
            f"{path.name} 解析失败：{exc}"
            + (f"\n  顺带发现这些问题：\n{detail}" if detail else "")
        ) from exc
    # 只有注释的 YAML：PyYAML 给 None，miniyaml 给 {}。统一成 {}，理由同上。
    return {} if parsed is None else parsed


@dataclass
class Vendor:
    """一个厂商接入点（一个 API Key + 一套接口地址）。

    存在的意义：让"配一次密钥、挂多个模型"成立。
    模型条目用 ``vendor: <id>`` 引用它，就不用每个模型都重复写 provider 和 api_key_env。
    """

    id: str
    provider: str
    label: str = ""
    api_key_env: str = ""
    #: 厂商目录里的条目 key（如 siliconflow 的 provider 是 openai，但校验端点不同）
    catalog: str = ""
    #: 按类目给接口地址，如 {"image": "https://...", "video": "https://..."}
    endpoints: dict[str, str] = field(default_factory=dict)
    #: 厂商级 options，会与模型级 options 合并（模型级优先）
    options: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "label": self.label or self.provider,
            "catalog": self.catalog,
            "api_key_env": self.api_key_env,
            "endpoints": dict(self.endpoints),
            "note": self.note,
        }


@dataclass
class ModelSpec:
    """模型池里的一个条目。"""

    id: str
    provider: str
    kind: str
    model: str = ""
    priority: int = 1
    weight: float = 1.0
    enabled: bool = True
    supports: list[str] = field(default_factory=list)
    api_key_env: str = ""
    endpoint: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    vendor: str = ""
    label: str = ""

    @property
    def is_native(self) -> bool:
        return self.provider == "native"

    def describes(self, requires: list[str]) -> bool:
        """是否满足所需能力。supports 为空视为该类目全支持。"""
        if not requires:
            return True
        if not self.supports:
            return True
        return set(requires).issubset(set(self.supports))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label or self.model or self.id,
            "provider": self.provider,
            "vendor": self.vendor,
            "kind": self.kind,
            "model": self.model or self.id,
            "priority": self.priority,
            "weight": self.weight,
            "enabled": self.enabled,
            "supports": self.supports,
            "endpoint": self.endpoint,
        }


@dataclass
class Pool:
    """某一媒体类目（image / video）的模型池。"""

    kind: str
    strategy: str
    models: list[ModelSpec]

    def enabled(self) -> list[ModelSpec]:
        return [m for m in self.models if m.enabled]


@dataclass
class RouterConfig:
    skill_dir: Path
    config_dir: Path
    state_dir: Path
    config_path: Path
    defaults: dict[str, Any]
    pools: dict[str, Pool]
    secrets: dict[str, Any]
    source_format: str
    warnings: list[str] = field(default_factory=list)
    vendors: dict[str, Vendor] = field(default_factory=dict)
    overlay_path: Path | None = None
    #: 已经记过的告警原文。resolve_api_key 这类"查询方法"可能被反复调用，
    #: 没有这层去重的话 warnings 会无限增长、同一句话在输出里出现好几遍。
    _warned: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        # 构造时已经带的告警也要进"已记"集合，否则 warn() 可能重复追加同一句话
        self._warned.update(self.warnings)

    def warn(self, message: str) -> None:
        """追加一条告警；同一句话只记一次。"""
        if message and message not in self._warned:
            self._warned.add(message)
            self.warnings.append(message)

    # ---------- 查询 ----------

    @property
    def output_dir(self) -> Path:
        custom = self.defaults.get("output_dir")
        if custom:
            return Path(str(custom)).expanduser()
        return DEFAULT_OUTPUT_DIR

    @property
    def health_path(self) -> Path:
        return self.state_dir / "health.json"

    def pool(self, kind: str) -> Pool:
        if kind not in self.pools:
            known = "、".join(sorted(self.pools)) or "（空）"
            raise ConfigError(f"配置里没有类目 {kind!r}；现有类目：{known}")
        return self.pools[kind]

    def kinds(self) -> list[str]:
        return sorted(self.pools)

    def find_model(self, model_id: str) -> ModelSpec:
        """按 id 取**已配置**的模型。

        只认配置里有的 id 是刻意的：路由本来就只从配置的池子里挑，这里是所有
        "指定某个模型"的入口（`generate --model`、`report --model`）共用的唯一
        关卡。调用方 —— 尤其是替用户干活的 AI 助手 —— 不许凭平台文档、模型列表
        或目录里的候选现编一个名字直接调，那等于绕开用户的选择去花他的钱。
        要新模型就让用户在 models.yaml 或配置页面里加，加完这里自然找得到。
        """
        for pool in self.pools.values():
            for spec in pool.models:
                if spec.id == model_id:
                    return spec
        raise ConfigError(
            f"找不到模型 id：{model_id}。只能调用配置里已有的模型"  # 策略，不是笔误
            f"（用 list 子命令看看有哪些）；要新增请先在 config/models.yaml"
            f" 或配置页面里添加。"
        )

    def _health_cfg(self) -> tuple[int, float]:
        hcfg = self.defaults.get("health") or {}
        threshold = int(hcfg.get("failure_threshold", 3) or 3)
        cooldown = float(hcfg.get("cooldown_seconds", 300) or 300)
        return threshold, cooldown

    def health_policy(self) -> tuple[int, float]:
        return self._health_cfg()

    # ---------- 密钥 ----------

    def resolve_api_key(self, spec: ModelSpec) -> tuple[str, str]:
        """返回 (key, 来源说明)。找不到时返回 ("", 原因)。"""
        inline = spec.options.get("api_key") or spec.raw.get("api_key")
        if inline:
            return str(inline), "models.yaml 内联字段"

        secrets_keys = (self.secrets or {}).get("keys") or {}

        if spec.api_key_env:
            env_val = os.environ.get(spec.api_key_env)
            if env_val:
                return env_val.strip(), f"环境变量 {spec.api_key_env}"
            if spec.api_key_env in secrets_keys and secrets_keys[spec.api_key_env]:
                return str(secrets_keys[spec.api_key_env]).strip(), f"secrets 文件 [{spec.api_key_env}]"
        else:
            self.warn(
                f"模型 {spec.id} 未设置 api_key_env，只能从 secrets 文件按 id 取键"
            )

        if spec.id in secrets_keys and secrets_keys[spec.id]:
            return str(secrets_keys[spec.id]).strip(), f"secrets 文件 [{spec.id}]"

        hint = spec.api_key_env or f"{spec.id}"
        return "", f"未找到密钥：请设置环境变量 {hint}，或在 config/secrets.yaml 的 keys 下填写 {hint}"

    # ---------- 导出 ----------

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_dir": str(self.skill_dir),
            "config_path": str(self.config_path),
            "overlay_path": str(self.overlay_path) if self.overlay_path else "",
            "state_dir": str(self.state_dir),
            "output_dir": str(self.output_dir),
            "source_format": self.source_format,
            "vendors": [v.to_dict() for v in self.vendors.values()],
            "defaults": self.defaults,
            "pools": {
                kind: {
                    "strategy": pool.strategy,
                    "models": [m.to_dict() for m in pool.models],
                }
                for kind, pool in self.pools.items()
            },
            "warnings": list(self.warnings),
        }


def coerce_enabled(value: Any) -> bool:
    """把配置里写的 enabled 归一成布尔。

    **空值（写了键没写值）按"启用"处理。** 早期用的是
    ``entry.get("enabled", True)`` —— 键存在、值为 None 时默认值不生效，
    bool(None) 变成 False，于是一行没写完的 `enabled:` 会让模型静默停用，
    用户只看到"没有可用模型"，完全查不出原因。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off")
    return bool(value)


def _coerce_spec(
    kind: str,
    entry: dict[str, Any],
    index: int,
    warnings: list[str],
    vendors: dict[str, Vendor],
) -> ModelSpec:
    if not isinstance(entry, dict):
        raise ConfigError(f"{kind}.models[{index}] 必须是映射，实际是 {type(entry).__name__}")

    model_id = entry.get("id")
    if not model_id:
        raise ConfigError(f"{kind}.models[{index}] 缺少必填字段 id")

    # 先解析厂商引用：provider / api_key_env / 接口地址都可以由厂商提供
    vendor_id = str(entry.get("vendor") or "").strip()
    vendor = vendors.get(vendor_id) if vendor_id else None
    if vendor_id and vendor is None:
        raise ConfigError(
            f"模型 {model_id} 引用了不存在的厂商 id：{vendor_id}"
            f"（现有厂商：{'、'.join(sorted(vendors)) or '无'}）"
        )

    provider = str(entry.get("provider") or "").strip()
    if not provider and vendor:
        provider = vendor.provider
    if not provider:
        raise ConfigError(f"模型 {model_id} 缺少 provider，也没有可继承的 vendor")

    api_key_env = str(entry.get("api_key_env") or "").strip()
    if not api_key_env and vendor:
        api_key_env = vendor.api_key_env

    endpoint = str(entry.get("endpoint") or "").strip()
    if not endpoint and vendor:
        endpoint = str(vendor.endpoints.get(kind) or "").strip()

    # 厂商级 options 打底，模型级覆盖。
    # options / params 必须是映射 —— 写成列表时 dict(...) 会抛 TypeError，
    # 那会被 CLI 归成"内部错误"，用户根本看不出是自己配置写错了。
    raw_options = entry.get("options")
    if raw_options is not None and not isinstance(raw_options, dict):
        raise ConfigError(
            f"模型 {model_id} 的 options 必须是映射（key: value），"
            f"实际是 {type(raw_options).__name__}"
        )
    options: dict[str, Any] = {}
    if vendor:
        options.update(vendor.options or {})
    options.update(raw_options or {})
    if entry.get("api_key"):
        options["api_key"] = entry["api_key"]

    raw_params = entry.get("params")
    if raw_params is not None and not isinstance(raw_params, dict):
        raise ConfigError(
            f"模型 {model_id} 的 params 必须是映射（key: value），"
            f"实际是 {type(raw_params).__name__}"
        )

    supports_raw = entry.get("supports") or []
    if isinstance(supports_raw, str):
        supports = [s.strip() for s in supports_raw.split(",") if s.strip()]
    elif isinstance(supports_raw, (list, tuple)):
        supports = [str(s).strip() for s in supports_raw if str(s).strip()]
    else:
        # `supports: 5` 这种写法原来会一路走到 `for s in supports_raw` 抛 TypeError，
        # 被 CLI 归成 kind=internal。与 params / options 的形状校验保持一致。
        raise ConfigError(
            f"模型 {model_id} 的 supports 必须是列表或逗号分隔的字符串，"
            f"实际是 {type(supports_raw).__name__}"
        )

    reserved = {
        "id",
        "vendor",
        "label",
        "name",
        "provider",
        "model",
        "priority",
        "weight",
        "enabled",
        "supports",
        "api_key_env",
        "endpoint",
        "params",
        "options",
        "api_key",
        "note",
        "description",
    }

    try:
        priority = int(entry.get("priority", 1))
    except (TypeError, ValueError):
        warnings.append(f"模型 {model_id} 的 priority 非法，已回退为 1")
        priority = 1

    try:
        weight = float(entry.get("weight", 1))
    except (TypeError, ValueError):
        warnings.append(f"模型 {model_id} 的 weight 非法，已回退为 1")
        weight = 1.0
    if weight < 0:
        warnings.append(f"模型 {model_id} 的 weight 为负，已归零")
        weight = 0.0

    return ModelSpec(
        id=str(model_id),
        provider=provider,
        kind=kind,
        model=str(entry.get("model") or model_id),
        priority=priority,
        weight=weight,
        enabled=coerce_enabled(entry.get("enabled", True)),
        supports=supports,
        api_key_env=api_key_env,
        endpoint=endpoint,
        params=dict(raw_params or {}),
        options=options,
        raw=dict(entry),
        vendor=vendor_id,
        label=str(entry.get("label") or entry.get("name") or "").strip(),
    )


def _coerce_vendors(raw: Any, warnings: list[str]) -> dict[str, Vendor]:
    vendors: dict[str, Vendor] = {}
    if not raw:
        return vendors
    if not isinstance(raw, list):
        raise ConfigError("vendors 必须是列表")
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"vendors[{index}] 必须是映射")
        if is_tombstone(entry):
            # 厂商墓碑：配置页面删掉一条写在 models.yaml 里的厂商时留下的标记，
            # 用来盖住手写层那条。_merge_config 走 _dedupe_last 时会丢掉它，但直接
            # 调 build_vendors 的地方（webserver 拼两层测真实生成）会拿到原始列表 ——
            # 墓碑没有 provider，不跳过就会报"缺少必填字段 provider"。
            continue
        vendor_id = str(entry.get("id") or "").strip()
        if not vendor_id:
            raise ConfigError(f"vendors[{index}] 缺少必填字段 id")
        provider = str(entry.get("provider") or "").strip()
        if not provider:
            raise ConfigError(f"厂商 {vendor_id} 缺少必填字段 provider")

        catalog_key = str(entry.get("catalog") or "").strip()
        endpoints_raw = entry.get("endpoints") or {}
        endpoints = (
            {str(k): str(v) for k, v in endpoints_raw.items() if v}
            if isinstance(endpoints_raw, dict)
            else {}
        )
        # 保存端刻意省略"与目录默认值相同"的地址（见 store.upsert_vendor），
        # 所以缺的必须在这里按 catalog 补回来。少了这一步 endpoint 就是空串，
        # 适配器会回落到 provider 自己的内置默认地址 —— 而智谱、硅基流动的
        # provider 都是 openai，请求于是被静默发到 api.openai.com：
        # 用户配的是 A 厂商，打到的是 B 厂商，而且"测试连通性"还是绿的
        # （probe / webserver 各自都有目录兜底，只有生成这条路没有）。
        # 文件里写了的地址优先（中转 / 自建网关），这里只补缺的。
        catalog_entry = catalog.get(catalog_key)
        if catalog_entry is not None:
            for pool_kind, url in (catalog_entry.endpoints or {}).items():
                if url:
                    endpoints.setdefault(str(pool_kind), str(url))
        options_raw = entry.get("options")
        if options_raw is not None and not isinstance(options_raw, dict):
            raise ConfigError(
                f"厂商 {vendor_id} 的 options 必须是映射，"
                f"实际是 {type(options_raw).__name__}"
            )
        if vendor_id in vendors:
            # 同 id 只保留最后一条（叠加层覆盖手写层，语义和模型一致）。
            # 静默丢一个厂商很难查，所以在 _merge_config 里按 id 去重后再到这里，
            # 真出现重复就说明同一份文件里写了两遍 —— 那也只需要提示一次。
            warnings.append(f"厂商 id {vendor_id} 在同一份配置里定义了多次，只有最后一条生效")
        vendors[vendor_id] = Vendor(
            id=vendor_id,
            provider=provider,
            label=str(entry.get("label") or entry.get("name") or "").strip(),
            api_key_env=str(entry.get("api_key_env") or "").strip(),
            catalog=catalog_key,
            endpoints=endpoints,
            options=dict(options_raw or {}),
            note=str(entry.get("note") or "").strip(),
            raw=dict(entry),
        )
    return vendors


def _first_existing(names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = CONFIG_DIR / name
        if candidate.exists():
            return candidate
    return None


def _config_paths(allow_missing: bool = False) -> tuple[list[Path], Path | None]:
    """返回 (要加载的文件列表, overlay 路径)。

    models.yaml 是给人手写的（带注释），models.web.yaml 是配置页面生成的。
    后者叠加在前者之上，这样 Web 配置不会抹掉你手写的注释。
    ``allow_missing=True`` 时，两个文件都不存在也不报错 —— 配置页面需要
    在"全新安装、什么都还没配"的状态下也能打开。
    """
    override = os.environ.get("MEDIA_ROUTER_CONFIG")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        if not candidate.exists():
            raise ConfigError(f"MEDIA_ROUTER_CONFIG 指向的文件不存在：{candidate}")
        return [candidate], None

    base = _first_existing(("models.yaml", "models.yml", "models.json"))
    overlay = _first_existing(("models.web.yaml", "models.web.yml", "models.web.json"))
    if base is None and overlay is None:
        if allow_missing:
            return [], None
        raise ConfigError(
            f"在 {CONFIG_DIR} 下找不到配置文件。"
            f"可以运行 `media_router.py web` 用配置页面生成，"
            f"或参考 references/providers.md 手写 models.yaml。"
        )
    paths = [p for p in (base, overlay) if p is not None]
    return paths, overlay


def _merge_pool(base_pool: Any, overlay_pool: Any) -> dict[str, Any]:
    """同一个类目的两层合并：标量后者胜，models 列表按"底层在前、叠加层在后"拼接。

    只在**至少有一层真的写了 models** 时才生成 models 键 —— 否则把一个普通
    字典硬塞一个空 models 进去，会被 load_config 当成一个空模型池。
    """
    base_dict = dict(base_pool) if isinstance(base_pool, dict) else {}
    overlay_dict = dict(overlay_pool) if isinstance(overlay_pool, dict) else {}
    merged = dict(base_dict)
    merged.update(overlay_dict)
    if "models" in base_dict or "models" in overlay_dict:
        merged["models"] = list(base_dict.get("models") or []) + list(
            overlay_dict.get("models") or []
        )
    return merged


def _merge_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """把 overlay 叠加到 base 上。列表按 id 去重时后者胜（在后面统一处理）。"""
    out = dict(base)
    for key, value in overlay.items():
        if key == "version":
            continue
        if key == "defaults":
            merged_defaults = dict(base.get("defaults") or {})
            for dk, dv in (value or {}).items():
                if isinstance(dv, dict) and isinstance(merged_defaults.get(dk), dict):
                    merged_defaults[dk] = {**merged_defaults[dk], **dv}
                else:
                    merged_defaults[dk] = dv
            out["defaults"] = merged_defaults
            continue
        if key == "vendors":
            # 按 id 去重、后者胜（叠加层覆盖手写层，与 models 的语义保持一致）。
            # 早期是简单拼接，同 id 会在 _coerce_vendors 里互相盖掉，
            # 哪个生效取决于顺序，用户会莫名其妙少一个厂商。
            out["vendors"] = _dedupe_last(
                list(base.get("vendors") or []) + list(value or [])
            )
            continue
        base_node = base.get(key)
        # 叠加层里只有 strategy 没写 models 时，**不能**整体覆盖 ——
        # 那会把手写层里这一整个类目的模型全部抹掉。
        if isinstance(value, dict) and (
            "models" in value
            or (isinstance(base_node, dict) and isinstance(base_node.get("models"), list))
        ):
            out[key] = _merge_pool(base_node, value)
            continue
        out[key] = value
    return out


#: 叠加层里用来"盖掉"手写层条目的墓碑标记
TOMBSTONE_KEY = "_deleted"


def is_tombstone(entry: Any) -> bool:
    """这条是不是墓碑（配置页面用来删掉手写层条目的标记）。"""
    return bool(isinstance(entry, dict) and entry.get(TOMBSTONE_KEY))


_is_tombstone = is_tombstone  # 兼容包内旧写法


def _dedupe_last(items: list[Any]) -> list[Any]:
    """同 id 时保留**最后一次**定义的内容，但保持**首次出现**的位置。

    这样 models.yaml 里手写的模型排在前面、配置页面新增的排在后面，
    而配置页面又能覆盖同 id 的手写条目。

    带 ``_deleted: true`` 的条目是**墓碑**：配置页面要删掉一条写在 models.yaml
    里的模型，但它不能去改用户手写的文件，于是在叠加层里放一条同 id 的墓碑把
    手写层那条盖掉。墓碑在合并后就被丢掉，不会进入任何下游逻辑。

    **形状不对的条目一律原样透传，绝不在这里静默丢弃。** 早期版本把"不是 dict"
    和"没有 id"的条目收进一个 no_id 列表然后……没有返回它，于是
    ``models: [{provider: x}]``（漏了 id）会凭空消失，用户看到一个空池子却
    没有任何提示。现在这些条目会走到 _coerce_spec，由它给出"第 N 条缺少必填
    字段 id"这类可读错误。
    """
    order: list[tuple[str, Any]] = []
    by_id: dict[str, Any] = {}
    for entry in items:
        if not isinstance(entry, dict):
            order.append(("raw", entry))
            continue
        key = str(entry.get("id") or "")
        if not key:
            order.append(("raw", entry))
            continue
        if key not in by_id:
            order.append(("id", key))
        by_id[key] = entry
    out: list[Any] = []
    for kind, item in order:
        if kind == "raw":
            out.append(item)
        elif not is_tombstone(by_id[item]):
            out.append(by_id[item])
    return out


def visible_entries(entries: list[Any]) -> list[Any]:
    """按加载语义整理一个模型列表：同 id 取后者、丢掉墓碑。

    配置页面用它保证"页面上看到的"就是"运行时真正用的" ——
    少了这一步，同一条模型会显示两遍（手写层一遍、覆盖层一遍），
    已经"删掉"的模型也会又被列出来。
    """
    return _dedupe_last(entries)


def base_layer() -> dict[str, Any]:
    """只读**手写层**（models.yaml / models.json）的原始数据，不含叠加层。

    配置页面需要它来回答两个问题：这条模型是用户手写的还是在页面里建的？
    删掉手写层的模型要写墓碑，删掉页面里的模型直接摘掉就行。
    """
    override = os.environ.get("MEDIA_ROUTER_CONFIG")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        if not candidate.exists():
            return {}
        layer = _load_structured(candidate)
        return layer if isinstance(layer, dict) else {}

    base = _first_existing(("models.yaml", "models.yml", "models.json"))
    if base is None:
        return {}
    layer = _load_structured(base)
    return layer if isinstance(layer, dict) else {}


def layer_models(layer: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """从某一层的原始数据里取出全部 (类目, 条目)。墓碑会被跳过。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for key, value in (layer or {}).items():
        if key in ("version", "defaults", "vendors") or not isinstance(value, dict):
            continue
        entries = value.get("models")
        if not isinstance(entries, list):
            continue
        for item in entries:
            if isinstance(item, dict) and item.get("id") and not is_tombstone(item):
                out.append((key, item))
    return out


def read_structured(path: Path) -> Any:
    """读取单个结构化文件（按扩展名选解析器）。配置页面用它读叠加层。"""
    return _load_structured(path)


#: 裸写的 `a:b`：YAML 1.1 会把它读成**六十进制整数**（1:1 = 61）。
#: `aspect_ratio: 1:1` 这种写法太自然了，静默变成 61 会直接发给接口，
#: 所以在读之前先扫一遍原文，把它挑出来告诉用户。
_BARE_SEXAGESIMAL = re.compile(
    r"^[ \t]*(?P<key>[^#\s:][^:]*?)[ \t]*:[ \t]*"
    r"(?P<val>[-+]?\d[\d_]*(?::[0-5]?\d)+)[ \t]*(?:#.*)?$"
)
_BARE_TIMESTAMP = re.compile(
    r"^[ \t]*(?P<key>[^#\s:][^:]*?)[ \t]*:[ \t]*"
    r"(?P<val>\d{4}-\d{1,2}-\d{1,2}(?:[Tt ].*)?)[ \t]*(?:#.*)?$"
)
_BARE_PLACEHOLDER = re.compile(
    r"^[ \t]*(?P<key>[^#\s:][^:]*?)[ \t]*:[ \t]*(?P<val>\{\{.*)$"
)
#: 值里裸写「冒号+空格」。YAML 不允许，PyYAML 会直接报错。
_COLON_IN_VALUE = re.compile(
    r"^[ \t]*(?P<key>[^#\s:][^:]*?)[ \t]*:[ \t]*(?P<lead>[^\s#'\"\[{])"
    r"(?P<val>.*?:[ \t].*)$"
)
#: 行内映射里冒号后没空格：`{size:1024x1024}`。PyYAML 会把它读成一个
#: 名叫 "size:1024x1024" 的键、值为 null —— 静默产出没意义的结果。
_UNSPACED_INLINE = re.compile(
    r"(?P<map>\{[^{}\n]*?[A-Za-z0-9_\u4e00-\u9fff]+:[^ \t,{}][^{}\n]*\})"
)


def lint_text(text: str) -> list[str]:
    """扫原文，把"能被解析但结果不是用户想的那样"的写法挑出来。

    这一层刻意做在**文本**上而不是解析器里 —— 因为装了 PyYAML 和没装是两条
    不同的解析路径，只有在文本层面才能同时覆盖到。
    """
    out: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _BARE_SEXAGESIMAL.match(line)
        if match:
            raw = match.group("val")
            total = 0
            for part in raw.split(":"):
                total = total * 60 + int(part)
            out.append(
                f"第 {lineno} 行 {match.group('key').strip()}: {raw} "
                f"没加引号，YAML 会把它当成六十进制数字读成 {total}。"
                f'比值、时间这类值要写成 "{raw}"（该行内容：{line.strip()}）'
            )
            continue
        match = _BARE_TIMESTAMP.match(line)
        if match:
            out.append(
                f"第 {lineno} 行 {match.group('key').strip()} 是日期形状且没加引号，"
                f"装了 PyYAML 的环境会读成 date 对象、没装的读成字符串。"
                f'加引号固定成字符串："{match.group("val")}"'
            )
            continue
        match = _BARE_PLACEHOLDER.match(line)
        if match:
            out.append(
                f"第 {lineno} 行 {match.group('key').strip()} 的值以 {{{{ 开头，"
                f'会被当成行内映射。占位符要加引号："{match.group("val").strip()}"'
            )
            continue
        match = _UNSPACED_INLINE.search(line)
        if match:
            out.append(
                f"第 {lineno} 行 {match.group('map')} 里的冒号后面少了空格，"
                f"YAML 会把整段当成一个键名（值为 null）。写成 "
                f"{match.group('map').replace(':', ': ', 1)}"
            )
            continue
        match = _COLON_IN_VALUE.match(line)
        if match:
            out.append(
                f"第 {lineno} 行 {match.group('key').strip()} 的值里有没加引号的"
                f"「冒号+空格」，YAML 不允许这样写（PyYAML 会直接报错）。"
                f'加引号：{match.group("key").strip()}: "{match.group("lead")}{match.group("val")}"'
            )
    return out


def _secrets_paths() -> list[Path]:
    """要合并的密钥文件，从低到高。

    secrets.yaml 是给人手写的，secrets.web.yaml 是配置页面生成的 —— 与
    models.yaml / models.web.yaml 同一套双层思路，页面不会覆盖你手写的注释。
    """
    override = os.environ.get("MEDIA_ROUTER_SECRETS")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        return [candidate] if candidate.exists() else []

    base = _first_existing(("secrets.yaml", "secrets.yml", "secrets.json"))
    overlay = _first_existing(
        ("secrets.web.yaml", "secrets.web.yml", "secrets.web.json")
    )
    return [p for p in (base, overlay) if p is not None]


def secrets_paths() -> list[Path]:
    """所有参与合并的密钥文件，从低到高。"""
    return _secrets_paths()


def locate_secrets() -> Path | None:
    """手写的那份密钥文件（存在时）。配置页面只读它，不写它。"""
    paths = _secrets_paths()
    return paths[0] if paths else None


def secrets_write_path() -> Path:
    """配置页面写密钥的目标文件。"""
    for name in ("secrets.web.yaml", "secrets.web.yml", "secrets.web.json"):
        candidate = CONFIG_DIR / name
        if candidate.exists():
            return candidate
    return CONFIG_DIR / "secrets.web.yaml"


def secrets_readonly_reason() -> str:
    """返回不可写的原因（空串表示可写）。"""
    if os.environ.get("MEDIA_ROUTER_SECRETS"):
        return (
            "环境变量 MEDIA_ROUTER_SECRETS 指定了密钥文件，"
            "配置页面不会写入它；请直接编辑该文件。"
        )
    return ""


def load_secrets(warnings: list[str] | None = None) -> dict[str, Any]:
    """合并所有密钥文件。后加载的覆盖先加载的，空值不覆盖非空值。"""
    merged: dict[str, Any] = {}
    for path in _secrets_paths():
        try:
            loaded = _load_structured(path)
        except Exception as exc:  # noqa: BLE001 - 密钥文件坏了不该阻断运行
            if warnings is not None:
                warnings.append(f"secrets 文件解析失败（已忽略）：{path} -> {exc}")
            continue
        if not isinstance(loaded, dict):
            continue
        for key, value in loaded.items():
            if key == "keys" and isinstance(value, dict):
                bucket = merged.setdefault("keys", {})
                for k, v in value.items():
                    if v is None or str(v).strip() == "":
                        continue  # 空占位不能把另一份文件里的真实密钥抹掉
                    bucket[k] = v
            else:
                merged[key] = value
    return merged


# ---------------------------------------------------------------- 原始层读取


def load_raw(allow_missing: bool = False) -> tuple[dict[str, Any], list[Path], Path | None]:
    """只做解析与叠加，不做语义校验。

    返回 (叠加后的原始数据, 参与的文件列表, overlay 路径)。

    配置页面靠它来回答"文件里到底写了什么" —— 即使 models.yaml 被手写改坏了，
    页面照样能打开去修，而不是直接报错关门。
    """
    paths, overlay_path = _config_paths(allow_missing=allow_missing)
    data: dict[str, Any] = {}
    for path in paths:
        layer = _load_structured(path)
        if not isinstance(layer, dict):
            raise ConfigError(f"配置文件顶层必须是映射：{path}")
        raw_defaults = layer.get("defaults")
        if raw_defaults is not None and not isinstance(raw_defaults, dict):
            # 不拦在这里的话，_merge_config 的 `.items()` 会抛 AttributeError、
            # load_config 的 `dict(...)` 会抛 TypeError —— 都不是 ConfigError，
            # CLI 归成 kind=internal，用户只看到"内部错误"，想不到是自己写错了形状。
            raise ConfigError(
                f"{path.name} 的顶层 defaults 必须是映射（key: value），"
                f"实际是 {type(raw_defaults).__name__}"
            )
        data = _merge_config(data, layer)
    return data, paths, overlay_path


def lint_files(paths: list[Path] | None = None, overlay: Path | None = None) -> list[str]:
    """对所有参与加载的文件做一次原文体检（见 lint_text）。

    注意 ``_config_paths`` 返回的 paths **已经包含** overlay，所以这里必须去重 ——
    否则叠加层会被体检两遍，同一句告警在页面/输出里各出现两次。
    """
    if paths is None:
        paths, overlay = _config_paths(allow_missing=True)
    targets: list[Path] = []
    for path in list(paths) + ([overlay] if overlay else []):
        if path not in targets:
            targets.append(path)
    out: list[str] = []
    for path in targets:
        if path.suffix.lower() not in (".yaml", ".yml"):
            continue  # JSON 没有这些歧义
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for message in lint_text(text):
            out.append(f"{path.name}：{message}")
    return out


def build_vendors(raw: Any) -> dict[str, Vendor]:
    """把原始 vendors 列表解析成 Vendor 表（忽略非致命告警）。"""
    return _coerce_vendors(raw, [])


def build_spec_from_entry(
    kind: str, entry: dict[str, Any], vendors: dict[str, Vendor] | None = None
) -> ModelSpec:
    """按配置语义把一条模型条目解析成 ModelSpec。

    入口和 models.yaml 走的是同一套继承规则 —— 页面表单里没写的 provider /
    api_key_env / endpoint 同样能从厂商继承，不会出现"页面里能用、写进文件就不认"。
    """
    return _coerce_spec(kind, dict(entry), 0, [], vendors or {})


def stub_config() -> RouterConfig:
    """降级配置：只带密钥，没有模型池。

    给配置页面用 —— models.yaml 被手写改坏时，页面仍然需要能测连通性，
    不能因为一个语法错误就把用户挡在门外。
    """
    return RouterConfig(
        skill_dir=SKILL_DIR,
        config_dir=CONFIG_DIR,
        state_dir=STATE_DIR,
        config_path=CONFIG_DIR / "models.yaml",
        defaults={},
        pools={},
        secrets=load_secrets(),
        source_format="yaml",
    )


def load_config() -> RouterConfig:
    """加载并校验配置（models.yaml 打底 → models.web.yaml 叠加）。"""
    warnings: list[str] = []
    data, paths, overlay_path = load_raw()
    config_path = paths[0]

    # 先扫原文：有些写法能被解析，但结果不是用户想要的那个值
    warnings.extend(lint_files(paths, overlay_path))

    defaults = dict(data.get("defaults") or {})
    vendors = _coerce_vendors(data.get("vendors"), warnings)

    pools: dict[str, Pool] = {}
    for key, value in data.items():
        if key in ("version", "defaults", "vendors"):
            continue
        if not isinstance(value, dict) or "models" not in value:
            continue
        entries = value.get("models") or []
        if not isinstance(entries, list):
            raise ConfigError(f"{key}.models 必须是列表")
        strategy = str(value.get("strategy") or "priority_then_weight").strip()
        if strategy not in VALID_STRATEGIES:
            warnings.append(
                f"{key}.strategy={strategy!r} 不在 {VALID_STRATEGIES} 中，已回退为 priority_then_weight"
            )
            strategy = "priority_then_weight"
        specs = [
            _coerce_spec(key, entry, idx, warnings, vendors)
            for idx, entry in enumerate(_dedupe_last(entries))
        ]
        pools[key] = Pool(kind=key, strategy=strategy, models=specs)

    if not pools:
        raise ConfigError(f"配置里没有任何模型池（需要一个含 models 列表的类目）：{config_path}")

    secrets = load_secrets(warnings)

    # 两个运行时目录允许被 config 覆盖，方便自检/多环境隔离
    state_override = defaults.get("state_dir")
    state_dir = Path(str(state_override)).expanduser() if state_override else STATE_DIR

    return RouterConfig(
        skill_dir=SKILL_DIR,
        config_dir=CONFIG_DIR,
        state_dir=state_dir,
        config_path=config_path,
        overlay_path=overlay_path,
        defaults=defaults,
        pools=pools,
        secrets=secrets,
        vendors=vendors,
        source_format=config_path.suffix.lower().lstrip("."),
        warnings=warnings,
    )
