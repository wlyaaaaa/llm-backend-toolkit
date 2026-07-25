# 模型调用观察台设计

## 最终产品效果

模型调用观察台是 `llm-backend-toolkit` 的本机只读可观察面。用户只需打开一次桌面 GUI；此后 AI 通过受管 skill 发起的任务会自动出现并实时更新，不需要手动刷新，也不会由每次调用反复抢焦点。

界面由三部分组成：

1. 调用记录与历史：显示并发任务、独立对话和 continuation 轮次，支持筛选、搜索和分页加载旧历史。
2. 中文工作时间线：显示排队、输入整理、GPU/模型连接、推理活动、公开输出、工具与文件编辑、OCR/ASR、校验和交付事件。
3. 详情：展示公开草稿、最终输出和校验回执，以及模型、推理等级、Token、TPS、耗时、GPU 与交付状态。

所有历史以耐久 job artifact 为准；读取 GUI 不增加轮询计数，也不等于 Codex 已取回结果。只有结果读取入口会记录 `handoff.collected`。

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

严禁进入事件投影的内容包括 prompt、隐藏 reasoning/thinking 正文、原始命令/argv、工具输入输出、环境变量、stdout/stderr、OCR/ASR 识别正文、绝对私密路径、PID 和 Broker lease token。

Token 指标必须说明依据：

- `eval_duration`：Ollama 最终 usage，精确；
- `wall_clock_estimate`：AICLI agent 返回的安全真实完成 token 数除以整段执行墙钟时间，近似，不冒充模型 eval TPS；
- `public_content_estimate`：运行中仅依据公开输出估算，近似；
- `unavailable` / `not_applicable`：无法可靠计算，OCR/ASR 不冒充 token TPS。

Token 卡片的主值是总计，悬停说明会分列输入、输出和缓存；TPS 一律带“输出 token/秒”单位，并在正文中区分模型评估时段精确值、整段墙钟估算或公开内容估算。

## 五条运行链

| Owner | 职责 |
|---|---|
| `llm-backend-toolkit` | job、cache、结果、统一安全事件、历史、SSE/API 和 GUI |
| AI CLI Profile Manager | Profile、原生 CLI、沙箱、硬预算和实时净化 machine event |
| LocalOCR | OCR 业务状态与只读安全 observer 投影 |
| ChineseASR | ASR 业务状态、chunk/RTF 与只读安全 observer 投影 |
| LocalGpuBroker/Ollama | GPU 仲裁、模型入口和原始精确 usage；不拥有业务 job |

Toolkit 调用 OCR/ASR 时会把开始/完成阶段写入同一个模型 job。各专项服务的 `/observer/jobs` 和 `/observer/jobs/{id}` 只用于安全聚合，不取代原有诊断接口，也不授权启动、取消或重试。

## 四基座边界

- `.agents`：AI 能力路由、skill 和自动打开观察台的个人 wrapper。
- GitHub 索引：仓库身份、PUBLIC/PRIVATE、remote、worktree 和发布事实。
- PCConfig：安装路径、端口、服务、计划任务、桌面入口和 LocalGpuBroker 机器事实。
- PersonalOS：个人连续性与授权语义；不拥有模型事件、GPU 或 GUI 历史。

## 实时与性能

- `/api/stream` 只发送 refresh 信号，详情仍由同源 loopback JSON API 读取；连接会持续发送 heartbeat，直到客户端主动断开，不会在任务仍运行时静默到期。
- 浏览器断线时降级为有界轮询；SSE 恢复后停止轮询。
- GUI 首屏只加载最近 100 条，旧历史按页加载。
- 服务缓存未变化的终态摘要；活跃任务继续计算新鲜耗时。
- 列表和详情分开请求，静态资源无外部依赖，长输出使用滚动容器和安全 `textContent`。

## Windows 桌面入口

`Start-LlmBackendObserver.ps1` 先确保 loopback 服务存活，再以 Edge `--app=<url>` 打开正式窗口。它用同用户互斥锁和精确窗口标题去重，不激活已有窗口。`Install-LlmBackendObserverShortcut.ps1` 使用 Windows Known Folder 同时创建当前用户桌面与开始菜单 Programs 快捷方式，不假设 OneDrive 或用户名路径，并绑定项目自带的白绿 ICO。安装、升级和 `-Remove` 都会验证 PowerShell 目标、完整 launcher/toolkit 参数、工作目录与描述；只有能证明属于本工具的链接才会修改或删除，旧 Edge 图标可安全迁移。首次全体预检会避免开始时已经存在的同名冲突导致半更新；每个目标在变更或确认 `unchanged` / `absent` 前还会按文件身份和链接契约最终复验。两个独立 Known Folder 不能组成原子事务：若两处操作之间出现并发变化，安装器会停止且不覆盖或删除变化目标，已经完成的本工具链接更新或删除可能保留，可在处理冲突后幂等重跑恢复。

个人 skill wrapper 在模型 `submit` / `probe` 之前调用启动器，因此可见 job 会在 worker 启动前写入；GUI 失败时不得静默开始不可观察的模型调用。
