"""Ephemeral, fail-closed routes used only by agent acceptance campaigns."""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from .backends import BackendRegistry, SAFE_ID


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _safe_id(value: str, field: str) -> str:
    token = str(value or "")
    if not SAFE_ID.fullmatch(token):
        raise ValueError(field)
    return token


def _loopback_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.port
    ):
        raise ValueError("base_url must be an explicit loopback HTTP endpoint")
    return value.rstrip("/")


def build_local_codex_benchmark_registry(
    source_registry: Mapping[str, Any],
    *,
    backend_id: str,
    provider_model: str,
    route_model: str,
    profile: str,
    context_window_tokens: int,
    model_digest: str,
    parent_model: str,
    reserved_output_tokens: int = 32768,
    provider_id: str = "",
    wire: str = "responses",
    parent_model_digest: str = "",
    model_layer_digest: str = "",
    parameters_digest: str = "",
    quantization: str = "",
    profile_fingerprint: str = "",
    aicli_entry_sha256: str = "",
    aicli_version: str = "",
    codex_cli_version: str = "",
    base_url: str = "http://127.0.0.1:32100",
) -> BackendRegistry:
    """Return an in-memory exact route without changing any live/default route.

    The provider-facing model may be a content-addressed local alias while the
    Codex profile uses the same alias or another exact model tag.  The route is
    accepted only while the provider status proves both the declared artifact
    digest and parent model.  It is intentionally neither a default nor an
    alias/fallback target.
    """

    if not isinstance(source_registry, Mapping):
        raise ValueError("source_registry")
    backend_id = _safe_id(backend_id, "backend_id")
    provider_model = _safe_id(provider_model, "provider_model")
    route_model = _safe_id(route_model, "route_model")
    profile = _safe_id(profile, "profile")
    if provider_id:
        provider_id = _safe_id(provider_id, "provider_id")
    if wire != "responses":
        raise ValueError("wire")
    digest = str(model_digest or "").lower()
    if not _SHA256.fullmatch(digest):
        raise ValueError("model_digest")
    optional_digests = {
        "parent_model_digest": parent_model_digest,
        "model_layer_digest": model_layer_digest,
        "parameters_digest": parameters_digest,
        "profile_fingerprint": profile_fingerprint,
        "aicli_entry_sha256": aicli_entry_sha256,
    }
    normalized_digests: dict[str, str] = {}
    for field, value in optional_digests.items():
        token = str(value or "").lower().removeprefix("sha256:")
        if token and not _SHA256.fullmatch(token):
            raise ValueError(field)
        normalized_digests[field] = token
    parent_model = str(parent_model or "").strip()
    if not parent_model:
        raise ValueError("parent_model")
    if (
        type(context_window_tokens) is not int
        or not 1024 <= context_window_tokens <= 1_048_576
    ):
        raise ValueError("context_window_tokens")
    if (
        type(reserved_output_tokens) is not int
        or not 1 <= reserved_output_tokens < context_window_tokens
    ):
        raise ValueError("reserved_output_tokens")
    quantization = str(quantization or "").strip()
    aicli_version = str(aicli_version or "").strip()
    codex_cli_version = str(codex_cli_version or "").strip()
    endpoint = _loopback_url(base_url)

    route_evidence = {
        "basis": "benchmark_only",
        "live_verified": True,
        "model_digest": digest,
        "alias_manifest_digest": digest,
        "parent_model": parent_model,
        "context_window_tokens": context_window_tokens,
        "reserved_output_tokens": reserved_output_tokens,
        "wire": wire,
    }
    optional_evidence = {
        "provider_id": provider_id,
        "parent_model_digest": normalized_digests["parent_model_digest"],
        "model_layer_digest": normalized_digests["model_layer_digest"],
        "parameters_digest": normalized_digests["parameters_digest"],
        "quantization": quantization,
        "profile_fingerprint": normalized_digests["profile_fingerprint"],
        "aicli_entry_sha256": normalized_digests["aicli_entry_sha256"],
        "aicli_version": aicli_version,
        "codex_cli_version": codex_cli_version,
    }
    route_evidence.update(
        {key: value for key, value in optional_evidence.items() if value}
    )

    payload = copy.deepcopy(dict(source_registry))
    backends = payload.get("backends")
    if not isinstance(backends, dict):
        raise ValueError("source_registry.backends")
    if backend_id in backends or backend_id in (payload.get("aliases") or {}):
        raise ValueError("backend_id already exists")
    backends[backend_id] = {
        "adapter": "ollama",
        "model": provider_model,
        "cloud": False,
        "supports_vision": False,
        "context_window_tokens": context_window_tokens,
        "reserved_output_tokens": reserved_output_tokens,
        "routing_role": "benchmark_only",
        "default_reasoning_mode": "on",
        "ollama_options": {
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "repeat_penalty": 1.0,
            "num_ctx": context_window_tokens,
            "num_predict": reserved_output_tokens,
        },
        "base_url_default": endpoint,
        "data_destination": "LocalGpuBroker benchmark-only loopback endpoint",
        "agent_routes": {
            "codex-cli": {
                "runner": "codex-cli",
                "profile": profile,
                "model": route_model,
                "reasoning_effort": "max",
                "evidence": route_evidence,
            }
        },
    }
    return BackendRegistry.from_dict(payload, source="benchmark-memory")


__all__ = ["build_local_codex_benchmark_registry"]
