# LLM Backend Toolkit

一个供 Codex 等顶级模型调用的轻量工具：把长任务整理成紧凑上下文，显式调用云端或本地模型，并通过结果回执而不是长思考过程进行监督。

它不是 Agent，也不会自行决定模型降级。

## 执行前自检

`llm-backend-toolkit version` 返回实际解释器、已安装版本和源码位置；`backends` 返回路由详细信息与运行器能力。路由或安装变化后，使用 `llm-backend-toolkit preflight --request request.json` 检查同一请求：不启动模型、不创建 job、不读取任务文件。Agent 预检使用当前 AICLI 的真实参数解析器，并核对实际模型/Profile。配置可用、接口兼容和真实能力验收是三个不同状态，预检不冒充实测通过。

未指定上下文预算时，采用所选后端的已登记窗口：窗口至少 262144 时预算 262144，否则取窗口的 90%；未知窗口沿用保守 16384。中文裁剪会保留可用原文，并报告有损状态，不能将整份输入裁成空标记。非正常终止、缺失流终止标记或仍需处理工具调用的答案以 `partial` 交付并保留原因，不当作成功缓存。


输出检查写在 `task.expected_output`；顶层同名字段、未知输出格式或非字符串数组的 `required_keys` 会在读取材料与调用模型前明确报错，不会悄悄忽略约束。公开 Agent 示例默认不启用可变工作区缓存，只有调用者提供真实内容身份时才显式设置 `execution.cache_key`。

## 核心能力

- 通过版本化 backend registry 接入可替换的本地模型和 API 平台；稳定角色 `local-default` 默认只解析到本地后端。
- 默认确定性上下文压缩，返回压缩前后估算和是否有损。
- token 估算区分中日韩字符与 ASCII，压缩循环按 token 预算收敛。
- 可直接引用 UTF-8 文本 source；工具在内部检索相关片段，Codex 无需先读取整份材料。
- 长结果自动外置为本地 artifact，默认返回短预览和 hash。
- thinking 默认值由 backend registry 显式声明；内置本地质量角色默认开启，但不会向顶级模型返回隐藏推理正文。
- 支持本地模型原生图片、LocalOCR 和 ChineseASR 三种媒体路线。
- 异步 Smart Job：提交立即返回，顶级模型无需被长时间命令阻塞。
- 欠费、额度、限流、权限、GPU 占用等错误只返回裁决选项，不自动调用另一个模型。
- 任何云端调用都要求显式 `privacy.cloud_allowed=true`，包括 task 文本、source 片段与媒体。
- agent mode 通过 aicli 调用原生 CLI：Codex 任务省略 `execution.policy` 时默认 `danger-full-access` 且不请求交互审批；当前 Codex machine 接口不支持收窄为 `workspace-write` 或 `read-only`；其他 runner 按 `backends.runner_capabilities` 选择已支持策略。完全访问允许模型在当前用户权限内读写工作区外文件并执行命令，只应用于可信任务；API Key 仍只注入目标子进程。正式 personal skill 会把 `LLM_TOOLKIT_AICLI_ENTRY` 钉到其受管的当前源码入口；该入口缺失时明确失败，不会静默改用可能过期的安装态。`data_factory` 从 registry 解析精确 Profile 与模型，不做运行时猜测或 fallback。

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

注册表把 backend ID、adapter、模型、端点环境变量、数据去向、`routing_role`、AICLI Profile、route、runner 与版本绑定证据分离。替换 Ollama 模型、OpenAI Chat 兼容 API 或已有 AICLI Profile 只需改注册表；route 名称可以自定义并映射到已实现的 runner adapter，全新 wire protocol 或全新智能体 CLI 才需要增加代码 adapter。`reasoning_request` 用安全 JSON 字段路径与 `on`/`off` 标量声明顶层或嵌套的 thinking 参数，Qwen 与 DeepSeek 共用此机制，不含厂商分支。已验收模型的 digest/父模型一旦不匹配，`live_verified` 自动失效并阻止沿用旧验收。注册表禁止内嵌凭据；云端 `openai-chat` 地址必须使用 HTTPS。

