#!/usr/bin/env python3
"""第二批修复的回归测试（零依赖、不联网、不花钱）。

覆盖：
  1. models.yaml / models.web.yaml 任一写坏时，配置页面仍能看到另一层
  2. 叠加层写坏时保存会自动备份并重建（不再卡死）
  3. 全部模型在冷却时，按"最早恢复"排序（不再被 priority 压回去）
  4. --count 在 qwen-image 通路真的发 N 次；native 的委托说明带数量；
     实际产物少于要求时 CLI 会明确提示
  5. audio 模型没写 supports 时默认 text2audio（不再是 text2video）
  6. secrets 的 keys 写成 null 时不再清空底层真实密钥，并给出告警
  7. 配置里自定义 output_dir 后，预览白名单包含它

运行：python tests/test_p1_fixes.py
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

_WORK = Path(tempfile.mkdtemp(prefix="mr-p1-"))
os.environ["MEDIA_ROUTER_CONFIG_DIR"] = str(_WORK / "bootstrap-config")
os.environ["MEDIA_ROUTER_STATE_DIR"] = str(_WORK / "state")
os.environ["MEDIA_ROUTER_OUTPUT_DIR"] = str(_WORK / "outputs")
os.environ.pop("MEDIA_ROUTER_CONFIG", None)
os.environ.pop("MEDIA_ROUTER_SECRETS", None)

from mrouter import adapters, cli, config, health, selector, store, webserver  # noqa: E402
from mrouter.adapters import Ctx, GenRequest, GenResult  # noqa: E402
from mrouter.config import ConfigError, ModelSpec  # noqa: E402

passed = 0
failed = 0


def check(cond: bool, message: str) -> None:
    global passed, failed
    if cond:
        passed += 1
        print("  [PASS] " + message)
    else:
        failed += 1
        print("  [FAIL] " + message)


def section(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 62 - len(title)))


def use_config(files: dict[str, str], name: str = "cfg") -> Path:
    root = Path(tempfile.mkdtemp(prefix=f"mr-p1-{name}-"))
    (root / "config").mkdir()
    for filename, text in files.items():
        (root / "config" / filename).write_text(text, encoding="utf-8")
    config.CONFIG_DIR = root / "config"
    config.STATE_DIR = root / "state"
    config.DEFAULT_OUTPUT_DIR = root / "outputs"
    return root


print("第二批修复回归测试")


# ---------------------------------------------------------------- 1. 容错
section("页面容错：任一配置文件写坏，另一层仍可见")

BROKEN_YAML = "image:\n  models:\n    - id: m1\n   provider: openai\n"
GOOD_OVERLAY = (
    "version: 1\n"
    "vendors:\n"
    "  - id: v-page\n    catalog: volcengine\n    provider: volcengine\n    label: 火山\n"
    "    api_key_env: ARK_API_KEY\n"
    "image:\n  models:\n"
    "    - id: m-page\n      vendor: v-page\n      model: doubao-seedream\n"
    "      priority: 1\n      weight: 1\n"
)

use_config({"models.yaml": BROKEN_YAML, "models.web.yaml": GOOD_OVERLAY}, "broken-base")
data = store.bootstrap()
ids = [m["id"] for m in (data.get("models") or {}).get("image", [])]
check("m-page" in ids, "models.yaml 写坏时，叠加层里页面建的模型依然可见")
check(any("models.yaml" in w for w in data["warnings"]), "并且给出「models.yaml 读不出来」的提示")

GOOD_BASE = (
    "image:\n  models:\n"
    "    - id: m-hand\n      provider: openai\n      model: gpt-image-1\n"
    "      api_key_env: OPENAI_API_KEY\n      priority: 1\n      weight: 1\n"
)
use_config({"models.yaml": GOOD_BASE, "models.web.yaml": "image: [坏掉了\n"}, "broken-overlay")
data = store.bootstrap()
ids = [m["id"] for m in (data.get("models") or {}).get("image", [])]
check("m-hand" in ids, "models.web.yaml 写坏时，手写的模型依然可见")
check(any("models.web.yaml" in w for w in data["warnings"]), "并且点名是哪个文件坏了")

# 保存：坏掉的叠加层会先备份再重建
manual = use_config({"models.yaml": "image:\n  models: []\n"}, "repair")
bad = manual / "config" / "models.web.yaml"
bad.write_text("image: [坏掉了\n", encoding="utf-8")
store.LAST_REPAIR_NOTE = ""
res = store.save_with(
    lambda ov: ov.upsert_vendor(
        {"id": "", "catalog_key": "dashscope", "label": "百炼", "api_key_env": "DASHSCOPE_API_KEY"}
    )
)
check(bool(res.get("id")), "叠加层坏掉时保存不再报解析失败（自动重建）")
backups = list(manual.glob("config/models.web.yaml.broken-*.bak"))
check(len(backups) == 1, "重建前把原文件备份成 .bak")
check("坏掉了" in backups[0].read_text(encoding="utf-8"), "备份内容是坏掉的原文件")
cfg = config.load_config()
check([v.id for v in cfg.vendors.values()] == ["v-dashscope"], "重建后新建的厂商能正常读回")


# ---------------------------------------------------------------- 3. 冷却排序
section("全部冷却时按最早恢复排序")


class FakeHealth:
    def __init__(self, remaining: dict[str, float]):
        self.remaining = remaining

    def is_cooling(self, model_id, now=None):
        return True

    def cooling_remaining(self, model_id, now=None):
        return self.remaining[model_id]


specs = [
    ModelSpec(id="slow", provider="x", kind="image", priority=1, weight=1),
    ModelSpec(id="soon", provider="x", kind="image", priority=2, weight=1),
]
ordered, notes = selector.rank_candidates(specs, ["text2img"], FakeHealth({"slow": 240.0, "soon": 5.0}))
check([m.id for m in ordered] == ["soon", "slow"], "优先级低的「先恢复」排在前面")
check(notes["health_relaxed"] is True and notes["excluded_cooling"] == [], "不再把实际会尝试的模型列为「已排除」")


# ---------------------------------------------------------------- 4. --count
section("--count 在各通路的行为")

CTX = Ctx(api_key="k", key_source="t", timeout=5, poll_interval=1, max_poll=5)


class Resp:
    status = 200
    body = b""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


calls: list[dict] = []


def qwen_fake(method, url, **kw):
    calls.append(kw.get("json_body") or {})
    return Resp({"output": {"choices": [{"message": {"content": [{"image": "https://x/a.png"}]}}]}})


adapters.request = qwen_fake
outs = Path(tempfile.mkdtemp(prefix="mr-p1-qwen-"))
real_download = adapters.download
adapters.download = lambda url, dest, **kw: (dest.write_bytes(b"\x89PNG\r\n\x1a\n"), dest)[1]
try:
    res = adapters.gen_dashscope(
        ModelSpec(id="qwen", provider="dashscope", kind="image", model="qwen-image-2.0"),
        GenRequest(kind="image", prompt="p", count=3, output_dir=outs),
        None,
        CTX,
    )
    check(len(calls) == 3, f"qwen-image 的 --count 3 真的发了 3 次请求（实际 {len(calls)}）")
    check(len(res.files) == 3 and res.meta.get("count") == 3, "产物数与 meta.count 一致")
finally:
    adapters.download = real_download

adapters.request = qwen_fake
deleg = adapters.gen_native(
    ModelSpec(id="builtin-imagen", provider="native", kind="image", model="builtin-imagen"),
    GenRequest(kind="image", prompt="p", count=3),
    None,
    CTX,
)
check("3" in deleg.delegate["note"] and "不要只出一份" in deleg.delegate["note"],
      "native 委托说明里带上数量要求")

# 实际产物少于要求时，CLI 要明确提示
root = use_config(
    {
        "models.yaml": (
            "image:\n  models:\n"
            "    - id: one\n      provider: openai\n      model: gpt-image-1\n"
            "      priority: 1\n      weight: 1\n      api_key_env: OPENAI_API_KEY\n"
        )
    },
    "count-cli",
)
os.environ["OPENAI_API_KEY"] = "sk-test"
one_file = root / "one.png"
one_file.write_bytes(b"\x89PNG\r\n\x1a\n")
real_run = cli.run_adapter
cli.run_adapter = lambda *a, **k: GenResult(
    status="ok", model_id="one", provider="openai", files=[str(one_file)], urls=[], meta={"model": "gpt-image-1"}
)
args = cli.build_parser().parse_args(
    ["generate", "--kind", "image", "--prompt", "p", "--count", "3", "--output-dir", str(root / "out")]
)
buf = io.StringIO()
try:
    with redirect_stdout(buf):
        rc = cli.cmd_generate(args)
finally:
    cli.run_adapter = real_run
payload = json.loads(buf.getvalue())
check(rc == 0 and payload["status"] == "ok", "产物少于要求时仍然是 ok（不是失败）")
check(any("要求 3 个产物" in n for n in payload["notes"]), "notes 里明确写出「要求 3 个、实际 1 个」")


# ---------------------------------------------------------------- 5. audio 默认能力
section("语音模型默认能力")

root = use_config({"models.yaml": "audio:\n  models: []\n"}, "audio-supports")
store.save_with(
    lambda ov: ov.upsert_model(
        {
            "id": "tts1",
            "vendor": ov.upsert_vendor(
                {"id": "", "catalog_key": "dashscope", "label": "百炼", "api_key_env": "DASHSCOPE_API_KEY"}
            )["id"],
            "model": "qwen3-tts-flash",
            "kind": "audio",
            "priority": 1,
            "weight": 1,
        }
    )
)
cfg = config.load_config()
tts = next(m for m in cfg.pool("audio").models if m.id == "tts1")
check(tts.supports == ["text2audio"], f"audio 默认 supports 是 text2audio（实际 {tts.supports}）")
check(tts.describes(["text2audio"]), "因此能被 text2audio 的路由选中")


# ---------------------------------------------------------------- 6. secrets 形状
section("secrets 的 keys 写坏时不能吃掉真实密钥")

root = use_config(
    {
        "models.yaml": "image:\n  models: []\n",
        "secrets.yaml": "keys:\n  ARK_API_KEY: real-key\n",
        "secrets.web.yaml": "keys:\n",
    },
    "secrets-shape",
)
warnings: list[str] = []
merged = config.load_secrets(warnings)
check((merged.get("keys") or {}).get("ARK_API_KEY") == "real-key", "keys: null 不会清空底层真实密钥")
check(any("keys 必须是映射" in w for w in warnings), "并且给出形态告警（不是静默忽略）")

(root / "config" / "secrets.web.yaml").write_text("keys: ARK_API_KEY\n", encoding="utf-8")
warnings.clear()
merged = config.load_secrets(warnings)
check((merged.get("keys") or {}).get("ARK_API_KEY") == "real-key", "keys 写成字符串时同样保留真实密钥")


# ---------------------------------------------------------------- 7. 预览白名单
section("自定义 output_dir 也能预览")

custom = Path(tempfile.mkdtemp(prefix="mr-p1-custom-"))
root = use_config(
    {
        "models.yaml": (
            f'defaults:\n  output_dir: "{custom.as_posix()}"\n'
            "image:\n  models: []\n"
        )
    },
    "preview",
)
roots = [str(p) for p in webserver.preview_roots()]
check(str(custom.resolve()) in roots, "preview_roots() 包含配置里的 output_dir")
check(any("outputs" in r for r in roots), "默认产物目录仍然在（旧产物不受影响）")


# ---------------------------------------------------------------- 8. 厂商/CLI 收尾
section("厂商密钥变量、级联删除与 CLI 输出契约")

# 换平台要重新推导环境变量名
root = use_config({"models.yaml": "vendors: []\nimage:\n  models: []\n"}, "vendor-switch")
created = store.save_with(
    lambda ov: ov.upsert_vendor(
        {"id": "", "catalog_key": "zhipu", "label": "智谱", "api_key_env": "ZHIPU_API_KEY"}
    )
)
vid = created["id"]
switched = store.save_with(
    lambda ov: ov.upsert_vendor(
        {
            "id": vid,
            "catalog_key": "siliconflow",
            "label": "硅基流动",
            "api_key_env": "SILICONFLOW_API_KEY",
        }
    )
)
cfg = config.load_config()
vendor = cfg.vendors[vid]
check(vendor.api_key_env == "SILICONFLOW_API_KEY", f"换平台后环境变量名跟着换（实际 {vendor.api_key_env}）")
check("平台已更换" in switched.get("message", ""), "返回里说明了变量名变化")

# 删除厂商只级联「当前生效」引用它的模型
root = use_config(
    {
        "models.yaml": (
            "vendors:\n"
            "  - id: v1\n    provider: openai\n    api_key_env: OPENAI_API_KEY\n"
            "  - id: v2\n    provider: dashscope\n    api_key_env: DASHSCOPE_API_KEY\n"
            "image:\n  models:\n"
            "    - id: m4\n      vendor: v1\n      model: gpt-image-1\n"
            "      priority: 1\n      weight: 1\n"
        )
    },
    "cascade",
)
store.save_with(
    lambda ov: ov.upsert_model(
        {"id": "m4", "vendor": "v2", "model": "qwen-image-2.0", "kind": "image",
         "priority": 1, "weight": 1, "supports": ["text2img"]}
    )
)
store.save_with(lambda ov: ov.delete_vendor("v1"))
cfg = config.load_config()
check([m.id for m in cfg.pool("image").models] == ["m4"], "删 v1 不会误删已迁到 v2 的 m4")
check(cfg.pool("image").models[0].vendor == "v2", "m4 仍在 v2 下")

# generic_http 缺配置 → ConfigError（不计熔断）
try:
    adapters.run_adapter(
        ModelSpec(id="g", provider="generic_http", kind="image", model="x"),
        GenRequest(kind="image", prompt="p"),
        None,
        CTX,
    )
    check(False, "缺提交地址应当报错")
except ConfigError:
    check(True, "generic_http 缺配置抛的是 ConfigError（不计熔断）")
except Exception as exc:  # noqa: BLE001
    check(False, f"应当是 ConfigError，实际是 {type(exc).__name__}")

# CLI 参数错误 → 单个 JSON + 退出码 1
buf = io.StringIO()
try:
    with redirect_stdout(buf):
        cli.build_parser().parse_args(["generate", "--kind", "nope", "--prompt", "x"])
    check(False, "非法参数应当退出")
except SystemExit as exc:
    payload = json.loads(buf.getvalue().strip().splitlines()[-1])
    check(exc.code == 1, f"退出码是 1（实际 {exc.code}）")
    check(payload.get("status") == "error" and "参数错误" in payload.get("error", ""),
          "stdout 是单个 JSON，说明参数错误")

# 语音未接平台的提示不再是「只有图像/视频能生成」
hint = cli._unsupported_hint("audio")
check("dashscope" in hint and "只有 image / video" not in hint, "语音提示点名了已接入的平台")


print()
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
