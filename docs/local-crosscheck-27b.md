# 27B 本地交叉验证角色

`local-crosscheck-27b` 为顶级模型提供一个显式的不同模型本地第二意见。它不是统计独立证据、主 backend、Agent route 或错误 fallback。

## 注册表合同

| 字段 | 值 |
| --- | --- |
| backend ID | `local-crosscheck-27b` |
| model alias | `qwen-review-v1` |
| parent model | Qwen3.6 27B |
| adapter | `ollama` |
| cloud | `false` |
| vision | `true` |
| context | `131072` |
| routing role | `crosscheck_only` |
| default reasoning | `on` |
| endpoint | LocalGpuBroker `http://127.0.0.1:32100` |
| Agent routes | 无 |

冻结的 direct 参数为：

```json
{
  "temperature": 0.6,
  "top_p": 0.95,
  "top_k": 20,
  "min_p": 0.0,
  "presence_penalty": 0.0,
  "repeat_penalty": 1.0,
  "num_ctx": 131072,
  "num_predict": 32768
}
```

注册表会拒绝把 `routing_role=crosscheck_only` 的 backend 设为 `default_backend`，也会拒绝为它配置非空 `agent_routes`。因此省略 `backend` 始终解析到当前 Qwen3.8 27B 的 `local-default`；`local-crosscheck-27b` 自身是独立的 Qwen3.6 27B 第二意见，`execution.mode=agent` 也不会借此模型运行。Toolkit 没有自动 fallback；一次失败只返回当前 backend 的错误和裁决选项。

## 何时选择

仅在不同模型的第二意见有预期信息增益、且顶级模型有办法比较两个公开结果时显式选择，例如：

- 对关键提取结果做不同模型结构的复核；
- 对边界明确的判断生成第二份候选，再由顶级模型核对证据；
- 在 verifier 存在时检查交叉验证结果是否稳定。

不要把它用于省略 backend 的常规任务、Agent 文件或命令循环、无 verifier 的最终裁决，或把一次失败静默重提给另一个模型。benchmark 候选注册表把它标记为 `crosscheck_available_not_primary`：可用于交叉验证，但不推翻当前 Qwen3.8 27B 主默认。

## Direct 请求

请求必须显式写 backend；省略 `reasoning` 时使用注册表的 `on`：

```json
{
  "backend": "local-crosscheck-27b",
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
