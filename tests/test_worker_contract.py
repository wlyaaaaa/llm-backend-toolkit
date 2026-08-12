import copy
import json
import tempfile
import unittest
from pathlib import Path

from llm_backend_toolkit.backends import BackendRegistry
from llm_backend_toolkit.jobs import JobStore
from llm_backend_toolkit.worker_contract import (
    LOCAL_ASYNC_WORKER_OBSERVED_BINDING_SCHEMA,
    LocalAsyncWorker,
    WorkerContractError,
)


MODEL_DIGEST = "46c6d39f92e76686e7e3ff0097029fdb7aedbdea5375857acdbdb08b1fd8783a"
PLACEHOLDER_SHA256 = "sha256:" + ("a" * 64)


def _registry() -> BackendRegistry:
    return BackendRegistry.from_dict(
        {
            "schema": "llm-backend-toolkit.backends.v1",
            "default_backend": "local-default",
            "aliases": {},
            "backends": {
                "local-default": {
                    "adapter": "ollama",
                    "model": "qwen-main-v1",
                    "cloud": False,
                    "supports_vision": True,
                    "context_window_tokens": 262144,
                    "agent_routes": {
                        "data_factory": {
                            "runner": "codex-cli",
                            "profile": "codex-ollama-main",
                            "model": "qwen-main-v1",
                            "reasoning_effort": "max",
                            "evidence": {
                                "basis": "synthetic-worker-contract",
                                "live_verified": True,
                                "model_digest": MODEL_DIGEST,
                                "parent_model": "qwen3.6:35b",
                            },
                        }
                    },
                }
            },
        }
    )


class _CancelBridge:
    def __init__(self, *, process_tree_confirmed: bool, gpu_lease_released: bool) -> None:
        self.process_tree_confirmed = process_tree_confirmed
        self.gpu_lease_released = gpu_lease_released
        self.calls: list[dict] = []

    def __call__(self, job_id: str, bridge: dict) -> dict:
        self.calls.append({"job_id": job_id, "bridge": copy.deepcopy(bridge)})
        return {
            "schema": "llm-backend-toolkit.controlled-cancel-cleanup.v1",
            "process_tree": {
                "status": (
                    "confirmed_absent"
                    if self.process_tree_confirmed
                    else "unconfirmed"
                )
            },
            "gpu_lease": {
                "status": "released" if self.gpu_lease_released else "unconfirmed"
            },
        }


def _envelope(workspace: Path) -> dict:
    return {
        "schema": "llm-backend-toolkit.local-async-worker-envelope.v1",
        "task_id": "synthetic-local-worker-task-1",
        "fresh_execution": True,
        "read_roots": [str(workspace)],
        "write_root": str(workspace),
        "expected_artifacts": [{"path": "answer.txt", "kind": "derived"}],
        "acceptance": {
            "deterministic": True,
            "verifier": {"id": "fixture-verifier", "sha256": PLACEHOLDER_SHA256},
        },
        "constraints": {
            "external_effects": "return_to_sol",
            "protected_actions": "return_to_sol",
            "highest_authority": "not_inherited",
            "delegation": "forbidden",
            "sandbox": "workspace-write",
        },
        "request": {
            "backend": "local-default",
            "task": {
                "goal": "Return the fixture marker.",
                "expected_output": {"format": "text"},
            },
            "context": {"mode": "compact", "target_tokens": 4096},
            "reasoning": {"mode": "on"},
            "privacy": {"cloud_allowed": False},
            "execution": {
                "mode": "agent",
                "runner": "data_factory",
                "workspace": str(workspace),
                "policy": "workspace-write",
                "budget": {
                    "idle_timeout_seconds": 3600,
                    "limit_mode": "completion_driven",
                },
            },
        },
        "bindings": {
            "backend_alias": "local-default",
            "artifact_digest": "sha256:" + MODEL_DIGEST,
            "parent_model": "qwen3.6:35b",
            "quantization": "Q4_K_M",
            "tokenizer": "qwen3.6",
            "chat_template": "qwen3.6",
            "harness": "codex-cli",
            "profile": "codex-ollama-main",
            "aicli": {
                "entry_sha256": PLACEHOLDER_SHA256,
                "version": "0.3.2",
            },
            "codex_event_protocol": "aicli.machine-event.v1",
            "serving_engine": "ollama",
            "toolkit_source_sha256": PLACEHOLDER_SHA256,
            "broker": {
                "id": "LocalGpuBroker",
                "lease_id": "fixture-gpu-lease",
                "serialization": "exclusive",
            },
            "context": {
                "total_tokens": 262144,
                "reserved_output_tokens": 32768,
            },
            "reasoning": {"mode": "on", "effort": "max"},
        },
    }


