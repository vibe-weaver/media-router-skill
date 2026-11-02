---
name: media-router
version: 1.4.0
description: >-
  当任务卡在生成图片/视频/语音步骤时，用用户配置的多模型池完成生成并回传产物；
  支持优先级/权重分流、失败降级与熔断。触发词：生成图片、文生图、图生图、改图、
  生成视频、文生视频、图生视频、首尾帧、语音合成、文字转语音、配音、TTS、
  多模型池、模型权重、模型优先级、失败降级、media router。
agent_created: true
---

# media-router

文本模型遇到「生成一张图」「做个视频」「配段旁白」这类步骤时，只会说「我做不到」——任务就断在那里。

media-router 用一个你自己维护的多模型池把这一步接上：按优先级分档、同档内按权重加权随机挑模型， 失败自动降级到下一个候选，连续失败自动熔断，最后把产物路径 + 文字描述交回给文本模型，让原任务继续往下走。

它是一份 agent skill：把运行时目录放进 skills 目录即可用，SKILL.md 给 agent 读，本文件给你读。

零依赖 — 只用 Python 3.8+ 标准库，不需要 pip install 任何东西。 可移植 — 所有路径相对 skill 自身解析，放哪个 agent 的 skills 目录都能跑。

## 快速路径（默认，直接执行）

```bash
python3 "<SKILL_DIR>/scripts/media_router.py" generate --kind image --prompt "一只戴圆框眼镜的橘猫，扁平插画风"
python3 "<SKILL_DIR>/scripts/media_router.py" generate --kind video --prompt "樱花飘落的慢镜头" --aspect-ratio 16:9 --duration 5
```

- `<SKILL_DIR>` 是本 SKILL.md 所在目录。Python 优先 `python3`，没有就 `python`。
  Windows 下直接调用 Python，不要用 `Start-Process` / `cmd /c` 包一层。
- **不要先跑 `resolve`。** `generate` 内部已经完成路由、跳过缺 Key 的模型、失败降级；
  只有内置兜底时会立即返回 delegate，不会白等。
- **正常情况下不要为了理解脚本去读 `scripts/`、`README.md` 或 `references/`。**
  只有下面明确要求时才读 `references/setup.md`；stdout 是单个 JSON，直接按下面的 status 处理即可，
  日志在 stderr，不要解析。
- 图生图/图生视频：加 `--image <绝对路径或 URL>`。
- 常用参数：`--size`、`--negative-prompt`、`--count`、`--output-dir`、`--max-attempts`、`--fail-fast`、
  `--param key=value`。`--param` 是「点名发给接口的字段」：适配器认识的键优先，
  其余键也会原样透传（火山视频按 `--key value` 拼进提示词），日志里会写出透了哪些。
- `--kind` 支持 `image` / `video` / `audio`。语音的通路：`dashscope`
  （qwen-audio-*-tts-*、qwen3-tts-*）、`openai`（`/v1/audio/speech`）、
  `kling`（`/audio/tts`，异步任务，`params.voice_id` 必填，语言默认 zh）、
  `volcengine`（豆包语音 v3 单向流式，`params.speaker` 必填，要带 `X-Api-Resource-Id`）。
  智谱（`glm-tts`）和硅基流动（`CosyVoice2-0.5B` 等）走的就是 `openai` 这条，
  默认音色/格式按域名自动取（智谱 tongtong + wav，它**只收** wav/pcm；硅基 `<模型>:alex` + mp3），
  要换就写 `--param voice=xxx` / `--param response_format=wav`。
  配置里若挂了别的平台的 TTS（replicate、fal 等协议各不相同），会明确报
  「该类目还没有接入适配器」，别当成密钥问题反复重试。
- `--model <id>` 只能填 `list` 里已有的 id；没配过的 id 会被拒绝。
- 产物默认落盘到当前目录的 `./outputs/image/`、`./outputs/video/`（按类目分子目录）；
  stdout 的 `files[].path` 是绝对路径。