`local-default` 是质量优先的本地 direct 默认，具体模型 artifact、上下文与生成参数仅由当前 registry 给出。省略 `reasoning.mode` 时采用注册表质量设定；`qwen-main-v1` 是请求侧兼容 alias，不代表当前 artifact。`data_factory` 和 `codex-cli` 使用注册表中同一精确 Profile/模型。历史任务通过不能证明替换后模型或新接口通过，`configured` 与真实 live 验收必须分开。低价值低延迟任务可显式选择 `reasoning.mode=off`；`local-hard-reasoning` 则要求 `on`，错误请求在读取材料或调用模型前拒绝。隐藏 thinking 在 provider 边界丢弃，只保留公开回答和计数。

`local-crosscheck-35b` 是显式、非默认的交叉验证角色：它的 Ollama/AICLI 模型名是 `qwen-main-v1`（Qwen3.6 35B），请求侧 selector alias 是 `qwen-crosscheck-35b`。不要混淆两层命名：请求 `backend=qwen-main-v1` 仍兼容解析到 Qwen3.8 27B 的 `local-default`，只有显式 `local-crosscheck-35b` 或 `qwen-crosscheck-35b` 才选择 35B。该 backend 在 catalog 中公开 `routing_role=crosscheck_only`，默认开启 thinking，固定 `temperature=0.6`、`top_p=0.95`、`top_k=20`、`min_p=0`、`presence_penalty=0`、`repeat_penalty=1`、`num_ctx=262144`、`num_predict=32768`，且只访问 LocalGpuBroker `127.0.0.1:32100`。它不能成为 `default_backend`、不参与 fallback；direct 可显式使用。唯一一次获授权的新模型 Agent Live 重新验收在 `2026-08-21T19:28:51.9871927Z` 以 `aicli.recovery.capture_exception` 失败关闭，没有 verified runtime identity，故精确 `codex-cli` route 仍保持 `unverified/pending_reacceptance` 并在调用 provider/runner 前拒绝执行。详细边界和非敏感 receipt 证据见 [35B 本地交叉验证角色](docs/local-crosscheck-35b.md)。

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

`invoke` 保留同步返回结果的接口语义，但执行前同样创建一个受管 JobStore job，
因此实时进度与最终结果也会自动进入观察台；它每次都建立新尝试，不复用结果
cache。Codex 的默认入口仍建议使用非阻塞的 `submit`。

外部 source/media 可在引用上成对声明 `expected_sha256` 与 `expected_bytes`：

```json
{
  "id": "source-1",
  "path": "X:/staging/source.jsonl",
  "expected_sha256": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "expected_bytes": 1234
}
```

异步 worker 在 provider 实际消费前把字节流式复制到 job 私有目录，同时校验源文件在读取期间未变化、声明的 SHA-256/字节数以及私有副本回读结果。Windows 上从创建副本开始持有拒绝写入/删除的受保护句柄，贯穿 SourceLoader、MediaProcessor 和 provider 消费；job 目录、input-spool 及其内部祖先发现 symlink/junction/reparse 或 canonical containment 失败时一律关闭失败。任一步不一致都会在调用 provider 和发布 result/cache 前失败关闭。

未提供声明的旧请求仍会进入私有 spool 并兼容执行，但引用只标为 `captured_unverified`、整体只标为 `spooled_unverified`，不能成为 cache hit；在非 Windows 平台，缺少等价不可变路径绑定时，带声明请求也会关闭失败而不是冒充已验证。安全回执只记录引用 ID、声明值、实测摘要和状态，不回显正文、原始路径或原始 cache key。worker 的 PID、进程创建身份和阶段写入耐久 lease；只有确认该进程已经死亡，`get`/`cleanup_inputs` 才会把运行中 job 原子转为 failed/cancelled 并清理 spool，活 worker 不会被误清理。进入终态后私有副本与 prepared request 会清理，并留下可重复验证的清理回执；撤回与接管可使用 Python `JobStore.cancel(job_id)` 和 `JobStore.cleanup_inputs(job_id)`，不提供绕过 owner 判断的公共批量 purge CLI。同步 `invoke` 与异步 `submit` 现在都经过同一套 job claim、输入消费边界、公开进度和清理回执；差别只在调用方是否等待结果。

