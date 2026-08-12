# PersonalOS 风格小型数据清洗专项验收报告

状态：**PASS**

验收日期：2026-07-22（UTC）
默认路由：`data_factory` → `codex-cli` → aicli `codex-ollama-main` → LocalGpuBroker `127.0.0.1:32100` → `qwen-main-v1`（Qwen3.6 35B）

适用范围：仅覆盖本报告中的 9 行合成 JSONL、任务合同、verifier、模型身份和 CLI/沙箱版本；不等于 PersonalOS 完整数据工厂验收，也不外推到其他场景。

通用代码修复、证据推理和约束规划的后续四 harness 实测见 [通用代理验收报告](general-agent-benchmark-v1.md)。它补充本专项，不追溯改写本报告的历史版本锚点。

版本锚点：`qwen-main-v1:latest` digest `46c6d39f92e76686e7e3ff0097029fdb7aedbdea5375857acdbdb08b1fd8783a`，父模型 `qwen3.6:35b`，`Q4_K_M`，262144 context；Toolkit `628e25c`，aicli `9674a94`。模型 alias/digest、CLI 版本、沙箱合同或任务提交发生实质变化后，本报告只能作为历史基线。

当前正式 skill 不依赖旧安装态：它把 `LLM_TOOLKIT_AICLI_ENTRY` 固定到受管的当前源码入口，该入口缺失就明确失败。当前 Codex app-server 最低已验证基线是 `codex-cli 0.145.0`，`0.146.0-alpha.3.1` 曾通过兼容门；未来更新默认尝试，但协议漂移必须明确失败。该现行调用合同不会追溯改写本报告的历史 benchmark 锚点。

## 结论

在本专项任务内默认只选 Codex CLI。它是四个 harness 中唯一同时满足“进程正常退出”和“确定性验收 21/21”的候选，并且比同样生成 21/21 产物但最终 exit 1 的 Claude Code 快约 53.4%。这证明当前 Codex CLI harness 最适合承载该任务，不证明 Codex、Claude、Qwen Code 或 OpenCode 的通用智能高低。

顶级模型不需要在日常调用前重复本报告。只有模型 alias/digest、CLI 主版本、沙箱协议、任务契约发生变化，或正式结果出现异常时，才建议做一个小样本复核。

## 同题实测

四个 CLI 的原始横向测试均在测试前升级到当时最新版本，使用同一个 `qwen-main-v1`、同一 9 行 JSONL、同一任务说明、30 step / 120 tool-call 请求预算和 900 秒墙钟上限。当前 Codex 回归使用 `distinct-non-output-thread-item-v2`：公开进度与最终消息不计行动 step；由于真实复测证明模型在完成 21/21 产物后还会继续执行验证，本场景单独保留 45 step / 120 tool-call 的硬上限，不改变 Toolkit 的全局预算语义。验收器检查：raw 不变、逐行流式处理、去重 lineage、冲突不覆盖、时间不确定性、验证码脱敏、损坏 Unicode 不臆修、hash、原子写、幂等、checkpoint 与 receipt。

排名规则：先要求进程 exit 0 且 21/21；合格者再按墙钟时间、工具调用数排序。

| Harness | 版本 | 产物检查 | 进程结果 | 墙钟/执行时间 | 观察到的工具调用 | 判定 |
| --- | ---: | ---: | --- | ---: | ---: | --- |
| Codex CLI | 0.145.0 | 21/21 | exit 0 | 47.343 秒 | 18 个真实 action items；另有 1 条模型元数据警告 | **通过，默认** |
| Claude Code | 2.1.207 | 21/21 | exit 1 | 约 101.7 秒 | 未可靠暴露 | 不通过：结果可用但协议未完整收口 |
| Qwen Code | 0.20.1 | 0/21（未生成 cleaner） | exit 53，达到 30 turns | 约 262.2 秒 | 未可靠暴露 | 不通过 |
| OpenCode | 1.18.4 | 0/21（未生成 cleaner） | exit 0，但只返回计划 | 417.289 秒 | 20 | 不通过 |

这是 harness + 本机模型 + 当前任务的结果，不是四个上游项目的通用质量排名。时间已进入决策：Claude 虽生成正确文件，但比 Codex 慢约 2.15 倍且退出失败；因此没有理由把它设为默认。

## 2026-07-25 当前源码链复测

现行回归固定使用当前 AICLI 源码入口和 Codex app-server 协议。数据工厂专项再次通过 **21/21**：exit 0、36 个 action step、17 次工具调用、118 个安全 machine event / 126 个 app-server event、当前上下文 31,155 / 258,400、执行 164.815 秒。回执明确为 `codex-app-server`、`distinct-non-output-thread-item-v2`，45 step / 120 tool-call / 900 秒硬预算均未命中，进程树清理已确认。

