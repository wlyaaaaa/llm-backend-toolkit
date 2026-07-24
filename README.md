# LLM Backend Toolkit

一个供 Codex 等顶级模型调用的轻量工具：把长任务整理成紧凑上下文，显式调用云端或本地模型，并通过结果回执而不是长思考过程进行监督。

它不是 Agent，也不会自行决定模型降级。

## 核心能力

- 通过版本化 backend registry 接入可替换的本地模型和 API 平台；稳定角色 `local-default` 默认只解析到本地后端。
- 默认确定性上下文压缩，返回压缩前后估算和是否有损。
- token 估算区分中日韩字符与 ASCII，压缩循环按 token 预算收敛。
- 可直接引用 UTF-8 文本 source；工具在内部检索相关片段，Codex 无需先读取整份材料。
- 长结果自动外置为本地 artifact，默认返回短预览和 hash。
- 默认关闭 thinking，不向顶级模型返回长推理过程。
- 支持本地模型原生图片、LocalOCR 和 ChineseASR 三种媒体路线。
- 异步 Smart Job：提交立即返回，顶级模型无需被长时间命令阻塞。
- 欠费、权限、GPU 占用等错误只返回裁决选项，不自动调用另一个模型。
- 任何云端调用都要求显式 `privacy.cloud_allowed=true`，包括 task 文本、source 片段与媒体。
- agent mode 通过 aicli 的 Windows 外层沙箱调用原生 CLI；`data_factory` 从 registry 解析精确 Profile 与模型，不做运行时猜测或 fallback。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

项目运行时无第三方 Python 依赖。

## Backend registry 与主动发现

查看当前安全元数据不会发模型请求：

```powershell
.\.venv\Scripts\llm-backend-toolkit.exe backends
.\.venv\Scripts\llm-backend-toolkit.exe status
```

内置注册表位于包内 `default_backends.json`；设置 `LLM_TOOLKIT_BACKEND_REGISTRY` 可让机器自己的 JSON 注册表成为运行时事实源。请求省略 `backend` 时只使用注册表的本地 `default_backend`，不会自动选择云端。旧字段 `provider` 和旧 Qwen ID 仅作为兼容 alias。

注册表把 backend ID、adapter、模型、端点环境变量、数据去向、AICLI Profile、route、runner 与版本绑定证据分离。替换 Ollama 模型、OpenAI Chat 兼容 API 或已有 AICLI Profile 只需改注册表；route 名称可以自定义并映射到已实现的 runner adapter，全新 wire protocol 或全新智能体 CLI 才需要增加代码 adapter。已验收模型的 digest/父模型一旦不匹配，`live_verified` 自动失效并阻止沿用旧验收。注册表禁止内嵌凭据；云端 `openai-chat` 地址必须使用 HTTPS。

`local-hard-reasoning` 是显式的本地 direct 角色，仍通过 `127.0.0.1:32100` 的 LocalGpuBroker 使用同一个 `qwen-main-v1`，不会创建或加载第二个模型别名。它把经校准的 Qwen thinking-general 采样参数作为受限 `ollama_options` 写入 `/api/chat`，并在 registry 中声明 `required_reasoning_mode=on`；漏写或关闭 thinking 会在读取 source、处理媒体或调用 provider 前失败关闭。省略 backend 仍解析到参数不变的 `local-default`，工具也不会因失败自动切换到此角色。隐藏 thinking 在 provider 边界即丢弃，只保留公开回答和非正文计数。

`ollama_options` 只允许出现在本地 Ollama backend 配置中，并且只接受 `temperature`、`top_p`、`top_k`、`min_p`、`presence_penalty`、`repeat_penalty`、`num_ctx` 与 `num_predict` 的有界数值。请求本身不能任意注入这些参数，云端 adapter 也会拒绝该字段。

## AI 默认入口：异步任务

提交请求会立即返回 `job_id`：

```powershell
.\.venv\Scripts\llm-backend-toolkit.exe submit --request .\examples\local-request.json
```

稍后读取紧凑结果：

```powershell
.\.venv\Scripts\llm-backend-toolkit.exe job --id <job_id> --result
```

如果输出很长，`output` 会变成 artifact 引用。只有确有必要时才读取完整结果：

```powershell
.\.venv\Scripts\llm-backend-toolkit.exe job --id <job_id> --full-result
```

`invoke` 是同步底层接口，不是 Codex 调用本地大模型的默认入口。

不含外部文件引用的相同请求默认复用已完成结果；agent workspace、source 或 media 等可变引用默认不缓存。只有调用者提供绑定真实内容 hash 的 `cache_key` 才允许这类请求命中缓存。明确需要一次新尝试时使用 `submit --force`。
回执同时给出 `recommended_check_utc` 与 `monitor_until_utc`。初次建议等待 30-60 秒，过早读取后指数退避；任务超过硬期限会显示 `stale`、停止建议轮询，并把重试或接管交回顶级模型。