不含外部文件引用的相同请求默认复用已完成结果；agent workspace、source 或 media 等可变引用默认不缓存。只有调用者在 `execution.cache_key` 提供通过验证、绑定真实内容与派生版本的语义身份，才允许这类请求命中缓存。显式 key 会忽略 `target_tokens`、预算和 workspace 等调度/工作元数据，但仍强制绑定已解析 backend、model、agent route/profile、隐私、reasoning、媒体与输出协议，不能跨 provider/model 或隐私边界误命中。v2 回执的 `cache_identity.mode=explicit` 只公开原始 key 的 SHA-256 `caller_cache_key_hash`，从不回显原始 key；两种模式都声明 `canonicalization=stdlib-json-sort-compact-utf8-v1`，对应 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 的 UTF-8 SHA-256。显式 `digest` 还绑定解析后的完整安全 scope，供调用方作为不透明身份比较，不要求仅凭精简回执字段复算；无显式 key 时，`mode=request_digest` 的 `request_sha256` 等于当前请求正文摘要，`digest` 还绑定已解析模型、后端配置与实际端点，因此换模型或端点不会误用旧结果。v1 或缺失身份的旧 job 仍可按 ID 查询，但不会被新 v2 提交当作缓存证明。只有完整且检查通过的 `ok` 结果可复用；`partial`、失败、取消和非 cacheable 结果不会成为命中目标。明确需要一次新尝试时使用 `submit --force`。
回执同时给出 `recommended_check_utc` 与 `monitor_until_utc`。初次建议等待 30-60 秒，过早读取后指数退避；任务超过硬期限会显示 `stale`、停止建议轮询，并把重试或接管交回顶级模型。

## 模型调用观察台

`0.9.0` 提供正式的本机只读 GUI。它沿用封存 Local Remote 的白底纯绿桌面视觉，在一个常驻窗口中自动展示：

- 多个模型调用和多轮 continuation，以“对话列表 / 连续对话 / 对话信息”三栏呈现；不包含项目、文件、设置、输入、停止或审批控制；
- 中文聚合工作记录、AICLI 命令/文件/网络活动、OCR/ASR 阶段与公开输出；没有真实公开 prompt 时不渲染用户气泡。主对话扫描本轮全部耐久事件，把完整公开思路、聚合活动、压缩和结局按真实 sequence 交错呈现，不受最近 160 条原始事件尾窗限制；原始技术事件仍保留独立分页 API，但不再用分页按钮暗示主对话缺失；
- AICLI 安全公开消息按增量通过 SSE 无刷新追加到草稿；`output.completed` 用有界完整公开文本对齐草稿且不会重复追加。草稿最多保留 20,000 字，达到上限会明确提示；原生 CLI 明确公开的 `commentary` 与 `reasoning.summary.delta` 分别按安全 group 累积为“工作思路”，始终默认展开、原位追加，缺少真实公开内容时不生成节点；
- 安全的 context、delegation、source、execution、delivery、cache identity、media route 回执与确定性检查保留在每轮回答后的折叠卡中；普通结构化 JSON 结果按有界文本显示，只有后端明确标记的 artifact preview 才显示摘要，不会因字段名碰巧为 `preview` 而丢失其他结果；
- 计划或回执中的模型名称、执行方式、推理模式或 reasoning effort；缺少供应商运行时身份回读时不会标成“实际模型”；
- Token、耗时和 Token/s；Token 卡可展开显示输入、输出、推理输出和非零缓存（缓存是输入子集，不重复计入总数）。AICLI/Codex 的累计统计来自 `tokenUsage.total`，不会把 `TokenUsage.last.outputTokens` 误标为整场累计；缓存为零或缺失时不显示。执行中耗时每秒更新，静默阶段仍会低频复核服务端状态，终态或过期后冻结；Ollama 完成后以 `completion_tokens / eval_duration_ns` 显示模型评估时段的精确输出速度，AICLI agent 的安全真实输出 token 只除以从启动到完成的整段执行墙钟并明确标为估算；
- Codex agent 同一条运行时通知实际同时上报当前上下文占用与上限后，显示例如“已用 202k / 共 258k”并实时更新；在完整实测到达前显示“等待 Codex 运行时实测”，不会显示 registry 配置值或结果回执替代品；自动压缩完成后同步回落并写入时间线；
- 最终结果、确定性校验和 Codex 是否已取回结果。

2026-08-13 的历史页面验收使用本地 `qwen-main-v1`、AICLI `0.3.5` 与 Codex CLI `0.147.0`：三轮真实运行共投影 19 个公开思路节点，首轮主对话完整交错显示 11 次命令、3 次 `public_web_search` 生命周期与 3 个工作区变化，失败草稿保留但不冒充最终答复；后续成功轮给出包含标题、列表、表格、引用、代码块和安全 HTTPS 链接的 Markdown 最终答复。搜索工具的三次调用是真实事件，但该次上游搜索端点返回不可用，因此不伪称取得了搜索结果。1440×1000 无窗口视觉复核无横向溢出，工作思路全部默认展开。该回执不证明当前 Qwen3.8 27B 之外的 legacy route 能力。

