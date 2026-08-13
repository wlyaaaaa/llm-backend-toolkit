# 模型调用观察台设计

## 最终产品效果

模型调用观察台是 `llm-backend-toolkit` 的本机只读可观察面。用户只需从桌面或开始菜单打开一次白底纯绿 GUI；此后 AI 通过受管 skill 发起的任务会自动出现并实时更新，不需要手动刷新，也不会由每次调用反复抢焦点。

界面采用封存 Local Remote Demo 的桌面三栏视觉，但移除本观察台不可提供的所有控制面：

1. 对话：受管 job 按 conversation root 聚合，多轮 continuation 共用一个入口，旧历史分页加载。
2. 连续对话：按轮次显示已公开的用户标签、折叠工作记录、压缩分隔与回答；同一个回答节点从流式草稿原位过渡到最终结果。
3. 对话信息：只显示状态、轮次、计划/回执模型、执行方式、累计 Token、实测上下文、耗时与交付状态。缺失字段显示“不可用”，不补造额度、子智能体、项目或设置。

本项目的正式验收范围仅为电脑端 Web 观察台；布局保持最小 1120px 的桌面三栏，不维护手机交互。

所有历史以耐久 job artifact 为准；读取 GUI 不增加轮询计数，也不等于 Codex 已取回结果。只有结果读取入口会记录 `handoff.collected`。

公开草稿只消费 AICLI 投影出的安全 `agent_message`：每个 `output.delta` 都作为单个增量进入 progress recorder，并借助 SSE refresh 在 DOM 中追加新后缀；增量本身不再逐条写入耐久时间线。`output.completed` 仍作为最终输出和时间线事件保留，并用有界完整公开文本 replacement 对齐草稿，因此已有 delta 不会重复，没有 delta 时也能直接建立 preview。草稿默认上限为 20,000 字，超过时投影显式 `public_preview_truncated`，界面提示改看最终结果。这些公开消息保持模型原文，不自动翻译，也不是隐藏 chain-of-thought。

Codex `0.145.x` 存在一个已实测的窄生命周期例外：一条或多条较早公开 `agentMessage` 可能已经发送 delta，却不再发送自己的 completed，随后由一条更晚的 completed final 收口。AICLI 只在版本确为 `0.145.x`、所有未完成项都是较早的公开 `agentMessage`、且之后存在非空 completed final 时兼容；推理、工具、文件项未完成，final 缺失，final 之后才出现未完成消息，或未来 Codex 版本都继续明确失败。这样既保留真实实时消息，也不会把任意协议漂移伪装成成功。

## 真实性边界

观察台不声称展示原始 chain-of-thought。它只显示：

- 中文工作摘要；
- reasoning 的模式、effort 和活动状态；
- 安全工具类型与开始/完成状态；
- `workspace-write` 智能体运行前后的有界文件元数据差异；
- 模型主动形成的公开消息；
- 结果侧 usage 与确定性校验。

调用方可以通过 `observability.public_label` 提供最多 80 字的非敏感公开标题，例如“修复缓存身份回执”。观察台绝不会从 `task.goal`、instructions 或输入正文自动推导标题；未提供时只显示任务类型级通用名称。

工作区复核默认只在 worker 内存中比较文件元数据，递归发现阶段永久不读取文件正文。worker 会先对调用方原始 workspace 的末端目录执行 `lstat`/reparse 检查，再解析 canonical 路径并绑定目录身份；根本身是 symlink、junction、其他 reparse point，或身份无法稳定确认时，会在 runner 启动前失败关闭。runner 与 observer 始终使用这一份已验证 canonical 路径。observer 不会直接信任 `scandir`/`lstat` 返回的路径：每个准备写入 snapshot 的文件都必须从同一打开 handle 取得元数据，并证明最终路径精确等于 `canonical_root/relative_path`。Windows 在检查期间持有根的父目录、根和内部祖先的 no-reparse handle，再以 `GetFinalPathNameByHandleW` 校验同一个文件 fd；POSIX 逐组件使用 `dir_fd + O_DIRECTORY + O_NOFOLLOW` 打开。显式 diff 的正文另走 exact secure reader，只有上述同一 fd 通过 containment、身份和稳定性检查后才读取。任何文件无法证明都不进入 snapshot，并使扫描状态降为 partial。扫描仍跳过依赖/版本控制缓存目录。安全绑定不代表运行时窗由智能体独占，因此默认耐久事件只包含观察到的变更文件数、扫描状态、`provenance=workspace_before_after` 和 `attribution=unverified_concurrent_window`，不会把并发变化归因给智能体或某个工具。`scoped_complete` 表示限定扫描范围完整；扫描未完整覆盖时数量只是已观察下限，没有变化事件也不等于已经证明整个工作区没有变化。

