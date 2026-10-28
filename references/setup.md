# 配置与排障（按需阅读）

只有这几种情况才需要读本文件：没有可用模型、缺 Key、要改配置、要接新平台。
**正常生成图片/视频时不要读它**，直接跑 `scripts/media_router.py generate ...` 即可。

## 最快的配法：网页

```bash
python3 "<SKILL_DIR>/scripts/media_router.py" web
```

终端会打印一个本机地址（形如 `http://127.0.0.1:8760/`）并自动打开浏览器：

1. **厂商配置**：点一个内置平台卡片（火山方舟 / 阿里云百炼 / 可灵 / OpenAI 兼容网关 /
   硅基流动 / Replicate / fal / 智谱 / 内置工具 / 自定义接口），粘贴 API Key 保存。
2. **模型配置**：选厂商 → 填模型名 → 选图像 / 视频 / 语音页 →（可选）填生成规格 → 配权重/优先级 → 保存。
   页面上有两个测试：
   - **测试连通性**：只调只读接口验密钥，不生成、不花钱；
   - **真实测试**：真跑一次生成，验证“提交→推理→下载→落盘”，会消耗额度，页面会先确认。
     这是 100% 确认模型名可用的唯一办法。

页面写两个文件：

- `config/models.web.yaml`：厂商 + 模型（不含密钥）；
- `config/secrets.web.yaml`：密钥。

它们**叠加**在用户手写的 `models.yaml` / `secrets.yaml` 之上，同 id 以页面为准，不会覆盖手写内容和注释。
两份都在 `.gitignore` 里。用 `MEDIA_ROUTER_CONFIG` / `MEDIA_ROUTER_SECRETS` 指向别的文件时是只读模式，
写接口返回 409。页面会一直阻塞到 Ctrl-C，**AI 助手不要用阻塞方式调它**。

## 命令行配法

改 `config/models.yaml`：

```yaml
image:
  strategy: priority_then_weight   # 默认：先分档，同档按权重加权随机
  models:
    - id: jimeng-seedream
      provider: volcengine
      model: doubao-seedream-3-0-t2i-250415
      priority: 1                  # 数值越小越优先，priority=1 永远压过 priority=2
      weight: 50                   # 同档内的权重，50 和 30 就是 50:30 的出手比
      enabled: true
      supports: [text2img]
      api_key_env: ARK_API_KEY     # 密钥从哪个环境变量读
```

- `strategy` 只剩 `priority_then_weight`：先按 `priority` 分档，同档内按 `weight` 加权随机。
  （`weight_only` / `fallback_chain` 已下线，老配置会被自动纠正并告警。）
- 密钥放环境变量，或 `secrets.yaml` 的 `keys` 下；**不要**写进 `models.yaml` 的 `api_key` 字段。
- 接入文档里没有的平台，用 `provider: generic_http` 填 YAML 即可，不用改代码。
  各家参数对照见 `references/providers.md`。
- **⚠️ 比值/日期/占位符必须加引号**：裸写的 `1:1` 会被 YAML 当成六十进制整数 **61**
  （`16:9` 是 969）。正确写法 `aspect_ratio: "1:1"`。同理 `"{{prompt}}"`、`"2026-01-01"`、
  `"重要: 别删"`。加载时会做原文体检，问题会出现在 `config` / `list` 的 `warnings` 里 ——
  看到 warnings 要转告用户。

## 命令

| 命令 | 用途 |
|---|---|
| `config` | 看配置解析结果、路径、可用 provider |
| `providers` | 看内置支持哪些 provider 及其接入方式 |
| `list [--kind image\|video\|audio]` | 看模型池、优先级、权重、Key 是否就绪、是否被熔断 |
| `resolve --kind ...` | **只看会选谁**，不真调用。排障用，不要每次生成前都跑 |
| `web [--port 8760] [--no-browser]` | 打开本机网页配置界面 |
| `generate --kind ... --prompt "..."` | 执行生成 |
| `report --model ID --ok\|--fail [--error MSG]` | 外部调用后回写某个模型的成败 |
| `health [--reset [--model ID]]` | 看/清熔断状态 |

## 输出字段（排障用）

`ok`（退出码 0）：

```json
{
  "status": "ok",
  "model": {"id": "jimeng-seedream", "provider": "volcengine", "model": "doubao-seedream-3-0-t2i-250415"},
  "files": [{"path": "/abs/out/image_xxx.png", "bytes": 148213, "size_human": "144.7KB",
             "format": "png", "width": 1024, "height": 1024}],
  "caption": "一只戴圆框眼镜的橘猫，扁平插画风格，暖色背景……",
  "fallback_trail": [],
  "notes": []
}
```

`delegate`（退出码 3）：`delegate.tool` / `delegate.arguments` / `delegate.note`。
没有产物，必须由 agent 用当前环境的内置工具补上；没有内置工具就停下告知用户。

`error`（退出码 1）：`fallback_trail[]` 里逐条列出失败模型、`kind`（`config` / `runtime`）和 `error`。
`config` 多半是缺 Key；`runtime` 是接口/网络/参数错误，原样转告用户。

## 内置兜底

出厂配置的图片池和视频池各带一条 `provider: native` 的 `builtin-imagen` / `builtin-video`，
不调接口、不花钱、不出图，只在真实模型全部不可用时把请求交回给 agent。它优先级是 1000000，
永远排最后；配置页面里只能隐藏，不能编辑/删除。
