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
- 模型主动形成的公开消息；
- 结果侧 usage 与确定性校验。

调用方可以通过 `observability.public_label` 提供最多 80 字的非敏感公开标题，例如“修复缓存身份回执”。观察台绝不会从 `task.goal`、instructions 或输入正文自动推导标题；未提供时只显示任务类型级通用名称。

严禁进入事件投影的内容包括 prompt、隐藏 reasoning/thinking 正文、原始命令/argv、工具输入输出、环境变量、stdout/stderr、OCR/ASR 识别正文、绝对私密路径、PID 和 Broker lease token。

Token 指标必须说明依据：

- `eval_duration`：Ollama 最终 usage，精确；
- `wall_clock_estimate`：完成 token 数除以墙钟时间，近似；
- `public_content_estimate`：运行中仅依据公开输出估算，近似；
- `unavailable` / `not_applicable`：无法可靠计算，OCR/ASR 不冒充 token TPS。

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

- `/api/stream` 只发送 refresh 信号，详情仍由同源 loopback JSON API 读取。
- 浏览器断线时降级为有界轮询；SSE 恢复后停止轮询。
- GUI 首屏只加载最近 100 条，旧历史按页加载。
- 服务缓存未变化的终态摘要；活跃任务继续计算新鲜耗时。
- 列表和详情分开请求，静态资源无外部依赖，长输出使用滚动容器和安全 `textContent`。

## Windows 桌面入口

`Start-LlmBackendObserver.ps1` 先确保 loopback 服务存活，再以 Edge `--app=<url>` 打开正式窗口。它用同用户互斥锁和精确窗口标题去重，不激活已有窗口。`Install-LlmBackendObserverShortcut.ps1` 使用 Windows Known Folder 创建桌面快捷方式，不假设 OneDrive 或用户名路径。

个人 skill wrapper 在模型 `submit` / `probe` 之前调用启动器，因此可见 job 会在 worker 启动前写入；GUI 失败时不得静默开始不可观察的模型调用。