- 视频可能要 1–3 分钟；脚本内部已轮询，**不要**在外面套 sleep 重试。
- 忘了命令：直接运行脚本（不带子命令）会列出全部子命令和示例。

## 按结果处理

| 退出码 | status | 处理 |
|---|---|---|
| 0 | ok | 本次生成成功，不代表需求全部验收；用 `files[].path` 继续，查看 `assessment` / `notes` 中时长、水印及未核验提示。`caption` 是元数据或可选描述，不凭空描述画面 |
| 3 | delegate | 没有生成任何文件，见下 |
| 1 | error | 见下 |

**delegate**

- 当前环境如果有内置出图/出视频工具（例如 `ImageGen` / `VideoGen`），用 `delegate.arguments` 调用；
- 如果没有，**不要猜工具名，也不要调用当前环境里不存在的工具**；直接告诉用户：
  需要在配置页添加一个真实的第三方模型。
- 绝对不要把 delegate 当成生成成功。

**error**

- `fallback_trail[].kind == "config"`：看公共 `diagnostic` 与厂商原文，可能是密钥、账户、模型或明确的参数校验问题；不要一概让用户换 Key。需要时读 `references/setup.md`。
- `submission_uncertain: true`：平台可能已经受理并计费，先查平台任务/账单；不得自动重跑、换模型或改用内置工具补生成。已受理任务查询失败、完成却缺少产物或合法但缺少约定结果的响应也属于这条。
- `phase: delivery` / `partial`，或错误返回含已有 `files` / `urls` / `tasks`：本次可能已有完整子项，不能重做整批。错误里的 `files` 是可直接使用的完整路径列表，`urls` 可单独下载，`tasks` 可用于核对平台任务；按 `hint` 先取回。内联图片撞上 `--no-download` 也不能直接换参数重跑，避免重复计费。
- 用户要求遇错即停时必须加 `--fail-fast`：禁用 HTTP 重试、候选切换和内置委托；正常等待状态轮询仍继续。`--max-attempts` 仅是候选预算，不能替代它。生成提交默认也不会自动重发，包括自定义接口指定的 PUT/GET；普通只读查询仍按查询策略。
- `kind == "runtime"`：把 `error` 原文告诉用户，不要含糊成“生成失败”。
- 如果 `status=error` 且没有 `fallback_trail`：看 `skipped_missing_key` / `notes`，多半是缺 Key
  或模型被停用；让用户先配置，不要反复重试。JSON 顶层直接是 `kind: "config"` 时同理。
- 不要编造图片描述或文件路径。
- `status=ok` 但 `notes` 提示描述或档案核验失败时，使用已生成的真实文件并转告限制；不要为补描述/元数据再生成一次媒体。

## 什么时候用 / 不用

- 用：任务确实卡在“要一张图 / 一段视频”上，而且你需要把它补上才能继续。
- 不用：用户只是问“能不能生成”；纯文本任务；用户指定了别的平台或工具。

## 需要配置或排障时

- 用户不会配：让他运行 `python3 "<SKILL_DIR>/scripts/media_router.py" web`，
  细节见 `references/setup.md`。
- 想看模型池 / 会选谁：`list` / `resolve`（排障用，不要每次生成前都跑）。
  每个模型都带 `input_modalities` / `output_modalities`（以及对应的
  `*_modalities_text` 中文标签），用它判断这个模型吃不吃图、出图还是出视频，
  不要靠模型名猜。内置兜底（`provider: native`）没有这两个字段 —— 它不调接口，
  看到 `provider: native` 就直接走 delegate。
- 要接新平台：`references/providers.md` 和 `references/setup.md`。
- **改配置是用户的事**：不要擅自改 `config/models.yaml`、权重或优先级。

## 硬性约束

- 只能调用配置里已有的模型；不许猜模型名，也不许绕过脚本直接 POST 第三方接口。
- 缺 Key 不要硬试，也不要假装成功。
- 第三方生成计费：用户只是“问问能不能”时不要真跑；批量生成前先确认。
- 没有 `files[].path` 就没有产物。