只有调用方明确声明以下 opt-in，观察台才会在本机持久历史中保存所列普通小文本文件的相对路径、`+/-` 行数和有界 unified diff：

```json
{
  "observability": {
    "file_changes": {
      "mode": "diff",
      "include": ["acceptance.md", "summary.json"]
    }
  }
}
```

`include` 是调用方对这些精确相对路径“可公开展示”的声明，最多 12 项；不支持绝对路径、`..` 或隐式宽域扫描。实现仍会检查相对路径的每个组件并拒绝 secret-like 名称，同时拒绝二进制、非 UTF-8、超限、含凭据形态，或含任意 POSIX、Windows drive、UNC/双斜线及 Windows NT 绝对路径的正文，并对单文件、总读取量、文件数和 diff 长度设硬上限。任何安全条件无法证明时只保留 count，不宽松公开详情。

为了让本机用户能直接定位文件，agent job 提交时会把已验证 canonical workspace 写入独立的 `.observer-local.json`。该文件只有 schema、job ID 和 canonical workspace，request spool 清理后仍保留；loopback job detail 会重新验证 workspace 身份和相对路径 containment，再动态添加可显示、复制的完整路径。元数据损坏、超限、身份不匹配、reparse 或越界时失败关闭。完整路径不会进入事件、状态、进度、结果、`job --result` 或 diff header。

严禁进入事件投影的内容包括 prompt、隐藏 reasoning/thinking 正文、原始命令/argv、工具输入输出、环境变量、stdout/stderr、OCR/ASR 识别正文、绝对私密路径、PID 和 Broker lease token。

Token/s 指标必须说明依据：

- `eval_duration`：Ollama 最终 `completion_tokens / eval_duration_ns`，精确表示模型评估时段的输出 token/秒，不把提示处理、排队或工具时间计入分母；
- `wall_clock_estimate`：AICLI agent 返回的安全真实输出 token 数除以从 runner 启动到完成的整段执行墙钟，近似，不冒充模型 eval TPS；
- `public_content_estimate`：运行中公开输出的估算 token 数除以该公开输出观察窗的墙钟时间，近似；
- `unavailable` / `not_applicable`：无法可靠计算，OCR/ASR 不冒充 token TPS。

Token 卡片的主值是上游本次运行累计总计，可展开直接查看输入、输出、推理输出和缓存；输入兼容 `prompt_tokens` / `input_tokens`，输出兼容 `completion_tokens` / `output_tokens`，缓存兼容 `cached_tokens` / `cached_input_tokens`。Codex/AICLI 的累计字段来自 `tokenUsage.total`，绝不把 `TokenUsage.last.outputTokens` 冒充整场输出；当前上下文仍只取 `last.totalTokens`。缓存属于输入子集，不再次加到总数；缓存为零或缺失时不显示，不据此宣称本地模型支持缓存统计。Token/s 一律带“输出 token/秒”单位，并明确标出“模型评估时段精确值”“整段执行墙钟估算”或“公开输出观察窗估算”。

“当前上下文”与累计 Token 严格分离：