启动本机服务：

```powershell
llm-backend-toolkit observer --no-open
```

Windows 桌面应用模式：

```powershell
pwsh -NoProfile -File .\scripts\Start-LlmBackendObserver.ps1
pwsh -NoProfile -File .\scripts\Install-LlmBackendObserverShortcut.ps1
```

正式启动器使用项目自带的无控制台 WinForms/WebView2 宿主，而不是 Edge app-mode。宿主使用独立 profile，并在页面导航成功后才显示默认最大化窗口；重复启动会激活已有观察台。窗口和快捷方式仍写入项目图标与 Shell 身份属性，但 Explorer 任务栏最终渲染未纳入本轮验收，不能把属性回读冒充任务栏图标视觉成功。宿主显式清除仅限当前进程的 WebView2 环境覆盖，避免 Windows Search/Widgets 的共享 profile 造成 `ERROR_INVALID_STATE` 页面加载失败。

启动器只创建一个 loopback 服务和一个原生观察台窗口。快捷方式直接指向 `LlmBackendObserverHost.exe`，后台服务和 AICLI/PowerShell 子进程均用 `CREATE_NO_WINDOW` / Hidden 语义，失败只写本机诊断文件，不弹控制台或错误框。窗口打开后通过 SSE 自动接收后续 `invoke` / `submit` / `probe` 受管调用，不需要手动刷新；处于最新对话时会自动切换到新调用，手动回看历史时不会抢走页面。重复启动会把已有观察台带到前台。安装器会同时创建当前用户桌面和开始菜单中的“模型调用观察台”快捷方式；它只升级或删除能由启动目标、完整参数、工作目录和描述共同证明属于本工具的链接，同名第三方文件会冲突失败而不会被覆盖。首次全体预检会拦截开始时已经存在的冲突；每个目标在最终变更或状态确认前还会复验。桌面和开始菜单是两个独立 Known Folder，不能组成原子事务：若两处操作之间出现并发变化，安装器会停止且不覆盖或删除变化目标，已经完成的本工具链接操作可能保留，可在处理冲突后幂等重跑。需要移除这两个精确入口时使用 `-Remove`。`Show-LlmBackendDashboard.ps1` 继续作为 PowerShell 降级视图。

观察台显示的是经过净化的可验证工作过程，不是隐藏 chain-of-thought。prompt、隐藏 thinking/reasoning 正文、原始命令和参数、工具输入输出、环境变量、OCR/ASR 正文及绝对私密路径都不会进入公开事件日志。正式 skill 只使用受管 AICLI 入口，不会因旧安装态缺少事件能力而静默降级。原观察台验收记录中的 AICLI `0.3.5` 与 `codex-cli 0.147.0` 是历史证据；当前入口与版本通过 `version` / `preflight` 回读，协议、生命周期或清理语义不匹配会返回明确错误。

受管 Codex harness 默认可发现并可多次调用 `public_web_search`；AICLI 提供搜索能力和显式 `--no-web-search` 关闭合同，Toolkit/GUI 只消费其安全 lifecycle。观察台仅在 `tool_name=public_web_search`、`search_provider=bing-rss-v1` 与运行期回执闭合时显示真实调用次数，不公开 query 或结果正文，也不会把“调用结束”伪称为“取得有效结果”。

“Token”卡片是上游本次运行累计输入、输出、推理输出与缓存 usage，兼容 provider 与 AICLI 的字段命名；AICLI/Codex 的累计值来自 `tokenUsage.total`，缓存为零或缺失时不显示，缓存作为输入子集也不会重复计入总数。“当前上下文”是另一项指标，只接受 Codex app-server 在同一条 `thread/tokenUsage/updated` 运行时通知中同时提供的 `last.totalTokens` 与 `modelContextWindow`。首个完整实测尚未到达时固定显示“等待 Codex 运行时实测”；如果运行结束仍缺少完整配对，或字段、通知、生命周期不兼容，AICLI 会返回明确协议错误，不会用 prompt token、累计输入、Toolkit 压缩估算、backend registry 上限或最终结果回执补成估算值。时间线中的“已压缩调用输入”是 Toolkit 发起模型调用前的确定性输入整理；“Codex 已自动压缩上下文”才是原生 agent 会话实际完成的 context compaction，后者只展示上游真实提供的压缩次数与上下文占用，不补造压缩前数据。

