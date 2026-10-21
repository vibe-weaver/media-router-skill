---
name: media-router
version: 1.2.0
description: >-
  当任务卡在生成图片/视频步骤时，用用户配置的多模型池完成生成并回传产物；
  支持优先级/权重分流、失败降级与熔断。触发词：生成图片、文生图、图生图、改图、
  生成视频、文生视频、图生视频、首尾帧、多模型池、模型权重、模型优先级、失败降级、media router。
agent_created: true
---

# media-router

给文本模型补上「出图 / 出视频」这条腿：用一个你自己维护的多模型池完成生成，
按 `priority` 分档、同档按 `weight` 加权随机，失败自动降级，连续失败熔断，
最后把产物路径和画面描述交回当前任务继续推进。

- 零依赖：Python 3.8+ 标准库，不需要 `pip install`。
- 覆盖图像/视频、图生图/首尾帧、多模型优先级与权重。
- 不想手写 YAML：运行下文 `web` 命令开网页配置台。
- 完整项目介绍与安装说明见 GitHub 仓库主页；下面是 agent 执行用的快速路径。

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
- 常用参数：`--size`、`--negative-prompt`、`--count`、`--output-dir`、`--max-attempts`、
  `--param key=value`。
- `--model <id>` 只能填 `list` 里已有的 id；没配过的 id 会被拒绝。
- 产物默认落盘到当前目录 `./outputs/`；stdout 的 `files[].path` 是绝对路径。
- 视频可能要 1–3 分钟；脚本内部已轮询，**不要**在外面套 sleep 重试。
- 忘了命令：直接运行脚本（不带子命令）会列出全部子命令和示例。

## 按结果处理

| 退出码 | status | 处理 |
|---|---|---|
| 0 | ok | 用 `files[].path` 继续原任务；汇报时用 `caption`，不要凭空描述画面；`notes` 里有需要告知用户的信息就转告 |
| 3 | delegate | 没有生成任何文件，见下 |
| 1 | error | 见下 |

**delegate**

- 当前环境如果有内置出图/出视频工具（例如 `ImageGen` / `VideoGen`），用 `delegate.arguments` 调用；
- 如果没有，**不要猜工具名，也不要调用当前环境里不存在的工具**；直接告诉用户：
  需要在配置页添加一个真实的第三方模型。
- 绝对不要把 delegate 当成生成成功。

**error**

- `fallback_trail[].kind == "config"`：多半缺 Key。让用户运行 `web` 配置，或读
  `references/setup.md`。
- `kind == "runtime"`：把 `error` 原文告诉用户，不要含糊成“生成失败”。
- 如果 `status=error` 且没有 `fallback_trail`：看 `skipped_missing_key` / `notes`，多半是缺 Key
  或模型被停用；让用户先配置，不要反复重试。JSON 顶层直接是 `kind: "config"` 时同理。
- 不要编造图片描述或文件路径。

## 什么时候用 / 不用

- 用：任务确实卡在“要一张图 / 一段视频”上，而且你需要把它补上才能继续。
- 不用：用户只是问“能不能生成”；纯文本任务；用户指定了别的平台或工具。

## 需要配置或排障时

- 用户不会配：让他运行 `python3 "<SKILL_DIR>/scripts/media_router.py" web`，
  细节见 `references/setup.md`。
- 想看模型池 / 会选谁：`list` / `resolve`（排障用，不要每次生成前都跑）。
- 要接新平台：`references/providers.md` 和 `references/setup.md`。
- **改配置是用户的事**：不要擅自改 `config/models.yaml`、权重或优先级。

## 硬性约束

- 只能调用配置里已有的模型；不许猜模型名，也不许绕过脚本直接 POST 第三方接口。
- 缺 Key 不要硬试，也不要假装成功。
- 第三方生成计费：用户只是“问问能不能”时不要真跑；批量生成前先确认。
- 没有 `files[].path` 就没有产物。