- 当前已用量只接受 Codex app-server 同一条 `thread/tokenUsage/updated` 运行时通知中的 `tokenUsage.last.totalTokens`；
- 总上限只接受同一条通知同时提供的 `tokenUsage.modelContextWindow`；两者必须是完整非负/正整数配对，不能跨事件拼接；
- GUI 主值采用“已用 202k / 共 258k”式紧凑显示，展开信息保留精确整数和占用比例；
- 首个完整运行时配对尚未到达时固定显示“等待 Codex 运行时实测”；如果运行结束仍缺少配对，或字段、通知、生命周期不兼容，AICLI 必须返回明确协议错误，绝不使用 prompt token、累计 input token、`context_receipt.estimated_tokens_after`、backend registry 配置上限或最终结果回执伪装；
- 高频 usage 通知只更新卡片，不灌满工作时间线；Codex app-server 实际完成 `contextCompaction` 时才写入“Codex 已自动压缩上下文”节点。

Toolkit 自己的 `context.compaction.completed` 是模型调用前的确定性输入压缩，界面标为“已压缩调用输入”；它与 Codex agent 会话的原生自动压缩是两条独立事实链。

## 2026-07-25 实机页面验收

最终真实 job `d50a2cbd068b2fa0c4537e40` 在已打开的同一 Edge app 页面完成，全程没有手动刷新：

- 历史数量从 21 自动增至 22，任务从“执行中”切换为“已完成”；时间线依次从 8、18、37 增至完成时的 63 个节点，Codex 取回结果后再增加 1 个 `handoff.collected` 节点。
- 13:11:21 起出现中文公开进度；随后工具开始、失败、恢复成功、文件编辑、只读复核和最终公开消息均按发生顺序进入时间线。失败事件没有被吞掉，后续成功也没有覆盖历史。
- 最终显示总计 9,399 token，其中输入 9,212、输出 187；当前上下文展开值为 9,399 / 258,400，二者都明确标记“Codex 运行时实测”；TPS 为约 4.1 输出 token/秒，口径为 45.884 秒整段执行墙钟估算。
- 回执为 `codex-app-server`、`distinct-non-output-thread-item-v2`、11 个 action step、4 次工具调用、50 个 app-server 事件和 46 个安全 machine event。
- `live-final-acceptance.md` 的 `+9/-1` diff 与经验证的本机完整路径均在页面可见、可复制；绝对值不写入公开文档或事件。扫描另观察到 1 个不在 opt-in 公开名单内的元数据变化，只保留计数；界面明确区分“未在本次公开名单”与安全/大小限制，不虚构其路径。
- 最终中文结果完整列出编辑文件、状态变化、新增内容与复核结论；隐藏 reasoning 正文始终没有进入公开事件。

## 五条运行链

| Owner | 职责 |
|---|---|
| `llm-backend-toolkit` | job、cache、结果、统一安全事件、历史、SSE/API 和 GUI |
| AI CLI Profile Manager | Profile、原生 CLI、沙箱、硬预算和实时净化 machine event |
| LocalOCR | OCR 业务状态与只读安全 observer 投影 |
| ChineseASR | ASR 业务状态、chunk/RTF 与只读安全 observer 投影 |
| LocalGpuBroker/Ollama | GPU 仲裁、模型入口和原始精确 usage；不拥有业务 job |

Toolkit 调用 OCR/ASR 时会把开始/完成阶段写入同一个模型 job。各专项服务的 `/observer/jobs` 和 `/observer/jobs/{id}` 只用于安全聚合，不取代原有诊断接口，也不授权启动、取消或重试。

千问、DeepSeek、Spark 或本地模型只有经 `llm-backend-toolkit submit` / `probe` 建立受管 job 时才会出现在观察台。受管 direct Ollama 可显示流式草稿与最终原生 usage；受管 OpenAI-compatible API 目前只在完成后显示该次请求的真实 usage；已登记的 Codex/AICLI agent route 才会获得 app-server 的公开消息、工具活动和上下文信号。直接运行 `aicli start/run`、Codex Desktop、同步 `llm-backend-toolkit invoke` 或任意第三方 API 客户端都不会写入 Toolkit JobStore，观察台不会全局嗅探或伪称已经记录。

当前默认注册表中，本地 Ollama、云 Qwen/DeepSeek direct job 都能进入观察台；local Qwen Codex routes 与 Spark agent route 已接入 AICLI/Codex 事件链。`cloud-qwen-flash` 和 `cloud-deepseek-v4-flash` 当前是 direct-only，只显示受管 job 生命周期、公开输出和实际存在的最终 usage，不能显示 Codex 工具/上下文事件。旧 `cloud-qwen3-8-max-agent` 已因当前 AICLI catalog 缺少精确 Profile 而撤出可选注册表，仅保留 `unverified/selectable=false` 的 reserved 记录；不会借用 Qwen 3.7 Profile 或历史回执。AICLI Profile Manager 自身存在某个 Profile 也不等于 Toolkit 已登记相应 agent route。

