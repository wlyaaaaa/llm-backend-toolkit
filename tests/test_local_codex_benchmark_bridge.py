import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_backend_toolkit.acceptance_routes import (
    build_local_codex_benchmark_registry,
)
from llm_backend_toolkit.agent_runners import (
    AgentResponse,
    AgentRunnerError,
    AiCliProfileRunner,
)
from llm_backend_toolkit.errors import ToolError
from llm_backend_toolkit.jobs import JobStore
from llm_backend_toolkit.toolkit import Toolkit
from llm_backend_toolkit.worker_contract import LocalAsyncWorker, WorkerContractError


ROOT = Path(__file__).resolve().parents[1]
SHA = "sha256:" + ("a" * 64)
ALIAS_DIGEST = "90a516a548f99c9a68f9915620e00bf1a800a507a9a2c86236a1354ab08e3195"
PARENT_DIGEST = "a50eda8ed977ab48a12431878896b27ffd5cef552c17af3317d9623b939a7f1e"
MODEL_LAYER_DIGEST = "83c54730a5fea8a0958598c01617c1419c431e93b33bacf980b49a420c798926"
PARAMETERS_DIGEST = "b8ed70126e3123aa8f88bbca5dcc0dac4fba772a5c28da9353036ab07c722a73"
PROFILE_FINGERPRINT = "c60cf0754075f48710dfd8b2bfe64aa9a8a123583ed03112eee7f02640ef1f49"
AICLI_DIGEST = "53acdd7b6312c52b679810f16c1b3c59c805ae5fea3c9e9be8bd7002ea6412aa"
RAW_AICLI_ENTRY_DIGEST = "00cf5e0cf8c1ecc6742a8501656efe51d2cf9bfca2d3c222a8b5eb566370249f"
CHILD_PROCESS_DIGEST = "29d78b7dc86ccb1f4be98c7742cdf3fd14e6eae48631fe2b1aa7d150b697acee"
LAUNCH_PLAN_DIGEST = "5053f0a1c8d3025e3bd092396aa0d290cff8256e288ba99a7fa50bf5fff68fcd"
CODEX_BRIDGE_DIGEST = "ea9e66e8453a9f0120e9e68a57782a264d7f7e00d3b42004b86f2bca798e45b2"


