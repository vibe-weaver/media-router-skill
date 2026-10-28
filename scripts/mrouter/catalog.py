"""厂商目录 —— 配置页面靠它把"填一堆参数"变成"点几下"。

每个厂商提供：
  * 中文名字与一句话说明（小白看得懂）
  * 需要填哪些密钥字段（含去控制台哪里拿的提示）
  * 各类目接口的默认地址（用户不用知道 URL）
  * 是否能安全地校验密钥（见 auth_check：全都是**不会触发生成、不花钱**的 GET）

注意 auth_check 的设计原则：只做"读"操作。绝不用"提交一个空任务看报错"
这种取巧办法 —— 万一平台把空提示词当合法输入，就会白花用户的钱。
拿不到安全校验端点的平台（如 fal）如实标注"无法免生成校验"，引导用户用深度测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass
class KeyField:
    env: str
    label: str
    placeholder: str = ""
    hint: str = ""
    required: bool = True


@dataclass
class ProviderEntry:
    key: str
    label: str
    provider: str
    beginner: str
    kinds: list[str] = field(default_factory=lambda: ["image"])
    key_fields: list[KeyField] = field(default_factory=list)
    endpoints: dict[str, str] = field(default_factory=dict)
    suggestions: dict[str, list[str]] = field(default_factory=dict)
    #: 安全的鉴权校验端点（只读）。为空表示该平台拿不到，只能靠深度测试。
    auth_check: dict[str, str] = field(default_factory=dict)
    #: 模型列表端点（同样是只读、不计费）。为空表示要么靠 endpoint 推导，
    #: 要么这个平台根本没有列表接口 —— 后者必须如实告诉用户，不能假装拉过了。
    list_models: dict[str, Any] = field(default_factory=dict)
    signup: str = ""
    #: 界面要不要展示「接口参数（options，JSON）」。
    #: 只有完全配置驱动的自定义接口需要；内置平台的 options 是手写 YAML 的逃生口
    #: （task_base / inputs / version 之类），对页面用户没有意义，展示了只会让人
    #: 以为自己漏填了什么。
    needs_options: bool = False
    keyless: bool = False


ARK = "https://ark.cn-beijing.volces.com/api/v3"
DASHSCOPE = "https://dashscope.aliyuncs.com/api/v1"
KLING = "https://api-beijing.klingai.com/v1"
ZHIPU = "https://open.bigmodel.cn/api/paas/v4"

#: OpenAI 兼容接口的生成路径。从这些路径往回退一层就是 base，再拼 /models
#: 就是模型列表地址 —— 这条规律对百炼、硅基、方舟、智谱等都成立，所以用户
#: 只填了一个生成地址时，我们也能替他推出列表地址，不用他再去查文档。
_OPENAI_SUFFIXES = (
    "/images/generations",
    "/images/edits",
    "/videos/generations",
    "/video/generations",
    "/chat/completions",
    "/completions",
    "/embeddings",
)

CATALOG: list[ProviderEntry] = [
    ProviderEntry(
        key="volcengine",
        label="火山方舟（即梦 / Seedance）",
        provider="volcengine",
        beginner="字节跳动自家的方舟协议，只能连火山官方地址 —— 用中转站/自建网关请选「OpenAI 及兼容网关」。出图用即梦（Seedream），出视频用 Seedance，国内直连。语音合成属于**豆包语音**（另一个服务、另一套 Key），要单独加一个厂商条目。",
        kinds=["image", "video", "audio"],
        key_fields=[
            KeyField(
                env="ARK_API_KEY",
                label="方舟 API Key",
                placeholder="粘贴你的 API Key",
                hint="火山方舟控制台 → API Key 管理 → 创建。注意：语音合成要用「语音技术」的 Key（或旧版 appid:token），两者的 Key 不通用，语音请单独加一个厂商条目",
            )
        ],
        endpoints={
            "image": f"{ARK}/images/generations",
            "video": f"{ARK}/contents/generations/tasks",
            # 语音在另一个域名（豆包语音），V3 单向流式接口
            "audio": "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
        },
        suggestions={
            "image": ["doubao-seedream-3-0-t2i-250415"],
            "video": ["doubao-seedance-1-0-pro-250528"],
            "audio": ["doubao-tts"],
        },
        auth_check={"url": f"{ARK}/contents/generations/tasks?page_size=1", "auth": "bearer"},
        # 方舟确实有列模型的接口（ListFoundationModels），但它要 AccessKey/SecretKey
        # 做 V4 签名，跟这里用的 ARK_API_KEY 不是一套东西 —— 拿 Key 去请求只会 401。
        # 地址推导又会推出一个不存在的 /api/v3/models。两头都不通，所以如实标注不支持。
        list_models={
            "unsupported": (
                "火山方舟的模型列表接口需要用 AccessKey/SecretKey 做签名，"
                "和这里填的 API Key 不是一套，页面拉不到。"
                "请到方舟控制台「开通管理 / 在线推理」里复制模型 ID"
                "（形如 doubao-seedream-3-0-t2i-250415）手动填写。"
            )
        },
        signup="https://console.volcengine.com/ark",
    ),
    ProviderEntry(
        key="dashscope",
        label="阿里云百炼（通义万相）",
        provider="dashscope",
        beginner="阿里云自家的百炼协议，只能连百炼官方地址，中转站不适用。通义万相出图、出视频，通义千问出图，还能做语音合成（qwen-audio-tts / qwen3-tts），有免费额度。",
        kinds=["image", "video", "audio"],
        key_fields=[
            KeyField(
                env="DASHSCOPE_API_KEY",
                label="百炼 API Key",
                placeholder="sk-...",
                hint="阿里云百炼控制台 → API-KEY 管理",
            )
        ],
        endpoints={
            "image": f"{DASHSCOPE}/services/aigc/text2image/image-synthesis",
            "video": f"{DASHSCOPE}/services/aigc/video-generation/video-synthesis",
            # 语音合成的端点按模型分两套（互不通用）：
            #   qwen-audio-*-tts-* / cosyvoice-* → /services/audio/tts/SpeechSynthesizer
            #   qwen3-tts-*                      → /services/aigc/multimodal-generation/generation
            # 这里登记的是前者，适配器会按模型名自动切到后者。
            "audio": f"{DASHSCOPE}/services/audio/tts/SpeechSynthesizer",
        },
        suggestions={
            "image": ["wanx2.5-t2i-turbo", "wanx2.1-t2i-turbo"],
            "video": ["wanx2.1-t2v-turbo"],
            "audio": ["qwen-audio-3.1-tts-flash", "qwen3-tts-flash"],
        },
        auth_check={"url": f"{DASHSCOPE}/tasks?page_no=1&page_size=1", "auth": "bearer"},
        list_models={
            "url": f"{DASHSCOPE}/models",
            "models_path": "output.models",
            "name_field": "model",
            "auth": "bearer",
            "page_params": {"page_size": 100},
            # 百炼原生列表支持按能力过滤，这是最可靠的分类方式
            "kind_param": "capabilities",
            "kind_values": {"image": ["IG"], "video": ["VG"]},
        },
        signup="https://bailian.console.aliyun.com",
    ),
    ProviderEntry(
        key="kling",
        label="可灵（快手）",
        provider="kling",
        beginner="快手自家协议，只能连可灵官方地址。新版控制台发的是**单个 API Key**（api-key-kling-…），直接当 Bearer 用；旧版的 AccessKey + SecretKey 也仍然支持（写成 AK:SK 一行）。视频效果口碑好，也能做语音合成（/audio/tts，音色用 voice_id 指定）。",
        kinds=["image", "video", "audio"],
        key_fields=[
            KeyField(
                env="KLING_KEYS",
                label="API Key（或旧版 AK:SK）",
                placeholder="api-key-kling-… 或 AccessKey:SecretKey",
                hint="可灵开放平台 → API 密钥，复制 api-key-kling-… 直接粘贴；只有旧版控制台才需要把 AccessKey 和 SecretKey 用英文冒号连起来",
            )
        ],
        endpoints={
            "image": f"{KLING}/images/generations",
            "video": f"{KLING}/videos/text2video",
            # 语音是异步任务：POST /audio/tts 提交，GET /audio/tts/{task_id} 查询
            "audio": f"{KLING}/audio/tts",
        },
        suggestions={"image": [], "video": ["kling-v2-master", "kling-v1-6"], "audio": ["kling-tts"]},
        auth_check={"url": f"{KLING}/videos/text2video?pageNum=1&pageSize=1", "auth": "kling_jwt"},
        # 可灵的鉴权是 JWT（AccessKey:SecretKey 换 token），模型列表端点也没有
        # 验证过。推导出来的 /v1/models 未必存在，与其让用户点了报错，
        # 不如如实说不支持，引导按控制台名称填写。
        list_models={
            "unsupported": (
                "可灵的模型列表接口需要单独验证，页面暂时拉不到。"
                "请到可灵开放平台控制台复制模型名手动填写。"
            )
        },
        signup="https://klingai.com",
    ),
    ProviderEntry(
        key="openai",
        label="OpenAI 及兼容网关",
        provider="openai",
        beginner="OpenAI 官方，以及任何兼容 /v1/images/generations 的网关（中转站、自建代理等都算）。用中转站就点这张，再把「高级选项」里的接口地址换成网关地址：留空会走 api.openai.com，中转站的 Key 打不通官网。视频走的是 OpenAI 风格异步协议（智谱那套 /videos/generations），地址同样要换成网关的。",
        kinds=["image", "video", "audio"],
        key_fields=[
            KeyField(
                env="OPENAI_API_KEY",
                label="API Key",
                placeholder="sk-...",
                hint="官方 OpenAI 和中转站/自建网关的 Key 都填这里",
            )
        ],
        endpoints={
            "image": "https://api.openai.com/v1/images/generations",
            # OpenAI 官方没有 /videos/generations 这个路径，本仓库的视频协议是
            # 「OpenAI 风格异步任务」那套（提交 /videos/generations、查询
            # /async-result/{id}，见 adapters.OPENAI_VIDEO），参考实现是智谱。
            # 这里把出处地址写出来是有意的：留空并不会发到别处，适配器照样会
            # 静默回落到同一个地址（adapters._openai_video_target），用户反而
            # 看不见自己实际打到了哪台机器。
            "video": f"{ZHIPU}/videos/generations",
            # 语音是同步接口，一次 POST 直接回二进制，路径固定
            "audio": "https://api.openai.com/v1/audio/speech",
        },
        suggestions={"image": ["gpt-image-1", "dall-e-3"]},
        auth_check={
            "url": "https://api.openai.com/v1/models",
            "auth": "bearer",
            "models_path": "data",
            "models_name_field": "id",
        },
        list_models={
            "url": "https://api.openai.com/v1/models",
            "models_path": "data",
            "name_field": "id",
        },
        signup="https://platform.openai.com/api-keys",
    ),
    ProviderEntry(
        key="siliconflow",
        label="硅基流动 SiliconFlow",
        provider="openai",
        beginner="国内聚合平台，接口本身就是 OpenAI 协议，点这张会自动预填硅基的官方地址。模型多、价格低，适合试水；用别家的中转站请选「OpenAI 及兼容网关」。除了出图，还有 CosyVoice / fish-speech 系列可做语音合成。",
        kinds=["image", "audio"],
        key_fields=[
            KeyField(env="SILICONFLOW_API_KEY", label="API Key", placeholder="sk-...")
        ],
        endpoints={
            "image": "https://api.siliconflow.cn/v1/images/generations",
            "audio": "https://api.siliconflow.cn/v1/audio/speech",
        },
        suggestions={
            "image": ["Kwai-Kolors/Kolors", "black-forest-labs/FLUX.1-schnell"],
            "audio": ["FunAudioLLM/CosyVoice2-0.5B", "fnlp/MOSS-TTSD-v0.5"],
        },
        auth_check={
            "url": "https://api.siliconflow.cn/v1/models",
            "auth": "bearer",
            "models_path": "data",
            "models_name_field": "id",
        },
        list_models={
            "url": "https://api.siliconflow.cn/v1/models",
            "models_path": "data",
            "name_field": "id",
        },
        signup="https://cloud.siliconflow.cn",
    ),
    ProviderEntry(
        key="replicate",
        label="Replicate",
        provider="replicate",
        beginner="海外模型托管平台，用的是 Replicate 自有协议（不是 OpenAI 协议），只能连 replicate.com。FLUX、SDXL 等开源模型都能跑。",
        kinds=["image", "video"],
        key_fields=[
            KeyField(env="REPLICATE_API_TOKEN", label="API Token", placeholder="r8_...")
        ],
        endpoints={},
        suggestions={
            "image": ["black-forest-labs/flux-schnell", "black-forest-labs/flux-dev"],
            "video": ["wan-video/wan-2.2-t2v-fast"],
        },
        auth_check={"url": "https://api.replicate.com/v1/account", "auth": "bearer"},
        signup="https://replicate.com/account/api-tokens",
    ),
    ProviderEntry(
        key="fal",
        label="fal.ai",
        provider="fal",
        beginner="海外平台，用 fal 自有协议，只能连 fal.ai。出图出视频都快；这家没有免生成的只读校验接口，密钥只能靠真实生成来验。",
        kinds=["image", "video"],
        key_fields=[KeyField(env="FAL_KEY", label="API Key", placeholder="key-id:key-secret")],
        endpoints={},
        suggestions={
            "image": ["fal-ai/flux/schnell"],
            "video": ["fal-ai/wan-t2v"],
        },
        auth_check={},
        signup="https://fal.ai/dashboard/keys",
    ),
    ProviderEntry(
        key="zhipu",
        label="智谱 BigModel（CogView / CogVideoX）",
        provider="openai",
        beginner="智谱开放平台，走的是 OpenAI 协议、但地址是智谱自己的（点这张会自动预填）。CogView 出图、CogVideoX 出视频，GLM-TTS 做语音合成（/audio/speech，音色用 voice 指定），有免费模型可试。",
        kinds=["image", "video", "audio"],
        key_fields=[
            KeyField(
                env="ZHIPU_API_KEY",
                label="API Key",
                placeholder="粘贴你的 API Key",
                hint="open.bigmodel.cn → 控制台 → API Keys",
            )
        ],
        endpoints={
            "image": f"{ZHIPU}/images/generations",
            "video": f"{ZHIPU}/videos/generations",
            "audio": f"{ZHIPU}/audio/speech",
        },
        suggestions={
            "image": ["cogview-3-flash", "cogview-4"],
            "video": ["cogvideox-flash", "cogvideox-3"],
            "audio": ["glm-tts"],
        },
        # 实测确认（2026-09，用真实密钥只读验证）：
        #   * /api/paas/v4/models 存在，但只返回 10 个 glm-* **文本**模型，
        #     一个图像/视频模型都没有；?capabilities=IG 等过滤参数全被忽略。
        #   * /images/models、/model/list 都是 404；/models/list 返回"系统异常"。
        # 所以这里如实声明"列表里没有媒体模型"，让页面直接给出正确的模型名，
        # 而不是拉回一堆用不了的 glm-5 让用户去生图 —— 那必然失败，还查不出原因。
        list_models={
            "url": f"{ZHIPU}/models",
            "models_path": "data",
            "name_field": "id",
            "auth": "bearer",
            "media_missing": (
                "智谱的模型列表接口只返回 GLM 文本模型，不包含 CogView / CogVideoX。"
            ),
        },
        signup="https://open.bigmodel.cn/usercenter/apikeys",
    ),
    ProviderEntry(
        key="native",
        label="内置工具（无需密钥）",
        provider="native",
        beginner=(
            "不调用外部接口，把生成请求交回给当前 AI 助手用它自己的内置出图能力完成。"
            "没配任何密钥时靠它兜底。"
        ),
        kinds=["image", "video"],
        keyless=True,
        endpoints={},
        suggestions={},
        auth_check={},
    ),
    ProviderEntry(
        key="generic_http",
        label="自定义接口（其他协议）",
        provider="generic_http",
        beginner=(
            "上面的卡片都对不上时才用：接口地址、请求参数和返回结构（高级选项里的 JSON）"
            "全部得自己填。它没有内置协议，也没有只读的密钥校验和模型列表，"
            "具体字段见 references/providers.md。能选上面的卡就别选它。"
        ),
        kinds=["image", "video"],
        needs_options=True,
        key_fields=[
            KeyField(
                env="MY_API_KEY",
                label="API Key",
                required=False,
                hint="如果你的接口不需要密钥，可以留空",
            )
        ],
        endpoints={},
        suggestions={},
        auth_check={},
    ),
]

BY_KEY: dict[str, ProviderEntry] = {entry.key: entry for entry in CATALOG}


def get(key: str) -> ProviderEntry | None:
    return BY_KEY.get(key)


def vendor_id_for(provider_key: str, existing: list[str]) -> str:
    """生成一个不冲突的厂商 id，如 v-volcengine-2。"""
    base = f"v-{provider_key}"
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


def guess_key(provider: str, endpoint: str = "") -> str:
    """猜一个模型该用哪个目录条目。

    手写层里的模型只写了 provider，没有厂商条目可用。多数情况下 provider 就是
    目录 key，但有几个平台共享同一个 provider（硅基流动用的也是 openai 协议），
    这时要靠接口地址的域名把它们分开 —— 否则会去校验 api.openai.com，
    用户看到的密钥错误就跟真正的问题无关了。
    """
    provider = str(provider or "").strip()
    host = ""
    if endpoint:
        try:
            host = (urlparse(endpoint).hostname or "").lower()
        except ValueError:
            host = ""
    if host:
        for entry in CATALOG:
            for url in (entry.endpoints or {}).values():
                try:
                    if (urlparse(url).hostname or "").lower() == host:
                        return entry.key
                except ValueError:
                    continue
    if get(provider):
        return provider
    for entry in CATALOG:
        if entry.provider == provider:
            return entry.key
    return provider


def derive_list_url(endpoint: str) -> str:
    """从生成接口地址推出模型列表地址。

    OpenAI 兼容接口的规律是 ``<base>/<资源>/<动作>``，列表就在 ``<base>/models``。
    用户在页面上填的是生成地址，我们替他退回去拼一个，就不用他再查文档。

    推不出来就返回空串 —— 调用方要如实说"这个地址推不出列表接口"，
    而不是硬猜一个地址去请求。
    """
    url = str(endpoint or "").strip()
    if not url:
        return ""
    try:
        parts = urlparse(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    path = parts.path.rstrip("/")
    for suffix in _OPENAI_SUFFIXES:
        if path.endswith(suffix):
            base = path[: -len(suffix)]
            return f"{parts.scheme}://{parts.netloc}{base}/models"
    # 地址本身就是 /models 结尾的，直接用
    if path.endswith("/models"):
        return f"{parts.scheme}://{parts.netloc}{path}"
    return ""


def rebase_readonly_url(declared: str, user_endpoint: str) -> str:
    """把目录里写死的只读地址（密钥校验、模型列表）搬到用户自己填的域名上。

    目录里的 ``auth_check.url`` / ``list_models.url`` 都是**官方**域名。用中转站
    或自建网关的用户填的是自己的地址、密钥也是那边发的 —— 拿这把密钥去敲
    ``api.openai.com`` 必然 401，而这个 401 会顺着"轻量预检不过就不真生成"
    一路把「真实测试」掐死，用户永远看不到真正的失败原因。

    两条路：

      * ``…/models`` 这种 OpenAI 兼容列表端点，用 :func:`derive_list_url` 从
        用户填的生成地址推 —— 中转站把接口挂在子路径下时也对得上。
      * 其余（方舟的任务列表、百炼的 tasks）只换域名、保留路径与查询串。
        中转站基本都是反代官方路径，多数能对上；对不上就是 404，探测会如实
        标"未能确认密钥"（WARN，不拦真实测试），比一个必然误判的 401 好得多。

    同源、或任一地址不可解析时原样返回 ``declared``，官方用户行为不变。
    """
    try:
        declared_parts = urlparse(str(declared or ""))
        user_parts = urlparse(str(user_endpoint or ""))
    except ValueError:
        return str(declared or "")
    if not (declared_parts.scheme and declared_parts.netloc):
        return str(declared or "")
    if not (user_parts.scheme and user_parts.netloc):
        return str(declared or "")
    if (user_parts.hostname or "").lower() == (declared_parts.hostname or "").lower():
        return str(declared)

    if declared_parts.path.rstrip("/").endswith("/models"):
        derived = derive_list_url(user_endpoint)
        if derived:
            return derived

    query = f"?{declared_parts.query}" if declared_parts.query else ""
    return f"{user_parts.scheme}://{user_parts.netloc}{declared_parts.path or '/'}{query}"


def can_list_models_for(
    entry: ProviderEntry | None, endpoints: dict[str, str] | None = None
) -> bool:
    """按"这个厂商实际填的接口地址"判断能不能拉列表。

    跟 can_list_models(entry) 的区别：后者用的是目录里的默认地址，
    对 generic_http（自定义接口）永远是空的 —— 但用户自己填的地址完全
    可能是 OpenAI 兼容的，照样能推导出 /models。所以厂商视图要用这个版本，
    把 vendor.endpoints 传进来，按钮才会对自建接口也亮起来。

    判定顺序与 fetch_models 完全一致：显式 unsupported 优先（火山/可灵），
    其次目录声明的列表地址，再次旧式 auth_check，最后才是地址推导。
    """
    if entry is None:
        # 没有目录条目（纯手写 provider）：只要地址能推导就认
        return any(derive_list_url(str(u)) for u in (endpoints or {}).values())
    if entry.keyless:
        return False
    spec = entry.list_models or {}
    if spec.get("unsupported"):
        return False
    if spec.get("url"):
        return True
    if (entry.auth_check or {}).get("models_path"):
        return True
    # 用厂商实际填的地址推导；没填就退回目录默认地址
    urls = list((endpoints or {}).values()) or list((entry.endpoints or {}).values())
    return any(derive_list_url(str(u)) for u in urls)


def can_list_models(entry: ProviderEntry) -> bool:
    """这个平台能不能拉到模型列表。

    判断依据是"有没有可用的列表端点"，而不是"能不能校验密钥" ——
    这两件事原本被混在一起用（按钮的显示条件写的是 can_verify_key），
    结果就是能列模型但没配校验端点的平台不显示按钮，反之亦然。

    还有一类反过来的坑：地址推导太乐观。火山方舟和可灵的官方地址长得像
    OpenAI 兼容（都以 /images/generations 结尾），照规律推出来的
    /api/v3/models、/v1/models 实际上都不存在 —— 按钮亮着，点了必错。
    所以这两家在目录里显式声明了 ``list_models={"unsupported": ...}``，
    这里优先尊重那个声明，不做推导。
    """
    if entry.keyless:
        return False
    spec = entry.list_models or {}
    if spec.get("unsupported"):
        return False
    if spec.get("url"):
        return True
    # 旧写法：校验端点顺带能列模型
    if (entry.auth_check or {}).get("models_path"):
        return True
    # 目录里没有，但用户填了生成地址，就能按 OpenAI 兼容规律推一个出来
    for url in (entry.endpoints or {}).values():
        if derive_list_url(str(url)):
            return True
    return False


def as_dict() -> list[dict]:
    """给配置页面用的可序列化形式。"""
    return [
        {
            "key": e.key,
            "label": e.label,
            "provider": e.provider,
            "beginner": e.beginner,
            "kinds": e.kinds,
            "needs_options": e.needs_options,
            "keyless": e.keyless,
            "signup": e.signup,
            "key_fields": [
                {
                    "env": f.env,
                    "label": f.label,
                    "placeholder": f.placeholder,
                    "hint": f.hint,
                    "required": f.required,
                }
                for f in e.key_fields
            ],
            "endpoints": e.endpoints,
            "suggestions": e.suggestions,
            "can_verify_key": bool(e.auth_check),
            "can_list_models": can_list_models(e),
            # 拉不了列表时，页面上要能说清"为什么拉不了、该去哪里抄模型名"，
            # 而不是一句笼统的"该平台不提供模型列表"。
            "list_note": str((e.list_models or {}).get("unsupported") or ""),
        }
        for e in CATALOG
    ]
