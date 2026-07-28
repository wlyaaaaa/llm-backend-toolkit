# Qwen3.7 Flash 与 Plus 综合实测报告

日期：2026-07-28

范围：`qwen3.7-flash` 对 `qwen3.7-plus`

口径：只依据本机自建任务、真实 API 和既有 Codex Agent 证据；不使用外部基准、排行榜或模型裁判

## 结论

在目前唯一有效的同条件能力对测——不连接智能体的直接 API 测试——Flash 与 Plus 大体同级，Flash 本轮略高 1 分、延迟明显更低。没有观察到 Plus 更强。

Codex Agent 的 4/30 不是有效能力分。两款云模型都遇到同一个 AICLI/Codex `workspace-write` 沙箱故障；模型能读取、推理和给出目标内容，却不能把内容写进验收文件。Flash 扩到 56 步仍明确报告所有写操作被策略拒绝，证明继续加步数只会继续付费，不能修复连接层。

因此当前建议是：

- 普通文本、结构化生成和一次性推理：优先 Flash。它与 Plus 本轮能力相当，但速度和用户给定价格更有优势。
- Codex 文件/命令智能体：Flash 与 Plus 的路由均已禁用；不能用本轮 Agent 结果给两者排序。
- `local-default` 保持不变；不静默 fallback，不再自动调用云端 Agent。

## 有效对测：直接 API

两模型使用相同 prompt、`temperature=0`、相同思考开关和输出上限。10 个题目均为本地编写的合成题，评分由确定性校验器完成，没有连接智能体。

| 指标 | qwen3.7-flash | qwen3.7-plus | 观察 |
|---|---:|---:|---|
| 成功调用 | 10/10 | 10/10 | 两边 API 均稳定返回 |
| 自动评分 | 18/20 | 17/20 | 唯一差异是 Plus 给正确 JSON 加了 Markdown 代码块 |
| 全部请求中位延迟 | 0.600 秒 | 1.520 秒 | Flash 低约 60.5% |
| 非思考 7 题中位延迟 | 0.463 秒 | 1.091 秒 | Flash 低约 57.6% |
| 思考 3 题中位延迟 | 8.178 秒 | 10.493 秒 | Flash 低约 22.1%，样本较小 |
| 总输出 Token | 4,173 | 2,858 | Flash 思考输出更长，速度快不等于 Token 更少 |

两者都通过严格 JSON、冲突归并、逻辑排程、代码修复、长文检索、引用数据中的提示注入抵抗、多步算术、精确函数调用和事实压缩。两者也都错在同一道现实意图题：洗车店离人 50 米，但任务是把车送到店内；两边都回答步行。

这部分支持“Flash 与 Plus 大体同级，Flash 通常更快”，不支持“Flash 普遍更聪明”或“每次都更快”。

## Codex Agent 结果为何作废

链路为：

`llm-backend-toolkit → AICLI → Codex CLI 0.145.0 app-server → qwen3.7-flash / qwen3.7-plus`

使用的 `general_agent_v1` suite fingerprint 为：

`7f46548f9aa914584e998c1f00df1db2a72b25061782dde5610ded4e3ce2b88e`

24 步记录中 Flash 与 Plus 都是 4/30、三题均触发步骤上限。这些记录一度看起来像模型执行效率低，但 56 步 Flash 记录给出了决定性反证：

- 证据题在第 33 步完成了正确方向的 JSON，却明确说所有文件写入被策略阻止，只能把 JSON 放在最终文本。
- 规划题在第 42 步给出了计划内容，也明确说无法写入要求的产物。
- 代码题到第 57 步仍不能修改目标源码。

隐藏 verifier 只检查 workspace 中的最终文件，因此连接层禁止写入必然得到接近空工作区的最低分。这个故障同时影响 Flash 和 Plus，4/30 不能用于能力比较。

先前修复了两个真实连接问题：远程 Provider 被外层禁网沙箱阻断，以及 Node 启动器提前退出导致进程树清理无法确认。修复后 API 传输和 app-server 生命周期可以工作，但云端 `workspace-write` 仍未达到可用门槛。按“查不清不继续付费”的约束，不再发起云端复测，直接禁用两款模型的 Agent routes。

## 旧 30/30 为什么可信

旧报告中的 30/30 是 2026-07-25 由同等级顶级模型验收的正式结果，应作为可信证据，不应归因于评测者较弱。

它使用相同 `general_agent_v1` fingerprint、本地 `qwen-main-v1 → qwen3.6:35b`、相同模型 digest、Ollama 0.32.1 和 Codex CLI 0.145.0；三题分别为 9/9、11/11、10/10。报告中的“固定环境”表来自 2026-07-22 原始横评，列出的 AICLI `e10fb74` 并不是 7 月 25 日新增 30/30 段落的准确提交。Git 时间线显示，30/30 段落在 AICLI `62d36d4` app-server 改造后约 14 分钟提交；这是旧报告的一处历史元数据错位，不影响其题目、分数、事件和模型身份记录。

2026-07-28 的本地复查为 22/30，但它是在本轮连接调试中的未提交 AICLI 运行态上执行，并且 benchmark 请求从显式 `provider/execution.model` 改成了 backend 解析。它不是对 7 月 25 日冻结链的精确复现，不能推翻旧 30/30。它最多说明单次 Agent 运行存在波动或当前调试链有差异；按用户要求不再重复复测。

最关键的区别是：旧本地 30/30 能正常写入 workspace；本轮云端两模型不能。因此云端 4/30 与旧本地 30/30 不可横比。

## 接入状态

- backend：`cloud-qwen-flash`
- alias：`qwen3.7-flash`
- direct adapter：`openai-chat`
- 登记上下文：1,000,000 Token
- 直接 API：已接入并通过真实调用
- Agent routes：已禁用
- 默认模型：仍为 `local-default`
- 失败策略：不自动切换 Plus、本地模型或其他 Provider

`qwen3.7-plus` 同样保留显式 direct API，Agent routes 同样禁用。

## 证据

- 直接 API 原始结果：`outputs/qwen37-flash-vs-plus-20260728.json`（Git 忽略）
- 直接 API SHA-256：`d2d86538c82eba1c4508a653120e2dbf563c1fb33f84e88919cfd88c9e5cbdef`
- Flash 24 步 summary SHA-256：`eb618c60c4e1d834baec82f79ce803cb1e315a6887ef8be1cc2122ad00986f54`
- Plus 24 步 summary SHA-256：`2a064644e21b4974ab6638c5edeedb15b7e0bc9a46986d2556b601d5f0434edd`
- Flash 56 步 summary SHA-256：`08d81f9729ee2606ea4c8ab47c0e49dd7ba44e39b818bd05a4a84bd9d70cf897`
- 当前本地复查 summary SHA-256：`43d8102b4a6f688dd988ed48cf4545be95bcbd31235187eda916da30a781866b`

价格比例来自本轮用户给出的“不高于 Plus 的约 1/6”。56 步只用于当时的预算设计；由于实际失败来自写入沙箱，不能作为等成本能力结论，也不应继续扩大步数。