## 人类只读仪表盘

Windows 上可用一个常驻 PowerShell 窗口观察最新任务：

```powershell
pwsh -NoProfile -File .\scripts\Show-LlmBackendDashboard.ps1
```

仪表盘使用增量行刷新，不会每两秒清屏；`R` 在人类视图与原始结果间展开/收起，`Q` 退出。direct Ollama 任务会显示中文阶段、公开回复片段、流式估算 TPS，最终块到达后改用 `eval_count / eval_duration` 的准确 TPS。结构化 JSON 在生成过程中不展示残缺正文。

`progress.json` 只保留阶段、计数、公开回复短预览和少量最近事件；prompt、隐藏 thinking 原文、原始子进程事件、命令正文与命令输出不会写入进度文件。仪表盘中的“思考与工作进展”是可验证的工作摘要，不是 chain-of-thought。

## 数据工厂智能体

默认请求见 [examples/local-agent-request.json](examples/local-agent-request.json)。关键字段：

- `execution.mode=agent`
- `execution.runner=data_factory`（可省略；从 backend registry 锁定精确 Profile/模型）
- `execution.policy=workspace-write|read-only`
- `execution.budget`（墙钟上限是所有 harness 的硬边界；step/tool-call 上限仅在上游 CLI 支持时强制）

`workspace-write` 必须指向隔离 worktree 或暂存任务目录。canonical raw、唯一事实源和不可恢复数据应留在可写根之外；智能体只产生 derived、checkpoint 和 receipt。可变工作区默认不复用已完成 job；只有调用者提供绑定输入内容 hash 的 `execution.cache_key` 才允许 cache hit。

显式候选 `qwen-code`、`opencode`、`codex-cli`、`claude-code` 供顶级模型有理由时选择，工具不会自行换 harness。任何失败都返回当前 runner、exit code、墙钟时间和顶级模型裁决选项，不回传事件流或 chain-of-thought。

当前 `local-default` 解析到 `codex-ollama-main + qwen-main-v1`，已经过本机版本绑定验收。当前 `cloud-qwen-plus` 解析到 `codex-qwen-paygo + qwen3.7-plus`；这是**未实测推荐**。将来注册表可以替换两者，而历史报告不会自动继承到新指纹。
`status` 会在不发模型生成请求的情况下返回当前 backend、模型指纹、agent 默认路由、证据状态和支持的 runner；实际任务回执同时记录精确 Profile、模型与是否采用默认。

云端示例见 [examples/cloud-agent-request.json](examples/cloud-agent-request.json)。普通单次摘要、抽取和结构化生成继续使用 `execution.mode=direct`，避免为不需要文件/命令循环的任务增加 Agent 调用成本。

本机 35B 的 PersonalOS 风格小型清洗专项、选择理由和严格适用范围见 [数据工厂智能体验收报告](docs/agent-data-factory.md)。该报告不代表通用智能排名。

## 四 harness 通用代理基准

`general_agent_v1` 将 PersonalOS 专项题之外的能力拆成三个可复现任务：证据推理与抗提示注入、代码修复、约束工作流规划。隐藏 verifier 不进入智能体可写工作区，安全和正确性是门禁，只有同分才比较时间。

```powershell
python scripts/run_general_agent_benchmark.py --list
python scripts/run_general_agent_benchmark.py
```

默认依次串行运行 Codex CLI、Claude Code、Qwen Code 与 OpenCode，均使用显式本地 `qwen-main-v1`。结果只对记录的 suite fingerprint、模型 digest、CLI 版本、沙箱合同和 Toolkit 提交有效；它比较的是“模型 + harness”的代理表现，不生成永久通用智商分。

2026-07-22 的完整实测、耗时、能力边界和默认建议见 [四 harness 通用代理验收报告](docs/general-agent-benchmark-v1.md)。当前版本的建议是 `data_factory` 继续固定使用 Codex CLI；日常调用不重复跑四 harness。

## 通过 source 引用减少 Codex 上下文

请求可以只传本地文本 artifact 的路径：

```json
{
  "backend": "local-default",
  "task": {
    "goal": "找出设计中与欠费裁决有关的边界",
    "instructions": ["只依据提供的 source"],
    "inputs": [],
    "sources": [
      {
        "id": "design",
        "path": "C:/path/to/design.md",
        "top_k": 4,
        "max_chars": 8000
      }
    ],
    "expected_output": {
      "format": "json",
      "required_keys": ["answer", "evidence"]
    }
  },
  "context": {
    "mode": "compact",
    "target_tokens": 8192,
    "pinned": ["不要编造未提供的事实"]
  },
  "reasoning": {"mode": "off"},
  "privacy": {"cloud_allowed": false}
}
```

