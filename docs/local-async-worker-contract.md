# 本地异步 Worker Contract v1

`LocalAsyncWorker` 为顶级 Sol 提供 `start`、`wait`、`cancel`、`result` 四个操作，
但它始终是 Toolkit 的 `local_async_job`：

```json
{"kind":"local_async_job","native_subagent":false}
```

它没有 native `agent_type`、parent thread、spawn receipt 或 native lineage，不能满足
managed worker 的早期 dispatch 要求，也不得称为 Luna/Terra child。目标、授权、高风险
判断、外部副作用和最终验收都留给 Sol。

## 使用边界

公共 envelope 见 [worker-contract.schema.json](../schemas/worker-contract.schema.json)。
Python API 为：

```python
from llm_backend_toolkit import LocalAsyncWorker
from llm_backend_toolkit.jobs import JobStore

worker = LocalAsyncWorker(JobStore(state_dir, cancel_bridge=controlled_bridge))
handle = worker.start(envelope)
status = worker.wait(handle, deadline=handle["recommended_check_utc"])
cancellation = worker.cancel(handle)
result = worker.result(handle)
```

`controlled_bridge` 是由宿主注入的受控 JobStore 依赖，不从 request 读取命令、URL 或
callback。它必须返回 `process_tree.confirmed_absent` 与
`gpu_lease.released`；少任何一项，`cancel` 保持
`cleanup_unconfirmed`，不会把 job 伪造为 `CANCELLED`。

`start` 强制 `fresh_execution=true`、`force=true` 且禁止 cache key。v1 封闭接受两类：

- legacy `local-default → data_factory → codex-cli`；
- 运行时重新解析为 exact non-cloud `routing_role=benchmark_only` 的临时 backend，且只走
  `codex-cli`。该类显式 `fallback_used=false`、network forbidden、search disabled，并绑定
  model/profile/AICLI/上下文 digest；其他 backend、runner、默认选择或 fallback 全部拒绝。

两类都使用隔离的 `workspace-write` 根作为唯一 read/write root。legacy 路线预算默认是
`limit_mode=watchdog_only`、`timeout_seconds=900`，不设置 step/tool-call 上限；可由调用方
显式调整 wall-clock watchdog。`completion_driven` 只保留为旧合同输入，当前 AICLI 未声明
可续期 idle lease 时会在模型启动前失败关闭，绝不静默降级。`bounded` 仍需调用方显式
选择并声明硬上限；watchdog-only 必须省略 `idle_timeout_seconds`、`max_steps` 和
`max_tool_calls`。benchmark-only
路线进一步固定为 7200 秒 watchdog-only，no fallback。禁止向本地 worker 继承最高权限
或再次委派。

## Binding receipt 与 route 门禁

`start` 会先校验并持久化 requested binding：backend alias/model/digest、quantization、
tokenizer/chat-template、Codex harness、AICLI entry/version/profile/event protocol、
serving engine、Toolkit source、exclusive LocalGpuBroker lease、sandbox、task/workspace
digest、deterministic verifier、reasoning 与 effective budget。缺少任一项即失败关闭；
requested/observed receipt 必须保持同一 `limit_mode` 与对应 watchdog/硬上限语义。

初始 receipt 固定为 `configured_unverified`。即使 registry 配置为 262,144 context 与
32,768 reserved output，仍只有实际 AICLI machine-event runtime proof 同时报告这两个值
后才可变为 `eligible_after_runtime_proof`。`result` 只在终态读取结果；若缺少完整
`execution_receipt.local_async_worker_observed` 或任意 requested/observed 不一致，返回
`local_worker_binding_incomplete`，不会交付未绑定结果。

当前 v1 只实现合同、持久 receipt 和注入式取消桥，不运行模型/GPU，也不把 registry
配置或历史验收冒充 runtime proof。因此在真实 Codex/AICLI/LocalGpuBroker 适配器提交
完整 observed binding 之前，路由状态保持 `configured_unverified` / `not_ready`。

## 生命周期

```text
VALIDATING -> QUEUED -> GPU_LEASED -> RUNNING
RUNNING -> COMPLETED | PARTIAL | BLOCKED | FAILED | TIMED_OUT
QUEUED/RUNNING -> CANCELLATION_REQUESTED -> CANCELLED
```

`wait` 是到达 `recommended_check_utc` 后的一次有界状态读取，不执行循环轮询。
`TIMED_OUT` 或 `CANCELLED` 同样要求完整的 process-tree 与 GPU lease cleanup receipt；
未确认时保持 `cleanup_unconfirmed`，Sol 必须显式决定等待、重新提交、改路由或自行完成，
不得 silent fallback 到较小本地模型、Luna、Terra 或 Sol。
