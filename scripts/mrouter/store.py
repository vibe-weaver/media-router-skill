"""配置页面的持久层。

页面上的每一次"保存"最终都落到两个文件里，分工很明确：

  config/models.web.yaml    厂商 + 模型（不含任何密钥，可以安全提交）
  config/secrets.web.yaml   密钥（已在 .gitignore 里）

为什么不直接写 models.yaml / secrets.yaml：那两个是给人手写的、带注释的。
页面只做"叠加层"，同 id 覆盖、不同 id 追加，你手写的内容和注释一个字都不会少。

所有写操作都走 `_WRITE_LOCK`，因为 Web 服务是多线程的（测试连通性要并发，
否则点一下测试整个页面就卡死）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import catalog, config, health, miniyaml

#: 最近一次「叠加层坏掉 → 备份后重建」的说明。
#: bootstrap 会把它带进 warnings，用户下次刷新页面就知道发生过什么。
LAST_REPAIR_NOTE = ""


def _kind_hint() -> str:
    """类目写错时的统一提示。写清"有哪些"比只说"错了"有用得多。"""
    names = "、".join(f"{k}（{config.KIND_LABELS.get(k, k)}）" for k in config.MODEL_KINDS)
    return f"模型类目只能是 {names}"


OVERLAY_NAMES = ("models.web.yaml", "models.web.yml", "models.web.json")
OVERLAY_DEFAULT = "models.web.yaml"

HEADER = """\
# ============================================================================
# 这个文件由配置页面生成（media_router.py web）。
# 想手写请改同目录下的 models.yaml，那份是本文件的"底层"，注释不会被动。
# ----------------------------------------------------------------------------
# 加载顺序：models.yaml 先加载，本文件叠加在上；同 id 的条目以本文件为准。
# 密钥不在这个文件里，而在 config/secrets.web.yaml，且不含任何明文以外的信息。
# ============================================================================

"""

SECRETS_HEADER = """\
# ============================================================================
# 配置页面保存的密钥。这个文件已在 .gitignore 里，不会被提交。
# 想手写密钥请改同目录下的 secrets.yaml —— 那是本文件的"底层"。
# ============================================================================

