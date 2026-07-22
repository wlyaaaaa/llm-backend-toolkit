# 四 harness 通用代理验收报告

状态：**PASS（Codex CLI 为默认；其余为显式候选）**

验收日期：2026-07-22（UTC）  
Toolkit：`0.3.1`  
最终 suite fingerprint：`eb8e7d81933de2351b231a17233b2e99a20e24b435467d0ea4bfe78cb3583b1a`

## 结论

在本报告限定的模型、CLI、沙箱和三项任务下，`data_factory` 继续固定路由到 Codex CLI 是最优默认。Codex CLI 是唯一三题全部通过的 harness，总耗时 91.618 秒；它比第二名 Claude Code 少 50.6% 墙钟时间。

这不是“Codex 模型比其他模型强”的结论。四个 harness 使用的是同一个本地 Qwen3.6 35B；报告比较的是 **同一模型经不同智能体外壳后的任务完成质量和耗时**。

顶级模型可以不同意默认建议并显式选择其他 runner，但日常调用不需要重复本次四 harness 烤机。只有模型 digest、CLI 主版本、沙箱合同、任务合同发生实质变化，或实际结果异常时，才建议做一个小样本复核。

## 固定环境

| 组件 | 验收版本 |
| --- | --- |
| 本地模型 | `qwen-main-v1` → `qwen3.6:35b`，36.0B，Q4_K_M |
| 模型 digest | `46c6d39f92e76686e7e3ff0097029fdb7aedbdea5375857acdbdb08b1fd8783a` |
| 模型上下文 | 262144；能力声明含 completion、vision、tools、thinking |
| Ollama | 0.32.1，经 LocalGpuBroker `127.0.0.1:32100` |
| AICLI | 0.2.1，提交 `e10fb74` |
| Codex CLI | 0.145.0 |
| Claude Code | 2.1.207 |
| Qwen Code | 0.20.1 |
| OpenCode | 1.18.4 |

所有任务均关闭 Toolkit thinking，使用 compact 上下文、同一模型、同一公开任务和隐藏 verifier；每个 runner 串行执行，墙钟上限 420 秒。低级智能体只获准写隔离任务工作区，外层 Windows 沙箱关闭网络。评分先看正确性与安全；只有真同分才比较耗时。

## 任务与结果

三个任务分别检验：带隐藏测试的代码修复、证据时序/来源/敏感值/提示注入处理、以及预算和依赖约束下的工作流规划。

| 排名 | Harness | 代码修复 | 证据推理 | 约束规划 | 总分 | 总耗时 | 判定 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | Codex CLI | 9/9 | 11/11 | 10/10 | **30/30** | **91.618 秒** | 三题通过，默认 |
| 2 | Claude Code | 9/9 | 11/11 | 9/10 | 29/30 | 185.425 秒 | 可用候选；规划标签有误 |
| 3 | Qwen Code | 9/9 | 11/11 | 7/10 | 27/30 | 355.940 秒 | 有限制；规划算术/最优性失误 |
| 4 | OpenCode | 9/9 | 9/11 | 6/10 | 24/30 | 395.228 秒 | 有限制；来源最小化和依赖约束失误 |

观察到的具体边界：

- 四个 harness 都完成了小型代码修复；Codex 用时 40.461 秒，Claude 80.159 秒，Qwen Code 168.966 秒，OpenCode 282.878 秒。
- Codex、Claude、Qwen Code 的证据题均通过。OpenCode 为一个结论引用了非必要历史来源，并在提示注入结论中漏引对应证据。
- 只有 Codex 完整满足规划题。Claude 选出了正确最优计划，但把一个预算排除项误标为依赖未选；Qwen Code 的选择、总价值和最优性不一致；OpenCode 选择了依赖未满足的任务。

## 评分修正说明

首次执行后发现证据题 verifier 把自然语言等价值和自定义 `reason_codes` 错判为失败。最终 verifier 只要求语义正确、理由非空、来源最小且输入/敏感值安全，并接受 `false` 与 `"no"` 这类等价否定。

模型产物没有为修正评分而重写或重跑；Claude、Qwen Code、OpenCode 使用原产物重新评分，Codex 使用修复 AICLI 通道后的正式产物重新评分。最终 fingerprint 绑定修正后的 verifier。旧的 5/11 和 Codex 通道秒退结果均为测试设施缺陷，不是模型能力证据。

## 建议委派边界

适合默认交给 Codex CLI + 本地 35B：

- 目标、输入、输出和验收条件明确的代码修复、格式转换、抽取、分类和派生数据生成。
- 可由 schema、hash、计数、测试、约束求解器或独立 verifier 判定的短阶段任务。
- PersonalOS 数据工厂中的隔离批次：canonical raw 在可写根之外，产物保留 lineage、冲突、不确定性、checkpoint 和 receipt。

不应让本地 35B 独立承担：

- 含糊产品策略、跨项目架构、授权边界、公开发布、付费或不可逆主机变更。
- 无独立验收器的 source-of-truth 改写，以及把冲突、缺时区、OCR/ASR 低置信度自行猜成确定事实。
- 医疗、法律、财务等高风险最终判断，或长时间无人看守且无法 checkpoint 的开放式任务。

顶级模型只需从结果端监督：提交有界任务，按 `poll_after_ms` 读取一次紧凑 receipt，检查最终文件和 verifier。失败后由顶级模型决定重试、换 runner、改用云端或亲自接管；工具不自动轮询四个 harness，也不返回 chain-of-thought。

## 尚未覆盖的验收

本报告不覆盖真实海量数据吞吐、数小时恢复、多模态图片质量、LocalOCR/ChineseASR 精度、云端 `qwen3.7-plus` 或 PersonalOS 当前阶段的真实业务 schema。后续只有对应场景进入使用时才做小型专项：

- PersonalOS owner 提供真实阶段合同、匿名/合成边界样本和确定性 verifier，再测一批成功、一批冲突、一批中断恢复。
- 原生视觉、OCR、ASR 分开验收并统一走 LocalGpuBroker；不同时加载本地模型和 OCR/ASR 重型模型。
- 长批次只测 checkpoint、幂等、断点续跑、吞吐和失败隔离，不重复四 harness 通用排名。

本报告只适用于上述版本与模型 digest，不能外推为永久通用智商排名，也不推断未经实测的 Qwen3.7 Plus 表现。
