"""Fail-closed lifecycle adapter for non-native local Codex worker jobs.

This module deliberately does not emulate ``spawn_agent``.  It gives a caller
the familiar start/wait/cancel/result shape while keeping the executor identity
truthful: a Toolkit background job with no native lineage or ``agent_type``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backends import REGISTRY_SCHEMA, BackendRegistry
from .jobs import (
    CONTROLLED_CANCEL_SCHEMA,
    WORKER_CONTRACT_ANCHOR_SCHEMA,
    JobStore,
)
from .workspace_observer import (
    WorkspaceRootError,
    is_safe_workspace_relative_path,
    revalidate_workspace_root,
    validate_workspace_root,
)


LOCAL_ASYNC_WORKER_ENVELOPE_SCHEMA = "llm-backend-toolkit.local-async-worker-envelope.v1"
LOCAL_ASYNC_WORKER_HANDLE_SCHEMA = "llm-backend-toolkit.local-async-worker-handle.v1"
LOCAL_ASYNC_WORKER_REQUESTED_BINDING_SCHEMA = (
    "llm-backend-toolkit.local-async-worker-requested-binding.v1"
)
LOCAL_ASYNC_WORKER_OBSERVED_BINDING_SCHEMA = (
    "llm-backend-toolkit.local-async-worker-observed-binding.v1"
)
LOCAL_ASYNC_WORKER_BINDING_RECEIPT_SCHEMA = (
    "llm-backend-toolkit.local-async-worker-binding-receipt.v1"
)
LOCAL_ASYNC_WORKER_CONTRACT_SCHEMA = "llm-backend-toolkit.local-async-worker-contract.v1"
LOCAL_CODEX_BENCHMARK_REGISTRY_SOURCE_SCHEMA = (
    "llm-backend-toolkit.local-codex-benchmark-registry-source.v1"
)

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})


class WorkerContractError(ValueError):
    """The caller did not provide a fully bound local worker envelope."""


@dataclass(frozen=True)
class _ValidatedEnvelope:
    task_id: str
    request: dict[str, Any]
    requested_binding: dict[str, Any]
    controlled_cancel: dict[str, Any]
    benchmark_registry_source: dict[str, Any] | None


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _validate_worker_contract_anchor(
    contract: dict[str, Any],
    expected_anchor: dict[str, Any],
) -> None:
    if not isinstance(expected_anchor, dict):
        raise WorkerContractError("worker contract anchor must be an object")
    expected_fields = {
        "schema",
        "job_id",
        "job_request_sha256",
        "requested_binding_sha256",
        "registry_source",
        "anchor_sha256",
    }
    if set(expected_anchor) != expected_fields:
        raise WorkerContractError("worker contract anchor fields are not closed")
    if expected_anchor.get("schema") != WORKER_CONTRACT_ANCHOR_SCHEMA:
        raise WorkerContractError("worker contract anchor schema is unsupported")
    anchor_scope = {
        field: expected_anchor[field]
        for field in expected_fields
        if field != "anchor_sha256"
    }
    if (
        _require_sha256(
            expected_anchor.get("anchor_sha256"),
            "worker_contract_anchor.anchor_sha256",
        )
        != _canonical_digest(anchor_scope)
    ):
        raise WorkerContractError("worker contract anchor digest is mismatched")

    requested = _require_object(contract.get("requested"), "contract.requested")
    task = _require_object(requested.get("task"), "contract.requested.task")
    if expected_anchor.get("job_request_sha256") != task.get("request_sha256"):
        raise WorkerContractError("worker contract anchor does not match the job request")
    if expected_anchor.get("requested_binding_sha256") != _canonical_digest(requested):
        raise WorkerContractError(
            "worker contract requested binding changed after handle creation"
        )

    if requested.get("binding_kind") == "local_codex_benchmark":
        source = _require_object(
            contract.get("benchmark_registry_source"),
            "contract.benchmark_registry_source",
        )
        source_receipt = {
            "schema": source.get("schema"),
            "backend_id": source.get("backend_id"),
            "source_sha256": source.get("source_sha256"),
        }
        if expected_anchor.get("registry_source") != source_receipt:
            raise WorkerContractError(
                "benchmark registry source changed after handle creation"
            )
        if requested.get("registry_source") != source_receipt:
            raise WorkerContractError(
                "benchmark registry source does not match its requested binding"
            )
    elif expected_anchor.get("registry_source") is not None:
        raise WorkerContractError(
            "non-benchmark worker anchor contains a registry source"
        )


def _benchmark_registry_source(
    registry: BackendRegistry,
    backend_id: str,
) -> dict[str, Any]:
    """Freeze one exact benchmark-only route for a fresh worker process."""

    resolved = registry.resolve(backend_id)
    registry_payload = {
        "schema": REGISTRY_SCHEMA,
        "default_backend": backend_id,
        "aliases": {},
        "backends": {backend_id: _copy_json(resolved.config)},
    }
    scope = {
        "schema": LOCAL_CODEX_BENCHMARK_REGISTRY_SOURCE_SCHEMA,
        "backend_id": backend_id,
        "registry": registry_payload,
    }
    return {**scope, "source_sha256": _canonical_digest(scope)}


def registry_from_worker_contract(
    contract: dict[str, Any],
    *,
    expected_anchor: dict[str, Any] | None = None,
) -> BackendRegistry | None:
    """Rebuild one persisted benchmark route for the fresh worker process.

    Ordinary jobs and the legacy local-default worker continue to use the live
    registry.  A benchmark contract is accepted only when its single-route
    source, digest, loopback endpoint, no-fallback shape, and requested binding
    all agree.
    """

    if not isinstance(contract, dict):
        raise WorkerContractError("worker contract must be an object")
    if contract.get("schema") != LOCAL_ASYNC_WORKER_CONTRACT_SCHEMA:
        raise WorkerContractError("worker contract schema is unsupported")
    requested = _require_object(contract.get("requested"), "contract.requested")
    if expected_anchor is not None:
        _validate_worker_contract_anchor(contract, expected_anchor)
    if requested.get("binding_kind") != "local_codex_benchmark":
        return None
    if expected_anchor is None:
        raise WorkerContractError(
            "benchmark worker contract requires its immutable creation anchor"
        )
    source = _require_object(
        contract.get("benchmark_registry_source"),
        "contract.benchmark_registry_source",
    )
    if set(source) != {"schema", "backend_id", "registry", "source_sha256"}:
        raise WorkerContractError("benchmark registry source fields are not closed")
    if source.get("schema") != LOCAL_CODEX_BENCHMARK_REGISTRY_SOURCE_SCHEMA:
        raise WorkerContractError("benchmark registry source schema is unsupported")
    source_scope = {
        "schema": source["schema"],
        "backend_id": source.get("backend_id"),
        "registry": source.get("registry"),
    }
    source_sha256 = _require_sha256(
        source.get("source_sha256"),
        "contract.benchmark_registry_source.source_sha256",
    )
    if source_sha256 != _canonical_digest(source_scope):
        raise WorkerContractError("benchmark registry source digest is mismatched")
    requested_source = _require_object(
        requested.get("registry_source"), "contract.requested.registry_source"
    )
    expected_source_receipt = {
        "schema": source["schema"],
        "backend_id": source.get("backend_id"),
        "source_sha256": source_sha256,
    }
    if (
        set(requested_source) != set(expected_source_receipt)
        or requested_source != expected_source_receipt
    ):
        raise WorkerContractError(
            "benchmark registry source does not match the requested receipt"
        )

    backend_id = _require_string(source.get("backend_id"), "registry_source.backend_id")
    requested_backend = _require_object(
        requested.get("backend"), "contract.requested.backend"
    )
    requested_harness = _require_object(
        requested.get("harness"), "contract.requested.harness"
    )
    requested_context = _require_object(
        requested.get("context"), "contract.requested.context"
    )
    if backend_id != requested_backend.get("alias"):
        raise WorkerContractError("benchmark registry backend does not match the request")

    registry_payload = _require_object(source.get("registry"), "registry_source.registry")
    if (
        set(registry_payload) != {"schema", "default_backend", "aliases", "backends"}
        or registry_payload.get("schema") != REGISTRY_SCHEMA
        or registry_payload.get("default_backend") != backend_id
        or registry_payload.get("aliases") != {}
        or not isinstance(registry_payload.get("backends"), dict)
        or set(registry_payload["backends"]) != {backend_id}
    ):
        raise WorkerContractError("benchmark registry must contain one exact no-alias route")
    try:
        registry = BackendRegistry.from_dict(
            _copy_json(registry_payload),
            source=f"worker-contract:{source_sha256}",
        )
    except ValueError as exc:
        raise WorkerContractError("benchmark registry source is invalid") from exc
    config = registry.resolve(backend_id).config
    expected_config_fields = {
        "adapter",
        "model",
        "cloud",
        "supports_vision",
        "context_window_tokens",
        "reserved_output_tokens",
        "routing_role",
        "default_reasoning_mode",
        "ollama_options",
        "base_url_default",
        "data_destination",
        "agent_routes",
    }
    if set(config) != expected_config_fields:
        raise WorkerContractError("benchmark registry backend fields are not closed")
    routes = config.get("agent_routes")
    route = routes.get("codex-cli") if isinstance(routes, dict) else None
    if not isinstance(route, dict) or set(routes) != {"codex-cli"}:
        raise WorkerContractError("benchmark registry lacks its single Codex route")
    evidence = route.get("evidence")
    if not isinstance(evidence, dict):
        raise WorkerContractError("benchmark Codex route lacks exact evidence")

    def unprefixed(value: Any) -> str:
        return _require_sha256(value, "requested digest").removeprefix("sha256:")

    expected_evidence = {
        "basis": "benchmark_only",
        "live_verified": True,
        "model_digest": unprefixed(requested_backend.get("artifact_digest")),
        "alias_manifest_digest": unprefixed(
            requested_backend.get("alias_manifest_digest")
        ),
        "parent_model": requested_backend.get("parent_model"),
        "context_window_tokens": requested_context.get("total_tokens"),
        "reserved_output_tokens": requested_context.get("reserved_output_tokens"),
        "wire": requested_harness.get("wire"),
        "provider_id": requested_harness.get("provider_id"),
        "parent_model_digest": unprefixed(
            requested_backend.get("parent_model_digest")
        ),
        "model_layer_digest": unprefixed(
            requested_backend.get("model_layer_digest")
        ),
        "parameters_digest": unprefixed(
            requested_backend.get("parameters_digest")
        ),
        "quantization": requested_backend.get("quantization"),
        "profile_fingerprint": requested_harness.get("profile_fingerprint"),
        "aicli_entry_sha256": unprefixed(
            requested_harness.get("aicli_entry_sha256")
        ),
        "aicli_version": requested_harness.get("aicli_version"),
        "codex_cli_version": requested_harness.get("codex_cli_version"),
    }
    expected_options = {
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repeat_penalty": 1.0,
        "num_ctx": requested_context.get("total_tokens"),
        "num_predict": requested_context.get("reserved_output_tokens"),
    }
    if (
        config.get("adapter") != "ollama"
        or config.get("model") != requested_backend.get("provider_model")
        or config.get("cloud") is not False
        or config.get("supports_vision") is not False
        or config.get("context_window_tokens") != requested_context.get("total_tokens")
        or config.get("reserved_output_tokens")
        != requested_context.get("reserved_output_tokens")
        or config.get("routing_role") != "benchmark_only"
        or config.get("default_reasoning_mode") != "on"
        or config.get("ollama_options") != expected_options
        or config.get("base_url_default") != "http://127.0.0.1:32100"
        or config.get("data_destination")
        != "LocalGpuBroker benchmark-only loopback endpoint"
        or set(route) != {"runner", "profile", "model", "reasoning_effort", "evidence"}
        or route.get("runner") != "codex-cli"
        or route.get("profile") != requested_harness.get("profile")
        or route.get("model") != requested_backend.get("model")
        or route.get("reasoning_effort") != "max"
        or evidence != expected_evidence
        or requested.get("fallback_used") is not False
    ):
        raise WorkerContractError("benchmark registry source drifted from its binding")
    return registry


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkerContractError(f"{field} must be an object")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkerContractError(f"{field} must be a non-empty string")
    return value.strip()


def _require_sha256(value: Any, field: str) -> str:
    digest = _require_string(value, field)
    if not _SHA256.fullmatch(digest):
        raise WorkerContractError(f"{field} must be a sha256: digest")
    return digest


def _parse_utc(value: Any, field: str) -> datetime:
    text = _require_string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkerContractError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise WorkerContractError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _cancellation_cleanup_confirmed(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    process_tree = value.get("process_tree")
    gpu_lease = value.get("gpu_lease")
    return bool(
        isinstance(process_tree, dict)
        and process_tree.get("status") == "confirmed_absent"
        and isinstance(gpu_lease, dict)
        and gpu_lease.get("status") == "released"
    )


class LocalAsyncWorker:
    """Expose a completion-driven Toolkit job as a truthful local worker."""

    def __init__(
        self,
        store: JobStore,
        *,
        registry: BackendRegistry | None = None,
    ) -> None:
        self.store = store
        self.registry = registry or store.registry or BackendRegistry.load()

    def start(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Validate every declared binding and queue one uncached local attempt."""
        validated = self._validate_envelope(envelope)
        if not self.store.has_controlled_cancel_bridge:
            raise WorkerContractError(
                "local_async_job requires a configured controlled JobStore cancel bridge"
            )
        contract = {
            "schema": LOCAL_ASYNC_WORKER_CONTRACT_SCHEMA,
            "task_id": validated.task_id,
            "executor": {"kind": "local_async_job", "native_subagent": False},
            "requested": validated.requested_binding,
            "observed": {"status": "pending"},
            "verification": {
                "status": "configured_unverified",
                "missing": [
                    "runtime_context_proof",
                    "observed_binding",
                    "process_tree_cleanup",
                    "gpu_lease_cleanup",
                ],
            },
            "routing_status": "configured_unverified",
            **(
                {
                    "benchmark_registry_source": validated.benchmark_registry_source,
                }
                if validated.benchmark_registry_source is not None
                else {}
            ),
        }

        contract_anchor: dict[str, Any] = {}

        def before_spawn(job_id: str) -> None:
            contract_anchor.update(
                self.store.record_worker_contract(job_id, contract)
            )
            self.store.arm_controlled_cancel(job_id, validated.controlled_cancel)

        submission = self.store.submit(
            validated.request,
            force=True,
            before_spawn=before_spawn,
        )
        return self._handle_from_submission(submission, contract, contract_anchor)

    def wait(self, handle: dict[str, Any], deadline: str) -> dict[str, Any]:
        """Perform exactly one status read once the caller's deadline is due."""
        try:
            contract = self._contract_for_handle(handle)
            deadline_utc = _parse_utc(deadline, "deadline")
            recommended_utc = _parse_utc(
                handle.get("recommended_check_utc"),
                "handle.recommended_check_utc",
            )
        except WorkerContractError as exc:
            return self._blocked("invalid_worker_handle", str(exc))
        now_utc = datetime.now(timezone.utc)
        if deadline_utc < now_utc:
            return self._blocked(
                "worker_wait_deadline_elapsed",
                "wait deadline elapsed before a bounded status read could begin.",
                worker_status=self._state_name(self.store.get(handle["job_id"])),
            )
        if now_utc < recommended_utc:
            return self._blocked(
                "worker_wait_not_due",
                "wait is a bounded status read and may not poll before recommended_check_utc.",
                worker_status=self._state_name(self.store.get(handle["job_id"])),
                recommended_check_utc=handle["recommended_check_utc"],
            )
        state = self.store.get(handle["job_id"])
        return {
            "status": "ok",
            "job_id": handle["job_id"],
            "worker_status": self._state_name(state),
            "terminal": str(state.get("job_status") or "") in _TERMINAL_JOB_STATUSES,
            "recommended_check_utc": state.get("recommended_check_utc")
            or handle.get("recommended_check_utc"),
            "monitor_until_utc": state.get("monitor_until_utc"),
            "binding_receipt": self._binding_receipt(contract),
        }

    def cancel(self, handle: dict[str, Any]) -> dict[str, Any]:
        """Request cancellation; terminality requires both cleanup confirmations."""
        try:
            contract = self._contract_for_handle(handle)
        except WorkerContractError as exc:
            return self._blocked("invalid_worker_handle", str(exc))
        cancelled = self.store.cancel(handle["job_id"])
        raw_status = str(cancelled.get("job_status") or "")
        if raw_status == "cancelled":
            worker_status = "CANCELLED"
        elif raw_status == "cleanup_unconfirmed":
            worker_status = "cleanup_unconfirmed"
        elif raw_status in {"completed", "failed"}:
            worker_status = self._state_name(self.store.get(handle["job_id"]))
        else:
            worker_status = "CANCELLATION_REQUESTED"
        result = {
            "status": str(cancelled.get("status") or "blocked"),
            "job_id": handle["job_id"],
            "worker_status": worker_status,
            "terminal": worker_status == "CANCELLED",
            "binding_receipt": self._binding_receipt(contract),
            "cancellation_receipt": {
                "controlled_cancel": cancelled.get("controlled_cancel") or {},
            },
        }
        if isinstance(cancelled.get("error"), dict):
            result["error"] = dict(cancelled["error"])
        return result

    def result(self, handle: dict[str, Any]) -> dict[str, Any]:
        """Return a result only from a terminal worker state and verified receipt."""
        try:
            contract = self._contract_for_handle(handle)
        except WorkerContractError as exc:
            return self._blocked("invalid_worker_handle", str(exc))
        state = self.store.get(handle["job_id"], include_result=True, full_result=True)
        job_status = str(state.get("job_status") or "")
        if job_status not in _TERMINAL_JOB_STATUSES:
            return self._blocked(
                "worker_not_terminal",
                "worker.result is available only after a terminal state.",
                worker_status=self._state_name(state),
            )
        if job_status == "cancelled":
            controlled = state.get("controlled_cancel") or {}
            cleanup = controlled.get("cleanup") if isinstance(controlled, dict) else {}
            if not _cancellation_cleanup_confirmed(cleanup):
                return self._blocked(
                    "local_worker_binding_incomplete",
                    "Cancelled local_async_job has no complete cleanup receipt.",
                    worker_status="cleanup_unconfirmed",
                )
            observed = {
                "status": "cancelled_before_result",
                "cleanup": dict(cleanup),
                "executor": {"kind": "local_async_job", "native_subagent": False},
                **(
                    {"registry_source": _copy_json(contract["requested"]["registry_source"])}
                    if contract["requested"].get("binding_kind")
                    == "local_codex_benchmark"
                    else {}
                ),
            }
            updated = self._update_contract(
                handle["job_id"],
                contract,
                observed=observed,
                verification={"status": "verified_cancellation"},
                routing_status="not_ready",
            )
            return {
                "status": "ok",
                "job_id": handle["job_id"],
                "worker_status": "CANCELLED",
                "terminal": True,
                "binding_receipt": self._binding_receipt(updated),
            }
        result = state.get("result")
        if not isinstance(result, dict):
            return self._blocked(
                "local_worker_binding_incomplete",
                "Terminal local_async_job did not retain a result receipt.",
                worker_status=self._state_name(state),
            )
        execution_receipt = result.get("execution_receipt")
        observed = (
            execution_receipt.get("local_async_worker_observed")
            if isinstance(execution_receipt, dict)
            else None
        )
        if (
            observed is None
            and contract["requested"].get("binding_kind")
            == "local_codex_benchmark"
            and isinstance(execution_receipt, dict)
        ):
            observed = self._benchmark_observed_binding(
                contract["requested"],
                execution_receipt.get("local_codex_benchmark_observation"),
                execution_receipt.get("local_async_worker_registry_source"),
            )
        failures = self._verify_observed_binding(contract["requested"], observed)
        if failures:
            updated = self._update_contract(
                handle["job_id"],
                contract,
                observed={
                    "status": "incomplete",
                    "missing_or_mismatched": failures,
                    **(
                        {
                            "registry_source": _copy_json(
                                observed.get("registry_source")
                                if isinstance(observed, dict)
                                and isinstance(
                                    observed.get("registry_source"), dict
                                )
                                else {}
                            )
                        }
                        if contract["requested"].get("binding_kind")
                        == "local_codex_benchmark"
                        else {}
                    ),
                },
                verification={"status": "failed_closed", "failures": failures},
                routing_status="not_ready",
            )
            network_infra_invalid = bool(
                contract["requested"].get("binding_kind")
                == "local_codex_benchmark"
                and "network_proof" in failures
            )
            return self._blocked(
                (
                    "local_worker_network_proof_incomplete"
                    if network_infra_invalid
                    else "local_worker_binding_incomplete"
                ),
                (
                    "Terminal local Codex benchmark lacks its bound network-denied receipt."
                    if network_infra_invalid
                    else "Terminal local_async_job lacks the required observed runtime bindings."
                ),
                worker_status=self._state_name(state),
                binding_receipt=self._binding_receipt(updated),
                **(
                    {
                        "benchmark_qualification": {
                            "state": "infra-invalid",
                            "scored": False,
                            "ranking_eligible": False,
                        }
                    }
                    if network_infra_invalid
                    else {}
                ),
            )
        updated = self._update_contract(
            handle["job_id"],
            contract,
            observed=_copy_json(observed),
            verification={"status": "verified"},
            routing_status="eligible_after_runtime_proof",
        )
        return {
            "status": "ok",
            "job_id": handle["job_id"],
            "worker_status": self._state_name(state),
            "terminal": True,
            "binding_receipt": self._binding_receipt(updated),
            "result": result,
        }

    def _validate_envelope(self, envelope: dict[str, Any]) -> _ValidatedEnvelope:
        if not isinstance(envelope, dict):
            raise WorkerContractError("worker envelope must be an object")
        if envelope.get("schema") != LOCAL_ASYNC_WORKER_ENVELOPE_SCHEMA:
            raise WorkerContractError("worker envelope schema is unsupported")
        task_id = _require_string(envelope.get("task_id"), "task_id")
        if envelope.get("fresh_execution") is not True:
            raise WorkerContractError("fresh_execution must be true for local_async_job")
        request = _copy_json(_require_object(envelope.get("request"), "request"))
        bindings = _require_object(envelope.get("bindings"), "bindings")
        constraints = _require_object(envelope.get("constraints"), "constraints")
        acceptance = _require_object(envelope.get("acceptance"), "acceptance")
        if acceptance.get("deterministic") is not True:
            raise WorkerContractError("acceptance.deterministic must be true")
        verifier = _require_object(acceptance.get("verifier"), "acceptance.verifier")
        verifier_binding = {
            "id": _require_string(verifier.get("id"), "acceptance.verifier.id"),
            "sha256": _require_sha256(
                verifier.get("sha256"),
                "acceptance.verifier.sha256",
            ),
        }
        expected_constraints = {
            "external_effects": "return_to_sol",
            "protected_actions": "return_to_sol",
            "highest_authority": "not_inherited",
            "delegation": "forbidden",
        }
        for field, expected in expected_constraints.items():
            if constraints.get(field) != expected:
                raise WorkerContractError(f"constraints.{field} must be {expected}")
        execution = _require_object(request.get("execution"), "request.execution")
        if execution.get("mode") != "agent":
            raise WorkerContractError("request.execution.mode must be agent")
        if "cache_key" in execution:
            raise WorkerContractError("local_async_job forbids execution.cache_key")
        if execution.get("policy") != "workspace-write":
            raise WorkerContractError("request.execution.policy must be workspace-write")
        if constraints.get("sandbox") != execution.get("policy"):
            raise WorkerContractError("constraints.sandbox must match request.execution.policy")
        budget = _require_object(execution.get("budget"), "request.execution.budget")
        requested_limit_mode = budget.get("limit_mode")
        if requested_limit_mode is None:
            has_legacy_cutoff = any(
                budget.get(name) is not None
                for name in ("timeout_seconds", "max_steps", "max_tool_calls")
            )
            limit_mode = "bounded" if has_legacy_cutoff else "watchdog_only"
        else:
            limit_mode = str(requested_limit_mode)
        if limit_mode not in {"completion_driven", "bounded", "watchdog_only"}:
            raise WorkerContractError(
                "request.execution.budget.limit_mode is unsupported"
            )
        try:
            if limit_mode == "completion_driven":
                idle_value = budget.get("idle_timeout_seconds", 3600)
                idle_timeout_seconds = int(
                    3600 if idle_value is None else idle_value
                )
                if any(
                    budget.get(name) is not None
                    for name in ("timeout_seconds", "max_steps", "max_tool_calls")
                ):
                    raise WorkerContractError(
                        "completion_driven local_async_job may not declare a total wall, step, or tool-call cutoff"
                    )
                timeout_seconds = None
                max_steps = None
                max_tool_calls = None
            elif limit_mode == "watchdog_only":
                if budget.get("idle_timeout_seconds") is not None:
                    raise WorkerContractError(
                        "watchdog_only local_async_job may not declare idle_timeout_seconds"
                    )
                idle_timeout_seconds = None
                if any(
                    budget.get(name) is not None
                    for name in ("max_steps", "max_tool_calls")
                ):
                    raise WorkerContractError(
                        "watchdog_only local_async_job may not declare max_steps or max_tool_calls"
                    )
                timeout_value = budget.get("timeout_seconds", 900)
                timeout_seconds = int(900 if timeout_value is None else timeout_value)
                max_steps = None
                max_tool_calls = None
            else:
                idle_value = budget.get("idle_timeout_seconds")
                idle_timeout_seconds = (
                    int(idle_value) if idle_value is not None else None
                )
                timeout_value = budget.get("timeout_seconds", 900)
                timeout_seconds = int(900 if timeout_value is None else timeout_value)
                max_steps_value = budget.get("max_steps", 20)
                max_tool_calls_value = budget.get("max_tool_calls", 80)
                max_steps = int(20 if max_steps_value is None else max_steps_value)
                max_tool_calls = int(
                    80 if max_tool_calls_value is None else max_tool_calls_value
                )
        except (TypeError, ValueError) as exc:
            raise WorkerContractError("agent budget values must be integers") from exc
        if (
            idle_timeout_seconds is not None
            and idle_timeout_seconds != 0
            and not 60 <= idle_timeout_seconds <= 604_800
        ):
            raise WorkerContractError(
                "budget.idle_timeout_seconds must be 0 or between 60 and 604800"
            )
        if timeout_seconds is not None and not 30 <= timeout_seconds <= 86_400:
            raise WorkerContractError("budget.timeout_seconds is outside the supported range")
        if limit_mode == "bounded":
            if not 1 <= max_steps <= 200:
                raise WorkerContractError("budget.max_steps is outside the supported range")
            if not 0 <= max_tool_calls <= 10_000:
                raise WorkerContractError(
                    "budget.max_tool_calls is outside the supported range"
                )

        # Persist the effective budget so the durable request, requested
        # binding, and result-side receipt all use the same completion contract.
        canonical_budget = {
            "timeout_seconds": timeout_seconds,
            "idle_timeout_seconds": idle_timeout_seconds,
            "limit_mode": limit_mode,
            "max_steps": max_steps,
            "max_tool_calls": max_tool_calls,
        }
        execution["budget"] = canonical_budget

        write_root = _require_string(envelope.get("write_root"), "write_root")
        try:
            validated_workspace = validate_workspace_root(write_root)
            canonical_workspace = revalidate_workspace_root(validated_workspace)
        except WorkspaceRootError as exc:
            raise WorkerContractError("write_root must be an existing isolated workspace") from exc
        requested_workspace = _require_string(
            execution.get("workspace"), "request.execution.workspace"
        )
        try:
            requested_workspace_path = Path(requested_workspace).expanduser().resolve()
        except OSError as exc:
            raise WorkerContractError("request.execution.workspace cannot be resolved") from exc
        if requested_workspace_path != canonical_workspace:
            raise WorkerContractError("request.execution.workspace must exactly bind write_root")
        read_roots = envelope.get("read_roots")
        if not isinstance(read_roots, list) or not read_roots:
            raise WorkerContractError("read_roots must contain the isolated write_root")
        canonical_read_roots: list[str] = []
        for index, root in enumerate(read_roots):
            candidate = _require_string(root, f"read_roots[{index}]")
            try:
                validated = validate_workspace_root(candidate)
                canonical = revalidate_workspace_root(validated)
            except WorkspaceRootError as exc:
                raise WorkerContractError(
                    f"read_roots[{index}] must be an existing non-reparse directory"
                ) from exc
            canonical_read_roots.append(str(canonical))
        if canonical_read_roots != [str(canonical_workspace)]:
            raise WorkerContractError(
                "v1 local_async_job permits only its isolated write_root as read_root"
            )
        expected_artifacts = envelope.get("expected_artifacts")
        if not isinstance(expected_artifacts, list) or not expected_artifacts:
            raise WorkerContractError("expected_artifacts must contain at least one derived artifact")
        for index, artifact in enumerate(expected_artifacts):
            item = _require_object(artifact, f"expected_artifacts[{index}]")
            if not is_safe_workspace_relative_path(item.get("path")):
                raise WorkerContractError(
                    f"expected_artifacts[{index}].path must be a safe relative path"
                )
            _require_string(item.get("kind"), f"expected_artifacts[{index}].kind")

        backend_alias = _require_string(bindings.get("backend_alias"), "bindings.backend_alias")
        requested_backend = str(request.get("backend") or request.get("provider") or "")
        if requested_backend != backend_alias:
            raise WorkerContractError("request backend must exactly equal bindings.backend_alias")
        try:
            resolved = self.registry.resolve(backend_alias)
        except ValueError as exc:
            raise WorkerContractError("bound backend is unavailable in the live registry") from exc
        config = dict(resolved.config)
        benchmark_route = config.get("routing_role") == "benchmark_only"
        requested_runner = _require_string(
            execution.get("runner"), "request.execution.runner"
        )
        if benchmark_route:
            if resolved.backend_id != backend_alias or bool(config.get("cloud")):
                raise WorkerContractError(
                    "benchmark local_async_job requires its exact non-cloud backend"
                )
            if requested_runner != "codex-cli":
                raise WorkerContractError(
                    "benchmark local_async_job requires the exact codex-cli route"
                )
            if (
                constraints.get("network") != "forbidden"
                or constraints.get("search") != "disabled"
            ):
                raise WorkerContractError(
                    "benchmark local_async_job requires network forbidden and search disabled"
                )
        elif (
            resolved.backend_id != "local-default"
            or backend_alias != "local-default"
            or bool(config.get("cloud"))
            or requested_runner != "data_factory"
        ):
            raise WorkerContractError(
                "local_async_job requires local-default/data_factory or an exact benchmark route"
            )
        route = (config.get("agent_routes") or {}).get(requested_runner)
        if not isinstance(route, dict):
            raise WorkerContractError("bound backend has no exact requested agent route")
        evidence = route.get("evidence")
        if not isinstance(evidence, dict):
            raise WorkerContractError("bound agent route has no evidence binding")
        if benchmark_route and (
            limit_mode != "watchdog_only"
            or timeout_seconds != 7200
            or idle_timeout_seconds is not None
            or max_steps is not None
            or max_tool_calls is not None
        ):
            raise WorkerContractError(
                "benchmark local_async_job requires the exact 7200 second watchdog-only budget"
            )
        artifact_digest = _require_sha256(
            bindings.get("artifact_digest"), "bindings.artifact_digest"
        )
        expected_model_digest = str(evidence.get("model_digest") or "")
        if artifact_digest != f"sha256:{expected_model_digest}":
            raise WorkerContractError("bindings.artifact_digest does not match the route evidence")
        parent_model = _require_string(bindings.get("parent_model"), "bindings.parent_model")
        if parent_model != str(evidence.get("parent_model") or ""):
            raise WorkerContractError("bindings.parent_model does not match the route evidence")
        harness = _require_string(bindings.get("harness"), "bindings.harness")
        if harness != str(route.get("runner") or "") or harness != "codex-cli":
            raise WorkerContractError("bindings.harness must exactly bind codex-cli")
        profile = _require_string(bindings.get("profile"), "bindings.profile")
        if profile != str(route.get("profile") or ""):
            raise WorkerContractError("bindings.profile does not match the route")
        benchmark_identity: dict[str, str] = {}
        if benchmark_route:
            if bindings.get("fallback_used") is not False:
                raise WorkerContractError(
                    "benchmark local_async_job must explicitly forbid fallback"
                )
            for field in (
                "parent_model_digest",
                "model_layer_digest",
                "parameters_digest",
            ):
                declared = str(evidence.get(field) or "")
                bound = _require_sha256(bindings.get(field), f"bindings.{field}")
                if bound != f"sha256:{declared}":
                    raise WorkerContractError(
                        f"bindings.{field} does not match the exact route evidence"
                    )
                benchmark_identity[field] = bound
            benchmark_identity.update(
                {
                    "profile_fingerprint": _require_string(
                        bindings.get("profile_fingerprint"),
                        "bindings.profile_fingerprint",
                    ),
                    "provider_id": _require_string(
                        bindings.get("provider_id"), "bindings.provider_id"
                    ),
                    "wire": _require_string(bindings.get("wire"), "bindings.wire"),
                }
            )
            for field in ("profile_fingerprint", "provider_id", "wire"):
                if benchmark_identity[field] != str(evidence.get(field) or ""):
                    raise WorkerContractError(
                        f"bindings.{field} does not match the exact route evidence"
                    )
        for field in ("quantization", "tokenizer", "chat_template"):
            _require_string(bindings.get(field), f"bindings.{field}")
        if benchmark_route and str(bindings.get("quantization")) != str(
            evidence.get("quantization") or ""
        ):
            raise WorkerContractError(
                "bindings.quantization does not match the exact route evidence"
            )
        if _require_string(bindings.get("serving_engine"), "bindings.serving_engine") != "ollama":
            raise WorkerContractError("bindings.serving_engine must be ollama")
        if (
            _require_string(
                bindings.get("codex_event_protocol"),
                "bindings.codex_event_protocol",
            )
            != "aicli.machine-event.v1"
        ):
            raise WorkerContractError(
                "bindings.codex_event_protocol must be aicli.machine-event.v1"
            )
        aicli = _require_object(bindings.get("aicli"), "bindings.aicli")
        aicli_binding = {
            "entry_sha256": _require_sha256(
                aicli.get("entry_sha256"), "bindings.aicli.entry_sha256"
            ),
            "version": _require_string(aicli.get("version"), "bindings.aicli.version"),
        }
        if benchmark_route:
            aicli_binding["codex_cli_version"] = _require_string(
                aicli.get("codex_cli_version"),
                "bindings.aicli.codex_cli_version",
            )
            expected_aicli_entry = str(evidence.get("aicli_entry_sha256") or "")
            if aicli_binding["entry_sha256"] != f"sha256:{expected_aicli_entry}":
                raise WorkerContractError(
                    "bindings.aicli.entry_sha256 does not match the route evidence"
                )
            if aicli_binding["version"] != str(evidence.get("aicli_version") or ""):
                raise WorkerContractError(
                    "bindings.aicli.version does not match the route evidence"
                )
            if aicli_binding["codex_cli_version"] != str(
                evidence.get("codex_cli_version") or ""
            ):
                raise WorkerContractError(
                    "bindings.aicli.codex_cli_version does not match the route evidence"
                )
        toolkit_source_sha256 = _require_sha256(
            bindings.get("toolkit_source_sha256"),
            "bindings.toolkit_source_sha256",
        )
        broker = _require_object(bindings.get("broker"), "bindings.broker")
        broker_binding = {
            "id": _require_string(broker.get("id"), "bindings.broker.id"),
            "lease_id": _require_string(
                broker.get("lease_id"), "bindings.broker.lease_id"
            ),
            "serialization": _require_string(
                broker.get("serialization"), "bindings.broker.serialization"
            ),
        }
        if broker_binding["id"] != "LocalGpuBroker" or broker_binding["serialization"] != "exclusive":
            raise WorkerContractError(
                "bindings.broker must be an exclusive LocalGpuBroker lease"
            )
        context = _require_object(bindings.get("context"), "bindings.context")
        expected_total_tokens = int(config.get("context_window_tokens") or 0)
        expected_output_tokens = int(
            config.get("reserved_output_tokens")
            or (32768 if not benchmark_route else 0)
        )
        if (
            context.get("total_tokens") != expected_total_tokens
            or context.get("reserved_output_tokens") != expected_output_tokens
        ):
            raise WorkerContractError(
                "local_async_job context must exactly match its bound backend"
            )
        if benchmark_route:
            options = config.get("ollama_options") or {}
            if (
                evidence.get("context_window_tokens") != expected_total_tokens
                or evidence.get("reserved_output_tokens") != expected_output_tokens
                or options.get("num_ctx") != expected_total_tokens
                or options.get("num_predict") != expected_output_tokens
            ):
                raise WorkerContractError(
                    "benchmark route does not preserve its exact context/output artifact binding"
                )
        reasoning = _require_object(bindings.get("reasoning"), "bindings.reasoning")
        expected_effort = str(route.get("reasoning_effort") or "")
        if reasoning.get("mode") != "on" or reasoning.get("effort") != expected_effort:
            raise WorkerContractError("bindings.reasoning does not match the exact route")
        if (request.get("reasoning") or {}).get("mode") != reasoning.get("mode"):
            raise WorkerContractError("request.reasoning.mode must match bindings.reasoning.mode")

        workspace_binding = {
            "canonical_path": str(canonical_workspace),
            "device": int(getattr(validated_workspace, "_device")),
            "inode": int(getattr(validated_workspace, "_inode")),
            "identity_current": True,
        }
        envelope_scope = {
            "schema": envelope["schema"],
            "task_id": task_id,
            "fresh_execution": True,
            "read_roots": canonical_read_roots,
            "write_root": str(canonical_workspace),
            "expected_artifacts": expected_artifacts,
            "acceptance": acceptance,
            "constraints": constraints,
            "request": request,
        }
        task_binding = {
            "task_id": task_id,
            "envelope_sha256": _canonical_digest(envelope_scope),
            "workspace_sha256": _canonical_digest(workspace_binding),
            "request_sha256": _canonical_digest(request),
        }
        benchmark_registry_source = (
            _benchmark_registry_source(self.registry, backend_alias)
            if benchmark_route
            else None
        )
        requested_binding = {
            "schema": LOCAL_ASYNC_WORKER_REQUESTED_BINDING_SCHEMA,
            "binding_kind": (
                "local_codex_benchmark" if benchmark_route else "local_default"
            ),
            "executor": {"kind": "local_async_job", "native_subagent": False},
            "backend": {
                "alias": resolved.backend_id,
                **(
                    {"provider_model": str(config.get("model") or "")}
                    if benchmark_route
                    else {}
                ),
                "model": str(route.get("model") or ""),
                "artifact_digest": artifact_digest,
                "parent_model": parent_model,
                **(
                    {
                        "alias_manifest_digest": artifact_digest,
                        "parent_model_digest": benchmark_identity[
                            "parent_model_digest"
                        ],
                        "model_layer_digest": benchmark_identity[
                            "model_layer_digest"
                        ],
                        "parameters_digest": benchmark_identity[
                            "parameters_digest"
                        ],
                    }
                    if benchmark_route
                    else {}
                ),
                "quantization": str(bindings["quantization"]),
                "tokenizer": str(bindings["tokenizer"]),
                "chat_template": str(bindings["chat_template"]),
            },
            "harness": {
                "runner": harness,
                "profile": profile,
                "aicli_entry_sha256": aicli_binding["entry_sha256"],
                "aicli_version": aicli_binding["version"],
                **(
                    {
                        "codex_cli_version": aicli_binding[
                            "codex_cli_version"
                        ],
                        "profile_fingerprint": benchmark_identity[
                            "profile_fingerprint"
                        ],
                        "provider_id": benchmark_identity["provider_id"],
                        "wire": benchmark_identity["wire"],
                    }
                    if benchmark_route
                    else {}
                ),
                "event_protocol": "aicli.machine-event.v1",
            },
            "context": {
                "total_tokens": expected_total_tokens,
                "reserved_output_tokens": expected_output_tokens,
                "status": "configured_unverified",
            },
            "reasoning": {"mode": "on", "effort": expected_effort},
            "budget": canonical_budget,
            "serving_engine": "ollama",
            "toolkit_source_sha256": toolkit_source_sha256,
            "broker": broker_binding,
            **({"workspace": workspace_binding} if benchmark_route else {}),
            **(
                {
                    "registry_source": {
                        "schema": benchmark_registry_source["schema"],
                        "backend_id": benchmark_registry_source["backend_id"],
                        "source_sha256": benchmark_registry_source["source_sha256"],
                    }
                }
                if benchmark_registry_source is not None
                else {}
            ),
            **(
                {
                    "network": {
                        "policy": "forbidden",
                        "search": "disabled",
                        "proof_required": (
                            "aicli-source-preflight-runtime-terminal-bound"
                        ),
                    }
                }
                if benchmark_route
                else {}
            ),
            "task": task_binding,
            "verifier": verifier_binding,
            "sandbox": "workspace-write",
            "fallback_used": False,
        }
        controlled_cancel = {
            "schema": CONTROLLED_CANCEL_SCHEMA,
            "executor_kind": "local_async_job",
            "broker": broker_binding,
        }
        return _ValidatedEnvelope(
            task_id=task_id,
            request=request,
            requested_binding=requested_binding,
            controlled_cancel=controlled_cancel,
            benchmark_registry_source=benchmark_registry_source,
        )

    def _handle_from_submission(
        self,
        submission: dict[str, Any],
        contract: dict[str, Any],
        contract_anchor: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema": LOCAL_ASYNC_WORKER_HANDLE_SCHEMA,
            "job_id": submission["job_id"],
            "executor": {"kind": "local_async_job", "native_subagent": False},
            "worker_status": "QUEUED",
            "terminal": False,
            "recommended_check_utc": submission["recommended_check_utc"],
            "monitor_until_utc": submission["monitor_until_utc"],
            "routing_status": "configured_unverified",
            "worker_contract_anchor": _copy_json(contract_anchor),
            "binding_receipt": self._binding_receipt(contract),
        }

    def _contract_for_handle(self, handle: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(handle, dict) or handle.get("schema") != LOCAL_ASYNC_WORKER_HANDLE_SCHEMA:
            raise WorkerContractError("worker handle schema is unsupported")
        job_id = _require_string(handle.get("job_id"), "handle.job_id")
        executor = _require_object(handle.get("executor"), "handle.executor")
        if executor != {"kind": "local_async_job", "native_subagent": False}:
            raise WorkerContractError("handle does not identify a local_async_job")
        try:
            contract, durable_anchor = self.store.read_worker_contract_binding(job_id)
        except ValueError as exc:
            raise WorkerContractError(
                "durable local worker contract lost its creation binding"
            ) from exc
        if contract.get("schema") != LOCAL_ASYNC_WORKER_CONTRACT_SCHEMA:
            raise WorkerContractError("durable local worker contract is incompatible")
        handle_anchor = _require_object(
            handle.get("worker_contract_anchor"),
            "handle.worker_contract_anchor",
        )
        if handle_anchor != durable_anchor:
            raise WorkerContractError(
                "handle worker contract anchor does not match the durable job"
            )
        _validate_worker_contract_anchor(contract, durable_anchor)
        if contract.get("requested", {}).get("binding_kind") == "local_codex_benchmark":
            registry_from_worker_contract(
                contract,
                expected_anchor=durable_anchor,
            )
        handle_receipt = _require_object(handle.get("binding_receipt"), "handle.binding_receipt")
        if handle_receipt.get("requested") != contract.get("requested"):
            raise WorkerContractError("handle requested binding does not match the durable job")
        return contract

    def _update_contract(
        self,
        job_id: str,
        contract: dict[str, Any],
        *,
        observed: dict[str, Any],
        verification: dict[str, Any],
        routing_status: str,
    ) -> dict[str, Any]:
        updated = _copy_json(contract)
        updated["observed"] = observed
        updated["verification"] = verification
        updated["routing_status"] = routing_status
        self.store.record_worker_contract(job_id, updated)
        return updated

    @staticmethod
    def _binding_receipt(contract: dict[str, Any]) -> dict[str, Any]:
        receipt = {
            "schema": LOCAL_ASYNC_WORKER_BINDING_RECEIPT_SCHEMA,
            "requested": _copy_json(contract["requested"]),
            "observed": _copy_json(contract["observed"]),
            "verification": _copy_json(contract["verification"]),
            "routing_status": str(contract.get("routing_status") or "not_ready"),
        }
        if contract["requested"].get("binding_kind") == "local_codex_benchmark":
            receipt["registry_source"] = _copy_json(
                contract["requested"].get("registry_source") or {}
            )
        return receipt

    @staticmethod
    def _state_name(state: dict[str, Any]) -> str:
        controlled = state.get("controlled_cancel")
        if (
            str(state.get("job_status") or "") == "cancellation_requested"
            and isinstance(controlled, dict)
            and controlled.get("status") == "cleanup_unconfirmed"
        ):
            return "cleanup_unconfirmed"
        job_status = str(state.get("job_status") or "")
        if job_status == "queued":
            return "QUEUED"
        if job_status == "running":
            phase = str(state.get("worker_phase") or "")
            return {
                "input_spooling": "VALIDATING",
                "inputs_captured": "GPU_LEASED",
                "provider_running": "RUNNING",
            }.get(phase, "RUNNING")
        if job_status == "cancellation_requested":
            return "CANCELLATION_REQUESTED"
        if job_status == "cancelled":
            return "CANCELLED"
        if job_status == "failed":
            return "FAILED"
        if job_status == "completed":
            result_status = str(state.get("result_status") or "")
            if result_status == "partial":
                return "PARTIAL"
            if result_status == "blocked":
                return "BLOCKED"
            if result_status == "failed":
                return "FAILED"
            return "COMPLETED"
        return "BLOCKED"

    @staticmethod
    def _blocked(
        category: str,
        summary: str,
        *,
        worker_status: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        result = {
            "status": "blocked",
            "error": {"category": category, "summary": summary, "retryable": False},
        }
        if worker_status is not None:
            result["worker_status"] = worker_status
        result.update(extra)
        return result

    @staticmethod
    def _benchmark_observed_binding(
        requested: dict[str, Any],
        observation: Any,
        registry_source: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(observation, dict):
            return None
        route = observation.get("route")
        route = route if isinstance(route, dict) else {}
        evidence = route.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        provider_before = observation.get("provider_before")
        provider_before = provider_before if isinstance(provider_before, dict) else {}
        provider_after = observation.get("provider_after")
        provider_after = provider_after if isinstance(provider_after, dict) else {}
        before_model = provider_before.get("model")
        before_model = before_model if isinstance(before_model, dict) else {}
        after_model = provider_after.get("model")
        after_model = after_model if isinstance(after_model, dict) else {}
        after_broker = provider_after.get("broker")
        after_broker = after_broker if isinstance(after_broker, dict) else {}
        runtime_identity = observation.get("runtime_identity")
        runtime_identity = (
            runtime_identity if isinstance(runtime_identity, dict) else {}
        )
        aicli_preflight = observation.get("aicli_preflight")
        aicli_preflight = (
            aicli_preflight if isinstance(aicli_preflight, dict) else {}
        )
        observed_profile = aicli_preflight.get("profile")
        observed_profile = (
            observed_profile if isinstance(observed_profile, dict) else {}
        )
        network_proof = aicli_preflight.get("network_proof")
        network_proof = network_proof if isinstance(network_proof, dict) else {}
        usage = observation.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        process_tree = observation.get("process_tree")
        process_tree = process_tree if isinstance(process_tree, dict) else {}
        observed_task = _copy_json(requested.get("task") or {})
        observation_task = observation.get("task")
        if isinstance(observation_task, dict):
            observed_task["request_sha256"] = str(
                observation_task.get("request_sha256") or ""
            )

        def sha256(value: Any) -> str:
            token = str(value or "").lower().removeprefix("sha256:")
            return f"sha256:{token}" if re.fullmatch(r"[0-9a-f]{64}", token) else ""

        expected_backend = requested.get("backend")
        expected_backend = expected_backend if isinstance(expected_backend, dict) else {}
        expected_harness = requested.get("harness")
        expected_harness = expected_harness if isinstance(expected_harness, dict) else {}
        expected_context = requested.get("context")
        expected_context = expected_context if isinstance(expected_context, dict) else {}
        before_current = bool(
            before_model.get("digest")
            and sha256(before_model.get("digest"))
            == expected_backend.get("artifact_digest")
        )
        after_current = bool(
            after_model.get("digest")
            and sha256(after_model.get("digest"))
            == expected_backend.get("artifact_digest")
        )
        context_proven = bool(
            after_current
            and evidence.get("reserved_output_tokens")
            == expected_context.get("reserved_output_tokens")
            and sha256(evidence.get("parameters_digest"))
            == expected_backend.get("parameters_digest")
        )
        broker_released = bool(
            after_broker.get("ok") is True
            and after_broker.get("lease") is None
            and after_broker.get("active_ollama_requests") == 0
        )
        return {
            "schema": LOCAL_ASYNC_WORKER_OBSERVED_BINDING_SCHEMA,
            "executor": {
                "kind": "local_async_job",
                "native_subagent": False,
                "native_agent_type": None,
                "native_lineage": None,
            },
            "backend": {
                "alias": str(observation.get("backend") or ""),
                "provider_model": str(provider_after.get("provider") or ""),
                "model": str(runtime_identity.get("model") or route.get("model") or ""),
                "artifact_digest": sha256(after_model.get("digest")),
                "alias_manifest_digest": sha256(after_model.get("digest")),
                "parent_model": str(after_model.get("parent_model") or ""),
                "parent_model_digest": sha256(evidence.get("parent_model_digest")),
                "model_layer_digest": sha256(evidence.get("model_layer_digest")),
                "parameters_digest": sha256(evidence.get("parameters_digest")),
                "quantization": str(after_model.get("quantization") or ""),
                "tokenizer": str(expected_backend.get("tokenizer") or ""),
                "chat_template": str(expected_backend.get("chat_template") or ""),
            },
            "harness": {
                "runner": str(route.get("runner") or ""),
                "profile": str(observed_profile.get("id") or ""),
                "aicli_entry_sha256": sha256(
                    aicli_preflight.get("entry_sha256")
                ),
                "aicli_version": str(aicli_preflight.get("version") or ""),
                "codex_cli_version": str(runtime_identity.get("cli_version") or ""),
                "profile_fingerprint": str(
                    observed_profile.get("fingerprint") or ""
                ),
                "provider_id": str(observed_profile.get("provider_id") or ""),
                "wire": str(evidence.get("wire") or ""),
                "event_protocol": str(expected_harness.get("event_protocol") or ""),
            },
            "registry_source": _copy_json(
                registry_source if isinstance(registry_source, dict) else {}
            ),
            "runtime_identity": _copy_json(runtime_identity),
            "network": _copy_json(network_proof),
            "budget_mode": str(observation.get("budget_mode") or ""),
            "limit_enforcement": _copy_json(
                observation.get("limit_enforcement") or {}
            ),
            "context": {
                "total_tokens": evidence.get("context_window_tokens"),
                "reserved_output_tokens": evidence.get("reserved_output_tokens"),
                "status": "exact_observed" if context_proven else "incomplete",
                "runtime_proof": {
                    "protocol": str(expected_harness.get("event_protocol") or ""),
                    "status": "proven" if context_proven else "incomplete",
                    "total_tokens": evidence.get("context_window_tokens"),
                    "reserved_output_tokens": evidence.get(
                        "reserved_output_tokens"
                    ),
                    "total_source": "ollama.alias-manifest.parameters-layer",
                    "reserved_output_source": (
                        "ollama.alias-manifest.parameters-layer"
                    ),
                    "alias_manifest_digest": sha256(after_model.get("digest")),
                    "parameters_digest": sha256(evidence.get("parameters_digest")),
                    "provider_model_info_context_tokens": after_model.get(
                        "context_length"
                    ),
                    "aicli_catalog_effective_context_tokens": usage.get(
                        "context_window_tokens"
                    ),
                },
            },
            "reasoning": {
                "mode": "on",
                "effort": str(route.get("reasoning_effort") or ""),
            },
            "budget": _copy_json(observation.get("budget") or {}),
            "serving_engine": requested.get("serving_engine"),
            "toolkit_source_sha256": requested.get("toolkit_source_sha256"),
            "broker": {
                **_copy_json(requested.get("broker") or {}),
                "lease_status": "released" if broker_released else "unconfirmed",
                "endpoint_status": "clear" if broker_released else "unconfirmed",
            },
            "workspace": _copy_json(observation.get("workspace") or {}),
            "task": observed_task,
            "verifier": _copy_json(requested.get("verifier") or {}),
            "sandbox": str(observation.get("policy") or ""),
            "fallback_used": observation.get("fallback_used"),
            "cleanup": {
                "process_tree_confirmed": bool(
                    process_tree.get("cleanup_confirmed")
                ),
                "process_tree_method": str(
                    process_tree.get("cleanup_method") or ""
                ),
                "gpu_lease_released": broker_released,
            },
            "provider_checks": {
                "before_current": before_current,
                "after_current": after_current,
            },
        }

    @staticmethod
    def _verify_observed_binding(
        requested: dict[str, Any],
        observed: Any,
    ) -> list[str]:
        if not isinstance(observed, dict):
            return ["observed_binding"]
        failures: list[str] = []
        if observed.get("schema") != LOCAL_ASYNC_WORKER_OBSERVED_BINDING_SCHEMA:
            failures.append("schema")
        executor = observed.get("executor")
        if executor != {
            "kind": "local_async_job",
            "native_subagent": False,
            "native_agent_type": None,
            "native_lineage": None,
        }:
            failures.append("native_identity_absence")
        benchmark = requested.get("binding_kind") == "local_codex_benchmark"
        exact_fields = [
            "backend",
            "harness",
            "reasoning",
            "budget",
            "task",
            "verifier",
        ]
        if benchmark:
            exact_fields.append("workspace")
        for field in exact_fields:
            if observed.get(field) != requested.get(field):
                failures.append(field)
        if observed.get("serving_engine") != requested.get("serving_engine"):
            failures.append("serving_engine")
        if observed.get("toolkit_source_sha256") != requested.get("toolkit_source_sha256"):
            failures.append("toolkit_source_sha256")
        if observed.get("sandbox") != requested.get("sandbox"):
            failures.append("sandbox")
        if observed.get("fallback_used") is not False:
            failures.append("fallback_used")
        context = observed.get("context")
        expected_context = requested.get("context")
        if not isinstance(context, dict) or not isinstance(expected_context, dict):
            failures.append("context")
        else:
            for field in ("total_tokens", "reserved_output_tokens"):
                if context.get(field) != expected_context.get(field):
                    failures.append(f"context.{field}")
            proof = context.get("runtime_proof")
            common_proof_invalid = bool(
                not isinstance(proof, dict)
                or proof.get("protocol")
                != requested["harness"]["event_protocol"]
                or proof.get("status") != "proven"
                or proof.get("total_tokens") != expected_context["total_tokens"]
                or proof.get("reserved_output_tokens")
                != expected_context["reserved_output_tokens"]
            )
            benchmark_proof_invalid = bool(
                benchmark
                and (
                    context.get("status") != "exact_observed"
                    or not isinstance(proof, dict)
                    or proof.get("total_source")
                    != "ollama.alias-manifest.parameters-layer"
                    or proof.get("reserved_output_source")
                    != "ollama.alias-manifest.parameters-layer"
                    or proof.get("alias_manifest_digest")
                    != requested["backend"]["alias_manifest_digest"]
                    or proof.get("parameters_digest")
                    != requested["backend"]["parameters_digest"]
                )
            )
            legacy_proof_invalid = bool(
                not benchmark and context.get("status") != "runtime_proven"
            )
            if common_proof_invalid or benchmark_proof_invalid or legacy_proof_invalid:
                failures.append("runtime_context_proof")
        if benchmark:
            requested_source = requested.get("registry_source")
            observed_source = observed.get("registry_source")
            valid_source = bool(
                isinstance(requested_source, dict)
                and set(requested_source)
                == {"schema", "backend_id", "source_sha256"}
                and requested_source.get("schema")
                == LOCAL_CODEX_BENCHMARK_REGISTRY_SOURCE_SCHEMA
                and requested_source.get("backend_id")
                == requested.get("backend", {}).get("alias")
                and _SHA256.fullmatch(
                    str(requested_source.get("source_sha256") or "")
                )
                and observed_source == requested_source
            )
            if not valid_source:
                failures.append("registry_source")
            network = observed.get("network")
            requested_network = requested.get("network")
            runtime_identity = observed.get("runtime_identity")
            permission = (
                runtime_identity.get("permission")
                if isinstance(runtime_identity, dict)
                else None
            )
            if (
                not isinstance(runtime_identity, dict)
                or runtime_identity.get("model") != requested["backend"]["model"]
                or runtime_identity.get("model_provider")
                != requested["harness"]["provider_id"]
                or runtime_identity.get("cli_version")
                != requested["harness"]["codex_cli_version"]
                or not isinstance(permission, dict)
                or permission.get("approval_policy") != "never"
                or permission.get("requested_policy") != requested["sandbox"]
            ):
                failures.append("runtime_identity")
            source_contract = (
                network.get("source_contract")
                if isinstance(network, dict)
                else None
            )
            request_contract = (
                network.get("request_contract")
                if isinstance(network, dict)
                else None
            )
            process_receipt = (
                network.get("process_receipt")
                if isinstance(network, dict)
                else None
            )
            machine_receipt = (
                network.get("machine_event_receipt")
                if isinstance(network, dict)
                else None
            )
            expected_request_contract = {
                "engine": "codex",
                "profile": requested["harness"]["profile"],
                "profile_fingerprint": requested["harness"][
                    "profile_fingerprint"
                ],
                "model": requested["backend"]["model"],
                "provider_id": requested["harness"]["provider_id"],
                "sandbox_policy": requested["sandbox"],
                "network": "forbidden",
                "search": "disabled",
                "runtime_permission": {
                    "approval_policy": "never",
                    "requested_policy": requested["sandbox"],
                    "sandbox_boundary": "outer-codex",
                    "sandbox_type": "externalSandbox",
                },
            }
            expected_machine_identity = {
                "model": requested["backend"]["model"],
                "provider_id": requested["harness"]["provider_id"],
                "cli_version": requested["harness"]["codex_cli_version"],
                "approval_policy": "never",
                "sandbox_policy": requested["sandbox"],
                "sandbox_boundary": "outer-codex",
                "sandbox_type": "externalSandbox",
            }
            network_invalid = bool(
                requested_network
                != {
                    "policy": "forbidden",
                    "search": "disabled",
                    "proof_required": (
                        "aicli-source-preflight-runtime-terminal-bound"
                    ),
                }
                or not isinstance(network, dict)
                or network.get("policy") != "forbidden"
                or network.get("search") != "disabled"
                or network.get("status") != "enforced"
                or network.get("evidence_kind")
                != "aicli-runtime-bound-source-contract"
                or network.get("enforcement") != "network-denied"
                or network.get("sandbox_policy") != requested["sandbox"]
                or network.get("sandbox_boundary") != "outer-codex"
                or network.get("sandbox_type") != "externalSandbox"
                or not isinstance(source_contract, dict)
                or source_contract.get("schema")
                != "llm-backend-toolkit.aicli-network-source-contract.v1"
                or source_contract.get("aicli_version")
                != requested["harness"]["aicli_version"]
                or source_contract.get("source_bundle_sha256")
                != requested["harness"]["aicli_entry_sha256"]
                or network.get("source_bundle_sha256")
                != source_contract.get("source_bundle_sha256")
                or not _SHA256.fullmatch(
                    str(source_contract.get("entry_sha256") or "")
                )
                or network.get("entry_sha256")
                != source_contract.get("entry_sha256")
                or source_contract.get("outer_launcher_network_flag")
                != "--sandbox-state-disable-network"
                or source_contract.get("runtime_sandbox_boundary")
                != "outer-codex"
                or source_contract.get("runtime_sandbox_type")
                != "externalSandbox"
                or source_contract.get("turn_network_access") != "restricted"
                or source_contract.get("terminal_event") != "turn.completed"
                or network.get("source_contract_sha256")
                != _canonical_digest(source_contract)
                or request_contract != expected_request_contract
                or network.get("request_contract_sha256")
                != _canonical_digest(request_contract)
                or network.get("runtime_identity_sha256")
                != _canonical_digest(runtime_identity)
                or not isinstance(process_receipt, dict)
                or network.get("process_receipt_sha256")
                != _canonical_digest(process_receipt)
                or process_receipt.get("runtime_identity") != runtime_identity
                or process_receipt.get("profile_id")
                != requested["harness"]["profile"]
                or process_receipt.get("model") != requested["backend"]["model"]
                or process_receipt.get("model_provider")
                != requested["harness"]["provider_id"]
                or process_receipt.get("sandbox_policy") != requested["sandbox"]
                or process_receipt.get("outer_exit_code") != 0
                or process_receipt.get("child_exit_code") != 0
                or process_receipt.get("timed_out") is not False
                or process_receipt.get("budget_mode") != "watchdog-only"
                or process_receipt.get("budget_mode")
                != observed.get("budget_mode")
                or process_receipt.get("limit_enforcement")
                != observed.get("limit_enforcement")
                or process_receipt.get("limit_hit") is not None
                or not isinstance(process_receipt.get("limit_usage"), dict)
                or process_receipt["limit_usage"].get("cleanupConfirmed")
                is not True
                or process_receipt["limit_usage"].get("cleanupMethod")
                != network.get("cleanup_method")
                or not isinstance(machine_receipt, dict)
                or network.get("machine_event_stream_sha256")
                != _canonical_digest(machine_receipt)
                or machine_receipt.get("schema")
                != "llm-backend-toolkit.aicli-machine-event-receipt.v1"
                or machine_receipt.get("projection") != "aicli.machine-event.v1"
                or machine_receipt.get("status") != "ok"
                or machine_receipt.get("count") != network.get("machine_event_count")
                or type(machine_receipt.get("count")) is not int
                or machine_receipt.get("count") <= 0
                or machine_receipt.get("sequences")
                != list(range(1, machine_receipt.get("count") + 1))
                or not isinstance(machine_receipt.get("kinds"), list)
                or machine_receipt.get("kinds", [])[-1:] != ["turn.completed"]
                or machine_receipt.get("runtime_identity")
                != expected_machine_identity
                or machine_receipt.get("terminal")
                != {
                    "kind": "turn.completed",
                    "status": "completed",
                    "sequence": machine_receipt.get("count"),
                }
                or network.get("terminal_event") != "turn.completed"
                or network.get("terminal_sequence")
                != network.get("machine_event_count")
                or network.get("cleanup_confirmed") is not True
                or not str(network.get("cleanup_method") or "")
            )
            if network_invalid:
                failures.append("network_proof")
            if observed.get("budget_mode") != "watchdog-only":
                failures.append("budget_mode")
            enforcement = observed.get("limit_enforcement")
            if (
                not isinstance(enforcement, dict)
                or enforcement.get("timeout") != "hard"
                or enforcement.get("maxSteps") != "not-configured"
                or enforcement.get("maxToolCalls") != "not-configured"
            ):
                failures.append("watchdog_enforcement")
            provider_checks = observed.get("provider_checks")
            if provider_checks != {
                "before_current": True,
                "after_current": True,
            }:
                failures.append("provider_checks")
        broker = observed.get("broker")
        requested_broker = requested.get("broker")
        if (
            not isinstance(broker, dict)
            or not isinstance(requested_broker, dict)
            or any(broker.get(key) != value for key, value in requested_broker.items())
        ):
            failures.append("broker")
        if not isinstance(broker, dict) or broker.get("lease_status") != "released":
            failures.append("broker_lease_release")
        cleanup = observed.get("cleanup")
        if (
            not isinstance(cleanup, dict)
            or cleanup.get("process_tree_confirmed") is not True
            or cleanup.get("gpu_lease_released") is not True
        ):
            failures.append("cleanup")
        elif benchmark and not str(cleanup.get("process_tree_method") or ""):
            failures.append("process_tree_cleanup_method")
        return failures
