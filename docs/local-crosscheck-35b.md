# 35B 本地交叉验证角色

`local-crosscheck-35b` 为顶级模型提供一个显式的不同模型本地第二意见。它不是统计独立证据、主 backend 或错误 fallback。

## 注册表合同

| 字段 | 值 |
| --- | --- |
| backend ID | `local-crosscheck-35b` |
| request selector alias | `qwen-crosscheck-35b` |
| Ollama/AICLI model | `qwen3.6-35b:256k` |
| parent model | Qwen3.6 35B |
| adapter | `ollama` |
| cloud | `false` |
| vision | `true` |
| context | `262144` |
| routing role | `crosscheck_only` |
| default reasoning | `on` |
| endpoint | LocalGpuBroker `http://127.0.0.1:32100` |
| Agent route | `codex-cli` / `codex-ollama-review`，当前 `unverified/configured` |

当前 35B backend 交给 Ollama/AICLI 的模型名是 `qwen3.6-35b:256k`。为保持默认路线兼容，注册表的请求侧 selector alias `qwen-main-v1` 仍指向 Qwen3.8 27B `local-default`；请求必须显式写 `local-crosscheck-35b` 或 `qwen-crosscheck-35b` 才会选择 35B。下文验收记录中的 `qwen-main-v1` 是当时的历史绑定。

当前注册表的 direct 参数为：

```json
{
  "temperature": 0.6,
  "top_p": 0.95,
  "top_k": 20,
  "min_p": 0.0,
  "presence_penalty": 1.5,
  "repeat_penalty": 1.0,
  "num_ctx": 262144,
  "num_predict": 32768
}
```

注册表会拒绝把 `routing_role=crosscheck_only` 的 backend 设为 `default_backend`，也禁止它参与 fallback。Qwen3.6 27B 的 `qwen-review-v1` / `local-crosscheck-27b` 已退役并从 live selector/backend surface 移除。35B direct 路径可显式调用；精确 `codex-cli` route 保留 Profile/模型绑定，但旧回执不迁移。当前注册表把 route 标为 `configured`、`live_verified=false`：配置可供尝试，不代表已经取得当前模型的真实 Agent 验收。后续模型或 Profile 变动若使状态变为 `pending_reacceptance`，则按注册表约束失败关闭。

## 2026-08-21 Agent 重新验收

本次唯一一次获授权的 35B Agent Live 在 `2026-08-21T19:28:51.9871927Z` 执行。注册表只保留下列公开安全、可核对的 receipt 标量，不保存 prompt、result、日志、线程或会话正文：

| 证据 | 值 |
| --- | --- |
| capability outcome | `attempted_failed` |
| receipt schema | `aicli.agent.acceptance-receipt.v1` |
| AICLI / profile fingerprint | `0.3.12` / `a8c3eacc18e1b552481524c4145d6156f6b34a86840c032ecc39eaedd65a5421` |
| requested binding | `qwen-main-v1` / `aicli_ollama_review` / `responses` / `max` / `danger-full-access` |
| recovery | run `2ecc37e425fc4e31ab476ea5d88b5521`；`failed_closed`；attempts `1`；resume count `0` |
| failure reason | `capture_exception_before_verified_receipt` |
| agent | exit `4`；`aicli.recovery.capture_exception`；steps `0`；tool calls `0`；cleanup confirmed `false` |
| runtime / verifier | 没有 verified runtime identity；verifier failed |

这次历史尝试不是 PASS，也没有可继承的 verified model identity。当前注册表的配置同步已登记新模型 digest，但 `capability_acceptance_state=configured`、`live_verified=false`、`evidence_state=unverified`，不能把 digest 当作真实 Agent 验收。本次失败后没有再次执行 Live。

## 何时选择

仅在不同模型的第二意见有预期信息增益、且顶级模型有办法比较两个公开结果时显式选择，例如：

- 对关键提取结果做不同模型结构的复核；
- 对边界明确的判断生成第二份候选，再由顶级模型核对证据；
- 在 verifier 存在时检查交叉验证结果是否稳定。

不要把它用于省略 backend 的常规任务、无 verifier 的最终裁决，或把一次失败静默重提给另一个模型。agent 路径待验收期间，不得用 direct 成功冒充 agent capability。

## Direct 请求

请求必须显式写 backend；省略 `reasoning` 时使用注册表的 `on`：

```json
{
  "backend": "local-crosscheck-35b",
  "task": {
    "goal": "用不同模型复核已给结论",
    "instructions": ["只依据提供的材料", "列出不一致及其证据"],
    "expected_output": {
      "format": "json",
      "required_keys": ["verdict", "differences", "evidence"]
    }
  },
  "context": {
    "mode": "compact"
  },
  "execution": {
    "mode": "direct"
  },
  "privacy": {
    "cloud_allowed": false
  }
}
```

正常入口仍是异步 `submit` / `job`。是否发起第二次调用、如何比较两份结果，以及失败后是否改投，都由顶级模型显式决定。
