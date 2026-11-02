"""命令行入口。

对 agent 的契约：**stdout 永远是单个 JSON 对象**，日志走 stderr。
退出码：0=成功，1=失败，3=需要 agent 亲自调用内置工具（委托）。

子命令：
  config     看配置解析结果与路径
  providers  看内置支持哪些 provider
  list       看模型池
  resolve    只看路由结果（不真调用，用来排查"会选谁"）
  generate   真正执行生成
  prompts    离线检索／查看图片、视频提示词参考
  report     手工上报某个模型的成败（用于外部调用后回写健康度）
  health     看/清健康度
  web        打开本机网页配置界面（配置 + 连通性检测都在这里）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import (
    SUPPORTED_KINDS,
    Ctx,
    DownloadError,
    GenRequest,
    GenResult,
    HttpError,
    KindNotSupported,
    known_providers,
    run_adapter,
)
from . import catalog
from .caption import build_caption, caption_enabled, profile_files
from .config import MODEL_KINDS, ConfigError, RouterConfig, load_config
from .health import HealthStore
from .lifecycle import GenerationInterrupted
from .selector import plan_attempts, rank_candidates
from .transport import execution_policy, is_fail_fast, proxy_description, redact
from .diagnostics import (
    CONFIG_ERROR_CODES,
    diagnose_error,
    error_payload as _error_payload,
    vendor_error_code as _vendor_error_code,
    vendor_error_detail as _vendor_error_detail,
)
from .outcomes import assess_result

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DELEGATE = 3


# ---------------------------------------------------------------- 基础设施


def _reconfigure_stdout() -> None:
    """Windows 控制台默认不是 UTF-8，中文 JSON 会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def _log(message: str) -> None:
    """日志走 stderr。空字符串用来打空行，别给它加前缀。

    出日志前统一打码：自建接口用 ?key= 传密钥时，整条 URL 会进日志。
    """
    if message:
        print(f"[media-router] {redact(message)}", file=sys.stderr, flush=True)
    else:
        print("", file=sys.stderr, flush=True)


def _emit(payload: dict[str, Any], pretty: bool) -> None:
    """输出单个 JSON。**产物地址里的签名必须原样保留**。

    以前这里把整段 JSON 过一遍 redact()，于是对象存储签名的 `Signature=…`
    被打成 `***`，`urls[]` 直接变成死链 —— 正常落盘不受影响，但 `--no-download`
    或事后想重新取回产物时就没法用了。现在按字段区分：
    地址类字段原样输出，其余文本（error / notes / 提示）照旧打码。
    """
    text = json.dumps(
        _redact_payload(payload), ensure_ascii=False, indent=2 if pretty else None
    )
    print(text, flush=True)


#: 这些字段的值是「可下载地址」，签名是它的一部分，打码等于把链接弄坏
_URL_FIELDS = {"urls", "url", "path", "files"}


def _redact_payload(value: Any, key: str = "") -> Any:
    """递归打码，但跳过地址类字段。

    地址类字段（``urls`` / ``files[].path``）原样保留 —— 签名是下载凭据，
    打掉链接就废了；其余文本（error / notes / hint）按默认规则打码，
    包括签名和 ``?key=``，避免把凭据写进错误的正文里。
    """
    if isinstance(value, dict):
        return {str(k): _redact_payload(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, key) for item in value]
    if isinstance(value, str) and key not in _URL_FIELDS:
        return redact(value)
    return value