def _canonical_digest(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _network_proof(*, status="enforced", runtime_identity=None):
    source_contract = {
        "schema": "llm-backend-toolkit.aicli-network-source-contract.v1",
        "aicli_version": "0.3.3",
        "source_bundle_sha256": "sha256:" + AICLI_DIGEST,
        "entry_sha256": "sha256:" + RAW_AICLI_ENTRY_DIGEST,
        "component_sha256": {
            "Private/ChildProcess.ps1": "sha256:" + CHILD_PROCESS_DIGEST,
            "Private/LaunchPlan.ps1": "sha256:" + LAUNCH_PLAN_DIGEST,
            "Support/CodexAppServerBridge.ps1": "sha256:" + CODEX_BRIDGE_DIGEST,
        },
        "outer_launcher_network_flag": "--sandbox-state-disable-network",
        "outer_launcher_applies_when_policy_is_not": "danger-full-access",
        "runtime_sandbox_boundary": "outer-codex",
        "runtime_sandbox_type": "externalSandbox",
        "turn_network_access": "restricted",
        "terminal_event": "turn.completed",
    }
    request_contract = {
        "engine": "codex",
        "profile": "codex-ollama-review",
        "profile_fingerprint": PROFILE_FINGERPRINT,
        "model": "qwen-review-v1",
        "provider_id": "aicli_ollama_review",
        "sandbox_policy": "workspace-write",
        "network": "forbidden",
        "search": "disabled",
        "runtime_permission": {
            "approval_policy": "never",
            "requested_policy": "workspace-write",
            "sandbox_boundary": "outer-codex",
            "sandbox_type": "externalSandbox",
        },
    }
    proof = {
        "policy": "forbidden",
        "search": "disabled",
        "status": status,
        "evidence_kind": "aicli-runtime-bound-source-contract",
        "source_contract": source_contract,
        "source_contract_sha256": _canonical_digest(source_contract),
        "request_contract": request_contract,
        "request_contract_sha256": _canonical_digest(request_contract),
        "source_bundle_sha256": "sha256:" + AICLI_DIGEST,
        "entry_sha256": "sha256:" + RAW_AICLI_ENTRY_DIGEST,
    }
    if status == "enforced":
        runtime_identity = runtime_identity or _runtime_identity()
        process_receipt = {
            "profile_id": "codex-ollama-review",
            "model": "qwen-review-v1",
            "model_provider": "aicli_ollama_review",
            "sandbox_policy": "workspace-write",
            "runtime_identity": runtime_identity,
            "outer_exit_code": 0,
            "child_exit_code": 0,
            "timed_out": False,
            "budget_mode": "watchdog-only",
            "limit_enforcement": {
                "timeout": "hard",
                "maxSteps": "not-configured",
                "maxToolCalls": "not-configured",
            },
            "limit_usage": {
                "cleanupConfirmed": True,
                "cleanupMethod": "job-object-tree-confirmed",
            },
            "limit_hit": None,
            "event_projection": "codex-public-v1",
            "machine_event_projection": "aicli.machine-event.v1",
            "machine_event_status": "ok",
            "machine_event_count": 4,
        }
        machine_event_receipt = {
            "schema": "llm-backend-toolkit.aicli-machine-event-receipt.v1",
            "projection": "aicli.machine-event.v1",
            "status": "ok",
            "count": 4,
            "sequences": [1, 2, 3, 4],
            "kinds": [
                "thread.started",
                "runtime.identity",
                "output.completed",
                "turn.completed",
            ],
            "runtime_identity": {
                "model": "qwen-review-v1",
                "provider_id": "aicli_ollama_review",
                "cli_version": "0.147.0",
                "approval_policy": "never",
                "sandbox_policy": "workspace-write",
                "sandbox_boundary": "outer-codex",
                "sandbox_type": "externalSandbox",
            },
            "terminal": {
                "kind": "turn.completed",
                "status": "completed",
                "sequence": 4,
            },
        }
        proof.update(
            {
                "enforcement": "network-denied",
                "sandbox_policy": "workspace-write",
                "sandbox_boundary": "outer-codex",
                "sandbox_type": "externalSandbox",
                "runtime_identity_sha256": _canonical_digest(runtime_identity),
                "process_receipt": process_receipt,
                "process_receipt_sha256": _canonical_digest(process_receipt),
                "machine_event_receipt": machine_event_receipt,
                "machine_event_stream_sha256": _canonical_digest(
                    machine_event_receipt
                ),
                "terminal_event": "turn.completed",
                "terminal_sequence": 4,
                "machine_event_count": 4,
                "cleanup_confirmed": True,
                "cleanup_method": "job-object-tree-confirmed",
            }
        )
    return proof


def _audited_source_receipt():
    return {
        "entry_sha256": "sha256:" + AICLI_DIGEST,
        "raw_entry_sha256": "sha256:" + RAW_AICLI_ENTRY_DIGEST,
        "fingerprint_scope": "module-source-bundle-v1",
        "file_count": 32,
        "component_sha256": {
            "Private/ChildProcess.ps1": "sha256:" + CHILD_PROCESS_DIGEST,
            "Private/LaunchPlan.ps1": "sha256:" + LAUNCH_PLAN_DIGEST,
            "Support/CodexAppServerBridge.ps1": "sha256:" + CODEX_BRIDGE_DIGEST,
        },
    }


def _runtime_identity():
    return {
        "model": "qwen-review-v1",
        "model_provider": "aicli_ollama_review",
        "cli_version": "0.147.0",
        "permission": {
            "approval_policy": "never",
            "requested_policy": "workspace-write",
            "sandbox_boundary": "outer-codex",
            "sandbox_type": "externalSandbox",
            "permission_profile": ":workspace-write",
        },
    }


def _machine_events(*, include_terminal=True):
    events = [
        {
            "schema": "aicli.machine-event.v1",
            "sequence": 1,
            "kind": "thread.started",
            "thread_id": "thread-fixture",
        },
        {
            "schema": "aicli.machine-event.v1",
            "sequence": 2,
            "kind": "runtime.identity",
            "model": "qwen-review-v1",
            "provider_id": "aicli_ollama_review",
            "cli_version": "0.147.0",
            "approval_policy": "never",
            "sandbox_policy": "workspace-write",
            "sandbox_boundary": "outer-codex",
            "sandbox_type": "externalSandbox",
        },
        {
            "schema": "aicli.machine-event.v1",
            "sequence": 3,
            "kind": "output.completed",
            "status": "completed",
        },
    ]
    if include_terminal:
        events.append(
            {
                "schema": "aicli.machine-event.v1",
                "sequence": 4,
                "kind": "turn.completed",
                "status": "completed",
            }
        )
    return events


def _aicli_run_envelope(*, include_terminal=True, cleanup_confirmed=True):
    events = _machine_events(include_terminal=include_terminal)
    child_stdout = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-fixture"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "done"},
                }
            ),
        )
    )
    return {
        "run": {
            "profileId": "codex-ollama-review",
            "model": "qwen-review-v1",
            "modelProvider": "aicli_ollama_review",
            "sandboxPolicy": "workspace-write",
            "runtimeIdentity": _runtime_identity(),
            "exitCode": 0,
            "stdout": child_stdout,
            "stderr": "",
            "timedOut": False,
            "durationMs": 5,
            "budgetMode": "watchdog-only",
            "limitEnforcement": {
                "timeout": "hard",
                "idleTimeout": "not-configured",
                "maxSteps": "not-configured",
                "maxToolCalls": "not-configured",
            },
            "limitUsage": {
                "steps": 2,
                "toolCalls": 0,
                "eventsSeen": 4,
                "protocol": "codex-jsonl",
                "stepDefinition": "distinct-non-output-thread-item-v2",
                "cleanupConfirmed": cleanup_confirmed,
                "cleanupMethod": (
                    "job-object-tree-confirmed" if cleanup_confirmed else ""
                ),
            },
            "limitHit": None,
            "eventProjection": "codex-public-v1",
            "machineEventProjection": "aicli.machine-event.v1",
            "machineEventStatus": "ok",
            "machineEventCount": len(events),
        }
    }