请求可选填 `observability.public_label` 作为持久历史中的非敏感短标题；观察台不会从私密任务正文自动生成标题。技能调用会在有合适公开名称时填写此字段。

可写模式（默认 `danger-full-access` 或显式 `workspace-write`）只持久化运行时窗内观察到的工作区变更文件数，不读取或展示文件正文。只有调用方通过 `observability.file_changes={mode:"diff",include:[...]}` 明确声明精确相对路径可在本机历史中公开时，观察台才保存这些普通小文本文件的有界 `+/-` 与 unified diff；任何二进制、疑似秘密、绝对路径、reparse、大小或总量门禁失败都会退回 count-only。loopback GUI 会从独立的本机观察元数据安全合成并支持复制完整路径，但事件、状态、结果、公开进度和 diff header 始终只保留相对路径。

完整产品与安全合同见 [模型调用观察台设计](docs/model-observer.md)。

## 数据工厂智能体

默认请求见 [examples/local-agent-request.json](examples/local-agent-request.json)。关键字段：

- `execution.mode=agent`
- `execution.runner=data_factory`（可省略；从 backend registry 锁定精确 Profile/模型）
- `execution.policy`：当前 Codex machine 仅支持 `danger-full-access`；其他 runner 使用 catalog 中声明的 `workspace-write` / `read-only`，不伪装成已支持的隔离。
- `execution.budget`（当前 AICLI machine 接口兼容默认是 `limit_mode=watchdog_only`、`timeout_seconds=900`，不设置 step 或 tool-call 上限；调用方可显式调整 wall-clock watchdog。`bounded` 只有显式选择时才配置 step/tool-call 硬上限。旧 `completion_driven` 输入只为兼容保留；当前 AICLI 未声明可续期 idle lease，因而会在模型启动前明确失败，绝不静默改成 watchdog。）

默认 `danger-full-access` 只属于 Codex 执行权限，不扩大任务授权。只用于可信任务，明确授权的输出仍应限定在隔离目录。其他 runner 的窄权限由各自实际接口决定，不能把 Codex 拒绝的 `workspace-write` 写入示例。普通配置同步使用 `configured`，不会复用旧实测证据，也不因此强制重跑 E2E；能力承诺才需要对应真实任务验收。可变工作区默认不复用已完成 job；显式 `execution.cache_key` 必须绑定真实输入内容。

显式候选 `qwen-code`、`opencode`、`codex-cli`、`claude-code` 供顶级模型有理由时选择，工具不会自行换 harness；其中三个 legacy route 当前为 `unverified` / `pending_reacceptance`，在新精确模型回执前会失败关闭，不能继承旧 Qwen3.6 35B 验收。任何失败都返回当前 runner、exit code、墙钟时间和顶级模型裁决选项，不回传事件流或隐藏 chain-of-thought。watchdog-only/bounded 任务只接受实际回执证明的相应约束；未知事件、无法确认完整进程树清理或越限都会失败关闭，不会把未执行的约束写成成功。

`local-default` 的 `data_factory` 与 `codex-cli` 通过版本化注册表绑定精确 AICLI Profile 和模型。当前身份、上下文、配置状态及历史验收标签以 `backends` 为准，README 不再复制易过时的模型 tag、摘要或“当前已验收”结论。`local-crosscheck-35b` 仍是非默认、`crosscheck_only`、no fallback 的显式交叉验证路线。普通配置变更撤销旧 live 证据；失败或没有可核验身份的历史记录不成为当前通过。临时历史 `routing_role=benchmark_only` 的 `codex-cli` route 仍受其独立指纹合同约束，保持 no fallback，不会借用普通 route 身份。任何付费 API 都必须显式选择 exact backend、提供调用者临时环境变量并设置真正的 JSON 布尔值 `privacy.cloud_allowed=true`；没有自动 fallback，也不读取其他应用的私有凭据。
`status` 会在不发模型生成请求的情况下返回当前 backend、模型指纹、agent 默认路由、证据状态和支持的 runner；实际任务回执同时记录精确 Profile、模型与是否采用默认。

