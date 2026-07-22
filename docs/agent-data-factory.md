# 本地 35B 数据工厂智能体验收报告

状态：**PASS**

验收日期：2026-07-22（UTC）
默认路由：`data_factory` → `codex-cli` → aicli `codex-ollama-main` → LocalGpuBroker `127.0.0.1:32100` → `qwen-main-v1`（Qwen3.6 35B）

## 结论

默认只选 Codex CLI。它是四个 harness 中唯一同时满足“进程正常退出”和“确定性验收 21/21”的候选，并且比同样生成 21/21 产物但最终 exit 1 的 Claude Code 快约 53.4%。Qwen Code 与 OpenCode 保留为显式实验候选，不参与自动路由。

顶级模型不需要在日常调用前重复本报告。只有模型 alias/digest、CLI 主版本、沙箱协议、任务契约发生变化，或正式结果出现异常时，才建议做一个小样本复核。

## 同题实测

四个 CLI 均在测试前升级到当时最新版本，使用同一个 `qwen-main-v1`、同一 9 行 JSONL、同一任务说明、30 step / 120 tool-call 请求预算和 900 秒墙钟上限。验收器检查：raw 不变、逐行流式处理、去重 lineage、冲突不覆盖、时间不确定性、验证码脱敏、损坏 Unicode 不臆修、hash、原子写、幂等、checkpoint 与 receipt。

排名规则：先要求进程 exit 0 且 21/21；合格者再按墙钟时间、工具调用数排序。

| Harness | 版本 | 产物检查 | 进程结果 | 墙钟/执行时间 | 观察到的工具调用 | 判定 |
| --- | ---: | ---: | --- | ---: | ---: | --- |
| Codex CLI | 0.145.0 | 21/21 | exit 0 | 47.343 秒 | 18 个真实 action items；另有 1 条模型元数据警告 | **通过，默认** |
| Claude Code | 2.1.207 | 21/21 | exit 1 | 约 101.7 秒 | 未可靠暴露 | 不通过：结果可用但协议未完整收口 |
| Qwen Code | 0.20.1 | 0/21（未生成 cleaner） | exit 53，达到 30 turns | 约 262.2 秒 | 未可靠暴露 | 不通过 |
| OpenCode | 1.18.4 | 0/21（未生成 cleaner） | exit 0，但只返回计划 | 417.289 秒 | 20 | 不通过 |

这是 harness + 本机模型 + 当前任务的结果，不是四个上游项目的通用质量排名。时间已进入决策：Claude 虽生成正确文件，但比 Codex 慢约 2.15 倍且退出失败；因此没有理由把它设为默认。

## 能力边界

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

顶级模型仍拥有：是否委派、选择 workspace、云端隐私授权、是否改用 `qwen3.7-plus`、失败后的重试/fallback、最终验收与对外结论。工具只返回最终答案、文件结果和紧凑 receipt，不让顶级模型持续观看思考过程。

## PersonalOS 数据工厂契约

- canonical raw 位于低级智能体可写根之外；任务工作区只放暂存副本或派生输入。
- derived 记录保留 source line/hash、冲突组、未知/不确定标记和损坏证据。
- 写入原子、可幂等复跑，checkpoint 必须绑定输入 hash。
- 不允许静默修复、latest-wins 或用模型猜测覆盖来源事实。
- 可变 agent workspace 默认禁用完成结果缓存；显式 cache key 必须绑定真实输入 hash。

## 沙箱与预算

- aicli 强制使用 Codex Windows 外层沙箱并关闭网络；无法建立沙箱时 fail closed。
- 内层 CLI 可自动批准，但只在外层允许的工作区/一次性运行目录内生效。
- `read-only` 运行把 CLI 状态放在临时可写根，来源工作区只读；`workspace-write` 应只指向隔离 worktree/暂存目录。
- 墙钟 timeout 是四个 harness 的统一硬上限。Qwen Code 还支持 turns/tool calls，Claude 支持 max turns；Codex/OpenCode 当前没有被本工具证明可硬限制 step/tool-call，因此回执中的相应数字只是请求预算或结果侧观测，不冒充强制门禁。

## Qwen3.7 Plus：不实测，只做官方资料判断

官方没有发布 `qwen3.7-plus` 与本机 `qwen3.6-35b-a3b` 的成对统一“智力百分比”，因此本项目不编造一个总提升比例。

最近的官方同表对照是 `qwen3.7-plus` 与 `qwen3.6-plus`，不是本地 35B。按官方表中共同的 8 个 coding-agent 单分数项目做无权平均，3.7 Plus 约高 **9.2%**；按 11 个 general-agent 单分数项目做同样计算，约高 **14.7%**。该计算只是方向性摘要，不能改写成“比本地 35B 强 14.7%”。

可直接比较的容量规格更明确：文本上下文 1M 对 256K（约 **+290.6%**），最大 thinking budget 256K 对 80K（约 **+220%**），视觉 URL 图片上限 2048 对 256（约 **+700%**）。这些是容量提升，不是智力提升。

产品意见：把 3.7 Plus 视为更高一级的长上下文、多模态和复杂 agent 云端能力是合理的；但工具仍不自动升级或降级。额度、隐私和任务价值由顶级模型裁决，欠费只返回 `billing_unavailable`。

官方来源：

- <https://help.aliyun.com/zh/model-studio/text-generation-model>
- <https://docs.qwencloud.com/developer-guides/getting-started/text-generation-models>
- <https://docs.qwencloud.com/developer-guides/getting-started/vision-models>
- <https://www.alibabacloud.com/blog/qwen3-7-plus-multimodal-agent-intelligence_603206>
