# 模型调用观察台设计

## 最终产品效果

模型调用观察台是 `llm-backend-toolkit` 的本机只读可观察面。用户从桌面或开始菜单打开白底纯绿 GUI 后，受管任务会自动出现并实时更新，不需要手动刷新。显式再次启动会尝试最大化并把已有窗口带到前台；Windows 可能按前台策略拒绝该请求。

界面采用封存 Local Remote Demo 的桌面三栏视觉，但移除本观察台不可提供的所有控制面：

1. 对话：受管 job 按 conversation root 聚合，多轮 continuation 共用一个入口，旧历史分页加载。
2. 连续对话：公开短标题只用于列表和页头，不冒充用户提示词；每轮优先显示实时草稿，并分别显示工作思路、聚合工作记录、压缩分隔与最终回答，同一个回答节点从流式草稿原位过渡到最终结果。重复的命令开始/结束事件和没有安全正文的成功活动只进入语义计数，进行中、失败或确有安全摘要的活动才展开成明细，避免把事件日志铺成页面。
3. 对话信息：只显示状态、轮次、计划/回执模型、执行方式、累计 Token、实测上下文、耗时与交付状态。缺失字段显示“不可用”，不补造额度、子智能体、项目或设置。

每轮回答后的“运行与验收回执”默认折叠：首层只显示安全执行字段与检查通过数，二次展开保留 API 已投影的 context、delegation、source、execution、delivery、cache identity、media route 和 checks。它不会输出原始命令、工具输入输出或未投影的 verification。普通 JSON 输出完整有界展示；只有后端明确投影为 `type=preview` 的 artifact 才按摘要显示。

本项目的正式验收范围仅为电脑端 Web 观察台；布局保持最小 1120px 的桌面三栏，不维护手机交互。

所有历史以耐久 job artifact 为准；读取 GUI 不增加轮询计数，也不等于 Codex 已取回结果。只有结果读取入口会记录 `handoff.collected`。

公开草稿只消费 AICLI 投影出的安全 `agent_message`：每个 `output.delta` 都作为单个增量进入 progress recorder，并借助 SSE refresh 在 DOM 中追加新后缀；增量本身不再逐条写入耐久时间线。活跃轮次始终优先显示最新 `public_preview`，不会被旧结果快照遮住；`output.completed` 仍作为最终输出和时间线事件保留，并用有界完整公开文本 replacement 对齐同一个回答节点，因此已有 delta 不会重复，没有 delta 时也能直接建立 preview。草稿默认上限为 20,000 字，超过时投影显式 `public_preview_truncated`，界面提示改看最终结果。这些公开消息保持模型原文，不自动翻译，也不是隐藏 chain-of-thought。

“工作思路”同时消费原生 CLI 明确公开并经安全投影的 `commentary` 与 `reasoning.summary.delta`，分别按安全 group 累积，不从普通 reasoning 活动、状态文案或隐藏正文推导内容。所有轮次始终默认展开；同一节点随 SSE 刷新只追加新增后缀。`progress.json` 仍是 12 段 / 20,000 字的实时 cache，但耐久公开事件与它解耦；主对话从完整事件流恢复全部安全语义节点。同一 group 若跨越命令、文件、搜索、压缩或结局，会按活动边界切成连续 part，避免把后半段思路错误放到工具之前。没有真实公开内容就不生成节点，工作思路与实时草稿、最终答复互不混用。

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

受管 Codex harness 的 `public_web_search` 是可发现、可多次调用的真实动态工具，不是 GUI 模拟能力。当前安全合同只接受 `tool_name=public_web_search`、`search_provider=bing-rss-v1` 的运行期 lifecycle；同一 turn 可产生多个独立 started/completed/failed 调用。观察台只显示调用次数、状态、provider 与 `runtime-lifecycle` 回执，不投影 query、结果正文、错误详情或调用 ID。搜索返回内容仍是不可信公共文本；缺少 exact lifecycle 或 receipt 时只按普通网页活动显示，绝不补造“已搜索成功”。AICLI 默认启用受管搜索，调用方可用 `--no-web-search` 显式关闭；Toolkit/GUI 只是该公开事件的消费者，不绕过 AICLI 权限、SecretRef 或网络边界。

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

## 2026-08-13 本地模型与完整主对话验收

当前实机 Demo 使用本地 `qwen-main-v1`、AICLI `0.3.5` 与 Codex CLI `0.147.0`，三轮均为真实模型运行而非 UI fixture。三轮共 1,962 条底层事件、19 个公开思路节点；首轮包含 11 个去重命令 lifecycle、3 个 `public_web_search` lifecycle 和 3 个工作区变化。首轮末端因未完成 item 失败关闭，GUI 将已公开正文标为“失败前草稿”；后续成功轮给出带标题、列表、GFM 表格、引用、代码块、粗体与安全 HTTPS 链接的 Markdown 最终答复。