"""

_WRITE_LOCK = threading.RLock()


class ConfigReadOnly(RuntimeError):
    """当前配置来源不允许写入。"""


# ---------------------------------------------------------------- 路径


def overlay_path() -> Path:
    """配置页面写的那份文件（沿用已存在的后缀）。"""
    for name in OVERLAY_NAMES:
        candidate = config.CONFIG_DIR / name
        if candidate.exists():
            return candidate
    return config.CONFIG_DIR / OVERLAY_DEFAULT


def readonly_reason() -> str:
    """返回不可写的原因，空串表示可写。"""
    if os.environ.get("MEDIA_ROUTER_CONFIG"):
        return (
            "当前通过环境变量 MEDIA_ROUTER_CONFIG 指定了配置文件，"
            "配置页面只会读取它，不会写入。要去掉这个变量才能在这里保存。"
        )
    return ""


def _guard_writable() -> None:
    reason = readonly_reason()
    if reason:
        raise ConfigReadOnly(reason)


# ---------------------------------------------------------------- id 生成


def _slug(text: str) -> str:
    """把模型名压成一个能用当 id 的短串。"""
    value = str(text or "").strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-._")
    return value[:60]


def _unique_id(base: str, taken: set[str]) -> str:
    if not base:
        base = "m"
    if base not in taken:
        return base
    index = 2
    while f"{base}-{index}" in taken:
        index += 1
    return f"{base}-{index}"


def _unique_env(base: str, taken: set[str]) -> str:
    """环境变量名只允许字母数字下划线。

    这里**不能**复用 _unique_id —— 它用连字符做分隔，
    而 ARK_API_KEY-2 这种名字在 shell / .env 里都是非法的。
    """
    if base not in taken:
        return base
    index = 2
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"


def coerce_options(raw: Any) -> dict[str, Any]:
    """把调用方送来的 options 归一成 dict。

    配置页面的"接口参数"输入框是文本框，``collectOptions()`` 返回的是**原始
    字符串**；终端调用方则直接送 dict。两条路都要认。

    只认 dict 的话，同一个表单"保存"能成、点"测试连通性"直接崩
    （``dict("...")`` 抛 ``ValueError: dictionary update sequence element #0
    has length 1; 2 is required``，页面只显示一句看不懂的 400），
    用户完全看不出这两处差在哪。形状彻底不对时报可读的配置错误。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise config.ConfigError(
                f"接口参数不是合法的 JSON：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
            ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise config.ConfigError(
            f"接口参数必须是 JSON 对象（{{...}}），实际是 {type(raw).__name__}"
        )
    return {str(k): v for k, v in raw.items()}


# ---------------------------------------------------------------- 叠加层读写


def read_overlay() -> dict[str, Any]:
    """读页面生成的那份文件；不存在时返回空壳。"""
    path = overlay_path()
    if not path.exists():
        return {"version": 1}
    loaded = config.read_structured(path)
    if not isinstance(loaded, dict):
        raise config.ConfigError(f"配置文件顶层必须是映射：{path}")
    return loaded


def read_secrets_overlay() -> dict[str, Any]:
    path = config.secrets_write_path()
    if not path.exists():
        return {"keys": {}}
    try:
        loaded = config.read_structured(path)
    except Exception:  # noqa: BLE001 - 坏文件直接重建，密钥本来就该由页面托管
        return {"keys": {}}
    return loaded if isinstance(loaded, dict) else {"keys": {}}


def write_structured(path: Path, data: dict[str, Any], header: str) -> None:
    """写回结构化文件。

    一律用内置 miniyaml 序列化，而不是环境里碰巧装了的 PyYAML ——
    同一份配置在不同机器上写出来的格式必须一模一样。

    **后缀是 ``.json`` 时必须写 JSON。** ``overlay_path()`` 会沿用已存在的后缀，
    而这里原来不管后缀一律写"注释头 + YAML"：用户手上有一个 models.web.json 的话，
    页面上第一次保存就把它写成非法 JSON，之后 load_config 报
    "models.web.json 不是合法的 JSON"，CLI 和配置页面一起打不开 ——
    而元凶就是页面自己，用户没法自救。JSON 没有注释语法，头部只能不写。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    else:
        text = header + miniyaml.dumps(data)
    # tmp 名带上 pid。_WRITE_LOCK 只管得住本进程：两个进程（比如终端里跑
    # generate 的同时开着配置页面）写同一个 tmp 再各自 replace，后写进去的
    # 内容会被前一个 replace 顺手覆盖掉，且没有任何报错。
    tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)  # 原子替换，避免写一半被读到
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 叠加层操作


class Overlay:
    """对 models.web.yaml 的读写。所有变更都在内存里改完再整体落盘。

    **必须同时认识手写层。** 页面列的是"手写层 + 叠加层"合并后的结果，
    如果只盯着叠加层，就会出现「明明看得见、点删除却报找不到」这种自相矛盾 ——
    只读这一层是不够的。

    两层的能力不一样，这点必须如实体现给用户：

      * 叠加层里的条目：随便改、随便删，写进 models.web.yaml 就行。
      * 手写层里的条目：**不能改用户手写的文件**。要删就在叠加层写一条同 id 的
        墓碑（``_deleted: true``）把它盖掉；要停用/改参数就写一条同 id 的覆盖条目。
    """

    def __init__(
        self, data: dict[str, Any] | None = None, base: dict[str, Any] | None = None
    ) -> None:
        self.data: dict[str, Any] = dict(data or {})
        self.data.setdefault("version", 1)
        self.data.setdefault("vendors", [])
        if not isinstance(self.data["vendors"], list):
            raise config.ConfigError("models.web.yaml 的 vendors 必须是列表")
        #: 手写层的原始数据，只读。
        #: soft=True：手写层写坏时按空层处理，页面还能打开（见 bootstrap 的告警）
        self.base: dict[str, Any] = (
            base if base is not None else config.base_layer(soft=True)
        )

    # ---------- 查询 ----------

    @property
    def vendors(self) -> list[dict[str, Any]]:
        return self.data["vendors"]

    def find_vendor(self, vendor_id: str) -> dict[str, Any] | None:
        """在两层里找厂商。叠加层优先。

        **必须认手写层。** 页面上列的是"手写层 + 叠加层"合并后的厂商，
        只盯叠加层的话，用户在 models.yaml 里手写的 `vendors:` 会出现在列表里、
        却一保存就报"找不到厂商" —— 看得见、用不了。
        """
        found = self.locate_vendor(vendor_id)
        return found[0] if found else None

    def base_vendors(self) -> list[dict[str, Any]]:
        """手写层里的厂商条目（只读）。"""
        raw = self.base.get("vendors")
        if not isinstance(raw, list):
            return []
        return [v for v in raw if isinstance(v, dict) and v.get("id")]

    def overlay_vendors(self) -> list[dict[str, Any]]:
        """叠加层里**活着**的厂商条目（墓碑不算）。

        厂商和模型一样有墓碑：删掉一条写在 models.yaml 里的厂商时，页面不能去改
        用户手写的文件，只能在叠加层放一条 ``_deleted: true`` 把它盖掉。
        所有"找厂商"的地方都必须跳过墓碑，否则删掉的厂商在页面上又找得到。
        """
        return [
            item
            for item in self.vendors
            if isinstance(item, dict) and not config.is_tombstone(item)
        ]

    def _drop_vendor_tombstone(self, vendor_id: str) -> None:
        """把某个厂商的墓碑清掉（用户重新加回来 / 改回来时用）。"""
        self.data["vendors"] = [
            v
            for v in self.vendors
            if not (
                isinstance(v, dict)
                and str(v.get("id")) == vendor_id
                and config.is_tombstone(v)
            )
        ]

    def _write_vendor_tombstone(self, vendor_id: str) -> None:
        """在叠加层写一条厂商墓碑，把手写层里的同 id 条目盖掉。"""
        self._drop_vendor_tombstone(vendor_id)
        self.vendors.append({"id": vendor_id, config.TOMBSTONE_KEY: True})

    def locate_vendor(self, vendor_id: str) -> tuple[dict[str, Any], str] | None:
        """返回 (条目, 来源)：来源是 ``"overlay"``（能真改）或 ``"base"``（只能覆盖）。"""
        for item in self.overlay_vendors():
            if str(item.get("id")) == vendor_id:
                return item, "overlay"
        for item in self.base_vendors():
            if str(item.get("id")) == vendor_id:
                return item, "base"
        return None

    def _used_env_names(self, except_id: str) -> set[str]:
        """两层里已经被占用的密钥变量名（排除自己）。"""
        out: set[str] = set()
        for item in self.overlay_vendors() + self.base_vendors():
            if str(item.get("id")) == except_id:
                continue
            value = item.get("api_key_env")
            if value:
                out.add(str(value))
        return out

    def model_ids(self) -> set[str]:
        """两层的 id 全集。生成新 id 时要避开手写层已经用掉的 id，
        否则新建的模型会静默顶掉用户手写的那条。"""
        return {str(item.get("id")) for _kind, item in self.all_models()} | {
            str(item.get("id")) for _kind, item in self.base_models()
        }

    def vendor_ids(self) -> set[str]:
        """两层的厂商 id 全集。

        生成新厂商 id 时同样要避开手写层 —— 撞车的话 `_merge_config` 会留下
        同 id 的两条，合并时后者把前者整个盖掉，用户会莫名其妙少一个厂商。
        """
        return {str(item.get("id")) for item in self.overlay_vendors()} | {
            str(item.get("id")) for item in self.base_vendors()
        }

    def base_models(self) -> list[tuple[str, dict[str, Any]]]:
        """手写层里的模型。"""
        return config.layer_models(self.base)

    def all_models(self) -> list[tuple[str, dict[str, Any]]]:
        """叠加层里的模型（不含墓碑）。"""
        out: list[tuple[str, dict[str, Any]]] = []
        for kind, bucket in self._pools().items():
            for item in bucket:
                if isinstance(item, dict) and item.get("id") and not config.is_tombstone(item):
                    out.append((kind, item))
        return out

    def locate_model(
        self, model_id: str
    ) -> tuple[str, dict[str, Any], str] | None:
        """在两层里找一条模型，返回 (类目, 条目, 来源)。

        来源是 ``"overlay"``（页面自己建的，能真删）或 ``"base"``（用户手写的，
        只能盖掉）。叠加层优先 —— 同 id 时以叠加层为准。
        """
        for kind, item in self.all_models():
            if str(item.get("id")) == model_id:
                return kind, item, "overlay"
        for kind, item in self.base_models():
            if str(item.get("id")) == model_id:
                return kind, item, "base"
        return None

    def find_model(self, model_id: str) -> tuple[str, dict[str, Any]] | None:
        """只在叠加层里找（写操作专用，因为只有这一层能改）。"""
        for kind, item in self.all_models():
            if str(item.get("id")) == model_id:
                return kind, item
        return None

    def merged_model_row(
        self, model_id: str
    ) -> tuple[str, dict[str, Any], str] | None:
        """两层合并后的「当前生效」条目（手写层打底、叠加层字段覆盖）。

        表单回填、启停状态、内置条目判断都应该看这一份：只看叠加层的话，
        页面上刚点过「隐藏」的手写模型就只剩 ``{id, enabled}`` 两个字段，
        再编辑时会把 provider / endpoint / params 之类全当成"原来没有"。
        """
        located = self.locate_model(model_id)
        if located is None:
            return None
        kind, item, layer = located
        if layer != "overlay":
            return located
        base_item = next(
            (row for _k, row in self.base_models() if str(row.get("id")) == model_id),
            None,
        )
        if base_item is None:
            return located
        merged = {k: v for k, v in base_item.items() if k != config.TOMBSTONE_KEY}
        merged.update(item)
        return kind, merged, layer

    def _pools(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for key, value in self.data.items():
            if key in ("version", "defaults", "vendors"):
                continue
            if isinstance(value, dict) and isinstance(value.get("models"), list):
                out[key] = value["models"]
        return out

    def _ensure_pool(self, kind: str) -> list[dict[str, Any]]:
        node = self.data.get(kind)
        if not isinstance(node, dict):
            node = {"strategy": "priority_then_weight", "models": []}
            self.data[kind] = node
        elif not isinstance(node.get("models"), list):
            # 只补 models 这一项。原来这里是把整个节点换成默认值，于是叠加层里
            # 已经有 `strategy`、但还没写 models 的池子，一旦被页面加进第一个模型，
            # 节点里的 strategy 就被悄悄冲掉。
            node["models"] = []
        return node["models"]

    def _drop_tombstone(self, model_id: str) -> None:
        """把某条模型的墓碑清掉（用户重新加回来 / 改回来时用）。"""
        for bucket in self._pools().values():
            bucket[:] = [
                m
                for m in bucket
                if not (
                    isinstance(m, dict)
                    and str(m.get("id")) == model_id
                    and config.is_tombstone(m)
                )
            ]

    def _write_tombstone(self, kind: str, model_id: str) -> None:
        """在叠加层写一条墓碑，把手写层里的同 id 条目盖掉。"""
        self._drop_tombstone(model_id)
        self._ensure_pool(kind).append(
            {"id": model_id, config.TOMBSTONE_KEY: True}
        )

    # ---------- 厂商 ----------

    def upsert_vendor(self, payload: dict[str, Any]) -> dict[str, Any]:
        catalog_key = str(payload.get("catalog_key") or "").strip()
        entry = catalog.get(catalog_key)
        if entry is None:
            raise config.ConfigError(f"未知的厂商类型：{catalog_key or '（空）'}")

        vendor_id = str(payload.get("id") or "").strip()
        created = not vendor_id
        if created:
            vendor_id = catalog.vendor_id_for(entry.key, sorted(self.vendor_ids()))

        located = self.locate_vendor(vendor_id) if vendor_id else None
        existing = located[0] if located else None
        layer = located[1] if located else ""
        label = str(payload.get("label") or "").strip() or entry.label

        item: dict[str, Any] = {
            "id": vendor_id,
            "provider": entry.provider,
            "catalog": entry.key,
            "label": label,
        }

        # 这两段要能表达「用户把它清空了」。字段级合并的语义是"没写的键沿用底稿"，
        # 所以清空时必须在叠加层里显式写一个空映射，否则手写层里的旧地址/旧参数
        # 会在合并时又浮上来 —— 页面上看着删掉了，实际还在用。
        base_row = next(
            (v for v in self.base_vendors() if str(v.get("id")) == vendor_id), None
        )
        existing_endpoints: dict[str, Any] = {}
        existing_options: dict[str, Any] = {}
        for source in (base_row if layer == "overlay" else None, existing):
            if not isinstance(source, dict):
                continue
            if isinstance(source.get("endpoints"), dict):
                existing_endpoints.update(source["endpoints"])
            if isinstance(source.get("options"), dict):
                existing_options.update(source["options"])

        # 自定义接口（generic_http）靠 options 描述请求形状，界面必须能填，
        # 否则选中「自定义接口」就是死路 —— 能选但永远跑不起来。
        raw_options = coerce_options(payload.get("options"))
        if raw_options:
            item["options"] = raw_options
        elif "options" in payload and existing_options:
            item["options"] = {}

        # 接口地址：与目录默认值相同的就不写，保持文件干净、也方便以后升级默认值
        defaults = entry.endpoints or {}
        endpoints: dict[str, str] = {}
        for kind, raw in (payload.get("endpoints") or {}).items():
            text = str(raw or "").strip()
            if not text or text == defaults.get(kind):
                continue
            endpoints[str(kind)] = text
        if endpoints:
            item["endpoints"] = endpoints
        elif "endpoints" in payload and existing_endpoints:
            item["endpoints"] = {}

        messages: list[str] = []

        if entry.keyless:
            # 内置工具通道，没有密钥
            pass
        else:
            requested = str(payload.get("api_key_env") or "").strip()
            if not requested and entry.key_fields:
                requested = entry.key_fields[0].env
            if not requested:
                requested = f"{entry.provider.upper()}_API_KEY"

            existing_env = str(existing.get("api_key_env") or "") if existing else ""
            # 同一个平台继续沿用原来的环境变量名（否则编辑一次就换名、把已有密钥弄丢）；
            # 但**换了平台卡片**时必须重新推导 —— 否则硅基流动的 Key 会被写进
            # ZHIPU_API_KEY，页面上还显示旧变量名，连带弄坏其它读同一变量的模型。
            existing_catalog = str(existing.get("catalog") or "").strip() if existing else ""
            same_platform = bool(existing) and (
                (existing_catalog and existing_catalog == entry.key)
                or (
                    not existing_catalog
                    and str(existing.get("provider") or "") == entry.provider
                )
            )
            if existing and existing_env and same_platform:
                env = existing_env
            else:
                used = self._used_env_names(vendor_id)
                env = requested if requested not in used else _unique_env(
                    requested, used
                )
                if env != requested:
                    messages.append(
                        f"为避免和已有厂商共用密钥，本次密钥记为 {env}"
                    )
                if existing_env and not same_platform:
                    messages.append(
                        f"平台已更换，密钥变量名改为 {env}"
                        f"（原来是 {existing_env}；旧变量里的密钥不会被自动迁移）"
                    )
            item["api_key_env"] = env

        # 写回列表：叠加层里原地更新（保持原有位置），手写层改成写一条覆盖条目
        self._drop_vendor_tombstone(vendor_id)
        if existing is not None and layer == "overlay":
            existing.clear()
            existing.update(item)
        else:
            # 手写层的厂商不能直接改 —— self.base 只是读进来的内存副本，改它
            # 既不会落盘、也不会生效，用户的编辑会凭空消失。改成在叠加层追加
            # 一条同 id 的条目：合并时叠加层优先，效果等同于"编辑过这个厂商"。
            self.vendors.append(item)
            if layer == "base":
                messages.append(
                    "这个厂商写在 config/models.yaml 里，页面不改动你手写的文件，"
                    "本次修改记在 models.web.yaml 中（同 id 覆盖）"
                )

        key_value = str(payload.get("api_key") or "").strip()
        key_saved = False
        if key_value and not entry.keyless:
            env = str(item.get("api_key_env") or "")
            if env:
                save_secret(env, key_value)
                key_saved = True
                messages.append(f"密钥已保存到 config/{config.secrets_write_path().name}")

        return {
            "id": vendor_id,
            "created": created,
            "key_saved": key_saved,
            "message": f"厂商「{label}」{'已添加' if created else '已更新'}"
            + ("；" + "；".join(messages) if messages else ""),
        }

    def delete_vendor(self, vendor_id: str) -> dict[str, Any]:
        located = self.locate_vendor(vendor_id)
        if located is None:
            raise config.ConfigError(f"找不到厂商：{vendor_id}")
        existing, _layer = located

        # 级联删除引用它的模型 —— 否则那些模型会因为没有 provider 把整份配置弄坏，
        # 而用户只会看到"页面打不开了"。宁可明确删掉并如实告知。
        #
        # 判断依据必须是**当前生效**的 vendor（两层合并后的结果），不是"任一层引用过"：
        # 一条模型从 v1 迁到 v2 之后，手写层里那句 `vendor: v1` 还在，按 id 级联会
        # 把这条仍在 v2 下正常工作的模型一起删掉。
        dropped: set[str] = set()
        for model_id in self.model_ids():
            merged = self.merged_model_row(model_id)
            if merged is not None and str(merged[1].get("vendor") or "") == vendor_id:
                dropped.add(model_id)
        for kind, bucket in self._pools().items():
            keep = [m for m in bucket if str(m.get("id")) not in dropped]
            if len(keep) != len(bucket):
                node = self.data.get(kind)
                if isinstance(node, dict):
                    node["models"] = keep
        # 手写层里如果有模型引用了这个厂商（比如用户手写了 vendor: v-xxx），
        # 也要一并盖掉，否则删完之后配置会因为引用不存在的厂商而加载失败
        for kind, item in self.base_models():
            if str(item.get("id")) in dropped:
                self._write_tombstone(kind, str(item.get("id")))

        # 只摘叠加层里的那条。手写层的厂商删不掉（不能改用户手写的文件），
        # 靠下面那条墓碑把它盖住。
        for index, item in enumerate(self.vendors):
            if item is existing:
                del self.vendors[index]
                break

        label = str(existing.get("label") or vendor_id)
        # 手写层里**也**有同 id 的条目吗（用户在页面上编辑过手写厂商，叠加层就会多
        # 一条同 id 的覆盖条目，于是 locate_vendor 报 "overlay"）？原来这里只摘掉
        # 覆盖条目就返回"已删除"，而合并时手写层那条又浮上来 —— 删完刷新，厂商还在，
        # 而且没有任何提示。和模型一样写一条墓碑把它盖掉。
        in_base = any(str(v.get("id")) == vendor_id for v in self.base_vendors())
        if in_base:
            self._write_vendor_tombstone(vendor_id)

        message = f"厂商「{label}」已删除"
        if in_base:
            message = (
                f"厂商「{label}」写在 config/models.yaml 里，页面不改动你手写的文件，"
                f"所以在叠加层里把它盖掉了（想彻底删掉请打开那个文件删掉这一段；"
                f"想恢复就在这里重新添加同 id 的厂商）。"
            )
        if dropped:
            base_model_ids = {
                str(item.get("id")) for _kind, item in self.base_models()
            }
            hidden = sorted(dropped & base_model_ids)
            message += (
                f"，同时移除了引用它的 {len(dropped)} 个模型："
                + "、".join(sorted(dropped))
            )
            if hidden:
                message += (
                    "（其中 " + "、".join(hidden) + " 是手写层模型，"
                    "只是隐藏，不会改你的文件）"
                )
        return {
            "id": vendor_id,
            "removed_models": sorted(dropped),
            "from_base": in_base,
            "message": message,
        }

    # ---------- 模型 ----------

    @staticmethod
    def _modalities_of(
        payload: dict[str, Any],
        which: str,
        kind: str,
        supports: list[str],
        previous_row: dict[str, Any] | None,
    ) -> list[str]:
        """取一条模型的输入/输出模态。

        优先用这次提交的值；表单没带（批量添加、旧页面、直接调 API）时沿用
        已有的值 —— 不能因为"这次没提交"就把用户之前标好的功能标签清空。
        两层都没有才按类目 + supports 推默认值。
        """
        field = f"{which}_modalities"
        raw = payload.get(field)
        if raw is None and previous_row is not None:
            raw = previous_row.get(field)
        return config.normalize_modalities(raw, kind, supports, which)

    def upsert_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or "").strip()
        if kind not in config.MODEL_KINDS:
            raise config.ConfigError(_kind_hint())

        model_name = str(payload.get("model") or "").strip()
        if not model_name:
            raise config.ConfigError("请填写模型名称")

        model_id = str(payload.get("id") or "").strip()
        created = not model_id
        if created:
            model_id = _unique_id(_slug(model_name) or "model", self.model_ids())

        # 编辑时可能对着一条手写层的模型下手，所以要在两层里找。
        # previous_row 用**合并后**的行：表单里没体现的字段（endpoint / options /
        # params / 内联密钥）要按"当前实际生效的样子"继承，而不是只看某一层。
        located = self.locate_model(model_id)
        previous = self.find_model(model_id)
        merged = self.merged_model_row(model_id)
        previous_row = merged[1] if merged is not None else None

        # 内置兜底条目（provider: native）是随 skill 出厂的能力，页面只允许
        # 隐藏（走 set_enabled），不允许改名、改优先级、改能力或增删 ——
        # 否则用户会把它改成 priority 1 或删掉，最后连兜底都没有了。
        if previous_row is not None and str(previous_row.get("provider") or "") == "native":
            raise config.ConfigError(
                "内置兜底模型不能编辑：它不调用任何接口，只在真实模型都不可用时兜底。"
                "不想让它参与调度，请用「隐藏」。"
            )

        vendor_id = str(payload.get("vendor") or "").strip()
        if vendor_id:
            if not self.find_vendor(vendor_id):
                raise config.ConfigError(
                    f"找不到厂商 {vendor_id}，请先在「厂商配置」里添加并保存它"
                )
        elif previous_row is None:
            # 没选厂商又不认识这个 id —— 新建的模型必须挂在某个厂商下，
            # 否则它不知道用哪把密钥、调哪个地址
            raise config.ConfigError(
                "新建模型要先选一个厂商。若这个模型写在 models.yaml 里，"
                "请先在列表里找到它再点「编辑」。"
            )

        try:
            priority = max(1, int(payload.get("priority", 1) or 1))
        except (TypeError, ValueError):
            priority = 1
        try:
            weight = max(0.0, float(payload.get("weight", 1) or 0))
        except (TypeError, ValueError):
            weight = 0.0

        supports_raw = payload.get("supports") or []
        if isinstance(supports_raw, str):
            supports = [s.strip() for s in supports_raw.split(",") if s.strip()]
        elif isinstance(supports_raw, (list, tuple)):
            supports = [str(s).strip() for s in supports_raw if str(s).strip()]
        else:
            raise config.ConfigError(
                f"supports 必须是列表或逗号分隔的字符串，"
                f"实际是 {type(supports_raw).__name__}"
            )
        if not supports:
            # 默认能力必须跟着类目走：audio 以前也落进 else，被存成 text2video，
            # 于是语音模型在路由阶段永远匹配不上 text2audio（配了等于没配）。
            supports = {
                "image": ["text2img"],
                "video": ["text2video"],
                "audio": ["text2audio"],
            }.get(kind, ["text2img"])

        # 输入/输出模态：功能标签。
        # 页面每次都提交两个完整数组，所以这里没有"清空某个模态"的歧义；
        # 表单没带这两个键时（批量添加、早期页面）沿用已有值，没有就按类目推默认值。
        input_modalities = self._modalities_of(
            payload, "input", kind, supports, previous_row
        )
        output_modalities = self._modalities_of(
            payload, "output", kind, supports, previous_row
        )

        # 编辑时保留原有的启用状态：表单里没有这个开关，不能顺手把停用的模型开回来。
        # 手写层的条目也要读到 —— 否则改一下手写层里停用的模型就把它激活了。
        if previous_row is not None:
            enabled = config.coerce_enabled(previous_row.get("enabled", True))
        else:
            enabled = config.coerce_enabled(payload.get("enabled", True))

        label = str(payload.get("label") or "").strip()

        item: dict[str, Any] = {
            "id": model_id,
            "model": model_name,
            "priority": priority,
            "weight": weight,
            "enabled": enabled,
            "supports": supports,
            "input_modalities": input_modalities,
            "output_modalities": output_modalities,
        }
        if label and label != model_name:
            item["label"] = label
        elif previous_row is not None and previous_row.get("label"):
            # 用户把「显示名称」清空了：显式写一个空串，否则字段级合并会让
            # 手写层里的旧 label 又冒出来（合并规则是"没写的键沿用底稿"）。
            item["label"] = ""

        # 生成参数（params）：尺寸/时长这类"每次生成都想固定用"的规格。
        # 合并规则：原有 params（可能来自手写层）打底，本次表单提交的键覆盖；
        # 提交空值 = 删掉那个键（用户在页面上清空了输入框）。
        # 不在表单管辖内的键原样保留，不顺手清掉用户手写的东西。
        params: dict[str, Any] = {}
        if previous_row is not None and isinstance(previous_row.get("params"), dict):
            params.update(previous_row["params"])
        params_in = payload.get("params")
        if isinstance(params_in, dict):
            for key, value in params_in.items():
                name = str(key).strip()
                if not name:
                    continue
                # 只有"真的空"才算用户清空了输入框。
                # 早期写的是 `value in ("", None, 0, "0")` —— 0 == False，
                # 于是 seed: 0、guidance_scale: 0 这种**合法的 0** 会被静默删掉，
                # 用户明明填了 0，存下来却没有这个键。
                if value is None or value == "":
                    params.pop(name, None)
                elif isinstance(value, (str, int, float, bool)):
                    params[name] = value.strip() if isinstance(value, str) else value
        if params:
            item["params"] = params

        if vendor_id:
            item["vendor"] = vendor_id
        # 没选厂商 = 在编辑一条内联 provider 的模型（手写层里都是这种写法）。
        # 这里**不再**把 provider / api_key_env / endpoint / options 抄进叠加层：
        # 读配置时 _dedupe_last 会把叠加层字段合到同 id 的手写条目上，缺的字段
        # 自然沿用底稿。抄一遍反而会把内联密钥、私有网关地址复制进
        # models.web.yaml —— 那份文件按承诺是不含密钥的。

        # 用户可能正在把一条被隐藏/被盖掉的模型改回来，把墓碑清掉
        self._drop_tombstone(model_id)

        # 类目变了就把条目从旧池挪到新池
        if previous is not None:
            old_kind, old_item = previous
            if old_kind != kind:
                node = self.data.get(old_kind)
                if isinstance(node, dict):
                    node["models"] = [
                        m for m in node.get("models", []) if str(m.get("id")) != model_id
                    ]
                previous = None
            else:
                old_item.clear()
                old_item.update(item)
        elif located is not None and located[0] != kind:
            # 改的是**手写层**里的模型，而且换了类目。手写层那条删不掉，
            # 只在叠加层的新池里加一条是不够的 —— _dedupe_last 只去重同一个池，
            # 结果 image 和 video 两个池里会各有一份同 id 的模型，
            # 路由到错误类目的那一次必然失败。所以给旧类目补一条墓碑把它盖掉。
            self._write_tombstone(located[0], model_id)

        if previous is None:
            # 手写层里有同 id 的条目时，这里写入的就是一条覆盖条目 ——
            # 位置由 _dedupe_last 保证还在原处，不会跑到列表末尾
            self._ensure_pool(kind).append(item)

        verb = "已添加" if created else "已更新"
        where = config.KIND_LABELS.get(kind, kind)
        return {
            "id": model_id,
            "created": created,
            "kind": kind,
            "message": f"{where}模型「{model_name}」{verb}",
        }

    def delete_model(self, model_id: str) -> dict[str, Any]:
        found = self.locate_model(model_id)
        if found is None:
            raise config.ConfigError(f"找不到模型：{model_id}")
        kind, _item, _layer = found
        merged = self.merged_model_row(model_id)
        current = merged[1] if merged is not None else _item
        if str(current.get("provider") or "") == "native":
            raise config.ConfigError(
                "内置兜底模型不能删除：它不调用任何接口，也不会产生费用。"
                "不想让它参与调度，请用「隐藏」。"
            )
        name = str(current.get("model") or model_id)

        # 叠加层里的条目直接摘掉
        node = self.data.get(kind)
        if isinstance(node, dict):
            node["models"] = [
                m for m in node.get("models", []) if str(m.get("id")) != model_id
            ]

        # 手写层里**也**有同 id 的条目吗？只看 locate_model 给的 layer 是不够的：
        # 用户在页面上编辑过一条手写模型 → 叠加层多了一条同 id 的覆盖条目 →
        # locate_model 报 "overlay"。原来这里就只摘覆盖条目、回一句"已删除"，
        # 而合并时手写层那条又浮上来 —— 删掉、刷新、它还在，且没有任何提示。
        base_hit = next(
            (k for k, base_item in self.base_models() if str(base_item.get("id")) == model_id),
            None,
        )
        if base_hit is not None:
            # 手写层里的条目不能真删 —— 那是用户自己的文件。写一条墓碑盖住它，
            # 对下游（路由、列表）的效果等同于删除，而且随时可以撤销。
            self._write_tombstone(base_hit, model_id)
            return {
                "id": model_id,
                "hidden": True,
                "message": (
                    f"模型「{name}」已从配置页面隐藏（它写在 config/models.yaml 里，"
                    f"页面不改动你手写的文件）。想彻底删掉请打开 models.yaml 删掉这一段；"
                    f"想恢复就在这里重新添加同 id 的模型。"
                ),
            }
        return {"id": model_id, "hidden": False, "message": f"模型「{name}」已删除"}

    def set_enabled(self, model_id: str, enabled: bool) -> dict[str, Any]:
        found = self.locate_model(model_id)
        if found is None:
            raise config.ConfigError(f"找不到模型：{model_id}")
        kind, item, layer = found
        merged = self.merged_model_row(model_id)
        current = merged[1] if merged is not None else item
        name = str(current.get("model") or model_id)

        if layer == "overlay":
            previous = self.find_model(model_id)
            if previous is not None:
                previous[1]["enabled"] = bool(enabled)
        else:
            # 手写层里的条目：叠加层只写「同 id + enabled」这一条。
            # 读配置时字段级合并会把这一条合到同 id 的手写条目上，其余字段
            # （provider / endpoint / options / 内联 api_key）一个字都不用抄 ——
            # 以前整条拷贝，会把手写层里的明文密钥复制进 models.web.yaml，
            # 而那份文件按 README 和文件头注释的承诺是不含密钥的。
            self._drop_tombstone(model_id)
            pool = self._ensure_pool(kind)
            pool[:] = [m for m in pool if str(m.get("id")) != model_id]
            pool.append({"id": model_id, "enabled": bool(enabled)})

        return {
            "id": model_id,
            "enabled": bool(enabled),
            "message": f"模型「{name}」已{'启用' if enabled else '停用'}",
        }

    # ---------- 落盘 ----------

    def _prune_empty_pools(self) -> None:
        """空的类目不写进文件 —— models.yaml 里没有的池子，别凭空多出来一个。"""
        for kind, bucket in list(self._pools().items()):
            if not bucket:
                self.data.pop(kind, None)


# ---------------------------------------------------------------- 密钥


def save_secret(env_name: str, value: str) -> Path:
    """把密钥写进 config/secrets.web.yaml。"""
    reason = config.secrets_readonly_reason()
    if reason:
        raise ConfigReadOnly(reason)
    with _WRITE_LOCK:
        data = read_secrets_overlay()
        keys = data.setdefault("keys", {})
        if not isinstance(keys, dict):
            keys = {}
            data["keys"] = keys
        keys[str(env_name)] = str(value)
        path = config.secrets_write_path()
        write_structured(path, data, SECRETS_HEADER)
        return path


# ---------------------------------------------------------------- 页面视图


def _vendor_view(
    item: dict[str, Any],
    resolved_keys: dict[str, str],
    model_count: int,
    from_base: bool = False,
) -> dict[str, Any]:
    vendor_id = str(item.get("id") or "")
    env = str(item.get("api_key_env") or "")
    entry = catalog.get(str(item.get("catalog") or ""))
    keyless = bool(entry and entry.keyless)
    # 目录里把密钥标成"可选"的平台（如自定义接口）：没填密钥不等于配错了，
    # 页面要能区分"缺密钥"和"这个平台本来就不用密钥"。
    key_optional = bool(
        entry
        and entry.key_fields
        and all(not field.required for field in entry.key_fields)
    )
    endpoints = dict(item.get("endpoints") or {})
    return {
        "id": vendor_id,
        "label": str(item.get("label") or vendor_id),
        "provider": str(item.get("provider") or ""),
        "catalog": str(item.get("catalog") or item.get("provider") or ""),
        "api_key_env": env,
        "endpoints": endpoints,
        "options": dict(item.get("options") or {}),
        "model_count": model_count,
        "keyless": keyless,
        "key_optional": key_optional,
        #: 这条是用户手写在 models.yaml 里的，还是页面建的？
        #: 页面据此说明"删除"和"编辑"分别会写到哪个文件。
        "from_base": from_base,
        "has_key": keyless or bool(resolved_keys.get(env) or resolved_keys.get(vendor_id)),
        # 按这个厂商**实际填的地址**算，而不是目录默认地址 ——
        # 自建接口（generic_http）的地址是用户自己填的，能推导就该亮按钮。
        "can_list_models": catalog.can_list_models_for(entry, endpoints),
        "list_note": str((entry.list_models or {}).get("unsupported") or "") if entry else "",
    }


def _model_view(
    kind: str,
    item: dict[str, Any],
    vendors: dict[str, dict[str, Any]],
    base_ids: set[str],
) -> dict[str, Any]:
    model_id = str(item.get("id") or "")
    vendor_id = str(item.get("vendor") or "")
    vendor = vendors.get(vendor_id) or {}
    entry = catalog.get(str(vendor.get("catalog") or ""))
    model_name = str(item.get("model") or model_id)
    try:
        priority = int(item.get("priority", 1))
    except (TypeError, ValueError):
        priority = 1
    try:
        weight = float(item.get("weight", 1))
    except (TypeError, ValueError):
        weight = 1.0
    supports = item.get("supports") or []
    if isinstance(supports, str):
        supports = [s.strip() for s in supports.split(",") if s.strip()]
    params = item.get("params")
    # 模态是标注字段：手写层里没写时按类目 + supports 推默认值，
    # 页面拿到的永远是可显示的两个数组，不用自己再判空兜底。
    # 内置兜底（native）例外：它不吃也不吐，只是把请求交回 agent 自己的工具，
    # 给它贴「输入 文本/图片 → 输出 图片」等于把兜底伪装成真模型。
    if str(item.get("provider") or vendor.get("provider") or "") == "native":
        input_modalities: list[str] = []
        output_modalities: list[str] = []
    else:
        input_modalities = config.normalize_modalities(
            item.get("input_modalities"), kind, supports, "input"
        )
        output_modalities = config.normalize_modalities(
            item.get("output_modalities"), kind, supports, "output"
        )
    return {
        "id": model_id,
        "kind": kind,
        "label": str(item.get("label") or model_name),
        "model": model_name,
        "vendor": vendor_id,
        "vendor_label": str(vendor.get("label") or vendor_id),
        "provider": str(item.get("provider") or vendor.get("provider") or ""),
        "catalog": str(vendor.get("catalog") or ""),
        "priority": priority,
        "weight": weight,
        "enabled": config.coerce_enabled(item.get("enabled", True)),
        "supports": [str(s) for s in supports],
        "input_modalities": input_modalities,
        "output_modalities": output_modalities,
        # 生成参数（尺寸/时长等）。页面用它回填"生成规格"输入框。
        "params": dict(params) if isinstance(params, dict) else {},
        # 这条是用户手写在 models.yaml 里的，还是在配置页面里建的？
        # 页面据此告诉用户"删除"为什么是"隐藏"。
        "from_base": model_id in base_ids,
        "warnings": [],
        "suggestions": (entry.suggestions.get(kind, []) if entry else []),
    }


def _resolved_keys() -> dict[str, str]:
    """当前能拿到的密钥：两份密钥文件 + 环境变量（环境变量优先，与运行时一致）。"""
    out: dict[str, str] = {}
    for path in config.secrets_paths():
        try:
            loaded = config.read_structured(path)
        except Exception:  # noqa: BLE001 - 坏文件不该让页面打不开
            continue
        if isinstance(loaded, dict):
            for key, value in (loaded.get("keys") or {}).items():
                if value and str(value).strip():
                    out[str(key)] = str(value).strip()
    for env_name in list(out) + [f.env for entry in catalog.CATALOG for f in entry.key_fields]:
        value = os.environ.get(env_name)
        if value and value.strip():
            out[env_name] = value.strip()
    return out


def bootstrap(host: str = "127.0.0.1") -> dict[str, Any]:
    """页面首屏要的全部数据。配置写坏时也要能返回。"""
    warnings: list[str] = []
    data: dict[str, Any] = {"version": 1}
    paths: list[Path] = []

    try:
        data, paths, _overlay, skipped = config.load_raw_tolerant(allow_missing=True)
        warnings.extend(skipped)
    except config.ConfigError as exc:
        warnings.append(f"{exc}（配置页面仍可打开，保存后会修正）")
    except Exception as exc:  # noqa: BLE001 - 语法错误不能让页面打不开
        warnings.append(
            f"配置文件解析失败：{type(exc).__name__}: {exc}"
            f"（仍可在这里修改，保存后会修正该文件）"
        )

    # 顶层再校验一遍，把"能读但有毛病"的问题也提前告诉用户
    cfg: config.RouterConfig | None = None
    try:
        cfg = config.load_config()
    except config.ConfigError as exc:
        warnings.append(str(exc))
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{type(exc).__name__}: {exc}")

    raw_vendors = data.get("vendors") or []
    if not isinstance(raw_vendors, list):
        raw_vendors = []
        warnings.append("vendors 必须是列表，已按空处理")
    vendor_index = {
        str(item.get("id")): item for item in raw_vendors if isinstance(item, dict)
    }

    resolved = _resolved_keys()
    counts: dict[str, int] = {}
    for key, value in data.items():
        if key in ("version", "defaults", "vendors"):
            continue
        if isinstance(value, dict) and isinstance(value.get("models"), list):
            for item in config.visible_entries(value["models"]):
                if isinstance(item, dict):
                    vid = str(item.get("vendor") or "")
                    counts[vid] = counts.get(vid, 0) + 1

    # 手写层写坏时 base_layer 会抛错：页面必须还能打开（哪怕手写层的
    # 「哪些模型是手写的」信息暂时缺失），否则用户只能去命令行改文件。
    try:
        base_layer = config.base_layer()
    except (config.ConfigError, OSError) as exc:
        base_layer = {}
        warnings.append(
            f"手写的 models.yaml 暂时读不出来（{exc}）；页面仍可查看和编辑页面里建的模型，"
            f"或直接改 models.yaml 修好它"
        )
    base_ids = {
        str(item.get("id")) for _kind, item in config.layer_models(base_layer)
    }
    base_vendor_ids = {
        str(v.get("id"))
        for v in (base_layer.get("vendors") or [])
        if isinstance(v, dict) and v.get("id")
    }

    vendors = [
        _vendor_view(
            item,
            resolved,
            counts.get(str(item.get("id")), 0),
            from_base=str(item.get("id")) in base_vendor_ids,
        )
        for item in raw_vendors
        if isinstance(item, dict) and item.get("id")
    ]

    models: dict[str, list[dict[str, Any]]] = {}
    for key, value in data.items():
        if key in ("version", "defaults", "vendors"):
            continue
        if isinstance(value, dict) and isinstance(value.get("models"), list):
            # 和运行时的加载逻辑保持一致：同 id 只留最后一条、墓碑丢掉。
            # 少了这一步，页面会把同一条模型显示两遍（手写层一遍、覆盖层一遍），
            # 而且会把已经"删掉"的模型又列出来。
            entries = config.visible_entries(value["models"])
            models[key] = [
                _model_view(key, item, vendor_index, base_ids)
                for item in entries
                if isinstance(item, dict) and item.get("id")
            ]

    # 给页面补上运行时的"能不能用"信息：缺 Key / 熔断中。
    # 页面原来只看 enabled，用户会看到模型亮着、实际生成时却被跳过。
    if cfg is not None:
        specs = {m.id: m for pool in cfg.pools.values() for m in pool.models}
        try:
            snapshot = health.HealthStore(cfg.health_path).snapshot(list(specs))
        except Exception:  # noqa: BLE001 - 状态文件坏了也不该让页面打不开
            snapshot = {}
        for bucket in models.values():
            for view in bucket:
                spec = specs.get(view["id"])
                row = snapshot.get(view["id"]) or {}
                keyless = bool(
                    spec and spec.provider in ("native", "generic_http_nokey")
                )
                ready = True
                if spec is not None and not keyless:
                    try:
                        ready = bool(cfg.resolve_api_key(spec)[0])
                    except Exception:  # noqa: BLE001
                        ready = False
                view["needs_api_key"] = bool(spec is not None and not keyless)
                view["api_key_ready"] = ready
                view["cooling"] = bool(row.get("cooling"))
                view["cooldown_remaining_seconds"] = float(
                    row.get("cooldown_remaining_seconds") or 0.0
                )

    # 页签固定出这三个已知类目，顺序 image → video → audio，**不看配置里有没有这个池**：
    # 池子可以事后由页面创建（upsert_model 会 _ensure_pool），少一个池就没页签的话，
    # 用户永远没法在页面里把语音模型加回来。
    # 也不把自定义池塞进来：模态/规格表渲染不出来，写回又会被类目校验拒掉，
    # 那只是个"点一下就报错"的死页签。
    kinds = list(config.MODEL_KINDS)

    # 引用了不存在的厂商的模型，等于运行时一定会失败，提前点出来
    for kind, bucket in models.items():
        for entry in bucket:
            if entry["vendor"] and entry["vendor"] not in vendor_index:
                warnings.append(
                    f"模型 {entry['id']} 引用了不存在的厂商 {entry['vendor']}，"
                    f"请到「厂商配置」里补上，或把它删掉"
                )

    # 同一句话不重复说（load_raw 与 load_config 可能报同一个错）
    global LAST_REPAIR_NOTE
    if LAST_REPAIR_NOTE:
        # 刚才保存时重建过叠加层：这次刷新必须告诉用户，否则他会以为
        # 「我明明配过厂商和模型，怎么全没了」。说过一次就清掉。
        warnings.append(LAST_REPAIR_NOTE)
        LAST_REPAIR_NOTE = ""
    deduped: list[str] = []
    for text in warnings:
        if text and text not in deduped:
            deduped.append(text)

    base_config = paths[0] if paths else config.CONFIG_DIR / "models.yaml"
    target = overlay_path()
    return {
        "catalog": catalog.as_dict(),
        "vendors": vendors,
        "models": models,
        "kinds": kinds,
        "warnings": deduped,
        "readonly": readonly_reason(),
        "secrets_readonly": config.secrets_readonly_reason(),
        "paths": {
            "skill_dir": str(config.SKILL_DIR),
            "config_path": str(base_config),
            "overlay_path": str(target) if target.exists() else "",
            "overlay_target": str(target),
            "secrets_path": str(config.secrets_write_path()),
            "secrets_base": str(config.locate_secrets() or ""),
            "host": host,
        },
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------- 供服务端调用


def load_overlay() -> Overlay:
    return Overlay(read_overlay())


def save_with(mutate: Callable[[Overlay], Any]) -> Any:
    """加载 → 在写锁里改 → 落盘。服务端所有写接口都走这里。

    **只读校验必须在这里做。** 以前只有 Overlay.save() 会调 _guard_writable，
    而那个方法全项目没人调用（像个装饰品），于是只读模式下 save_with 照样
    把 models.web.yaml 写下去 —— 页面和终端横幅都还在说"只会读取，不会写入"。
    """
    with _WRITE_LOCK:
        _guard_writable()
        global LAST_REPAIR_NOTE
        try:
            data = read_overlay()
        except config.ConfigError as exc:
            # 叠加层自己坏了：先原样备份，再从空白重建。
            # 不这么做的话，页面会陷入「模型看得见、怎么保存都说解析失败」的死循环，
            # 而 models.web.yaml 是页面自己的文件，重建它比让用户去命令行修更合理。
            path = overlay_path()
            backup = path.with_name(
                f"{path.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}.bak"
            )
            note = f"{path.name} 解析失败（{exc}），已从空白重建"
            try:
                backup.write_bytes(path.read_bytes())
                note += f"；原文件已备份为 {backup.name}"
            except OSError:
                note += "；原文件备份失败，内容可能已丢失"
            LAST_REPAIR_NOTE = note
            data = {"version": 1}
        overlay = Overlay(data)
        result = mutate(overlay)
        overlay._prune_empty_pools()  # noqa: SLF001 - 同类协作
        write_structured(overlay_path(), overlay.data, HEADER)
        return result