def _registry():
    source = json.loads(
        (ROOT / "src" / "llm_backend_toolkit" / "default_backends.json").read_text(
            encoding="utf-8"
        )
    )
    return build_local_codex_benchmark_registry(
        source,
        backend_id="cacb-local-27b-formal-v10",
        provider_model="qwen-review-v1",
        route_model="qwen-review-v1",
        profile="codex-ollama-review",
        provider_id="aicli_ollama_review",
        wire="responses",
        context_window_tokens=131072,
        reserved_output_tokens=8192,
        model_digest=ALIAS_DIGEST,
        parent_model="qwen3.6:27b",
        parent_model_digest=PARENT_DIGEST,
        model_layer_digest=MODEL_LAYER_DIGEST,
        parameters_digest=PARAMETERS_DIGEST,
        quantization="Q4_K_M",
        profile_fingerprint=PROFILE_FINGERPRINT,
        aicli_entry_sha256=AICLI_DIGEST,
        aicli_version="0.3.3",
        codex_cli_version="0.147.0",
    )


def _envelope(workspace: Path) -> dict:
    return {
        "schema": "llm-backend-toolkit.local-async-worker-envelope.v1",
        "task_id": "cacb-local-27b-formal-v10-run-1",
        "fresh_execution": True,
        "read_roots": [str(workspace)],
        "write_root": str(workspace),
        "expected_artifacts": [{"path": "answer.txt", "kind": "derived"}],
        "acceptance": {
            "deterministic": True,
            "verifier": {"id": "cacb-formal-verifier", "sha256": SHA},
        },
        "constraints": {
            "external_effects": "return_to_sol",
            "protected_actions": "return_to_sol",
            "highest_authority": "not_inherited",
            "delegation": "forbidden",
            "sandbox": "workspace-write",
            "network": "forbidden",
            "search": "disabled",
        },
        "request": {
            "backend": "cacb-local-27b-formal-v10",
            "task": {
                "goal": "Complete the frozen local CACB episode.",
                "expected_output": {"format": "text"},
            },
            "context": {"mode": "compact", "target_tokens": 122880},
            "reasoning": {"mode": "on"},
            "privacy": {"cloud_allowed": False},
            "execution": {
                "mode": "agent",
                "runner": "codex-cli",
                "workspace": str(workspace),
                "policy": "workspace-write",
                "budget": {
                    "limit_mode": "watchdog_only",
                    "timeout_seconds": 7200,
                },
            },
        },
        "bindings": {
            "backend_alias": "cacb-local-27b-formal-v10",
            "fallback_used": False,
            "artifact_digest": "sha256:" + ALIAS_DIGEST,
            "parent_model": "qwen3.6:27b",
            "parent_model_digest": "sha256:" + PARENT_DIGEST,
            "model_layer_digest": "sha256:" + MODEL_LAYER_DIGEST,
            "parameters_digest": "sha256:" + PARAMETERS_DIGEST,
            "quantization": "Q4_K_M",
            "tokenizer": "qwen3.6",
            "chat_template": "qwen3.6",
            "harness": "codex-cli",
            "profile": "codex-ollama-review",
            "profile_fingerprint": PROFILE_FINGERPRINT,
            "provider_id": "aicli_ollama_review",
            "wire": "responses",
            "aicli": {
                "entry_sha256": "sha256:" + AICLI_DIGEST,
                "version": "0.3.3",
                "codex_cli_version": "0.147.0",
            },
            "codex_event_protocol": "aicli.machine-event.v1",
            "serving_engine": "ollama",
            "toolkit_source_sha256": SHA,
            "broker": {
                "id": "LocalGpuBroker",
                "lease_id": "cacb-local-27b-formal-v10-run-1",
                "serialization": "exclusive",
            },
            "context": {
                "total_tokens": 131072,
                "reserved_output_tokens": 8192,
            },
            "reasoning": {"mode": "on", "effort": "max"},
        },
    }


