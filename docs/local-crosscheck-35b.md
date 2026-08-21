# 35B 本地交叉验证角色

`local-crosscheck-35b` 为顶级模型提供一个显式的不同模型本地第二意见。它不是统计独立证据、主 backend 或错误 fallback。

## 注册表合同

| 字段 | 值 |
| --- | --- |
| backend ID | `local-crosscheck-35b` |
| request selector alias | `qwen-crosscheck-35b` |
| Ollama/AICLI model | `qwen-main-v1` |
| parent model | Qwen3.6 35B |
| adapter | `ollama` |
| cloud | `false` |
| vision | `true` |
| context | `262144` |
| routing role | `crosscheck_only` |
| default reasoning | `on` |
| endpoint | LocalGpuBroker `http://127.0.0.1:32100` |
| Agent route | `codex-cli` / `codex-ollama-review`，当前 `unverified/pending_reacceptance` |

这里的 `qwen-main-v1` 是 35B backend 交给 Ollama/AICLI 的模型名，不是请求侧 selector。为保持默认路线兼容，注册表的 selector alias `qwen-main-v1` 仍指向 Qwen3.8 27B `local-default`；请求必须显式写 `local-crosscheck-35b` 或 `qwen-crosscheck-35b` 才会选择 35B。

冻结的 direct 参数为：

```json
{
  "temperature": 0.6,
  "top_p": 0.95,
  "top_k": 20,
  "min_p": 0.0,
  "presence_penalty": 0.0,
  "repeat_penalty": 1.0,
  "num_ctx": 262144,
  "num_predict": 32768
}
```

注册表会拒绝把 `routing_role=crosscheck_only` 的 backend 设为 `default_backend`，也禁止它参与 fallback。Qwen3.6 27B 的 `qwen-review-v1` / `local-crosscheck-27b` 已退役并从 live selector/backend surface 移除。35B direct 路径可显式调用；精确 `codex-cli` route 保留 Profile/模型绑定，但旧 27B 回执不迁移。只要 evidence 仍是 `pending_reacceptance`，agent 请求就在 provider 生成或 runner 调用前以 `route_evidence_pending_reacceptance` 失败关闭。只有取得并登记新的精确 AICLI acceptance 后，agent 路径才可执行。

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
