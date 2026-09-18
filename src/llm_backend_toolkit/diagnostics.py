"""No-generation diagnostics. Declared, installed and observed facts stay separate."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from .agent_runners import AiCliProfileRunner, AgentRunnerError, _bounded_process, _json_values


def aicli_diagnostics(entry: str | None, bridge_registry: str | None = None) -> dict[str, Any]:
    if not entry:
        return {"state": "not_configured", "runtime_loaded": "unknown"}
    runner = AiCliProfileRunner(name="codex-cli", engine="codex", default_profile="", entry=entry)
    try:
        prefix = runner._prefix()
        code, output, _, _ = _bounded_process(prefix + ["version", "--json"],
            cwd=Path(entry).resolve().parent, stdin_text="", timeout_seconds=15, max_output_chars=100_000)
        values = _json_values(output)
        capabilities = (values[-1].get("capabilities") or {}) if values else {}
        if code != 0 or not isinstance(capabilities, dict) or capabilities.get("runtimeDiagnostics") != "aicli.runtime-diagnostics.v1":
            return {"state": "unsupported", "entry": str(Path(entry).resolve()), "runtime_loaded": "unknown"}
        command = prefix + ["diagnose", "--json"]
        if bridge_registry:
            command.extend(["--bridge-registry", bridge_registry])
        code, output, _, _ = _bounded_process(command, cwd=Path(entry).resolve().parent,
            stdin_text="", timeout_seconds=20, max_output_chars=200_000)
        values = _json_values(output)
        receipt = (values[-1].get("diagnostics") or {}) if values else {}
        if (code != 0 or not isinstance(receipt, dict) or receipt.get("schema") != "aicli.runtime-diagnostics.v1"
                or receipt.get("write_mode") != "zero_write" or receipt.get("network_performed") is not False
                or receipt.get("model_invoked") is not False or receipt.get("credentials_read") is not False):
            return {"state": "invalid_diagnostic_receipt", "entry": str(Path(entry).resolve())}
        return {"state": "observed", "entry": str(Path(entry).resolve()), "receipt": receipt}
    except (AgentRunnerError, ValueError, OSError):
        return {"state": "unavailable", "entry": str(entry), "runtime_loaded": "unknown"}


def diagnose(toolkit, backend: str | None = None, *, aicli_entry: str | None = None,
             installed_aicli_entry: str | None = None, bridge_registry: str | None = None) -> dict[str, Any]:
    snapshot = {"default_backend": toolkit.registry.default_backend, "backends": toolkit.registry.backends,
                "aliases": toolkit.registry.aliases}
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    result = {"status": "ok", "schema": "llm-backend-toolkit.diagnostics.v1", "write_mode": "zero_write",
              "network_performed": False, "model_invoked": False, "materials_read": False, "job_created": False,
              "runtime": toolkit.runtime_identity(),
              "registry": {"source": toolkit.registry.source, "snapshot_sha256": digest, "hash_basis": "loaded_normalized_registry"},
              "observer_environment": {"user_profile": os.environ.get("USERPROFILE") or os.environ.get("HOME"),
                                       "credential_scope": "caller_process_only"},
              "route": {"state": "unavailable", "live_acceptance": "not_checked"}}
    try:
        resolved, provider = toolkit._resolve_provider(backend)
        result["route"] = {"state": "configured", "backend": toolkit._backend_receipt(resolved),
            "credential_env": resolved.config.get("api_key_env"),
            "credential_present": bool(getattr(provider, "api_key", False)) if resolved.config.get("api_key_env") else None,
            "live_acceptance": "not_checked", "agent_routes": {
                key: {"runner": value.get("runner"), "profile": value.get("profile"),
                      "evidence": toolkit.registry.evaluate_route_evidence(value, None)}
                for key, value in (resolved.config.get("agent_routes") or {}).items()}}
    except (ValueError, OSError):
        result["route"]["error"] = "selected_backend_configuration_invalid"
    entry = aicli_entry or os.environ.get("LLM_TOOLKIT_AICLI_ENTRY")
    bridge = bridge_registry or os.environ.get("LLM_TOOLKIT_AICLI_BRIDGE_REGISTRY")
    result["aicli"] = {"selected_entry": aicli_diagnostics(entry, bridge)}
    if installed_aicli_entry:
        result["aicli"]["installed_entry"] = aicli_diagnostics(installed_aicli_entry, bridge)
    return result