class _CancelBridge:
    def __call__(self, job_id, bridge):
        del job_id, bridge
        return {
            "schema": "llm-backend-toolkit.controlled-cancel-cleanup.v1",
            "process_tree": {"status": "confirmed_absent"},
            "gpu_lease": {"status": "released"},
        }


class _Provider:
    cloud = False
    supports_vision = False

    def __init__(self):
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return {
            "provider": "qwen-review-v1",
            "cloud": False,
            "broker": {"ok": True, "lease": None, "active_ollama_requests": 0},
            "model": {
                "parent_model": "qwen3.6:27b",
                "quantization": "Q4_K_M",
                "digest": ALIAS_DIGEST,
                # /api/show model_info may expose the architecture maximum;
                # the alias parameter layer is the serving-window authority.
                "context_length": 262144,
            },
            "runtime": {"ollama_version": "fixture"},
            "live_call_performed": False,
        }


class _MissingEvidenceProvider(_Provider):
    def status(self):
        result = super().status()
        result["model"].pop("digest")
        return result


class _MustNotRun:
    def invoke(self, prompt, execution):
        del prompt, execution
        raise AssertionError("benchmark runner started without current provider evidence")


class _NetworkProofIncomplete:
    def invoke(self, prompt, execution):
        del prompt, execution
        raise AgentRunnerError(
            ToolError(
                category="agent_network_proof_incomplete",
                summary="runtime-bound network proof was incomplete",
                retryable=False,
            ),
            {
                "aicli_preflight": {
                    "network_proof": _network_proof(status="incomplete_postrun")
                },
                "benchmark_qualification": {
                    "state": "infra-invalid",
                    "scored": False,
                    "ranking_eligible": False,
                },
            },
        )


class _Runner:
    def invoke(self, prompt, execution):
        del prompt
        assert execution["provider_id"] == "aicli_ollama_review"
        assert execution["codex_cli_version"] == "0.147.0"
        assert execution["aicli_entry_sha256"] == AICLI_DIGEST
        assert execution["aicli_version"] == "0.3.3"
        assert execution["profile_fingerprint"] == PROFILE_FINGERPRINT
        assert execution["require_network_proof"] is True
        runtime_identity = {
            "model": "qwen-review-v1",
            "model_provider": "aicli_ollama_review",
            "cli_version": "0.147.0",
            "permission": {
                "approval_policy": "never",
                "requested_policy": "workspace-write",
                "sandbox_boundary": "outer-codex",
                "sandbox_type": "externalSandbox",
                "permission_profile": ":workspace-write",
            },
        }
        network_proof = _network_proof()
        network_proof["runtime_identity_sha256"] = _canonical_digest(runtime_identity)
        return AgentResponse(
            content="done",
            runner="codex-cli",
            model="qwen-review-v1",
            exit_code=0,
            duration_ms=100,
            stop_reason="completed",
            limit_enforcement={
                "timeout": "hard",
                "maxSteps": "not-configured",
                "maxToolCalls": "not-configured",
            },
            limit_usage={
                "cleanup_confirmed": True,
                "cleanup_method": "job-object-tree-confirmed",
            },
            usage={
                "current_context_tokens": 4096,
                # Codex reports its catalog-effective window. It is not the
                # Ollama runtime context proof for this formal arm.
                "context_window_tokens": 249036,
            },
            profile_id="codex-ollama-review",
            model_provider="aicli_ollama_review",
            budget_mode="watchdog-only",
            runtime_identity=runtime_identity,
            aicli_preflight={
                "schema": "llm-backend-toolkit.aicli-benchmark-preflight.v1",
                "entry_sha256": "sha256:" + AICLI_DIGEST,
                "raw_entry_sha256": "sha256:" + ("b" * 64),
                "fingerprint_scope": "module-source-bundle-v1",
                "file_count": 32,
                "version": "0.3.3",
                "machine_event_projection": "aicli.machine-event.v1",
                "profile": {
                    "id": "codex-ollama-review",
                    "engine": "codex",
                    "provider": "ollama",
                    "provider_id": "aicli_ollama_review",
                    "model": "qwen-review-v1",
                    "fingerprint": PROFILE_FINGERPRINT,
                },
                "network_proof": network_proof,
            },
        )