工具按目标对文本分块、打分并选取少量片段，向结果返回 source hash 和行号范围。它只支持明确批准的 UTF-8 文本 artifact；PDF、Office、图片和音频应先由相应结构化读取器或媒体 adapter 处理。

## 媒体路线

- `native`：把图片直接交给声明支持视觉的后端。
- `specialist`：图片交给 LocalOCR，音频交给 ChineseASR，再把文本交给后端。
- `auto`：一般图片优先原生视觉；精确文字、表格、公式、扫描件优先 OCR；音频使用 ASR。

LocalOCR 与 ChineseASR 的入口通过环境变量注入：

```text
LLM_TOOLKIT_LOCALOCR_ENTRY
LLM_TOOLKIT_CHINESEASR_ENTRY
```

本地 Ollama 默认访问 `http://127.0.0.1:32100`。该入口应由机器自己的 GPU broker 管理。
专项媒体在后台 worker 内串行完成；OCR 使用 `-StopAfter` 释放 GPU 后才启动本地 Qwen，ASR 完成并释放 Broker 租约后才进入模型阶段。
agent mode 的默认 Codex CLI 会用原生 `--image` 附加一般图片；OpenCode 使用文件附件。精确 OCR 仍走 LocalOCR，音频仍走 ChineseASR。Qwen Code/Claude Code 对本地图片的 CLI 传递能力标为有限制，不把“能看到路径”冒充原生多模态已验证。

## 云端与其他 API 平台

公开项目只读取：

```text
DASHSCOPE_API_KEY
LLM_TOOLKIT_QWEN_BASE_URL
```

当前公开示例是 `cloud-qwen-plus`，但 `openai-chat` adapter 可通过外部注册表接入其他兼容平台；只有全新协议才需要新增 adapter。API key 只写环境变量名，远程云端地址必须是 HTTPS。测试套件不会发起真实云端调用；云端请求格式和错误分类使用 mock 验证。
选择任何云端 backend 仍不等于授权传输；请求必须同时设置 `privacy.cloud_allowed=true`。云端 probe 还需显式传入 `--cloud-allowed`。
Agent 模式下 Toolkit 会把 Codex 原生参数显式锁定为 `--model qwen3.7-plus`，不会继承 AICLI 通用千问 Profile 的 Max 默认值。欠费会归一化为 `billing_unavailable` 并把本地调用、顶级模型接管或账务处理选项返回调用者，不会自动降级。

百炼官方资料：

- [OpenAI 兼容 Chat Completions](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)
- [Codex 接入](https://help.aliyun.com/zh/model-studio/codex)
- [OpenAI 兼容 Responses API](https://help.aliyun.com/zh/model-studio/compatibility-with-openai-responses-api)
- [错误码](https://help.aliyun.com/zh/model-studio/error-code)

## 有界能力探测

探测是顶级模型的候选工具，不是强制门禁：

```powershell
.\.venv\Scripts\llm-backend-toolkit.exe probe --backend local-default --case instruction
.\.venv\Scripts\llm-backend-toolkit.exe probe --backend local-default --case json
.\.venv\Scripts\llm-backend-toolkit.exe probe --backend local-default --case context
.\.venv\Scripts\llm-backend-toolkit.exe probe --backend local-default --case vision --attachment <image>
```

建议只在首次重要使用、模型别名/digest 变化或结果异常时做小样本测试。
探测命令同样只提交后台任务并立即返回 `job_id`，随后用 `job --id <job_id> --result` 读取结果。

## 有界子对话

默认只做一次任务。确需修正或追问时，在新请求中加入：

```json
"continuation": {"from_job_id": "<completed-job-id>", "max_turns": 3}
```

工具只携带上一轮紧凑结果预览与 receipt，默认最多 3 轮、硬上限 8 轮；它不依赖 Codex、Claude 或某家 API 的隐藏 session，因此更换模型和平台后仍可延续。`delegation_receipt` 记录后端压缩和按路径引用的数据规模，`delivery_receipt` 记录长结果预览避免回传的估算量；二者是成本判断证据，不冒充 Codex 计费 token。

## 不做什么

- 不自动 fallback。
- 不持续监控模型思考过程。
- 不提供无限会话、长期隐式记忆或无界 Agent loop；只提供显式、有轮数上限的 portable continuation。
- 不让低级模型决定云端授权、fallback、公开发布、不可逆操作或验收结论。
- 不保存 API Key、原始媒体、OCR/ASR 私人结果或完整提示词日志。
- 不建立新的 GPU 锁、常驻端口或 Windows 计划任务。

## 测试

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v
```