旧 `cloud-qwen3-8-max-agent` / `qwen3.8-max` 路线仍从可选注册表移除：AICLI catalog 中出现同名 Profile 不会自行激活 Toolkit backend、fallback 或本地默认。保留的 reserved 记录为 `unverified/selectable=false`；旧请求会以 Unknown backend 失败关闭，不会静默改投 Qwen 3.7、其他付费模型或本地模型。这个本地 Qwen3.8 27B 切换不改变云端 route 的独立验收与激活边界。

`fast-middle-agent` 是显式 opt-in 的文本型 Agent 角色：`codex-spark-xhigh + gpt-5.3-codex-spark + xhigh`，catalog 将其标为 `routing_role=latency_crosscheck`。它只通过官方 Codex CLI/ChatGPT 登录链调用，不伪装成按调用计费的 OpenAI API，也不支持原生图片输入。2026-07-29 的真实源码链验证证明 `workspace-write` 路由已经生效，但 Spark 在冻结代码修复题上用到 81/80 步后硬停止，仅得 2/9；按“不因步数失败复测”的门禁，当前只保留显式候选，不作为自动任务推荐。选择它必须显式写 `backend=fast-middle-agent` 和 `privacy.cloud_allowed=true`；省略 backend 仍只走本地 Qwen。额度、限流、资格或预算失败会返回规范错误和 `invoke:local-default` 裁决选项，但不会自动重提；调用方如选择本地回退，必须保留两次独立回执和实际模型身份。

云端示例见 [examples/cloud-direct-request.json](examples/cloud-direct-request.json)。普通单次摘要、抽取和结构化生成继续使用 `execution.mode=direct`，避免为不需要文件/命令循环的任务增加 Agent 调用成本。

本机 35B 的 PersonalOS 风格小型清洗专项、选择理由和严格适用范围见 [数据工厂智能体验收报告](docs/agent-data-factory.md)；它是历史基线，不是当前默认模型或 legacy route 的新验收。该报告不代表通用智能排名。

Spark 与本地 Qwen 的合成数据清洗、跨文件工程、非代码路由和现实因果对照见 [高速中档智能体验收报告](docs/fast-middle-agent.md)。结论是互补分层，不是替代本地默认。

## 四 harness 通用代理基准

`general_agent_v1` 将 PersonalOS 专项题之外的能力拆成三个可复现任务：证据推理与抗提示注入、代码修复、约束工作流规划。隐藏 verifier 不进入智能体可写工作区，安全和正确性是门禁，只有同分才比较时间。

```powershell
python scripts/run_general_agent_benchmark.py --list
python scripts/run_general_agent_benchmark.py --aicli-entry C:\path\to\aicli.ps1
```

默认依次串行运行 Codex CLI、Claude Code、Qwen Code 与 OpenCode，并使用 `local-default`。真实运行必须通过 `--aicli-entry` 或 `LLM_TOOLKIT_AICLI_ENTRY` 固定一个现存的 `aicli.ps1`；缺失时在创建输出目录和调用模型前失败关闭，不再生成看似 4/30 的空跑结果。`--backend` 可选择注册表中的其他精确后端；当前 agent 请求默认采用 900 秒 watchdog-only，`--max-steps` / `--max-tool-calls` 仅在显式选择 bounded 模式时生效并写入 summary。扩大步数不会改变题目、workspace 或隐藏 verifier。若按模型价格比较，应同时保留同步数结果，并说明上下文累积使总 Token 成本通常不是步数的线性倍数。结果只对记录的 suite fingerprint、模型身份、CLI 版本、沙箱合同和 Toolkit 提交有效；它比较的是“模型 + harness”的代理表现，不生成永久通用智商分。（早期验收报告中的 completion-driven 与 24 步/80 工具调用是历史合同，不是当前默认。）

