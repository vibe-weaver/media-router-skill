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
     成功只证明本次调用，不能保证今后一直可用，也不能替代画面、听感和规格验收。
     真实测试**只使用已保存且已启用的模型**：未保存的表单不参与，改了参数要先保存再测；
     额度确认只是页面提示，服务端不再接受表单里的模型/厂商/地址/密钥覆盖。
   图像、视频、语音均有两个入口；「首次失败即停」仅影响本次测试，不保存模型配置。
   列表重绘会保留当前页的结果及处理中按钮锁；整页刷新后内存记录会清空。真实测试失败时也显示已有文件、产物 URL 和任务。API 的 `deep` 只接受 JSON `true` / `false`，缺省免费；其他类型提前返回 400。

页面写两个文件：

- `config/models.web.yaml`：厂商 + 模型（不含密钥）；
- `config/secrets.web.yaml`：密钥。

它们**叠加**在用户手写的 `models.yaml` / `secrets.yaml` 之上，同 id 以页面为准，不会覆盖手写内容和注释。
两份都在 `.gitignore` 里。用 `MEDIA_ROUTER_CONFIG` / `MEDIA_ROUTER_SECRETS` 指向别的文件时是只读模式，
写接口返回 409；但**只读模式只拦住网页保存**，不限制同一用户权限下的程序改文件或直接发请求。
页面会一直阻塞到 Ctrl-C，**AI 助手不要用阻塞方式调它**。

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
- `--param` 只能改运行参数：`model`、`model_name`、`version`、`resource_id`、`endpoint`
  和凭据字段会在取 Key 之前被拒绝；`--model` 也必须是已配置且未停用的条目。
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

`error`（退出码 1）：`fallback_trail[]` 逐条列出失败模型、`kind`（`config` / `runtime` / `uncertain` / `delivery`，下载保留原有 `download`）、诊断及厂商原文。下载的旧错误结构继续兼容；生命周期停止另有公共 `diagnostic`。
`config` 包含密钥、账户、模型及明确的参数校验错误，不计熔断；引擎错误可能原因未明，不能仅凭 `InvalidParameter` 断言是用户配置错误。智谱 1113 是余额/资源包不足，不等同于限流。

如果厂商原文是 HTML 页面（如 `<!doctype html>`、登录页或网关错误页），通常说明 `endpoint` 填成了网页/控制台地址，或请求被登录页、代理、防火墙拦截。2xx 的 HTML 仍会标为 `submission_uncertain`，先核对接口地址与网络登录状态，再到平台确认任务和计费；确认前不要直接重跑。

`--max-attempts` 是第三方候选预算，不控制 HTTP 次数、默认也不包含内置兜底。要首次失败即停，用 `--fail-fast`，禁用 HTTP 重试、候选切换和内置委托；正常等待状态轮询仍继续。

生成提交不自动重发，包括自定义接口指定的 PUT/GET。提交超时、断连、HTTP协议接收错误、408/425、5xx，以及需解析 JSON 却返回空、损坏或合法但不完整的响应时，均可能返回 `submission_uncertain: true`。已取得任务 ID 后，查询错误/超时/未知状态/完成却缺产物同样停止，先查平台任务和账单，不要直接重跑。默认只读 GET 与下载保留有界重试。

已有产物后的目录、写入、重命名、下载错误，以及多次提交仅部分成功，返回错误且停止后续生成，不计模型故障。错误中 `files` 是完整文件路径列表，`urls` 是可单独下载的恢复地址，`tasks` 含任务 ID、脱敏查询地址与状态，`phase` 为 `submission` / `polling` / `delivery` / `partial`。产物 URL 保留可用签名，请勿当日志公开分享。内联图片与 `--no-download` 不兼容时仍保留已有远程地址，不回传整段 base64，也不会换模型重生成。明确失败任务若已附部分产物同样停止；无已有产物的明确拒绝/终止失败才保留默认降级。

媒体已生成后，描述、元数据评估或健康记录失败仅追加 `notes`，不重新生成、不累计生成模型故障；真实产物路径保留，核验不足标 `unverified`。

`status: ok` 只是生成调用成功；查看 `assessment` 和 `notes` 中时长差异、水印及未核验项。没有水印证据是未知，不等于无水印；未指定尺寸时平台默认的文件大小不可预知，不自动裁剪、压缩或重生成。任意嵌套模板的尺寸语义未建立时不当作平台默认；同时发送的多组精确尺寸冲突时标 `unverified` 并说明冲突，不猜平台覆盖顺序。

## 内置兜底

出厂配置的图片池和视频池各带一条 `provider: native` 的 `builtin-imagen` / `builtin-video`，
不调接口、不花钱、不出图，只在真实模型全部不可用时把请求交回给 agent。它优先级是 1000000，
永远排最后；配置页面里只能隐藏，不能编辑/删除。
