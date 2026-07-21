from __future__ import annotations

import json
from typing import Any

from .context import ContextOverflow, compact_task
from .errors import MediaError, ProviderCallError, ToolError
from .media import MediaProcessor
from .providers import default_providers
from .sources import SourceLoader


class Toolkit:
    def __init__(
        self,
        *,
        providers: dict[str, Any] | None = None,
        media_processor: MediaProcessor | None = None,
        source_loader: SourceLoader | None = None,
    ) -> None:
        self.providers = providers or default_providers()
        self.media_processor = media_processor or MediaProcessor()
        self.source_loader = source_loader or SourceLoader()

    def invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        provider_name = str(request.get("provider") or "")
        provider = self.providers.get(provider_name)
        if provider is None:
            return self._blocked("invalid_request", f"Unsupported provider: {provider_name}")

        media_config = request.get("media") or {}
        attachments = list(media_config.get("attachments") or [])
        privacy = request.get("privacy") or {}
        if attachments and bool(getattr(provider, "cloud", False)) and not bool(privacy.get("cloud_allowed")):
            return self._blocked(
                "privacy_block",
                "Cloud media transfer requires privacy.cloud_allowed=true.",
                options=("use-local-provider", "allow-cloud-explicitly", "handle-in-codex"),
            )

        try:
            task = request.get("task") or {}
            query = "\n".join(
                [str(task.get("goal") or ""), *[str(value) for value in task.get("instructions") or []]]
            )
            sources = self.source_loader.load(list(task.get("sources") or []), query=query)
            media = self.media_processor.process(
                attachments,
                provider_supports_vision=bool(getattr(provider, "supports_vision", False)),
                mode=str(media_config.get("mode") or "auto"),
            )
            supplemental = [
                {"attachment_id": item["id"], "kind": item["kind"], "text": item["text"]}
                for item in media.supplemental_text
            ]
            supplemental.extend(sources.inputs)
            compacted = compact_task(request, supplemental)
        except MediaError as exc:
            return self._from_error("blocked", exc.error)
        except ContextOverflow as exc:
            return self._blocked(
                "context_overflow",
                str(exc),
                options=("increase-context-budget", "repack-in-codex"),
            )
        except (TypeError, ValueError) as exc:
            return self._blocked("invalid_request", str(exc))

        reasoning_mode = str((request.get("reasoning") or {}).get("mode") or "off")
        if reasoning_mode not in {"off", "on"}:
            return self._blocked("invalid_request", f"Unsupported reasoning mode: {reasoning_mode}")
        try:
            response = provider.invoke(compacted.prompt, media.native_images, reasoning_mode)
        except ProviderCallError as exc:
            result = self._from_error("failed", exc.error)
            result["provider"] = {"requested": provider_name, "actual": provider_name}
            result["context_receipt"] = compacted.receipt
            result["media_routes"] = media.routes
            result["source_receipt"] = sources.receipt
            return result

        output, checks = self._check_output(response.content, (request.get("task") or {}).get("expected_output") or {})
        status = "ok" if all(check["passed"] for check in checks) else "partial"
        return {
            "status": status,
            "output": output,
            "provider": {"requested": provider_name, "actual": response.model or provider_name},
            "context_receipt": compacted.receipt,
            "usage": response.usage,
            "checks": checks,
            "uncertainties": [] if status == "ok" else ["One or more deterministic result checks failed."],
            "artifacts": media.artifacts,
            "media_routes": media.routes,
            "source_receipt": sources.receipt,
            "decision": None,
        }

    def status(self, provider_name: str) -> dict[str, Any]:
        provider = self.providers.get(provider_name)
        if provider is None:
            return self._blocked("invalid_request", f"Unsupported provider: {provider_name}")
        try:
            return {"status": "ok", "provider_status": provider.status()}
        except ProviderCallError as exc:
            return self._from_error("failed", exc.error)

    @staticmethod
    def _check_output(content: str, expected: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
        checks: list[dict[str, Any]] = [
            {"id": "nonempty_output", "passed": bool(content.strip()), "summary": "Provider returned content."}
        ]
        output: Any = content
        if str(expected.get("format") or "text").lower() == "json":
            try:
                output = json.loads(content)
                checks.append({"id": "valid_json", "passed": True, "summary": "Output is valid JSON."})
            except json.JSONDecodeError:
                checks.append({"id": "valid_json", "passed": False, "summary": "Output is not valid JSON."})
                return output, checks
            required_keys = list(expected.get("required_keys") or [])
            missing = [key for key in required_keys if not isinstance(output, dict) or key not in output]
            checks.append(
                {
                    "id": "required_keys",
                    "passed": not missing,
                    "summary": "Required keys are present." if not missing else f"Missing keys: {', '.join(missing)}",
                }
            )
        return output, checks

    @staticmethod
    def _from_error(status: str, error: ToolError) -> dict[str, Any]:
        return {"status": status, "error": error.to_dict(), "decision": error.decision()}

    @classmethod
    def _blocked(cls, category: str, summary: str, *, options: tuple[str, ...] = ("inspect-request", "handle-in-codex")) -> dict[str, Any]:
        return cls._from_error(
            "blocked",
            ToolError(category=category, summary=summary, retryable=False, options=options),
        )