class LocalCodexBenchmarkBridgeTests(unittest.TestCase):
    @staticmethod
    def _aicli_fixture(root: Path) -> tuple[Path, str]:
        entry = root / "bin" / "aicli.ps1"
        module_root = root / "src" / "AiCliProfileManager"
        entry.parent.mkdir(parents=True)
        module_root.mkdir(parents=True)
        entry.write_text("# fixture entry\n", encoding="utf-8")
        module = module_root / "AiCliProfileManager.psm1"
        module.write_text("# fixture module\n", encoding="utf-8")
        file_digest = hashlib.sha256(module.read_bytes()).hexdigest()
        manifest = f"AiCliProfileManager.psm1\0{file_digest}\n".encode()
        return entry, hashlib.sha256(manifest).hexdigest()

    @staticmethod
    def _preflight_execution(workspace: Path, digest: str) -> dict:
        return {
            "workspace": str(workspace),
            "budget": {"limit_mode": "watchdog_only", "timeout_seconds": 7200},
            "policy": "workspace-write",
            "model": "qwen-review-v1",
            "profile": "codex-ollama-review",
            "provider_id": "aicli_ollama_review",
            "codex_cli_version": "0.147.0",
            "aicli_entry_sha256": digest,
            "aicli_version": "0.3.3",
            "profile_fingerprint": PROFILE_FINGERPRINT,
            "require_runtime_identity": True,
            "require_benchmark_preflight": True,
            "require_network_proof": True,
            "network_policy": "forbidden",
            "search_policy": "disabled",
        }

    def test_runner_preflight_marks_audited_network_contract_pending_without_model_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            entry, _ = self._aicli_fixture(root)
            commands = []

            def fake_process(command, **kwargs):
                del kwargs
                commands.append(command)
                if command[-2:] == ["version", "--json"]:
                    payload = {
                        "command": "aicli",
                        "version": "0.3.3",
                        "capabilities": {
                            "machineEventProjection": "aicli.machine-event.v1"
                        },
                    }
                elif command[-4:] == [
                    "profile",
                    "show",
                    "codex-ollama-review",
                    "--json",
                ]:
                    payload = {
                        "command": "profile show",
                        "profile": {
                            "id": "codex-ollama-review",
                            "engine": "codex",
                            "provider": "ollama",
                            "codexProviderId": "aicli_ollama_review",
                            "models": {"primary": "qwen-review-v1"},
                            "profileFingerprint": PROFILE_FINGERPRINT,
                        },
                    }
                else:  # pragma: no cover - proves no model-bearing run starts
                    raise AssertionError(command)
                return 0, json.dumps(payload), "", 1

            runner = AiCliProfileRunner(
                name="codex-cli",
                engine="codex",
                default_profile="codex-ollama-review",
                entry=str(entry),
            )
            with patch.object(
                AiCliProfileRunner,
                "_benchmark_source_receipt",
                return_value=_audited_source_receipt(),
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=fake_process,
            ):
                preflight = runner._benchmark_preflight(
                    ["pwsh", "-NoProfile", "-File", str(entry)],
                    workspace,
                    self._preflight_execution(workspace, AICLI_DIGEST),
                )

        self.assertEqual(2, len(commands))
        self.assertEqual("sha256:" + AICLI_DIGEST, preflight["entry_sha256"])
        self.assertEqual("0.3.3", preflight["version"])
        self.assertEqual(PROFILE_FINGERPRINT, preflight["profile"]["fingerprint"])
        proof = preflight["network_proof"]
        self.assertEqual("agent_network_proof_pending_prelaunch", proof["status"])
        self.assertEqual(
            "--sandbox-state-disable-network",
            proof["source_contract"]["outer_launcher_network_flag"],
        )
        self.assertEqual("workspace-write", proof["request_contract"]["sandbox_policy"])
        self.assertEqual(
            _canonical_digest(proof["source_contract"]),
            proof["source_contract_sha256"],
        )
        self.assertEqual(
            _canonical_digest(proof["request_contract"]),
            proof["request_contract_sha256"],
        )

    def test_runner_rejects_source_or_version_drift_before_model_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            entry, _ = self._aicli_fixture(root)
            commands = []

            def fake_process(command, **kwargs):
                del kwargs
                commands.append(command)
                payload = (
                    {
                        "command": "aicli",
                        "version": "0.3.4",
                        "capabilities": {
                            "machineEventProjection": "aicli.machine-event.v1"
                        },
                    }
                    if command[-2:] == ["version", "--json"]
                    else {
                        "command": "profile show",
                        "profile": {
                            "id": "codex-ollama-review",
                            "engine": "codex",
                            "provider": "ollama",
                            "codexProviderId": "aicli_ollama_review",
                            "models": {"primary": "qwen-review-v1"},
                            "profileFingerprint": PROFILE_FINGERPRINT,
                        },
                    }
                )
                return 0, json.dumps(payload), "", 1

            runner = AiCliProfileRunner(
                name="codex-cli",
                engine="codex",
                default_profile="codex-ollama-review",
                entry=str(entry),
            )
            with patch(
                "llm_backend_toolkit.agent_runners.shutil.which",
                return_value="pwsh",
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=fake_process,
            ), self.assertRaises(AgentRunnerError) as raised:
                runner.invoke(
                    "must not run",
                    self._preflight_execution(workspace, "0" * 64),
                )

        self.assertEqual("agent_runner_identity_mismatch", raised.exception.error.category)
        self.assertEqual(2, len(commands))
        self.assertEqual(
            [
                "aicli_entry_sha256",
                "aicli_network_source_bundle",
                "aicli_raw_entry_sha256",
                "aicli_network_source_components",
                "aicli_version",
                "aicli_network_source_version",
            ],
            raised.exception.receipt["identity_mismatches"],
        )

    def test_runner_rejects_non_forbidden_network_request_before_model_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            entry, _ = self._aicli_fixture(root)
            commands = []

            def fake_process(command, **kwargs):
                del kwargs
                commands.append(command)
                payload = (
                    {
                        "version": "0.3.3",
                        "capabilities": {
                            "machineEventProjection": "aicli.machine-event.v1"
                        },
                    }
                    if command[-2:] == ["version", "--json"]
                    else {
                        "profile": {
                            "id": "codex-ollama-review",
                            "engine": "codex",
                            "provider": "ollama",
                            "codexProviderId": "aicli_ollama_review",
                            "models": {"primary": "qwen-review-v1"},
                            "profileFingerprint": PROFILE_FINGERPRINT,
                        }
                    }
                )
                return 0, json.dumps(payload), "", 1

            execution = self._preflight_execution(workspace, AICLI_DIGEST)
            execution["policy"] = "danger-full-access"
            runner = AiCliProfileRunner(
                name="codex-cli",
                engine="codex",
                default_profile="codex-ollama-review",
                entry=str(entry),
            )
            with patch.object(
                AiCliProfileRunner,
                "_benchmark_source_receipt",
                return_value=_audited_source_receipt(),
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=fake_process,
            ), self.assertRaises(AgentRunnerError) as raised:
                runner._benchmark_preflight(
                    ["pwsh", "-NoProfile", "-File", str(entry)],
                    workspace,
                    execution,
                )

        self.assertEqual(2, len(commands))
        self.assertEqual(
            "agent_runner_identity_mismatch", raised.exception.error.category
        )
        self.assertIn(
            "network_request_contract",
            raised.exception.receipt["identity_mismatches"],
        )

    def test_runner_promotes_pending_network_proof_from_bound_runtime_terminal_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            entry, _ = self._aicli_fixture(root)
            commands = []

            def fake_process(command, **kwargs):
                commands.append(command)
                if command[-2:] == ["version", "--json"]:
                    payload = {
                        "version": "0.3.3",
                        "capabilities": {
                            "machineEventProjection": "aicli.machine-event.v1"
                        },
                    }
                elif command[-4:] == [
                    "profile",
                    "show",
                    "codex-ollama-review",
                    "--json",
                ]:
                    payload = {
                        "profile": {
                            "id": "codex-ollama-review",
                            "engine": "codex",
                            "provider": "ollama",
                            "codexProviderId": "aicli_ollama_review",
                            "models": {"primary": "qwen-review-v1"},
                            "profileFingerprint": PROFILE_FINGERPRINT,
                        }
                    }
                else:
                    events = _machine_events()
                    for event in events:
                        kwargs["on_event"](event)
                    payload = _aicli_run_envelope()
                return 0, json.dumps(payload), "", 1

            runner = AiCliProfileRunner(
                name="codex-cli",
                engine="codex",
                default_profile="codex-ollama-review",
                entry=str(entry),
            )
            with patch(
                "llm_backend_toolkit.agent_runners.shutil.which",
                return_value="pwsh",
            ), patch.object(
                AiCliProfileRunner,
                "_benchmark_source_receipt",
                return_value=_audited_source_receipt(),
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=fake_process,
            ):
                response = runner.invoke(
                    "fixture only",
                    self._preflight_execution(workspace, AICLI_DIGEST),
                )

        self.assertEqual(3, len(commands))
        proof = response.aicli_preflight["network_proof"]
        self.assertEqual("enforced", proof["status"])
        self.assertEqual("network-denied", proof["enforcement"])
        self.assertEqual("turn.completed", proof["terminal_event"])
        self.assertEqual(4, proof["terminal_sequence"])
        self.assertEqual(4, proof["machine_event_count"])
        self.assertEqual(
            _canonical_digest(response.runtime_identity),
            proof["runtime_identity_sha256"],
        )
        for field in (
            "source_contract_sha256",
            "request_contract_sha256",
            "process_receipt_sha256",
            "machine_event_stream_sha256",
        ):
            self.assertRegex(proof[field], r"^sha256:[0-9a-f]{64}$")

    def test_runner_fails_postrun_network_proof_as_infra_invalid_after_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            entry, _ = self._aicli_fixture(root)

            def fake_process(command, **kwargs):
                if command[-2:] == ["version", "--json"]:
                    payload = {
                        "version": "0.3.3",
                        "capabilities": {
                            "machineEventProjection": "aicli.machine-event.v1"
                        },
                    }
                elif command[-4:] == [
                    "profile",
                    "show",
                    "codex-ollama-review",
                    "--json",
                ]:
                    payload = {
                        "profile": {
                            "id": "codex-ollama-review",
                            "engine": "codex",
                            "provider": "ollama",
                            "codexProviderId": "aicli_ollama_review",
                            "models": {"primary": "qwen-review-v1"},
                            "profileFingerprint": PROFILE_FINGERPRINT,
                        }
                    }
                else:
                    for event in _machine_events(include_terminal=False):
                        kwargs["on_event"](event)
                    payload = _aicli_run_envelope(include_terminal=False)
                return 0, json.dumps(payload), "", 1

            runner = AiCliProfileRunner(
                name="codex-cli",
                engine="codex",
                default_profile="codex-ollama-review",
                entry=str(entry),
            )
            with patch(
                "llm_backend_toolkit.agent_runners.shutil.which",
                return_value="pwsh",
            ), patch.object(
                AiCliProfileRunner,
                "_benchmark_source_receipt",
                return_value=_audited_source_receipt(),
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=fake_process,
            ), self.assertRaises(AgentRunnerError) as raised:
                runner.invoke(
                    "fixture only",
                    self._preflight_execution(workspace, AICLI_DIGEST),
                )

        self.assertEqual(
            "agent_network_proof_incomplete", raised.exception.error.category
        )
        self.assertEqual(
            {
                "state": "infra-invalid",
                "scored": False,
                "ranking_eligible": False,
            },
            raised.exception.receipt["benchmark_qualification"],
        )
        self.assertTrue(
            raised.exception.receipt["limit_usage"]["cleanup_confirmed"]
        )
        self.assertIn(
            "terminal_event",
            raised.exception.receipt["aicli_preflight"]["network_proof"][
                "failure_codes"
            ],
        )

    def test_registry_carries_exact_artifact_profile_and_window_bindings(self):
        registry = _registry()
        backend = registry.resolve("cacb-local-27b-formal-v10").config
        route = backend["agent_routes"]["codex-cli"]
        evidence = route["evidence"]

        self.assertEqual(131072, backend["context_window_tokens"])
        self.assertEqual(8192, backend["reserved_output_tokens"])
        self.assertEqual(8192, backend["ollama_options"]["num_predict"])
        self.assertEqual(PROFILE_FINGERPRINT, evidence["profile_fingerprint"])
        self.assertEqual(PARAMETERS_DIGEST, evidence["parameters_digest"])
        self.assertEqual("aicli_ollama_review", evidence["provider_id"])
        self.assertEqual("responses", evidence["wire"])
        self.assertEqual("0.147.0", evidence["codex_cli_version"])

    def test_benchmark_worker_requires_codex_exact_7200_watchdog_and_real_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp).resolve()
            registry = _registry()
            worker = LocalAsyncWorker(object(), registry=registry)
            validated = worker._validate_envelope(_envelope(workspace))

            self.assertEqual("codex-cli", validated.request["execution"]["runner"])
            self.assertEqual(
                {
                    "timeout_seconds": 7200,
                    "idle_timeout_seconds": None,
                    "limit_mode": "watchdog_only",
                    "max_steps": None,
                    "max_tool_calls": None,
                },
                validated.requested_binding["budget"],
            )
            self.assertEqual(
                {"total_tokens": 131072, "reserved_output_tokens": 8192,
                 "status": "configured_unverified"},
                validated.requested_binding["context"],
            )
            self.assertEqual(
                {
                    "policy": "forbidden",
                    "search": "disabled",
                    "proof_required": "aicli-source-preflight-runtime-terminal-bound",
                },
                validated.requested_binding["network"],
            )

            for mutate in (
                lambda value: value["request"]["execution"].update(runner="data_factory"),
                lambda value: value["request"]["execution"]["budget"].update(
                    timeout_seconds=7201
                ),
                lambda value: value["bindings"]["context"].update(
                    reserved_output_tokens=32768
                ),
            ):
                invalid = copy.deepcopy(_envelope(workspace))
                mutate(invalid)
                with self.assertRaises(WorkerContractError):
                    worker._validate_envelope(invalid)

    def test_benchmark_route_fails_closed_before_runner_when_provider_evidence_is_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp).resolve()
            toolkit = Toolkit(
                registry=_registry(),
                providers={"cacb-local-27b-formal-v10": _MissingEvidenceProvider()},
                runners={"codex-cli": _MustNotRun()},
            )

            result = toolkit.invoke(_envelope(workspace)["request"])

        self.assertEqual("blocked", result["status"])
        self.assertEqual("route_evidence_unverified", result["error"]["category"])

    def test_toolkit_returns_infra_invalid_when_runtime_network_proof_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "arm"
            workspace.mkdir()
            registry = _registry()
            toolkit = Toolkit(
                registry=registry,
                providers={"cacb-local-27b-formal-v10": _Provider()},
                runners={"codex-cli": _NetworkProofIncomplete()},
            )
            envelope = _envelope(workspace)
            store = JobStore(
                root / "jobs",
                spawner=lambda *_: None,
                registry=registry,
                cancel_bridge=_CancelBridge(),
            )
            worker = LocalAsyncWorker(store, registry=registry)
            result = toolkit.invoke(worker._validate_envelope(envelope).request)
            handle = worker.start(envelope)
            store.complete(handle["job_id"], result)
            terminal = worker.result(handle)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("agent_network_proof_incomplete", result["error"]["category"])
        self.assertEqual(
            "infra-invalid",
            result["execution_receipt"]["benchmark_qualification"]["state"],
        )
        observation = result["execution_receipt"]["local_codex_benchmark_observation"]
        self.assertEqual(
            "incomplete_postrun",
            observation["aicli_preflight"]["network_proof"]["status"],
        )
        self.assertEqual("blocked", terminal["status"])
        self.assertEqual(
            "local_worker_network_proof_incomplete", terminal["error"]["category"]
        )
        self.assertEqual(
            "infra-invalid", terminal["benchmark_qualification"]["state"]
        )
        self.assertFalse(terminal["benchmark_qualification"]["scored"])

    def test_fake_execution_projects_and_verifies_full_terminal_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "arm"
            workspace.mkdir()
            registry = _registry()
            provider = _Provider()
            toolkit = Toolkit(
                registry=registry,
                providers={"cacb-local-27b-formal-v10": provider},
                runners={"codex-cli": _Runner()},
            )
            envelope = _envelope(workspace)
            store = JobStore(
                root / "jobs",
                spawner=lambda *_: None,
                registry=registry,
                cancel_bridge=_CancelBridge(),
            )
            worker = LocalAsyncWorker(store, registry=registry)
            canonical_request = worker._validate_envelope(envelope).request
            result = toolkit.invoke(canonical_request)

            observation = result["execution_receipt"]["local_codex_benchmark_observation"]
            self.assertEqual("qwen-review-v1", observation["runtime_identity"]["model"])
            self.assertEqual(
                PROFILE_FINGERPRINT,
                observation["aicli_preflight"]["profile"]["fingerprint"],
            )
            self.assertEqual(262144, observation["provider_after"]["model"]["context_length"])
            self.assertEqual(2, provider.status_calls)

            handle = worker.start(envelope)
            store.complete(handle["job_id"], result)
            completed = worker.result(handle)

        self.assertEqual("ok", completed["status"])
        observed = completed["binding_receipt"]["observed"]
        self.assertEqual("verified", completed["binding_receipt"]["verification"]["status"])
        self.assertEqual("exact_observed", observed["context"]["status"])
        self.assertEqual(131072, observed["context"]["runtime_proof"]["total_tokens"])
        self.assertEqual(8192, observed["context"]["runtime_proof"]["reserved_output_tokens"])
        self.assertEqual(
            249036,
            observed["context"]["runtime_proof"]["aicli_catalog_effective_context_tokens"],
        )
        self.assertEqual(
            262144,
            observed["context"]["runtime_proof"]["provider_model_info_context_tokens"],
        )
        self.assertTrue(observed["cleanup"]["process_tree_confirmed"])
        self.assertTrue(observed["cleanup"]["gpu_lease_released"])
        self.assertEqual("enforced", observed["network"]["status"])
        self.assertEqual(
            "aicli-runtime-bound-source-contract",
            observed["network"]["evidence_kind"],
        )
        self.assertEqual("network-denied", observed["network"]["enforcement"])
        self.assertEqual("turn.completed", observed["network"]["terminal_event"])
        self.assertEqual(
            _canonical_digest(observed["runtime_identity"]),
            observed["network"]["runtime_identity_sha256"],
        )
        self.assertEqual("sha256:" + AICLI_DIGEST, observed["harness"]["aicli_entry_sha256"])
        self.assertEqual("0.3.3", observed["harness"]["aicli_version"])
        self.assertEqual(PROFILE_FINGERPRINT, observed["harness"]["profile_fingerprint"])
        self.assertEqual("aicli_ollama_review", observed["harness"]["provider_id"])
        self.assertEqual("responses", observed["harness"]["wire"])
        self.assertFalse(observed["fallback_used"])


if __name__ == "__main__":
    unittest.main()
