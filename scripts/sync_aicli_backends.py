#!/usr/bin/env python3
"""Preview or apply one explicit AICLI-to-Toolkit local route update.

This is a low-frequency maintenance command.  It reads local JSON only and
never starts AICLI, a model, the GPU broker, or a background service.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


MAIN_PROFILES = (
    "codex-ollama-main",
    "claude-ollama-main",
    "qwen-code-ollama-main",
    "opencode-ollama-main",
)
ROUTE_PROFILES = {
    "data_factory": "codex-ollama-main",
    "codex-cli": "codex-ollama-main",
    "claude-code": "claude-ollama-main",
    "qwen-code": "qwen-code-ollama-main",
    "opencode": "opencode-ollama-main",
}
REVIEW_PROFILE = "codex-ollama-review"
EXACT_PROFILE_BACKENDS = {
    "codex-ollama-qwen3-6-35b-abliterated": "local-qwen3-6-35b-abliterated",
    "codex-ollama-qwen3-8-27b-abliterated": "local-qwen3-8-27b-abliterated",
}
EXPECTED_CONTEXT = 262_144
PROFILE_CONTRACTS = {
    "codex-ollama-main": ("codex", "responses"),
    "claude-ollama-main": ("claude", "anthropic-messages"),
    "qwen-code-ollama-main": ("qwen-code", "openai-compatible"),
    "opencode-ollama-main": ("opencode", "openai-compatible"),
    "codex-ollama-review": ("codex", "responses"),
    "codex-ollama-qwen3-6-35b-abliterated": ("codex", "responses"),
    "codex-ollama-qwen3-8-27b-abliterated": ("codex", "responses"),
}


class SyncError(ValueError):
    pass


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SyncError(f"JSON object required: {path}")
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _data_dir(aicli_root: Path) -> Path:
    root = aicli_root.resolve()
    candidate = root / "data"
    if (candidate / "providers").is_dir() and (candidate / "model-catalogs").is_dir():
        return candidate
    raise SyncError(f"AICLI root must directly contain data/providers and data/model-catalogs: {root}")


def _origin(value: object, profile_id: str) -> str:
    parsed = urlparse(str(value or ""))
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or not parsed.port
    ):
        raise SyncError(f"{profile_id} has an invalid endpoint")
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


def _display_name(raw: dict, model: str) -> str:
    value = str(raw.get("displayName") or "").strip()
    if "+" in value:
        value = value.rsplit("+", 1)[1].strip()
    return value or model


def _catalog_entry(data: Path, raw: dict, model: str, profile_id: str) -> tuple[dict, dict]:
    name = str(raw.get("codexModelCatalog") or "").strip()
    if not name:
        if profile_id in {"codex-ollama-main", REVIEW_PROFILE}:
            raise SyncError(f"{profile_id} requires a Codex model catalog")
        return {}, {}
    path = data / "model-catalogs" / name
    catalog = _read_json(path)
    models = catalog.get("models")
    if not isinstance(models, list):
        raise SyncError(f"{profile_id} catalog lacks models")
    entries = [entry for entry in models if isinstance(entry, dict) and entry.get("slug") == model]
    if len(entries) != 1:
        raise SyncError(f"{profile_id} catalog has no exact entry for {model}")
    entry = entries[0]
    for key in ("context_window", "max_context_window"):
        value = entry.get(key)
        if value is None and profile_id in {"codex-ollama-main", REVIEW_PROFILE}:
            raise SyncError(f"{profile_id} catalog lacks {key}")
        if value is not None and value != EXPECTED_CONTEXT:
            raise SyncError(f"{profile_id} catalog {key} must be {EXPECTED_CONTEXT}")
    levels = entry.get("supported_reasoning_levels")
    if profile_id in {"codex-ollama-main", REVIEW_PROFILE} and entry.get("default_reasoning_level") != "max":
        raise SyncError(f"{profile_id} catalog default reasoning level must be max")
    if levels is None or not isinstance(levels, list) or "max" not in {item.get("effort") for item in levels if isinstance(item, dict)}:
        raise SyncError(f"{profile_id} catalog must support max effort")
    return catalog, entry


def _profile(data: Path, profile_id: str) -> dict:
    raw = _read_json(data / "providers" / f"{profile_id}.json")
    if raw.get("id") != profile_id:
        raise SyncError(f"Provider file identity mismatch for {profile_id}")
    models = raw.get("models")
    if not isinstance(models, dict) or not isinstance(models.get("primary"), str) or not models["primary"]:
        raise SyncError(f"{profile_id} has no primary model")
    model = models["primary"]
    candidates = models.get("candidates", [model])
    if isinstance(candidates, str):
        candidates = [candidates]
    if (
        not isinstance(candidates, list)
        or any(not isinstance(candidate, str) for candidate in candidates)
        or set(candidates) != {model}
        or models.get("small", model) != model
    ):
        raise SyncError(f"{profile_id} must expose exactly one model")
    for key in ("engine", "provider", "transport"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise SyncError(f"{profile_id} has no {key}")
    if raw["provider"] != "ollama":
        raise SyncError(f"{profile_id} provider must remain ollama")
    expected_engine, expected_transport = PROFILE_CONTRACTS[profile_id]
    if raw["engine"] != expected_engine or raw["transport"] != expected_transport:
        raise SyncError(f"{profile_id} engine or transport drifted")
    auth = raw.get("auth")
    if not isinstance(auth, dict) or auth.get("type") != "none":
        raise SyncError(f"{profile_id} auth must remain none")
    metadata = raw.get("modelMetadata") or {}
    item = metadata.get(model) if isinstance(metadata, dict) else {}
    if item and not isinstance(item, dict):
        raise SyncError(f"{profile_id} has invalid model metadata")
    artifact = ((raw.get("compatibility") or {}).get("ollamaArtifact") or {})
    if artifact and not isinstance(artifact, dict):
        raise SyncError(f"{profile_id} has invalid ollama artifact")
    catalog, catalog_model = _catalog_entry(data, raw, model, profile_id)
    contexts = [
        item.get("contextWindowTokens") if isinstance(item, dict) else None,
        artifact.get("numCtx") if isinstance(artifact, dict) else None,
        catalog_model.get("context_window") if isinstance(catalog_model, dict) else None,
    ]
    contexts = [value for value in contexts if isinstance(value, int)]
    if not contexts:
        raise SyncError(f"{profile_id} has no context capacity")
    outputs = [item.get("outputWindowTokens") if isinstance(item, dict) else None]
    outputs = [value for value in outputs if isinstance(value, int)]
    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, dict):
        raise SyncError(f"{profile_id} has no capabilities")
    for key in ("tools", "streaming", "images", "machineRun"):
        if not isinstance(capabilities.get(key), bool):
            raise SyncError(f"{profile_id} has no {key} capability")
    for key in ("tools", "streaming", "machineRun"):
        if capabilities[key] is not True:
            raise SyncError(f"{profile_id} {key} capability must remain true")
    return {
        "id": profile_id,
        "raw": raw,
        "catalog": catalog,
        "model": model,
        "display_name": _display_name(raw, model),
        "origin": _origin(raw.get("endpoint"), profile_id),
        "contexts": contexts,
        "outputs": outputs,
        "images": capabilities["images"],
    }


def _consistent(label: str, values: list[object]) -> object:
    distinct = {value for value in values}
    if len(distinct) != 1:
        raise SyncError(f"{label} differs: {sorted(distinct)!r}")
    return next(iter(distinct))


def _profile_fingerprints(aicli_root: Path, profile_ids: tuple[str, ...]) -> dict[str, str]:
    root = aicli_root.resolve()
    pwsh = "pwsh.exe" if os.name == "nt" else "pwsh"
    source_entry = root / "bin" / "aicli.ps1"
    module_manifest = root / "AiCliProfileManager.psd1"
    fingerprints: dict[str, str] = {}
    for profile_id in profile_ids:
        if source_entry.is_file():
            command = [pwsh, "-NoProfile", "-File", str(source_entry), "profile", "show", profile_id, "--json"]
        elif module_manifest.is_file():
            manifest_literal = str(module_manifest).replace("'", "''")
            command = [
                pwsh,
                "-NoProfile",
                "-Command",
                f"Import-Module '{manifest_literal}' -Force; exit (Invoke-AiCli -Tokens @('profile','show','{profile_id}','--json'))",
            ]
        else:
            raise SyncError(f"AICLI root has neither bin/aicli.ps1 nor AiCliProfileManager.psd1: {root}")
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=30)
        except FileNotFoundError as exc:
            raise SyncError("PowerShell 7 is unavailable for AICLI profile show") from exc
        except subprocess.TimeoutExpired as exc:
            raise SyncError(f"AICLI profile show timed out for {profile_id}") from exc
        if completed.returncode != 0:
            raise SyncError(f"AICLI profile show failed for {profile_id}")
        try:
            envelope = json.loads(completed.stdout)
            fingerprint = str((envelope.get("profile") or {}).get("profileFingerprint") or "")
        except json.JSONDecodeError as exc:
            raise SyncError(f"AICLI profile show returned invalid JSON for {profile_id}") from exc
        if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            raise SyncError(f"AICLI profile show has no valid fingerprint for {profile_id}")
        fingerprints[profile_id] = fingerprint
    return fingerprints


def _replace_evidence(route: dict, *, fingerprint: str, profile: dict) -> None:
    route["evidence"] = {
        "basis": "aicli_registry_sync_pending_reacceptance",
        "live_verified": False,
        "evidence_state": "unverified",
        "receipt_schema": "aicli.agent.acceptance-receipt.v1",
        "receipt_authority": "aicli",
        "capability_acceptance_state": "pending_reacceptance",
        "identity_scope": "current-model-unverified",
        "profile_fingerprint": fingerprint,
        "provider_id": str(profile["raw"].get("codexProviderId") or ""),
        "wire": str(profile["raw"].get("transport") or ""),
    }


def _validate_output(config: dict, limit: int, label: str) -> None:
    options = config.get("ollama_options") or {}
    value = options.get("num_predict") if isinstance(options, dict) else None
    if not isinstance(value, int) or value < 1 or value > limit:
        raise SyncError(f"{label} num_predict must be an integer no greater than AICLI output capacity {limit}")


def _field_changes(before: dict, after: dict, fields: tuple[str, ...]) -> list[dict]:
    return [
        {"field": field, "before": before.get(field), "after": after.get(field)}
        for field in fields
        if before.get(field) != after.get(field)
    ]


def _evidence_summary(value: dict) -> dict:
    return {
        "state": value.get("evidence_state"),
        "live_verified": value.get("live_verified"),
        "profile_fingerprint": value.get("profile_fingerprint"),
    }


def _apply_backend(config: dict, *, model: str, display_name: str, origin: str, images: bool, label: str) -> bool:
    if config.get("context_window_tokens") != EXPECTED_CONTEXT:
        raise SyncError(f"{label} context_window_tokens must remain {EXPECTED_CONTEXT}")
    options = config.get("ollama_options") or {}
    if not isinstance(options, dict) or options.get("num_ctx") != EXPECTED_CONTEXT:
        raise SyncError(f"{label} ollama_options.num_ctx must remain {EXPECTED_CONTEXT}")
    before = _canonical_bytes(config)
    config["model"] = model
    config["display_name"] = display_name
    config["base_url_default"] = origin
    config["supports_vision"] = images
    return before != _canonical_bytes(config)


def build_plan(
    registry: dict,
    aicli_data: Path,
    *,
    main_direct_vision: bool | None = None,
    review_direct_vision: bool | None = None,
    profile_fingerprints: dict[str, str],
) -> tuple[dict, list[dict]]:
    backends = registry.get("backends")
    if not isinstance(backends, dict):
        raise SyncError("Toolkit registry lacks backends")
    for name in ("local-default", "local-hard-reasoning", "local-crosscheck-35b"):
        if not isinstance(backends.get(name), dict):
            raise SyncError(f"Toolkit registry lacks {name}")
    main_profiles = [_profile(aicli_data, name) for name in MAIN_PROFILES]
    review = _profile(aicli_data, REVIEW_PROFILE)
    fingerprints = profile_fingerprints
    if set(fingerprints) != {*MAIN_PROFILES, REVIEW_PROFILE, *EXACT_PROFILE_BACKENDS}:
        raise SyncError("AICLI profile fingerprint set is incomplete")
    main_model = _consistent("AICLI main models", [profile["model"] for profile in main_profiles])
    main_origin = _consistent("AICLI main endpoint origins", [profile["origin"] for profile in main_profiles])
    _consistent("AICLI main image capability", [profile["images"] for profile in main_profiles])
    main_contexts = [context for profile in main_profiles for context in profile["contexts"]]
    if any(context != EXPECTED_CONTEXT for context in main_contexts):
        raise SyncError(f"AICLI main context must be {EXPECTED_CONTEXT}: {main_contexts!r}")
    codex_main_outputs = main_profiles[0]["outputs"]
    if not codex_main_outputs:
        raise SyncError("codex-ollama-main provides no output capacity")
    main_output = _consistent("codex-ollama-main output capacity", codex_main_outputs)
    if any(context != EXPECTED_CONTEXT for context in review["contexts"]):
        raise SyncError(f"AICLI review context must be {EXPECTED_CONTEXT}: {review['contexts']!r}")
    candidate = copy.deepcopy(registry)
    candidate_backends = candidate["backends"]
    _validate_output(candidate_backends["local-default"], main_output, "local-default")
    _validate_output(candidate_backends["local-hard-reasoning"], main_output, "local-hard-reasoning")
    main_display = main_profiles[0]["display_name"]
    original_main_model = registry["backends"]["local-default"].get("model")
    original_review_model = registry["backends"]["local-crosscheck-35b"].get("model")
    if review["model"] == main_model:
        raise SyncError("AICLI review model must remain distinct from the main model")
    if main_model != original_main_model and main_direct_vision is None:
        raise SyncError("Main model changed; pass --main-direct-vision true or false for Toolkit direct capability")
    if review["model"] != original_review_model and review_direct_vision is None:
        raise SyncError("Review model changed; pass --review-direct-vision true or false for Toolkit direct capability")
    effective_main_vision = main_direct_vision if main_direct_vision is not None else bool(registry["backends"]["local-default"].get("supports_vision"))
    effective_review_vision = review_direct_vision if review_direct_vision is not None else bool(registry["backends"]["local-crosscheck-35b"].get("supports_vision"))
    main_changed = False
    for name in ("local-default", "local-hard-reasoning"):
        main_changed |= _apply_backend(
            candidate_backends[name],
            model=main_model,
            display_name=main_display,
            origin=main_origin,
            images=effective_main_vision,
            label=name,
        )
    default = candidate_backends["local-default"]
    routes = default.get("agent_routes")
    if not isinstance(routes, dict):
        raise SyncError("local-default lacks agent_routes")
    original_default = registry["backends"]["local-default"]
    original_routes = original_default.get("agent_routes") or {}
    for route_name, profile_id in ROUTE_PROFILES.items():
        route = routes.get(route_name)
        original_route = original_routes.get(route_name)
        if not isinstance(route, dict) or not isinstance(original_route, dict):
            raise SyncError(f"local-default lacks {route_name} route")
        if route.get("profile") != profile_id:
            raise SyncError(f"local-default {route_name} profile must remain {profile_id}")
        if route.get("model") != main_model:
            route["model"] = main_model
            main_changed = True
    for route_name, profile_id in ROUTE_PROFILES.items():
        evidence = original_routes[route_name].get("evidence") or {}
        fingerprint_changed = bool(evidence.get("profile_fingerprint")) and evidence.get("profile_fingerprint") != fingerprints[profile_id]
        if main_changed or fingerprint_changed:
            profile = next(item for item in main_profiles if item["id"] == profile_id)
            _replace_evidence(routes[route_name], fingerprint=fingerprints[profile_id], profile=profile)

    review_config = candidate_backends["local-crosscheck-35b"]
    original_review = registry["backends"]["local-crosscheck-35b"]
    review_changed = _apply_backend(
        review_config,
        model=review["model"],
        display_name=review["display_name"],
        origin=review["origin"],
        images=effective_review_vision,
        label="local-crosscheck-35b",
    )
    review_route = (review_config.get("agent_routes") or {}).get("codex-cli")
    original_review_route = (original_review.get("agent_routes") or {}).get("codex-cli")
    if not isinstance(review_route, dict) or not isinstance(original_review_route, dict):
        raise SyncError("local-crosscheck-35b lacks codex-cli route")
    if review_route.get("profile") != REVIEW_PROFILE:
        raise SyncError("local-crosscheck-35b codex-cli profile must remain codex-ollama-review")
    if review_route.get("model") != review["model"]:
        review_route["model"] = review["model"]
        review_changed = True
    review_evidence = original_review_route.get("evidence") or {}
    review_binding_changed = bool(review_evidence.get("profile_fingerprint")) and review_evidence.get("profile_fingerprint") != fingerprints[REVIEW_PROFILE]
    if review_changed or review_binding_changed:
        _replace_evidence(review_route, fingerprint=fingerprints[REVIEW_PROFILE], profile=review)

    exact_changes = []
    for profile_id, backend_id in EXACT_PROFILE_BACKENDS.items():
        if not isinstance(candidate_backends.get(backend_id), dict):
            raise SyncError(f"Toolkit registry lacks {backend_id}")
        profile = _profile(aicli_data, profile_id)
        if any(context != EXPECTED_CONTEXT for context in profile["contexts"]):
            raise SyncError(f"{profile_id} context must be {EXPECTED_CONTEXT}")
        outputs = profile["outputs"]
        if not outputs:
            raise SyncError(f"{profile_id} provides no output capacity")
        _validate_output(candidate_backends[backend_id], _consistent(f"{profile_id} output capacity", outputs), backend_id)
        config = candidate_backends[backend_id]
        original = registry["backends"][backend_id]
        changed = _apply_backend(config, model=profile["model"], display_name=profile["display_name"], origin=profile["origin"], images=profile["images"], label=backend_id)
        route = (config.get("agent_routes") or {}).get("codex-cli")
        original_route = (original.get("agent_routes") or {}).get("codex-cli")
        if not isinstance(route, dict) or not isinstance(original_route, dict) or route.get("profile") != profile_id:
            raise SyncError(f"{backend_id} lacks its exact {profile_id} codex-cli route")
        if route.get("model") != profile["model"]:
            route["model"] = profile["model"]
            changed = True
        if changed or (original_route.get("evidence") or {}).get("profile_fingerprint") != fingerprints[profile_id]:
            _replace_evidence(route, fingerprint=fingerprints[profile_id], profile=profile)
        if _canonical_bytes(original) != _canonical_bytes(config):
            exact_changes.append({"backend": backend_id, "profile": profile_id})

    changes = []
    if _canonical_bytes(candidate) != _canonical_bytes(registry):
        for backend_id in ("local-default", "local-hard-reasoning", "local-crosscheck-35b"):
            before = registry["backends"][backend_id]
            after = candidate["backends"][backend_id]
            fields = _field_changes(before, after, ("model", "display_name", "base_url_default", "supports_vision"))
            if fields:
                changes.append({"backend": backend_id, "fields": fields})
        for route_name in ROUTE_PROFILES:
            before = original_routes[route_name]
            after = routes[route_name]
            if _canonical_bytes(before) != _canonical_bytes(after):
                fields = _field_changes(before, after, ("model",))
                if _canonical_bytes(before.get("evidence") or {}) != _canonical_bytes(after.get("evidence") or {}):
                    fields.append({"field": "evidence", "before": _evidence_summary(before.get("evidence") or {}), "after": _evidence_summary(after.get("evidence") or {})})
                changes.append({"backend": "local-default", "route": route_name, "fields": fields})
        if _canonical_bytes(original_review_route) != _canonical_bytes(review_route):
            fields = _field_changes(original_review_route, review_route, ("model",))
            if _canonical_bytes(original_review_route.get("evidence") or {}) != _canonical_bytes(review_route.get("evidence") or {}):
                fields.append({"field": "evidence", "before": _evidence_summary(original_review_route.get("evidence") or {}), "after": _evidence_summary(review_route.get("evidence") or {})})
            changes.append({"backend": "local-crosscheck-35b", "route": "codex-cli", "fields": fields})
        changes.extend(exact_changes)
    return candidate, changes


def _write_json(path: Path, value: dict, expected_bytes: bytes) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    encoded = content.encode("utf-8")
    if path.read_bytes() != expected_bytes:
        raise SyncError("Registry changed after preview; refusing to overwrite")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(encoded)
        if path.read_bytes() != expected_bytes:
            raise SyncError("Registry changed before apply; refusing to overwrite")
        temporary.replace(path)
        if path.read_bytes() != encoded:
            raise SyncError("Registry readback differs after apply")
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aicli-root", required=True, type=Path, help="AICLI source root or installed module root")
    parser.add_argument("--registry", type=Path, default=Path(__file__).resolve().parents[1] / "src" / "llm_backend_toolkit" / "default_backends.json")
    parser.add_argument("--main-direct-vision", choices=("true", "false"), help="Required direct Toolkit vision capability when the main model changes")
    parser.add_argument("--review-direct-vision", choices=("true", "false"), help="Required direct Toolkit vision capability when the review model changes")
    parser.add_argument("--apply", action="store_true", help="Write the planned canonical Toolkit registry update")
    args = parser.parse_args(argv)
    try:
        data = _data_dir(args.aicli_root)
        registry_path = args.registry.resolve()
        registry_bytes = registry_path.read_bytes()
        registry = json.loads(registry_bytes.decode("utf-8"))
        if not isinstance(registry, dict):
            raise SyncError("Registry JSON object required")
        fingerprints = _profile_fingerprints(args.aicli_root, (*MAIN_PROFILES, REVIEW_PROFILE, *EXACT_PROFILE_BACKENDS))
        candidate, changes = build_plan(
            registry,
            data,
            main_direct_vision={"true": True, "false": False}.get(args.main_direct_vision),
            review_direct_vision={"true": True, "false": False}.get(args.review_direct_vision),
            profile_fingerprints=fingerprints,
        )
        if args.apply and changes:
            _write_json(registry_path, candidate, registry_bytes)
        print(json.dumps({"status": "applied" if args.apply else "preview", "changed": bool(changes), "changes": changes}, ensure_ascii=False, indent=2))
        return 0
    except SyncError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