def _observed(handle: dict) -> dict:
    requested = handle["binding_receipt"]["requested"]
    return {
        "schema": LOCAL_ASYNC_WORKER_OBSERVED_BINDING_SCHEMA,
        "executor": {
            "kind": "local_async_job",
            "native_subagent": False,
            "native_agent_type": None,
            "native_lineage": None,
        },
        "backend": dict(requested["backend"]),
        "harness": dict(requested["harness"]),
        "context": {
            **dict(requested["context"]),
            "status": "runtime_proven",
            "runtime_proof": {
                "protocol": "aicli.machine-event.v1",
                "status": "proven",
                "total_tokens": 262144,
                "reserved_output_tokens": 32768,
            },
        },
        "reasoning": dict(requested["reasoning"]),
        "budget": dict(requested["budget"]),
        "serving_engine": requested["serving_engine"],
        "toolkit_source_sha256": requested["toolkit_source_sha256"],
        "broker": {
            **dict(requested["broker"]),
            "lease_status": "released",
        },
        "task": dict(requested["task"]),
        "verifier": dict(requested["verifier"]),
        "sandbox": requested["sandbox"],
        "fallback_used": False,
        "cleanup": {
            "process_tree_confirmed": True,
            "gpu_lease_released": True,
        },
    }


class LocalAsyncWorkerContractTests(unittest.TestCase):
    def _worker(self, root: Path, bridge: _CancelBridge) -> LocalAsyncWorker:
        registry = _registry()
        store = JobStore(
            root / "jobs",
            spawner=lambda *_: None,
            registry=registry,
            cancel_bridge=bridge,
        )
        return LocalAsyncWorker(store, registry=registry)

    def test_start_returns_explicit_non_native_handle_and_pending_runtime_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "staging"
            workspace.mkdir()
            worker = self._worker(root, _CancelBridge(
                process_tree_confirmed=True,
                gpu_lease_released=True,
            ))

            handle = worker.start(_envelope(workspace))

            self.assertEqual("local_async_job", handle["executor"]["kind"])
            self.assertFalse(handle["executor"]["native_subagent"])
            self.assertNotIn("agent_type", handle["executor"])
            self.assertEqual("QUEUED", handle["worker_status"])
            self.assertEqual("configured_unverified", handle["routing_status"])
            self.assertEqual("pending", handle["binding_receipt"]["observed"]["status"])
            self.assertEqual(
                "completion_driven",
                handle["binding_receipt"]["requested"]["budget"]["limit_mode"],
            )
            state = worker.store.get(handle["job_id"])
            self.assertFalse(state["cacheable"])
            self.assertEqual(
                "local_async_job",
                state["controlled_cancel"]["executor_kind"],
            )

    def test_start_fails_closed_when_required_binding_is_missing(self):
        required_paths = (
            ("bindings", "artifact_digest"),
            ("bindings", "chat_template"),
            ("bindings", "harness"),
            ("bindings", "profile"),
            ("bindings", "broker"),
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "staging"
            workspace.mkdir()
            worker = self._worker(root, _CancelBridge(
                process_tree_confirmed=True,
                gpu_lease_released=True,
            ))
            for first, second in required_paths:
                with self.subTest(binding=f"{first}.{second}"):
                    envelope = _envelope(workspace)
                    envelope[first].pop(second)
                    with self.assertRaises(WorkerContractError):
                        worker.start(envelope)

    def test_wait_is_one_bounded_read_and_result_requires_terminal_complete_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "staging"
            workspace.mkdir()
            worker = self._worker(root, _CancelBridge(
                process_tree_confirmed=True,
                gpu_lease_released=True,
            ))
            handle = worker.start(_envelope(workspace))

            not_due = worker.wait(handle, deadline="9999-01-01T00:00:00Z")
            due_handle = dict(handle)
            due_handle["recommended_check_utc"] = "2000-01-01T00:00:00Z"
            waiting = worker.wait(due_handle, deadline="9999-01-01T00:00:00Z")
            before_terminal = worker.result(handle)

            self.assertEqual("worker_wait_not_due", not_due["error"]["category"])
            self.assertEqual("QUEUED", waiting["worker_status"])
            self.assertEqual("worker_not_terminal", before_terminal["error"]["category"])
            worker.store.complete(
                handle["job_id"],
                {
                    "status": "ok",
                    "output": "fixture-result",
                    "execution_receipt": {
                        "local_async_worker_observed": _observed(handle)
                    },
                },
            )
            completed = worker.result(handle)

            self.assertEqual("ok", completed["status"])
            self.assertEqual("COMPLETED", completed["worker_status"])
            self.assertEqual("verified", completed["binding_receipt"]["verification"]["status"])
            self.assertFalse(completed["binding_receipt"]["observed"]["fallback_used"])
            self.assertEqual(
                "verified",
                worker.store.read_worker_contract(handle["job_id"])["verification"]["status"],
            )

    def test_cancel_is_terminal_only_after_process_tree_and_gpu_cleanup_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "staging"
            workspace.mkdir()
            bridge = _CancelBridge(
                process_tree_confirmed=False,
                gpu_lease_released=False,
            )
            worker = self._worker(root, bridge)
            handle = worker.start(_envelope(workspace))

            unconfirmed = worker.cancel(handle)

            self.assertEqual("blocked", unconfirmed["status"])
            self.assertEqual("cleanup_unconfirmed", unconfirmed["worker_status"])
            self.assertEqual(
                "cancellation_requested",
                worker.store.get(handle["job_id"])["job_status"],
            )
            self.assertEqual(1, len(bridge.calls))
            worker.store.complete(
                handle["job_id"],
                {"status": "ok", "output": "must-not-terminalize-cancel"},
            )
            self.assertEqual(
                "cancellation_requested",
                worker.store.get(handle["job_id"])["job_status"],
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "staging"
            workspace.mkdir()
            bridge = _CancelBridge(
                process_tree_confirmed=True,
                gpu_lease_released=True,
            )
            worker = self._worker(root, bridge)
            handle = worker.start(_envelope(workspace))

            cancelled = worker.cancel(handle)

            self.assertEqual("ok", cancelled["status"])
            self.assertEqual("CANCELLED", cancelled["worker_status"])
            self.assertEqual("cancelled", worker.store.get(handle["job_id"])["job_status"])

    def test_terminal_result_fails_closed_without_runtime_context_and_observed_bindings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "staging"
            workspace.mkdir()
            worker = self._worker(root, _CancelBridge(
                process_tree_confirmed=True,
                gpu_lease_released=True,
            ))
            handle = worker.start(_envelope(workspace))
            worker.store.complete(
                handle["job_id"],
                {"status": "ok", "output": "unbound-result"},
            )

            blocked = worker.result(handle)

            self.assertEqual("blocked", blocked["status"])
            self.assertEqual(
                "local_worker_binding_incomplete",
                blocked["error"]["category"],
            )
            self.assertNotIn("result", blocked)

    def test_request_schema_exposes_budget_limit_mode_and_worker_schema_is_public(self):
        root = Path(__file__).resolve().parents[1]
        request_schema = json.loads(
            (root / "schemas" / "request.schema.json").read_text(encoding="utf-8")
        )
        worker_schema = json.loads(
            (root / "schemas" / "worker-contract.schema.json").read_text(encoding="utf-8")
        )

        budget = request_schema["properties"]["execution"]["properties"]["budget"]
        self.assertEqual(
            ["completion_driven", "bounded", "watchdog_only"],
            budget["properties"]["limit_mode"]["enum"],
        )
        self.assertEqual(
            "completion_driven",
            budget["properties"]["limit_mode"]["default"],
        )
        self.assertIn("bindings", worker_schema["required"])
        self.assertIn("constraints", worker_schema["required"])

    def test_completion_driven_idle_zero_is_preserved_in_worker_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "staging"
            workspace.mkdir()
            envelope = _envelope(workspace)
            envelope["request"]["execution"]["budget"]["idle_timeout_seconds"] = 0
            worker = self._worker(
                root,
                _CancelBridge(process_tree_confirmed=True, gpu_lease_released=True),
            )

            handle = worker.start(envelope)

        budget = handle["binding_receipt"]["requested"]["budget"]
        self.assertEqual("completion_driven", budget["limit_mode"])
        self.assertEqual(0, budget["idle_timeout_seconds"])
        self.assertIsNone(budget["timeout_seconds"])
        self.assertIsNone(budget["max_steps"])
        self.assertIsNone(budget["max_tool_calls"])


if __name__ == "__main__":
    unittest.main()