def _coerce_scalar(text: str) -> Any:
    low = text.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _parse_params(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ConfigError(f"--param 需要 key=value 形式，收到：{item!r}")
        key, _, value = item.partition("=")
        out[key.strip()] = _coerce_scalar(value)
    return out


# 厂商错误解析统一由 diagnostics 提供；保留以上导入别名供嵌入调用兼容。


def _can_generate(kind: str, spec: Any) -> bool:
    """这个候选在这个类目下能否真的被 generate 调用。

    路由只回答「按优先级/权重会选谁」，不等于「这类目能生成」：语音池现在能算出
    完整候选序列，但所有 adapter 都只声明了 image / video，generate 一进
    run_adapter 就抛 KindNotSupported。这个判断同时被 resolve 的输出和
    generate 的建目录逻辑用到，口径必须一致。
    """
    return kind in SUPPORTED_KINDS.get(spec.provider, set())


def _requires_key(spec: Any) -> bool:
    """这个模型是否需要 API Key。

    三处判断（这里 / 目录条目的 keyless / 探测时的 resolve_key）必须口径一致，
    否则会出现"页面说不用密钥、命令行说缺密钥"这种自相矛盾的提示。

    generic_http 是配置驱动的：只有显式声明了鉴权方式才需要密钥，
    `auth: {type: none}` 或不写 auth 都视为公开接口。
    """
    if spec.is_native:
        return False
    opts = spec.options if isinstance(spec.options, dict) else {}
    if opts.get("keyless") is True:
        return False
    # 目录里标了 keyless 的平台（内置工具通道），一律不需要密钥
    entry = catalog.get(catalog.guess_key(spec.provider, spec.endpoint))
    if entry is not None and entry.keyless:
        return False
    auth = opts.get("auth") or {}
    kind = str((auth.get("type") if isinstance(auth, dict) else "") or "").strip().lower()
    if kind in ("none", "noauth", "no-auth", "public"):
        return False
    if spec.provider in ("generic", "generic_http", "http"):
        return bool(auth)
    return True


def _unsupported_hint(kind: str) -> str:
    """「这个平台还没接该类目」时的准确提示。

    早期这里写死「目前只有 image / video 能生成」，语音通了之后就成了假话：
    用户的模型其实能跑，只是那个平台没接，照这句话去改配置只会更迷路。
    """
    base = "这不是密钥或网络问题，重试也不会变。"
    if kind == "audio":
        return base + (
            "语音已接入 dashscope / openai（智谱、硅基流动也走这条）/ kling / volcengine；"
            "把这个语音模型换到这几家即可，其它平台暂未接 TTS。"
        )
    return base + (
        f"{kind} 类目目前只有部分平台接了适配器；换一个平台，"
        "或给这个模型配 provider: generic_http 自己填接口。"
    )


def _make_ctx(cfg: RouterConfig, key: str, source: str) -> Ctx:
    defaults = cfg.defaults
    return Ctx(
        api_key=key,
        key_source=source,
        timeout=float(defaults.get("timeout_seconds", 120) or 120),
        poll_interval=float(defaults.get("poll_interval_seconds", 5) or 5),
        max_poll=float(defaults.get("max_poll_seconds", 900) or 900),
        log=_log,
    )


def _infer_requires(kind: str, images: list[str], override: str | None) -> list[str]:
    """按类目推能力标签。音频只有 text2audio / audio2audio 两种，别退化成图像那套。"""
    if override:
        return [s.strip() for s in override.split(",") if s.strip()]
    if kind == "audio":
        return ["audio2audio"] if images else ["text2audio"]
    if images:
        return ["img2video"] if kind == "video" else ["img2img"]
    return ["text2video"] if kind == "video" else ["text2img"]


def _plan(
    cfg: RouterConfig, ordered: list[Any], max_attempts: int | None
) -> tuple[list[Any], list[str]]:
    """把候选序列裁成实际要尝试的清单。

    两条重要规则：
      1. 缺密钥的模型直接跳过，且**不占用** max_attempts 预算 —— 否则用户还没配 key，
         重试次数就被空转吃光，内置兜底通道永远轮不到。
      2. native（内置工具通道）不占预算，它永远是最后那个"总能落地"的兜底。
    """
    limit = max(1, int(max_attempts or 2))
    planned: list[Any] = []
    skipped: list[str] = []
    spent = 0
    for spec in ordered:
        if spent >= limit and not spec.is_native:
            continue
        if _requires_key(spec) and not cfg.resolve_api_key(spec)[0]:
            skipped.append(spec.id)
            continue
        planned.append(spec)
        if not spec.is_native:
            spent += 1
    # native 必须排在最后。它是兜底，一旦排在前面，第一轮就 delegate 交还给
    # agent 了，后面那些真模型根本没机会被尝试（排序稳定，其余顺序不变）。
    planned.sort(key=lambda spec: 1 if spec.is_native else 0)
    return planned, skipped


# ---------------------------------------------------------------- 子命令


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    payload = {"status": "ok", "version": __version__, **cfg.to_dict()}
    payload["kinds"] = cfg.kinds()
    payload["providers"] = known_providers()
    payload["caption_mode"] = caption_enabled(cfg)
    payload["proxy"] = proxy_description()
    _emit(payload, args.pretty)
    return EXIT_OK


def cmd_providers(args: argparse.Namespace) -> int:
    table = {
        "native": "内置工具委托：不调 HTTP，返回调用 ImageGen / VideoGen 的指令",
        "openai": "OpenAI 风格 /v1/images/generations 与 /v1/images/edits（改 endpoint 可兼容多数网关）",
        "dashscope": "阿里云百炼 / 通义万相（异步任务 + 轮询）",
        "volcengine": "火山方舟：即梦图片同步、Seedance 视频异步",
        "kling": "可灵：AK/SK 签 JWT，图片与视频均异步",
        "replicate": "Replicate：version 或 owner/name 两种提交方式",
        "fal": "fal.ai 队列接口",
        "generic_http": "完全配置驱动：填 YAML 即可接任意 REST 接口",
    }
    _emit({"status": "ok", "providers": table}, args.pretty)
    return EXIT_OK


def _pool_report(cfg: RouterConfig, health: HealthStore, kind: str) -> dict[str, Any]:
    pool = cfg.pool(kind)
    snapshot = health.snapshot([m.id for m in pool.models])
    models = []
    for spec in pool.models:
        item = spec.to_dict()
        needs_key = _requires_key(spec)
        key, source = cfg.resolve_api_key(spec) if needs_key else ("", "无需密钥")
        item["needs_api_key"] = needs_key
        item["api_key_ready"] = (not needs_key) or bool(key)
        item["api_key_source"] = source if key else ""
        item.update(snapshot.get(spec.id) or {})
        models.append(item)
    return {"kind": kind, "strategy": pool.strategy, "models": models}


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config()
    health = HealthStore(cfg.health_path)
    kinds = [args.kind] if args.kind else cfg.kinds()
    _emit(
        {
            "status": "ok",
            "config_path": str(cfg.config_path),
            "pools": [_pool_report(cfg, health, kind) for kind in kinds],
            "warnings": cfg.warnings,
        },
        args.pretty,
    )
    return EXIT_OK


def _resolve(cfg: RouterConfig, health: HealthStore, args: argparse.Namespace):
    pool = cfg.pool(args.kind)
    images = list(args.image or [])
    requires = _infer_requires(args.kind, images, args.supports)

    if args.model:
        spec = cfg.find_model(args.model)
        if spec.kind != args.kind:
            raise ConfigError(f"模型 {args.model} 属于 {spec.kind} 类目，与 --kind {args.kind} 不一致")
        return [spec], {
            "strategy": "forced",
            "requires": requires,
            "candidate_count": 1,
            "capability_relaxed": False,
            "health_relaxed": False,
            "excluded_cooling": [],
        }

    ordered, notes = rank_candidates(pool.models, requires, health)
    if not ordered:
        raise ConfigError(f"类目 {args.kind} 下没有任何可用（enabled）模型")
    return ordered, notes


def cmd_resolve(args: argparse.Namespace) -> int:
    cfg = load_config()
    health = HealthStore(cfg.health_path)
    ordered, notes = _resolve(cfg, health, args)
    attempts, skipped = _plan(cfg, ordered, args.max_attempts)
    if getattr(args, "fail_fast", False):
        attempts = [spec for spec in attempts if not spec.is_native][:1]
        notes["fail_fast"] = True
    snapshot = health.snapshot([m.id for m in ordered])

    blocked = [m.id for m in attempts if not _can_generate(args.kind, m)]
    resolve_notes: list[str] = list(cfg.warnings)
    if blocked:
        resolve_notes.append(
            f"{args.kind} 类目的生成还没有接入适配器（{'、'.join(blocked)}）："
            f"下面的 routed/will_attempt 只是路由推算，"
            f"generate --kind {args.kind} 会直接报错退出，别据此判断密钥或网络"
        )

    _emit(
        {
            "status": "ok",
            "kind": args.kind,
            "routing": notes,
            "ranked": [
                {
                    **m.to_dict(),
                    "generatable": _can_generate(args.kind, m),
                    "api_key_ready": (not _requires_key(m))
                    or bool(cfg.resolve_api_key(m)[0]),
                    **(snapshot.get(m.id) or {}),
                }
                for m in ordered
            ],
            "will_attempt": [m.id for m in attempts],
            "skipped_missing_key": skipped,
            "warnings": cfg.warnings,
            "notes": resolve_notes,
        },
        args.pretty,
    )
    return EXIT_OK


def cmd_generate(args: argparse.Namespace) -> int:
    """策略覆盖适配器、下载和可选描述；直接嵌入调用同样生效。"""
    with execution_policy(fail_fast=bool(getattr(args, "fail_fast", False))):
        return _cmd_generate(args)


def _cmd_generate(args: argparse.Namespace) -> int:
    started = time.time()
    cfg = load_config()
    health = HealthStore(cfg.health_path)
    threshold, cooldown = cfg.health_policy()

    base_dir = Path(args.output_dir).expanduser() if args.output_dir else cfg.output_dir
    # 图片和视频混在同一个目录里，几十个产物之后就分不清谁是谁了。按类目分子目录，
    # 同时保留 image_/video_ 文件名前缀 —— 下游按前缀筛选的老用法不受影响。
    out_dir = (base_dir / args.kind) if base_dir is not None else None

    ordered, notes = _resolve(cfg, health, args)
    attempts, skipped_candidates = _plan(cfg, ordered, args.max_attempts)

    if out_dir is not None and any(_can_generate(args.kind, m) for m in attempts):
        # 只在「这次真有可能产出文件」时才建目录。以前无条件建，
        # 结果一次失败的语音调用会在 outputs 下留下一个空的 audio/ ——
        # 看起来像「语音已经能出东西了」，反而误导人。
        # （图、视频都先建好目录：以前只给 image 建，video 靠 download()
        #   内部顺手 mkdir 兜底 —— 能跑通，但"这个目录到底什么时候被创建"
        #   变得取决于走哪条分支，出问题时很难推理。）
        out_dir.mkdir(parents=True, exist_ok=True)

    req = GenRequest(
        kind=args.kind,
        prompt=args.prompt or "",
        requires=notes.get("requires", []),
        images=list(args.image or []),
        size=args.size or "",
        aspect_ratio=args.aspect_ratio or "",
        duration=int(args.duration or 0),
        negative_prompt=args.negative_prompt or "",
        count=int(args.count or 1),
        output_dir=None if args.no_download else out_dir,
        extra=_parse_params(args.param),
    )

    if args.kind == "audio" and req.images:
        # 语音克隆 / 变声（audio2audio）没有适配器。不拦住的话，适配器会**静默忽略**
        # 输入音频，照常合成一段普通 TTS —— 用户以为克隆成功了，其实只是念了稿子，
        # 而且钱照扣。这里明确拒绝，别让它走进生成。
        _emit(
            {
                "status": "error",
                "kind": "audio",
                "error": "语音克隆 / 变声（audio2audio）还没有接入：当前只支持文本转语音",
                "fallback_trail": [],
                "routing": notes,
                "notes": list(cfg.warnings),
                "elapsed_ms": int((time.time() - started) * 1000),
                "hint": (
                    "去掉 --image 就是普通语音合成；要用自己的音色，"
                    "先在厂商控制台做声音复刻，再在模型配置里用 params.voice 指定音色 ID"
                ),
            },
            args.pretty,
        )
        return EXIT_ERROR

    trail: list[dict[str, Any]] = []
    attempted_ids: list[str] = []
    stop_reason = ""
    last_diagnostic: dict[str, Any] = {}
    #: 因为"这类目还没接适配器"而失败的次数。它不算模型故障，既不计熔断，
    #: 也不该让顶层提示变成"去检查密钥"。
    unsupported: list[str] = []
    notes_out: list[str] = list(cfg.warnings)
    if skipped_candidates:
        notes_out.append(
            "以下候选因未配置密钥被跳过（不占用重试次数）：" + "、".join(skipped_candidates)
        )

    if not attempts:
        # 所有候选都因缺 Key / 被停用被跳过。原来的报错是“所有候选模型都失败了”，
        # 但 fallback_trail 是空的，agent 会误以为接口挂了。这里直接给配置类错误，
        # 让“快速路径”不用先跑 resolve 也能正确判断。
        _emit(
            {
                "status": "error",
                "kind": args.kind,
                "error": "没有可尝试的模型：候选都被跳过或未启用",
                "skipped_missing_key": skipped_candidates,
                "routing": notes,
                "notes": notes_out,
                "elapsed_ms": int((time.time() - started) * 1000),
                "hint": "缺 Key 请运行 web 配置或读 references/setup.md；再用 list 确认模型是否 enabled。",
            },
            args.pretty,
        )
        return EXIT_ERROR

    for spec in attempts:
        if spec.is_native and is_fail_fast():
            _emit(
                {
                    "status": "error", "kind": args.kind,
                    "error": "严格模式不能在本进程完成内置生成验证，未执行内置委托",
                    "attempted": attempted_ids, "fallback_trail": trail,
                    "routing": notes, "notes": notes_out,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "hint": "请选择已配置的第三方模型，或在明确接受委托时关闭 --fail-fast。",
                }, args.pretty,
            )
            return EXIT_ERROR
        attempt_started = time.time()
        label = f"{spec.id}({spec.provider})"
        attempted_ids.append(spec.id)
        try:
            if spec.is_native:
                key, source = "", "无需密钥"
            else:
                key, source = cfg.resolve_api_key(spec)
                if _requires_key(spec) and not key:
                    raise ConfigError(source)

            _log(f"尝试 {label}，优先级={spec.priority} 权重={spec.weight}")
            result: GenResult = run_adapter(spec, req, cfg, _make_ctx(cfg, key, source))

        except KindNotSupported as exc:
            _log(f"{label} 跳过：{exc}")
            unsupported.append(exc.kind or args.kind)
            trail.append(
                {
                    "model": spec.id,
                    "provider": spec.provider,
                    "error": str(exc),
                    "kind": "config",
                    "elapsed_ms": int((time.time() - attempt_started) * 1000),
                }
            )
        except ConfigError as exc:
            # 配置类问题不计入熔断，否则会误伤模型
            _log(f"{label} 配置不可用：{exc}")
            trail.append(
                {
                    "model": spec.id,
                    "provider": spec.provider,
                    "error": str(exc),
                    "kind": "config",
                    "elapsed_ms": int((time.time() - attempt_started) * 1000),
                }
            )
        except DownloadError as exc:
            # 产物已经生成（平台侧多半已经扣费），只是下载/落盘失败。
            # 这里**不能**当成模型故障继续降级 —— 换个模型重跑就是第二次计费，
            # 而这一单的产物还在原来的 URL 上。所以直接收尾并把 urls 交出去。
            _log(f"{label} 产物下载失败：{exc}")
            trail.append(
                {
                    "model": spec.id,
                    "provider": spec.provider,
                    "error": str(exc),
                    "kind": "download",
                    "elapsed_ms": int((time.time() - attempt_started) * 1000),
                }
            )
            _emit(
                {
                    "status": "error",
                    "kind": args.kind,
                    "error": f"产物已生成，但下载失败：{exc}",
                    "model": {"id": spec.id, "provider": spec.provider},
                    "urls": list(getattr(exc, "urls", []) or []),
                    "files": list(getattr(exc, "files", []) or []),
                    "tasks": list(getattr(exc, "tasks", []) or []),
                    "phase": exc.phase,
                    "submission_uncertain": exc.submission_uncertain,
                    "stop_reason": "generated",
                    "fallback_trail": trail,
                    "routing": notes,
                    "attempted": attempted_ids,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "notes": notes_out,
                    "hint": (
                        "不要再跑一次 generate（会重复生成、重复计费）。"
                        "urls 里的地址通常还能直接下载；也可以到平台控制台的历史任务里取回产物。"
                    ),
                },
                args.pretty,
            )
            return EXIT_ERROR
        except GenerationInterrupted as exc:
            diagnostic = diagnose_error(exc)
            message = redact(str(exc))
            trail.append({"model": spec.id, "provider": spec.provider,
                          "error": message, "kind": diagnostic["kind"],
                          "diagnostic": diagnostic,
                          "elapsed_ms": int((time.time() - attempt_started) * 1000)})
            _log(f"{label} 已停止后续生成：{message}")
            _emit({
                "status": "error", "kind": args.kind, "error": message,
                "model": {"id": spec.id, "provider": spec.provider},
                "files": list(exc.files), "urls": list(exc.urls), "tasks": list(exc.tasks),
                "phase": exc.phase, "submission_uncertain": exc.submission_uncertain,
                "stop_reason": "uncertain" if exc.submission_uncertain else "generated",
                "diagnostic": diagnostic, "fallback_trail": trail,
                "routing": notes, "attempted": attempted_ids, "notes": notes_out,
                "elapsed_ms": int((time.time() - started) * 1000), "hint": exc.hint,
            }, args.pretty)
            return EXIT_ERROR
        except Exception as exc:  # noqa: BLE001 - 统一诊断后再决定是否允许降级
            last_diagnostic = diagnose_error(exc)
            message = f"{type(exc).__name__}: {exc}"
            if last_diagnostic["detail"]:
                message += " —— " + last_diagnostic["detail"]
            kind = last_diagnostic["kind"]
            _log(f"{label} {'配置不可用' if kind == 'config' else '失败'}：{message}")
            entry = {
                "model": spec.id, "provider": spec.provider,
                "error": message, "kind": kind,
                "diagnostic": last_diagnostic,
                "elapsed_ms": int((time.time() - attempt_started) * 1000),
            }
            if last_diagnostic["error_code"]:
                entry["error_code"] = last_diagnostic["error_code"]
            if isinstance(exc, HttpError) and exc.body:
                entry["response_body"] = exc.body[:600]
            if kind == "runtime":
                outcome = health.record_failure(spec.id, redact(message), threshold, cooldown)
                entry.update(outcome)
                if outcome.get("circuit_open"):
                    entry["note"] = f"连续失败达 {threshold} 次，进入 {cooldown:.0f} 秒冷却"
            trail.append(entry)
            if last_diagnostic["submission_uncertain"]:
                stop_reason = "uncertain"
                break
        else:
            # 适配器已完成：下面任何后处理或输出异常都不得回到生成降级分支。
            if result.status == "delegate":
                _log(f"{label} 走内置工具通道，交还 agent 执行")
                delegate_notes = list(notes_out)
                if trail:
                    delegate_notes.append(
                        "本次不是首选通道：内置兜底是在 "
                        + "、".join(str(t.get("model")) for t in trail)
                        + " 失败之后才接手的，失败原因见 fallback_trail"
                    )
                _emit({
                    "status": "delegate", "kind": args.kind,
                    "model": {"id": spec.id, "provider": spec.provider},
                    "delegate": result.delegate, "routing": notes,
                    "fallback_trail": trail,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "notes": delegate_notes,
                }, args.pretty)
                return EXIT_DELEGATE

            try:
                if health.record_success(spec.id) is False:
                    notes_out.append("生成已成功，健康状态未能回写：本次状态未保存，不影响已生成产物。")
            except Exception as exc:  # noqa: BLE001 - 状态记录不影响已生成产物
                notes_out.append(redact(f"生成已成功，健康状态未能回写：{type(exc).__name__}: {exc}"))

            files = list(result.files)
            # 后处理失败也保留真实路径，不用猜测的大小/格式冒充本地核验结果。
            profiles = [{"path": str(path)} for path in files]
            assessment = {
                "status": "unverified", "duration": [], "watermark": "unknown",
                "local_files_checked": False, "effective_spec": {},
            }
            try:
                profiles = profile_files(files, args.kind)
            except Exception as exc:  # noqa: BLE001 - 禁止因档案读取失败重新生成
                notes_out.append(redact(f"生成已成功，产物档案和规格未核验：{type(exc).__name__}: {exc}"))
            else:
                try:
                    assessment, assessment_notes = assess_result(spec, req, result, profiles)
                    notes_out.extend(assessment_notes)
                except Exception as exc:  # noqa: BLE001 - 不将验收故障计为生成故障
                    notes_out.append(redact(f"生成已成功，产物规格未核验：{type(exc).__name__}: {exc}"))

            if req.count and req.count > 1 and len(files) < req.count:
                notes_out.append(
                    f"本次要求 {req.count} 个产物，实际只得到 {len(files)} 个："
                    f"该模型的接口可能不支持批量，或平台只返回了一部分。"
                    f"需要更多可以分批执行，或用 --param <数量字段名>={req.count} 指定平台字段。"
                )

            caption_text = ""
            if files:
                try:
                    caption_text, cap_notes = build_caption(
                        cfg, files, args.kind, goal=args.prompt or "", profiles=profiles
                    )
                    notes_out.extend(cap_notes)
                except Exception as exc:  # noqa: BLE001 - 可选描述失败不能再次生成媒体
                    notes_out.append(redact(f"生成已成功，文字描述未生成：{type(exc).__name__}: {exc}"))

            _emit({
                "status": "ok", "kind": args.kind,
                "model": {"id": spec.id, "provider": spec.provider,
                          "model": result.meta.get("model") or spec.model or spec.id},
                "files": profiles, "urls": list(result.urls),
                "caption": caption_text, "caption_mode": caption_enabled(cfg),
                "request": {
                    "prompt": req.prompt, "negative_prompt": req.negative_prompt,
                    "size": req.size, "aspect_ratio": req.aspect_ratio,
                    "duration": req.duration, "count": req.count, "images": req.images,
                },
                "meta": result.meta, "assessment": assessment,
                "routing": notes, "fallback_trail": trail,
                "elapsed_ms": int((time.time() - started) * 1000), "notes": notes_out,
                "hint": (
                    "产物已落盘。若后续步骤需要引用它，直接用 files[].path 的绝对路径。"
                    + ("caption 字段即文字描述，可直接用于文字汇报。" if caption_text else "")
                ),
            }, args.pretty)
            return EXIT_OK

        if is_fail_fast():
            stop_reason = "fail_fast"
            break

    if unsupported and len(unsupported) == len(trail):
        _emit(
            {
                "status": "error",
                "kind": args.kind,
                "error": (
                    f"{args.kind} 类目的生成还没有接入适配器：模型能配置、能出现在 list 里，"
                    f"但暂时不能生成"
                ),
                "fallback_trail": trail,
                "routing": notes,
                "attempted": attempted_ids,
                "elapsed_ms": int((time.time() - started) * 1000),
                "notes": notes_out,
                "stop_reason": stop_reason or "unsupported",
                "hint": _unsupported_hint(args.kind),
            },
            args.pretty,
        )
        return EXIT_ERROR

    _emit(
        {
            "status": "error",
            "kind": args.kind,
            "error": (
                "提交结果不确定，已停止后续生成以避免重复计费" if stop_reason == "uncertain"
                else "首次失败即停，未尝试后续候选" if stop_reason == "fail_fast"
                else "所有候选模型都失败了"
            ),
            "stop_reason": stop_reason or "exhausted",
            "submission_uncertain": stop_reason == "uncertain",
            "diagnostic": last_diagnostic,
            "fallback_trail": trail,
            "routing": notes,
            "attempted": attempted_ids,
            "elapsed_ms": int((time.time() - started) * 1000),
            "notes": notes_out,
            "hint": last_diagnostic.get("hint") or (
                "先看 fallback_trail 中的错误与厂商原文；密钥就绪仅表示已配置，"
                "不保证模型可用。可在 web 中先做免费预检，再明确确认真实测试。"
            ),
        },
        args.pretty,
    )
    return EXIT_ERROR


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config()
    health = HealthStore(cfg.health_path)
    spec = cfg.find_model(args.model)
    threshold, cooldown = cfg.health_policy()
    if args.ok:
        health.record_success(spec.id)
        outcome: dict[str, Any] = {"recorded": "success"}
    else:
        outcome = health.record_failure(
            spec.id, args.error or "agent 上报失败", threshold, cooldown
        )
        outcome["recorded"] = "failure"
    snapshot = health.snapshot([spec.id])[spec.id]
    _emit(
        {"status": "ok", "model": spec.id, **outcome, "health": snapshot},
        args.pretty,
    )
    return EXIT_OK


def cmd_health(args: argparse.Namespace) -> int:
    cfg = load_config()
    health = HealthStore(cfg.health_path)
    if args.reset:
        # reset() 内部已经落盘，这里不用再 save 一次
        health.reset(args.model)
    ids: list[str] = []
    for pool in cfg.pools.values():
        ids.extend(m.id for m in pool.models)
    _emit(
        {
            "status": "ok",
            "health_path": str(cfg.health_path),
            "policy": dict(zip(("failure_threshold", "cooldown_seconds"), cfg.health_policy())),
            "models": health.snapshot(ids),
            **({"reset": args.model or "all"} if args.reset else {}),
        },
        args.pretty,
    )
    return EXIT_OK


def cmd_prompts(args: argparse.Namespace) -> int:
    """只读随包参考资料；成功正文不经过厂商错误文本打码。"""
    from .prompts import PromptLibrary, PromptLibraryError

    try:
        library = PromptLibrary()
        if args.prompt_command == "search":
            payload = library.search(
                kind=args.kind, query=args.query, category=args.category, limit=args.limit
            )
        else:
            payload = library.show(case_id=args.case_id, category=args.category)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    except PromptLibraryError as exc:
        _emit(
            {"status": "error", "kind": "prompt_library", "code": exc.code, "error": str(exc)},
            getattr(args, "pretty", False),
        )
        return EXIT_ERROR

    # 仅输出已审查的随包参考数据；不读取用户配置、凭据或媒体文件。
    print(
        json.dumps(payload, ensure_ascii=False, indent=2 if getattr(args, "pretty", False) else None),
        flush=True,
    )
    return EXIT_OK


def cmd_web(args: argparse.Namespace) -> int:
    """启动本机网页配置界面。"""
    from . import webserver

    return webserver.run(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


#: 不带子命令时要展示的引导。顺序按"新手最可能需要的"排，web 放第一个。
GUIDE: list[tuple[str, str]] = [
    ("web", "打开本机网页配置界面 —— 第一次用就选这个，浏览器里点着配"),
    ("config", "看配置解析结果、路径、有没有告警"),
    ("list", "看模型池：优先级、权重、Key 是否就绪、有没有被熔断"),
    ("resolve", "只看会选谁，不真调用（排查首选/降级序列）"),
    ("generate", "执行生成"),
    ("prompts", "离线检索／查看提示词参考，不调用模型"),
    ("providers", "看内置支持哪些平台"),
    ("report", "回写某个模型的成败（外部调用后同步健康度）"),
    ("health", "看/清熔断状态"),
]

GUIDE_EXAMPLES: list[tuple[str, str]] = [
    ("python scripts/media_router.py web", "配模型池（新手从这里开始）"),
    ("python scripts/media_router.py list --pretty", "看模型池现状"),
    ("python scripts/media_router.py generate --kind image --prompt \"一只戴圆框眼镜的橘猫\"", "生成一张图"),
    ("python scripts/media_router.py resolve --kind video --pretty", "只看视频会选哪个模型"),
    ('python scripts/media_router.py prompts search --kind image --query "商品 极简"', "检索图片提示词参考"),
]


def _guide_payload() -> dict[str, Any]:
    return {
        "status": "help",
        "message": "没有指定子命令，下面是可用命令。",
        "suggest": "第一次用请先跑 web 配好厂商和模型。",
        "commands": {name: text for name, text in GUIDE},
        "examples": [cmd for cmd, _ in GUIDE_EXAMPLES],
        "help": "python scripts/media_router.py <子命令> --help",
    }


def cmd_guide(args: argparse.Namespace) -> int:
    """没带子命令时的引导。

    小白用户很可能就是这么跑起来的，所以这里必须给出"下一步做什么"，
    而不是一句用法错误。人看的那份走 stderr，stdout 仍然只放一个 JSON。
    """
    _log("没有指定子命令。可用命令：")
    for name, text in GUIDE:
        _log(f"  {name:<10} {text}")
    _log("")
    _log("常用示例：")
    for command, note in GUIDE_EXAMPLES:
        _log(f"  {command}")
        _log(f"      {note}")
    _log("")
    _log("每个子命令的详细参数：python scripts/media_router.py <子命令> --help")
    _emit(_guide_payload(), args.pretty)
    return EXIT_OK


# ---------------------------------------------------------------- 解析器


class _JsonArgumentParser(argparse.ArgumentParser):
    """参数错误也要输出 JSON。

    argparse 默认把 usage 打到 stderr、退出码 2、stdout 一个字节都没有；
    而本项目对 agent 的契约是「stdout 是单个 JSON、退出码 0/1/3」。
    少了这一层，agent 少写一个 --prompt 就只能拿到空 stdout + exit 2。
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        _emit({"status": "error", "kind": "config", "error": f"参数错误：{message}"}, False)
        raise SystemExit(EXIT_ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog="media_router",
        description=(
            "媒体生成调度器：按优先级与权重从模型池里挑一个模型，生成图片或视频，"
            "失败自动按候选序列降级。stdout 输出单个 JSON，日志走 stderr。"
        ),
    )
    # 让 --pretty 既能放在子命令前，也能放在子命令后
    common = _JsonArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="JSON 缩进输出（人看的时候用）")

    parser.add_argument("--pretty", action="store_true", help="JSON 缩进输出（人看的时候用）")
    parser.add_argument("--version", action="version", version=f"media-router {__version__}")
    # 刻意不设 required：光敲 `media_router.py` 是很自然的动作，
    # 这时应该给一份引导而不是甩一句 "arguments are required: command"。
    sub = parser.add_subparsers(dest="command", required=False)
    parser.set_defaults(func=cmd_guide)

    p_config = sub.add_parser("config", parents=[common], help="查看配置解析结果与路径")
    p_config.set_defaults(func=cmd_config)

    p_providers = sub.add_parser("providers", parents=[common], help="列出支持的 provider")
    p_providers.set_defaults(func=cmd_providers)

    p_list = sub.add_parser("list", parents=[common], help="列出模型池")
    p_list.add_argument("--kind", choices=list(MODEL_KINDS), help="只看某个类目")
    p_list.set_defaults(func=cmd_list)

    def add_routing_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--kind", required=True, choices=list(MODEL_KINDS), help="要生成哪类媒体")
        p.add_argument("--supports", help="能力过滤，逗号分隔，如 text2img,img2img")
        p.add_argument("--fail-fast", action="store_true", help="首次失败即停：不重试HTTP、不切换候选、不内置委托；正常任务轮询不受影响")
        p.add_argument("--image", action="append", help="输入图片（本地路径或 URL），可重复")
        p.add_argument(
            "--model",
            help="强制指定模型 id，跳过路由（只能是配置里已有的 id，见 list）",
        )

    p_resolve = sub.add_parser("resolve", parents=[common], help="只看会选谁，不真调用")
    add_routing_flags(p_resolve)
    p_resolve.add_argument("--max-attempts", type=int, default=None)
    p_resolve.set_defaults(func=cmd_resolve)

    p_gen = sub.add_parser("generate", parents=[common], help="执行生成")
    add_routing_flags(p_gen)
    p_gen.add_argument("--prompt", required=True, help="生成提示词")
    p_gen.add_argument("--negative-prompt", help="负面提示词")
    p_gen.add_argument("--size", help="图片尺寸，如 1024x1024")
    p_gen.add_argument("--aspect-ratio", help="视频比例，如 16:9 / 9:16")
    p_gen.add_argument("--duration", type=int, help="视频时长（秒）")
    p_gen.add_argument("--count", type=int, default=1, help="生成数量")
    p_gen.add_argument(
        "--output-dir",
        help="产物落盘的**根**目录，默认 ./outputs（当前工作区）；"
        "实际写入 <根>/image 或 <根>/video",
    )
    p_gen.add_argument("--max-attempts", type=int, default=None, help="第三方候选预算，不控制HTTP重试，默认不含内置兜底；首次失败即停用 --fail-fast")
    p_gen.add_argument("--no-download", action="store_true", help="只拿远程 URL，不下载")
    p_gen.add_argument(
        "--param", action="append", help="透传给 provider 的额外参数，key=value，可重复"
    )
    p_gen.set_defaults(func=cmd_generate)

    p_report = sub.add_parser("report", parents=[common], help="外部调用后回写某个模型的成败")
    p_report.add_argument("--model", required=True, help="模型 id")
    group = p_report.add_mutually_exclusive_group(required=True)
    group.add_argument("--ok", action="store_true", help="记一次成功")
    group.add_argument("--fail", action="store_true", help="记一次失败")
    p_report.add_argument("--error", help="失败原因")
    p_report.set_defaults(func=cmd_report)

    p_health = sub.add_parser("health", parents=[common], help="查看或清空健康度")
    p_health.add_argument("--reset", action="store_true", help="清空熔断状态")
    p_health.add_argument("--model", help="只清某个模型（配合 --reset）")
    p_health.set_defaults(func=cmd_health)

    p_web = sub.add_parser(
        "web",
        parents=[common],
        help="打开本机网页配置界面（添加厂商、填 Key、配模型与权重、测试连通性）",
    )
    p_web.add_argument("--port", type=int, default=8760, help="端口，默认 8760")
    p_web.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址，默认只监听本机（改成别的地址等于把配置页面暴露到网络上，慎用）",
    )
    p_web.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_web.set_defaults(func=cmd_web)

    # 新增嵌套命令只补 pretty 的显式值，不能覆盖前一层已经解析的 True。
    prompt_common = _JsonArgumentParser(add_help=False)
    prompt_common.add_argument(
        "--pretty", action="store_true", default=argparse.SUPPRESS, help="JSON 缩进输出"
    )
    p_prompts = sub.add_parser(
        "prompts", parents=[prompt_common], help="只读检索图片／视频提示词参考"
    )
    prompt_sub = p_prompts.add_subparsers(dest="prompt_command", required=True)

    def prompt_query(text: str) -> str:
        if not text.strip():
            raise argparse.ArgumentTypeError("--query 不能为空")
        return text

    p_search = prompt_sub.add_parser(
        "search", parents=[prompt_common], help="搜索分类与案例，不调用模型"
    )
    p_search.add_argument("--kind", required=True, choices=["image", "video"])
    p_search.add_argument("--query", required=True, type=prompt_query)
    p_search.add_argument("--category")
    p_search.add_argument("--limit", type=int, choices=range(1, 6), default=3)
    p_search.set_defaults(func=cmd_prompts)

    p_show = prompt_sub.add_parser(
        "show", parents=[prompt_common], help="查看一个分类模板或案例"
    )
    selector = p_show.add_mutually_exclusive_group(required=True)
    selector.add_argument("--id", type=int, dest="case_id")
    selector.add_argument("--category")
    p_show.set_defaults(func=cmd_prompts)

    return parser


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) in ("generate", "resolve") and getattr(
        args, "max_attempts", None
    ) is None:
        try:
            cfg_for_default = load_config()
            args.max_attempts = int(cfg_for_default.defaults.get("max_attempts", 2) or 2)
        except Exception:  # noqa: BLE001 - 读不到配置就用默认值，真正的报错交给下面的 try
            args.max_attempts = 2

    try:
        return int(args.func(args))
    except ConfigError as exc:
        _emit({"status": "error", "error": str(exc), "kind": "config"}, args.pretty)
        return EXIT_ERROR
    except KeyboardInterrupt:
        _emit({"status": "error", "error": "被中断"}, getattr(args, "pretty", False))
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001
        _emit(
            {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "kind": "internal",
            },
            getattr(args, "pretty", False),
        )
        return EXIT_ERROR