## 四基座边界

- `.agents`：AI 能力路由、skill 和自动打开观察台的个人 wrapper。
- GitHub 索引：仓库身份、PUBLIC/PRIVATE、remote、worktree 和发布事实。
- PCConfig：安装路径、端口、服务、计划任务、桌面入口和 LocalGpuBroker 机器事实。
- PersonalOS：个人连续性与授权语义；不拥有模型事件、GPU 或 GUI 历史。

## 实时与性能

- `/api/stream` 只发送 refresh 信号，详情仍由同源 loopback JSON API 读取；连接会持续发送 heartbeat，直到客户端主动断开，不会在任务仍运行时静默到期。
- 浏览器断线时降级为有界轮询；SSE 恢复后停止通用轮询。选中的活跃任务仍用本地 1 秒 ticker 更新耗时，并每 5 秒低频复核详情，使静默工具阶段和 `monitor_until_utc` 过期仍能及时反映；终态耗时冻结。
- GUI 首屏只加载最近 100 条，旧历史按页加载。
- 单轮详情只加载最近 160 个事件，`/api/runs/{job_id}/events` 以 `before_sequence` 向前分页；前端浏览窗口最多保留 240 个事件，并可明确返回本轮最新记录。旧版连续 `agent.output.delta` 不作为工作行重复渲染。
- 服务缓存未变化的终态摘要；活跃任务继续计算新鲜耗时。
- 列表和详情分开请求，静态资源无外部依赖，长输出使用滚动容器和安全 `textContent`。

## Windows 桌面入口

正式启动器会在精确观察台窗口上写入独立 `AppUserModelID=Wly.LlmBackendToolkit.Observer` 与项目 ICO 的窗口级任务栏身份；属性写入或回读失败时返回失败，新启动且未完成身份设置的观察台窗口会被关闭，不把 Edge 身份冒充为已修复。

`Start-LlmBackendObserver.ps1` 先确保 loopback 服务存活，再以 Edge `--app=<url>` 打开正式窗口。它用同用户互斥锁和精确窗口标题去重，不激活已有窗口。`Install-LlmBackendObserverShortcut.ps1` 使用 Windows Known Folder 同时创建当前用户桌面与开始菜单 Programs 快捷方式，不假设 OneDrive 或用户名路径，并绑定项目自带的白绿 ICO。安装、升级和 `-Remove` 都会验证 PowerShell 目标、完整 launcher/toolkit 参数、工作目录与描述；只有能证明属于本工具的链接才会修改或删除，旧 Edge 图标可安全迁移。首次全体预检会避免开始时已经存在的同名冲突导致半更新；每个目标在变更或确认 `unchanged` / `absent` 前还会按文件身份和链接契约最终复验。两个独立 Known Folder 不能组成原子事务：若两处操作之间出现并发变化，安装器会停止且不覆盖或删除变化目标，已经完成的本工具链接更新或删除可能保留，可在处理冲突后幂等重跑恢复。

个人 skill wrapper 在模型 `submit` / `probe` 之前调用启动器，因此可见 job 会在 worker 启动前写入；GUI 失败时不得静默开始不可观察的模型调用。

正式 wrapper 同时把 `LLM_TOOLKIT_AICLI_ENTRY` 固定为受管的当前源码入口；入口不存在时先失败，不搜索或回落到旧安装态。当前 Codex app-server 最低已验证基线是 `codex-cli 0.145.0`，`0.146.0-alpha.3.1` 曾通过兼容验收；未来版本默认尝试，但必要字段、通知、thread/turn、item 生命周期或清理协议漂移必须明确失败。官方与本地 Ollama Codex machine run 使用 Codex 原生 workspace sandbox；第三方 Codex 及其他适用引擎才使用 Windows 外层沙箱。
