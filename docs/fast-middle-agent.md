# Spark 高速中档智能体验收报告

状态：**ROUTE_VERIFIED / CURRENT_CAPABILITY_NOT_QUALIFIED**

验收日期：2026-07-24（UTC）

候选链路：`fast-middle-agent → data_factory → codex-cli → aicli codex-spark-xhigh → gpt-5.3-codex-spark / xhigh`

对照链路（历史基线）：`local-default → data_factory → codex-cli → aicli codex-ollama-main → LocalGpuBroker:32100 → qwen-main-v1 → Qwen3.6 35B Q4_K_M`

协议失效安全默认保持：省略 backend 时解析到质量优先的免费 `local-default`。Spark 只允许显式选择，不自动回退或自动推荐。

当前 Toolkit 的 `local-default` 已切换为 Qwen3.8 27B。本文的 Qwen3.6 35B 只保留为历史对照证据；三条非 Codex legacy route（`claude-code`、`qwen-code`、`opencode`）当前为 `unverified` / `pending_reacceptance`，不会继承这份旧验收或冒充当前能力。

## 2026-07-29 当前裁决

Codex 0.145 原生 app-server 的 `workspace-write` 根目录合同已经修正并由真实源码入口验证：本轮回执确认 `policy=workspace-write`、模型为 `gpt-5.3-codex-spark`、强度为 `xhigh`，没有退回只读或其他模型。

但同一真实运行在冻结 `code_repair` 题上消耗 81 个 step，触发 80 步硬上限，35 次工具调用，最终 2/9；进程树清理已确认。依照“步数失败不增加预算重测”的门禁，本轮不再运行其余题，也不以历史成功覆盖当前失败。因此：

- 路由/写权限修复：通过；
- 当前能力验收：未通过；
- 2026-07-24 的以下结果保留为历史证据，不再支撑自动优先 Spark；
- Spark 仍可由顶级模型显式选择，但默认使用本地 35B 或更高等级 Codex。

当前正式 skill 把 `LLM_TOOLKIT_AICLI_ENTRY` 固定为受管的当前源码入口，入口缺失时明确失败，不会静默调用旧安装态。Codex app-server 以 `codex-cli 0.145.0` 为当前最低已验证基线，`0.146.0-alpha.3.1` 曾通过兼容验收；后续版本默认尝试，但字段、通知、生命周期或清理协议漂移必须明确失败。

## 裁决

Spark 值得作为 PersonalOS 与 `.agents` 的高速中档候选，但不是 Qwen 的全面替代：

- Spark 适合文本型、边界清晰、低延迟、有独立 verifier 的跨文件读取、工具任务、长对话降噪和严格结构输出。
- 本地 Qwen 保留 Spark 额度回退，以及隐含现实目标/常识因果任务。
- 顶级模型继续负责歧义、真实意图、授权、公共发布、不可逆操作、高风险判断和无 verifier 的最终裁决。

当前设备与云链视为可信，隐私不参与 Spark/Qwen 能力排序；`privacy.cloud_allowed=true` 继续作为云路由的显式协议回执。注册表默认仍不改变，不自动 fallback，不把本地回退结果记作 Spark。

## “桌面能选”与“工具包能调用”是两条证据

1. Codex 桌面独立任务可以选择 `gpt-5.3-codex-spark / xhigh`，合成零文件任务 22.389 秒返回精确 JSON。
2. Codex CLI 直接指定同一模型/强度，13.812 秒、exit 0。
3. 修复 AICLI 后，已安装 `aicli 0.3.1` 的只读 machine run 15.603 秒、exit 0，正文严格 `{"status":"SPARK_AICLI_OK"}`。
4. 最终从 Toolkit 发起真实 `fast-middle-agent` 合成路由任务：子进程 43.186 秒、exit 0、13 steps、9 tool calls，严格 JSON；timeout、maxSteps、maxToolCalls 均为 `hard`，`profile=codex-spark-xhigh`、`model=gpt-5.3-codex-spark`、`reasoning_effort=xhigh`、`fallback_used=false`。

第 4 条才证明 llm-backend-toolkit 已能程序化调用 Spark。这个 backend 的 adapter 是 `agent-only`；它不是 OpenAI Chat/Responses API backend。

## 合成数据清洗实测

所有输入均为合成材料，没有读取真人对话。专项覆盖用户后续纠正优先、AI 长回答降噪、时间未知保留、外部来源与未核验状态、模型风格分离和跨样例隔离。

| 任务 | Spark/xhigh | 本地 Qwen | 结论 |
| --- | --- | --- | --- |
| 4 样例小清洗 | 平均 20.719 秒，72/78，原生 JSON 3/3 | 平均 54.019 秒，51/52，原生 JSON 0/2 | Spark 约快 2.61 倍且协议稳；Qwen 小样本语义略稳 |
| 95,550 字节长清洗 | 71.430 秒，26/26，原生 JSON | 109.066 秒，21/26，需 fenced JSON 救援 | Spark 长 AI 噪声过滤和结构服从明显胜出 |
| 跨文件工程诊断 | 46.306 秒，正确诊断，原生 JSON | 20 步先硬失败；30 步重跑 67.458 秒后正确，fenced JSON | Spark 步骤更少、恢复报告和结构更稳 |
| 非代码证据/角色路由 | 31.785 秒，12/12 | 38.208 秒，12/12 | 两者都可靠，Spark 只快约 20% |
| “50 米去洗车” | 连续 2 次误答步行 | 正确选择开车；早期一次误写距离，最终已安装链路保持 50 米语义并答对 | Qwen 在隐含目标/现实因果上形成互补 |