同日完整通用代理回归也在最终任务合同下通过 30/30、协议 3/3；详情见 [四 harness 通用代理验收报告](general-agent-benchmark-v1.md#2026-07-25-当前源码链回归)。这两项分别证明数据清洗专项和通用小型代理任务，不能互相替代，也不外推到没有 verifier 的开放式长期任务。

## 本专项能够证明的能力边界

直接证据只证明：当前模型经 Codex CLI 能在明确合同和独立 verifier 下编写并运行这个小型确定性清洗器。下列“适合交给”是工具的保守委派候选范围；分类、抽取、多模态和长批次仍应按新场景做小型校准，不能冒充已由本题全部证明。

适合交给本地 35B：

- 输入、输出和成功条件明确的批量清洗、格式转换、分类、抽取和派生文件生成。
- 能用脚本、schema、hash、计数、diff 或独立 verifier 判定的工作。
- 可拆成短阶段，失败可从 checkpoint 恢复的本地长任务。
- 一般图片的本地原生视觉初筛；精确文字/表格/公式改走 LocalOCR，音频改走 ChineseASR。

不应让本地 35B 独立承担：

- 含糊产品策略、跨项目架构裁决、授权边界判断或事实 owner 冲突。
- 公开发布、付费、密钥处理、生产变更、不可逆删除或主机安全策略变更。
- 医疗、法律、财务等高风险最终判断。
- 在没有确定性验收器时改写 canonical raw，或以“最新记录覆盖旧记录”消灭冲突证据。
- 把 OCR/ASR 的低置信度、损坏文本或时间缺失自行猜成确定事实。

顶级模型仍拥有：是否委派、选择 workspace、云端隐私授权、是否显式改用付费 `cloud-qwen-flash`、失败后的重试/fallback、最终验收与对外结论。省略 backend 只走免费本地默认，工具不会自主选择付费 API。工具只返回最终答案、文件结果和紧凑 receipt，不让顶级模型持续观看思考过程。

## PersonalOS 数据工厂契约

- canonical raw 位于低级智能体可写根之外；任务工作区只放暂存副本或派生输入。
- derived 记录保留 source line/hash、冲突组、未知/不确定标记和损坏证据。
- 写入原子、可幂等复跑，checkpoint 必须绑定输入 hash。
- 不允许静默修复、latest-wins 或用模型猜测覆盖来源事实。
- 可变 agent workspace 默认禁用完成结果缓存；显式 cache key 必须绑定真实输入 hash。

## 沙箱与预算

- 本报告 2026-07-22 的历史验收使用当时的 Codex Windows 外层沙箱；因此 21/21 只证明上方版本锚点，不冒充现行沙箱合同的重复验收。
- Codex machine run 省略策略时默认使用原生 `danger-full-access` 与 `approvalPolicy=never`；显式 `read-only` / `workspace-write` 仍用于主动收窄。默认完全访问只改变执行沙箱，不扩大任务授权，且 API Key 仍只注入目标子进程。
- `read-only` 运行把 CLI 状态放在临时可写根，来源工作区只读；`workspace-write` 应只指向隔离 worktree/暂存目录。声明 agent-capable 时，仍须在隔离目录完成一次真实可写任务，而不能用 PONG 代替。
- 历史四 harness 的统一硬上限只有墙钟 timeout；当时 Qwen Code 另有 turns/tool calls、Claude 有 max turns，Codex/OpenCode 未被该版本证明可硬限制 step/tool-call。现行 AICLI Codex app-server 路径新增版本化安全事件投影与硬限制，但新的验收仍必须以该次结果回执为准，不能用现行能力追溯改写历史测试。

## Qwen3.7 Plus：历史资料判断（已被直连实测取代）

官方没有发布 `qwen3.7-plus` 与本机 `qwen3.6-35b-a3b` 的成对统一“智力百分比”，因此本项目不编造一个总提升比例。

最近的官方同表对照是 `qwen3.7-plus` 与 `qwen3.6-plus`，不是本地 35B。按官方表中共同的 8 个 coding-agent 单分数项目做无权平均，3.7 Plus 约高 **9.2%**；按 11 个 general-agent 单分数项目做同样计算，约高 **14.7%**。该计算只是方向性摘要，不能改写成“比本地 35B 强 14.7%”。

可直接比较的容量规格更明确：文本上下文 1M 对 256K（约 **+290.6%**），最大 thinking budget 256K 对 80K（约 **+220%**），视觉 URL 图片上限 2048 对 256（约 **+700%**）。这些是容量提升，不是智力提升。

这段官方资料比较仅是 2026-07-22 的历史背景，不再作为当前路由依据。2026-07-29 的本机自建同题直连复核未观察到 Plus 对本地或 Flash 的语义能力优势；用户随后授权退役，内置 registry 已移除 Plus。额度、隐私和任务价值仍由顶级模型裁决，欠费只返回 `billing_unavailable`。

官方来源：

- <https://help.aliyun.com/zh/model-studio/text-generation-model>
- <https://docs.qwencloud.com/developer-guides/getting-started/text-generation-models>
- <https://docs.qwencloud.com/developer-guides/getting-started/vision-models>
- <https://www.alibabacloud.com/blog/qwen3-7-plus-multimodal-agent-intelligence_603206>
