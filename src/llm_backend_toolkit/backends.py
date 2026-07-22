from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REGISTRY_SCHEMA = "llm-backend-toolkit.backends.v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ResolvedBackend:
    requested: str
    backend_id: str
    config: dict[str, Any]
    default_applied: bool
    alias_applied: bool


class BackendRegistry:
    def __init__(
        self,
        *,
        default_backend: str,
        backends: dict[str, dict[str, Any]],
        aliases: dict[str, str] | None = None,
        source: str = "memory",
    ) -> None:
        self.default_backend = default_backend
        self.backends = backends
        self.aliases = aliases or {}
        self.source = source

    @classmethod
    def default_path(cls) -> Path:
        return Path(__file__).with_name("default_backends.json")

    @classmethod
    def load(cls, path: Path | str | None = None) -> "BackendRegistry":
        configured = path or os.environ.get("LLM_TOOLKIT_BACKEND_REGISTRY")
        source = Path(configured).expanduser().resolve() if configured else cls.default_path()
        value = json.loads(source.read_text(encoding="utf-8"))
        return cls.from_dict(value, source=str(source))

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, source: str = "memory") -> "BackendRegistry":
        if not isinstance(value, dict) or value.get("schema") != REGISTRY_SCHEMA:
            raise ValueError(f"Backend registry schema must be {REGISTRY_SCHEMA}")
        default_backend = str(value.get("default_backend") or "")
        raw_backends = value.get("backends")
        raw_aliases = value.get("aliases") or {}
        if not SAFE_ID.fullmatch(default_backend):
            raise ValueError("Backend registry default_backend is invalid")
        if not isinstance(raw_backends, dict) or not raw_backends:
            raise ValueError("Backend registry requires at least one backend")
        if not isinstance(raw_aliases, dict):
            raise ValueError("Backend registry aliases must be an object")
        backends: dict[str, dict[str, Any]] = {}
        for backend_id, raw in raw_backends.items():
            if not isinstance(backend_id, str) or not SAFE_ID.fullmatch(backend_id):
                raise ValueError(f"Invalid backend ID: {backend_id}")
            if not isinstance(raw, dict):
                raise ValueError(f"Backend {backend_id} must be an object")
            forbidden_secret_fields = {"api_key", "apikey", "authorization", "token", "secret"}
            if forbidden_secret_fields.intersection(str(key).lower() for key in raw):
                raise ValueError(
                    f"Backend {backend_id} must reference credentials by environment variable, not inline values"
                )
            adapter = str(raw.get("adapter") or "")
            model = str(raw.get("model") or "")
            if adapter not in {"ollama", "openai-chat", "agent-only"}:
                raise ValueError(f"Backend {backend_id} has unsupported adapter: {adapter}")
            if not model:
                raise ValueError(f"Backend {backend_id} requires a model")
            routes = raw.get("agent_routes") or {}
            if not isinstance(routes, dict):
                raise ValueError(f"Backend {backend_id} agent_routes must be an object")
            for route_id, route in routes.items():
                if not SAFE_ID.fullmatch(str(route_id)) or not isinstance(route, dict):
                    raise ValueError(f"Backend {backend_id} has an invalid route")
                for required in ("runner", "profile", "model"):
                    if not str(route.get(required) or ""):
                        raise ValueError(f"Backend {backend_id} route {route_id} requires {required}")
            backends[backend_id] = dict(raw)
        if default_backend not in backends:
            raise ValueError("Backend registry default_backend does not exist")
        if bool(backends[default_backend].get("cloud")):
            raise ValueError("Backend registry default_backend must be local")
        aliases: dict[str, str] = {}
        for alias, target in raw_aliases.items():
            if not isinstance(alias, str) or not SAFE_ID.fullmatch(alias):
                raise ValueError(f"Invalid backend alias: {alias}")
            if target not in backends:
                raise ValueError(f"Backend alias {alias} points to an unknown backend")
            aliases[alias] = str(target)
        return cls(
            default_backend=default_backend,
            backends=backends,
            aliases=aliases,
            source=source,
        )

    def resolve(self, requested: str | None) -> ResolvedBackend:
        default_applied = not bool(str(requested or "").strip())
        name = str(requested or self.default_backend).strip()
        backend_id = self.aliases.get(name, name)
        config = self.backends.get(backend_id)
        if config is None:
            raise ValueError(f"Unknown backend: {name}")
        return ResolvedBackend(
            requested=name,
            backend_id=backend_id,
            config=config,
            default_applied=default_applied,
            alias_applied=backend_id != name,
        )

    def catalog(self) -> dict[str, Any]:
        output: list[dict[str, Any]] = []
        for backend_id in sorted(self.backends):
            config = self.backends[backend_id]
            output.append(
                {
                    "id": backend_id,
                    "default": backend_id == self.default_backend,
                    "cloud": bool(config.get("cloud")),
                    "adapter": config.get("adapter"),
                    "model": config.get("model"),
                    "supports_vision": bool(config.get("supports_vision")),
                    "data_destination": config.get("data_destination"),
                    "agent_routes": sorted((config.get("agent_routes") or {}).keys()),
                }
            )
        return {
            "schema": REGISTRY_SCHEMA,
            "source": self.source,
            "default_backend": self.default_backend,
            "aliases": dict(sorted(self.aliases.items())),
            "backends": output,
        }

    @staticmethod
    def evaluate_route_evidence(route: dict[str, Any], provider_status: dict[str, Any] | None) -> dict[str, Any]:
        evidence = dict(route.get("evidence") or {})
        basis = str(evidence.get("basis") or route.get("basis") or "unverified")
        declared_live = bool(evidence.get("live_verified", route.get("live_verified", False)))
        expected = {
            key: str(evidence.get(key) or "")
            for key in ("model_digest", "parent_model")
            if str(evidence.get(key) or "")
        }
        if not declared_live:
            return {
                "basis": basis,
                "live_verified": False,
                "evidence_state": "unverified",
                "evidence_mismatches": [],
            }
        if not expected:
            return {
                "basis": basis,
                "live_verified": True,
                "evidence_state": "current",
                "evidence_mismatches": [],
            }
        model = (provider_status or {}).get("model") or {}
        observed = {
            "model_digest": str(model.get("digest") or ""),
            "parent_model": str(model.get("parent_model") or ""),
        }
        if not provider_status or any(not observed[key] for key in expected):
            return {
                "basis": basis,
                "live_verified": False,
                "evidence_state": "unknown",
                "evidence_mismatches": [],
            }
        mismatches = [key for key, value in expected.items() if observed.get(key) != value]
        return {
            "basis": basis,
            "live_verified": not mismatches,
            "evidence_state": "stale" if mismatches else "current",
            "evidence_mismatches": mismatches,
        }