主对话扫描完整耐久事件流并只生成语义节点，因此 160 条 raw event page 不会截断这些思路和活动；原始技术事件仍可通过 API 分页诊断。1440×1000 的隔离无窗口浏览器验收确认：12 个首轮思路全部默认展开，12 个相邻活动块按 sequence 穿插，三次网络搜索分别可见，命令合计 11、工作区变化合计 3，页面无横向溢出。该次搜索工具确实被调用三次，但上游端点返回 `PUBLIC_WEB_SEARCH_UNAVAILABLE`；验收只证明工具发现、调用和 lifecycle 投影，不声称取得有效搜索结果。

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

千问、DeepSeek、Spark 或本地模型经 `llm-backend-toolkit invoke` / `submit` / `probe` 都会先建立受管 job 并进入观察台；`invoke` 在同一进程完成该 job 后同步返回原结果，`submit` / `probe` 保持非阻塞 worker 语义。受管 direct Ollama 可显示流式草稿与最终原生 usage；受管 OpenAI-compatible API 目前只在完成后显示该次请求的真实 usage；已登记的 Codex/AICLI agent route 才会获得 app-server 的公开消息、工具活动和上下文信号。直接运行 `aicli start/run`、Codex Desktop、直接 `codex` 或任意第三方 API 客户端仍不会写入 Toolkit JobStore，观察台不会全局嗅探或伪称已经记录。

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
- 主对话默认展示本轮完整的公开语义投影：全部安全思路节点、按相邻思路分段的命令/文件/网络活动、压缩与结局。最近 160 条 / 前端 240 条只约束独立的 raw 技术事件分页与旧记录兼容，不约束主对话，也不在 canonical 主视图显示“加载更早”按钮。旧版连续 `agent.output.delta` 不作为工作行重复渲染。
- 服务缓存未变化的终态摘要；活跃任务继续计算新鲜耗时。
- 列表和详情分开请求，静态资源无外部依赖，长输出使用滚动容器和安全 `textContent`。

## Windows 桌面入口

正式入口是项目自带的无控制台 WinForms/WebView2 宿主。它使用独立 LocalAppData profile，只有导航成功才显示默认最大化窗口；新窗口与重复启动都会尝试前台激活，但 Windows 仍可能拒绝 foreground 切换。宿主继续设置 AUMID、`RelaunchIconResource` 与 `WM_SETICON`，但这些回读只证明 metadata/handle 层生效；Explorer 任务栏最终图标渲染已退出当前 acceptance，不能称为视觉 PASS。WebView 初始化或导航失败只写本机有界诊断并关闭失败，不弹控制台或错误框，也不把占位页冒充成功。

`Start-LlmBackendObserver.ps1` 先确保 loopback 服务存活，再启动原生宿主；它用同用户互斥锁、精确窗口标题和宿主路径去重，并尝试最大化和激活已有窗口。`Install-LlmBackendObserverShortcut.ps1` 使用 Windows Known Folder 同时创建当前用户桌面与开始菜单 Programs 快捷方式，不假设 OneDrive 或用户名路径，并让快捷方式直接指向 `LlmBackendObserverHost.exe`。正式运行链不以 `pwsh.exe` 为快捷方式目标；观察服务和 AICLI 子进程均用 `CREATE_NO_WINDOW` / Hidden 语义，失败不产生可见 PowerShell/控制台。安装、升级和 `-Remove` 都会验证宿主目标、完整参数、工作目录与描述；只有能证明属于本工具的链接才会修改或删除。两个独立 Known Folder 不能组成原子事务：若两处操作之间出现并发变化，安装器会停止且不覆盖或删除变化目标，可在处理冲突后幂等重跑恢复。

个人 skill wrapper 在模型 `submit` / `probe` 之前调用启动器，因此可见 job 会在 worker 启动前写入；GUI 失败时不得静默开始不可观察的模型调用。

正式 wrapper 把 `LLM_TOOLKIT_AICLI_ENTRY` 固定为受管入口；入口不存在时先失败，不搜索或回落到旧安装态。当前普通本地观察链已用 AICLI `0.3.5` / Codex CLI `0.147.0` 完成真实验收；benchmark-only 路由仍以自身 registry 中的精确 source/hash/version 证据为准，不能借用普通链的版本回执。未来版本默认尝试，但必要字段、通知、thread/turn、item 生命周期或清理协议漂移必须明确失败。官方与本地 Ollama Codex machine run 使用 Codex 原生 workspace sandbox；第三方 Codex 及其他适用引擎才使用 Windows 外层沙箱。