2026-07-22 的完整横向实测、耗时、能力边界和默认建议见 [四 harness 通用代理验收报告](docs/general-agent-benchmark-v1.md)。2026-07-25 又用当前 AICLI 源码入口、`codex-app-server` 与 `distinct-non-output-thread-item-v2` 完整复测 Codex 三题，最终 30/30、协议 3/3；数据工厂专项同链路复测 21/21。2026-07-29（UTC+8）因 CLI 更新兼容与错误空跑做了仅本地四 runner 调查：Codex 单次 28/30 且协议 3/3，Qwen Code 产物 30/30 但达到上游会话轮次，两个 4/30 设施根因均已定位；详见 [本地 Qwen 四智能体复核与更新兼容调查](docs/local-qwen-four-agent-investigation-2026-07-29.md)。当前版本的建议仍是 `data_factory` 固定使用 Codex CLI；日常调用不重复跑四 harness。

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
  "privacy": {"cloud_allowed": false}
}
```

工具按目标对文本分块、打分并选取少量片段，向结果返回 source hash 和行号范围。它只支持明确批准的 UTF-8 文本 artifact；PDF、Office、图片和音频应先由相应结构化读取器或媒体 adapter 处理。

## 媒体路线

- `native`：direct 模式把图片直接交给声明支持视觉的后端。AICLI machine Agent 当前不接受原生图片，启动前明确拒绝；需要图中文字时显式选择 OCR，不能静默丢弃图片。
- `specialist`：图片交给 LocalOCR，音频交给 ChineseASR，再把文本交给后端。
- `auto`：一般图片优先原生视觉；精确文字、表格、公式、扫描件优先 OCR；音频使用 ASR。

LocalOCR 与 ChineseASR 的入口通过环境变量注入：

```text
LLM_TOOLKIT_LOCALOCR_ENTRY
LLM_TOOLKIT_CHINESEASR_ENTRY
```

本地 Ollama 默认访问 `http://127.0.0.1:32100`。该入口应由机器自己的 GPU broker 管理。
专项媒体在后台 worker 内串行完成；OCR 使用 `-StopAfter` 释放 GPU 后才启动本地 Qwen，ASR 完成并释放 Broker 租约后才进入模型阶段。
当前 AICLI machine Agent 接口不接收原生图片，预检和执行都会在启动前明确拒绝。原生视觉走 direct provider；精确 OCR 仍走 LocalOCR，音频仍走 ChineseASR。不能把“能看到图片路径”冒充原生多模态已验证。

## 云端与其他 API 平台

公开项目只读取：

```text
DASHSCOPE_API_KEY
LLM_TOOLKIT_QWEN_BASE_URL
DEEPSEEK_API_KEY
```

当前内置 direct 云端 backend 是 Qwen `cloud-qwen-flash` 与 DeepSeek `cloud-deepseek-v4-flash`；`openai-chat` adapter 也可通过外部注册表接入其他兼容平台，只有全新协议才需要新增 adapter。API key 只写环境变量名，远程云端地址必须是 HTTPS。自动测试套件不会发起真实云端调用；云端请求格式和错误分类使用 mock 验证。
选择任何云端 backend 仍不等于授权传输；请求必须同时设置 `privacy.cloud_allowed=true`。云端 probe 还需显式传入 `--cloud-allowed`。
Flash 当前仅支持显式 direct API；Agent 模式会因没有已验收 route 而失败关闭，不会调用云端或自动改投其他模型。欠费会归一化为 `billing_unavailable` 并把本地调用、顶级模型接管或账务处理选项返回调用者，不会自动降级。Plus 名称在内置 registry 中会返回 unknown backend。

DeepSeek V4 Flash 0731 路由固定使用 `deepseek-v4-flash` 与 `POST https://api.deepseek.com/chat/completions`。省略 `reasoning.mode` 时默认发送 `{"thinking":{"type":"enabled"}}`，显式 `off` 发送 `disabled`。调用方可仅在 Toolkit 子进程所需生命周期内临时设置 `DEEPSEEK_API_KEY`；该路由不发现或复制 AICLI/OpenClaw secret。provider 返回公开 `content`/`tool_calls` 并立即丢弃 `reasoning_content`。DeepSeek V4 Pro 未注册，Agent 模式、缺 key、非 HTTPS 或未显式允许云端均失败关闭。当前验收只覆盖官方 [Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode) 合同与本地 mock，没有真实 API probe。

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

工具在新请求的 `task.inputs` 中追加一个 `previous_result`，只包含前轮 `job_id`、`result_status` 和 `output_preview`；不会自动携带完整回执、完整输出或原始输入。若下一轮需要这些依据，调用者须明确提供。默认最多 3 轮、硬上限 8 轮；它不依赖 Codex、Claude 或某家 API 的隐藏 session，因此更换模型和平台后仍可延续。每次任务自身仍分别返回 `delegation_receipt`（后端压缩和按路径引用的数据规模）与 `delivery_receipt`（长结果预览避免回传的估算量）；二者是成本判断证据，不冒充 Codex 计费 token，也不自动成为下一轮输入。

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
