# LLM Backend Toolkit

一个供 Codex 等顶级模型调用的轻量工具：把长任务整理成紧凑上下文，显式调用云端或本地模型，并通过结果回执而不是长思考过程进行监督。

它不是 Agent，也不会自行决定模型降级。

## 核心能力

- 仅支持两个显式后端：`qwen3.7-plus` 与本地 `qwen-main-v1`。
- 默认确定性上下文压缩，返回压缩前后估算和是否有损。
- token 估算区分中日韩字符与 ASCII，压缩循环按 token 预算收敛。
- 可直接引用 UTF-8 文本 source；工具在内部检索相关片段，Codex 无需先读取整份材料。
- 长结果自动外置为本地 artifact，默认返回短预览和 hash。
- 默认关闭 thinking，不向顶级模型返回长推理过程。
- 支持本地模型原生图片、LocalOCR 和 ChineseASR 三种媒体路线。
- 异步 Smart Job：提交立即返回，顶级模型无需被长时间命令阻塞。
- 欠费、权限、GPU 占用等错误只返回裁决选项，不自动调用另一个模型。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

项目运行时无第三方 Python 依赖。

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

相同请求默认复用已完成结果；只有明确需要一次新尝试时才使用 `submit --force`。
回执同时给出 `monitor_until_utc`。任务超过硬期限会显示 `stale`、停止建议轮询，并把重试或接管交回顶级模型。

## 通过 source 引用减少 Codex 上下文

请求可以只传本地文本 artifact 的路径：

```json
{
  "provider": "qwen-main-v1",
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

## 云端千问

公开项目只读取：

```text
DASHSCOPE_API_KEY
LLM_TOOLKIT_QWEN_BASE_URL
```

默认 model 固定为 `qwen3.7-plus`。测试套件不会发起真实云端调用；云端请求格式和错误分类使用 mock 验证。

百炼官方资料：

- [OpenAI 兼容 Chat Completions](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)
- [错误码](https://help.aliyun.com/zh/model-studio/error-code)

## 有界能力探测

探测是顶级模型的候选工具，不是强制门禁：

```powershell
.\.venv\Scripts\llm-backend-toolkit.exe probe --provider qwen-main-v1 --case instruction
.\.venv\Scripts\llm-backend-toolkit.exe probe --provider qwen-main-v1 --case json
.\.venv\Scripts\llm-backend-toolkit.exe probe --provider qwen-main-v1 --case context
.\.venv\Scripts\llm-backend-toolkit.exe probe --provider qwen-main-v1 --case vision --attachment <image>
```

建议只在首次重要使用、模型别名/digest 变化或结果异常时做小样本测试。
探测命令同样只提交后台任务并立即返回 `job_id`，随后用 `job --id <job_id> --result` 读取结果。

## 不做什么

- 不自动 fallback。
- 不持续监控模型思考过程。
- 不提供长期记忆或 Agent loop。
- 不保存 API Key、原始媒体、OCR/ASR 私人结果或完整提示词日志。
- 不建立新的 GPU 锁、常驻端口或 Windows 计划任务。

## 测试

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v
```