模型调用墙钟合计（原始对照批次）：648.43 秒，其中 Spark 256.30 秒、Qwen 392.13 秒。集成修复后另做的真实子进程 smoke 为 15.603 秒（AICLI Spark）、43.186 秒（Toolkit Spark）和 49.766 秒（Toolkit Qwen）。这些是各调用墙钟，不包含人工准备和本地测试时间。

本报告不使用代码榜单代替清洗实测，也不把当前顶级桌面任务表现计入 Spark。

## PersonalOS 与本地调优基线

PersonalOS 已有冻结的本地模型能力回执 `receipt:model-calibration-20260724-v1`，本地角色为 `local-default/qwen-main-v1`，状态 `pass_with_restricted_scope`。本次不覆盖该基线，只增加互补的 Spark 证据：

- PersonalOS 的业务门禁仍要求 unknown 存活、来源闭包、角色/语义约束和高影响升级。
- canonical raw 不交给中档或本地模型无验证改写；两者只在 staged/derived 工作区产出。
- Qwen 的 Ollama/GPU/alias/digest/stability 事实继续由本地模型调优项目维护；Toolkit 只拥有“选哪个角色、如何调用、回执如何表达”的事实。
- `qwen3.7-plus` 后续直连复核未显示能力优势，并于 2026-07-29 从内置 registry 退役；本报告中的静态候选判断仅是历史背景。

## 额度回退

Spark 额度剩余是外部动态状态，不能推断。quota、rate-limit 或 entitlement 错误统一返回规范类别和以下裁决候选：

- 稍后重试原 Spark；
- 由顶级模型显式改投 `local-default`；
- 顶级模型接管。

若选择本地回退，必须产生第二个请求和第二份回执。原错误、实际执行模型、`fallback_used` 与回退原因都保持可见；AICLI 和 Toolkit 均不静默切换。

## 已发现并修复的硬错误

- 原 AICLI `codex-official` machine run 把桌面 `codex.exe` 当成机器入口，随后报 `Codex machine runtime could not locate codex.js`。
- 修复为交互入口继续使用桌面 Codex，machine run 使用 npm Codex；一次性 `CODEX_HOME` 只复制 `auth.json`。
- 第一版云端 machine run 仍套用断网外层沙箱，55.183 秒后出现 `stream disconnected ... /codex/responses`；当时先修复为官方云端使用 Codex 原生 read-only/workspace-write 沙箱，本地/第三方仍用断网外层沙箱。现行合同又将本地 Ollama Codex 迁到同一原生 workspace sandbox，使 app-server 可访问 LocalGpuBroker；第三方 Codex 及其他适用引擎才继续使用断网外层沙箱。
- Qwen 工程题 20 步预算 exit 75；增加到 30 步后完成，说明本地模型工具任务应给更宽步骤预算。
- 一次 Spark 结构化测试因夹具 JSON Schema 的 `const` 缺少显式 `type` 返回 HTTP 400；这是夹具错误，不计入模型能力。

早期只读评测曾因宽域搜索排除规则遗漏而触及 `.codex/archived_sessions` 的元数据文件。未将其中真人内容用作输入或结论，但该流程门禁判失败。后续与本次接入验证均改为合成工作区精确 allowlist；自动化验收不得递归搜索会话、history 或 archive。

## 适用边界

### 顶级模型

- 真实目标不清、权威事实冲突、无可靠 verifier；
- 授权、公共发布、法律/医疗/财务、安全、不可逆变更；
- 中档/本地结果的最终验收和跨 owner 裁决。

### Spark 高速中档

- 文本型、`privacy.cloud_allowed=true` 协议回执和文件 allowlist 明确；
- 有界跨文件读取、证据归并、定位、草拟、严格 JSON；
- 低延迟有价值，且产物有 schema、脚本或语义 verifier；
- 不承担图像输入、隐含现实目标的最终判断、高风险或 canonical raw 无验证写入。

### 本地 Qwen

- Spark 额度/资格/限流后的显式回退；
- 某些隐含目标和现实因果的独立复核；
- 严格 JSON 需外部校验/救援解析，工具任务应给更宽步骤预算。

## 复验门禁

只有 Spark 模型 ID、AICLI/Codex CLI 主协议、沙箱合同、Toolkit route、Qwen digest、PersonalOS 语义合同或异常行为发生实质变化时才重跑完整对照。日常使用只读取 registry 和回执；不为证明可用反复消耗 Spark 额度。Codex CLI 更新本身先走运行时兼容门，兼容则继续，不兼容则明确失败；不得以旧安装态或静默降级掩盖协议漂移。
