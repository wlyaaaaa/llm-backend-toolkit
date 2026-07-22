from __future__ import annotations

import json
from typing import Any
from pathlib import Path

from .agent_runners import AgentRunnerError, default_runners
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
        runners: dict[str, Any] | None = None,
    ) -> None:
        self.providers = providers or default_providers()
        self.media_processor = media_processor or MediaProcessor()
        self.source_loader = source_loader or SourceLoader()
        self.runners = default_runners() if runners is None else runners

    def invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        provider_name = str(request.get("provider") or "")
        provider = self.providers.get(provider_name)
        if provider is None:
            return self._blocked("invalid_request", f"Unsupported provider: {provider_name}")

        media_config = request.get("media") or {}
        attachments = list(media_config.get("attachments") or [])
        privacy = request.get("privacy") or {}
        if bool(getattr(provider, "cloud", False)) and not bool(privacy.get("cloud_allowed")):
            return self._blocked(
                "privacy_block",
                "Any cloud provider transfer requires privacy.cloud_allowed=true.",
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
        execution = request.get("execution") or {}
        execution_mode = str(execution.get("mode") or "direct")
        if execution_mode not in {"direct", "agent"}:
            return self._blocked("invalid_request", f"Unsupported execution mode: {execution_mode}")
        if execution_mode == "agent":
            return self._invoke_agent(
                request=request,
                provider_name=provider_name,
                provider=provider,
                execution=execution,
                compacted=compacted,
                media=media,
                sources=sources,
            )
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

    def _invoke_agent(
        self,
        *,
        request: dict[str, Any],
        provider_name: str,
        provider: Any,
        execution: dict[str, Any],
        compacted: Any,
        media: Any,
        sources: Any,
    ) -> dict[str, Any]:
        if bool(getattr(provider, "cloud", False)) or provider_name != "qwen-main-v1":
            return self._blocked(
                "invalid_request",
                "Agent execution is local-only; provider must be qwen-main-v1.",
                options=("use-local-provider", "handle-in-codex"),
            )
        workspace = Path(str(execution.get("workspace") or "")).expanduser()
        if not workspace.is_absolute() or not workspace.resolve().is_dir():
            return self._blocked("invalid_request", "execution.workspace must be an existing absolute directory.")
        policy = str(execution.get("policy") or "read-only")
        if policy not in {"read-only", "workspace-write"}:
            return self._blocked("invalid_request", f"Unsupported agent policy: {policy}")
        runner_name = str(execution.get("runner") or "data_factory")
        runner = self.runners.get(runner_name)
        if runner is None:
            return self._blocked(
                "agent_runner_unavailable",
                f"Requested agent runner is unavailable: {runner_name}",
                options=("inspect-runner", "handle-in-codex"),
            )
        budget_input = execution.get("budget") or {}
        try:
            budget = {
                "timeout_seconds": int(budget_input.get("timeout_seconds") or 900),
                "max_steps": int(budget_input.get("max_steps") or 20),
                "max_tool_calls": int(budget_input.get("max_tool_calls") or 80),
            }
        except (TypeError, ValueError):
            return self._blocked("invalid_request", "Agent budget values must be integers.")
        if not 30 <= budget["timeout_seconds"] <= 86_400:
            return self._blocked("invalid_request", "Agent timeout_seconds must be between 30 and 86400.")
        if not 1 <= budget["max_steps"] <= 200:
            return self._blocked("invalid_request", "Agent max_steps must be between 1 and 200.")
        if not 0 <= budget["max_tool_calls"] <= 10_000:
            return self._blocked("invalid_request", "Agent max_tool_calls must be between 0 and 10000.")
        resolved_execution = dict(execution)
        resolved_execution.update(
            {
                "workspace": str(workspace.resolve()),
                "policy": policy,
                "budget": budget,
                "native_images": list(media.native_images),
            }
        )
        prompt = compacted.prompt
        if media.native_images:
            prompt += "\n\nApproved native image paths:\n" + "\n".join(media.native_images)
        try:
            response = runner.invoke(prompt, resolved_execution)
        except AgentRunnerError as exc:
            result = self._from_error("failed", exc.error)
            result["provider"] = {"requested": provider_name, "actual": provider_name}
            result["context_receipt"] = compacted.receipt
            result["media_routes"] = media.routes
            result["source_receipt"] = sources.receipt
            result["execution_receipt"] = {
                "mode": "agent",
                "requested_runner": runner_name,
                "fallback_used": False,
                "policy": policy,
                "budget": budget,
            }
            result["execution_receipt"].update(exc.receipt)
            return result
        output, checks = self._check_output(response.content, (request.get("task") or {}).get("expected_output") or {})
        status = "ok" if all(check["passed"] for check in checks) else "partial"
        return {
            "status": status,
            "output": output,
            "provider": {"requested": provider_name, "actual": response.model or provider_name},
            "context_receipt": compacted.receipt,
            "usage": {},
            "checks": checks,
            "uncertainties": [] if status == "ok" else ["One or more deterministic result checks failed."],
            "artifacts": media.artifacts,
            "media_routes": media.routes,
            "source_receipt": sources.receipt,
            "execution_receipt": {
                "mode": "agent",
                "requested_runner": runner_name,
                "runner": response.runner,
                "model": response.model,
                "exit_code": response.exit_code,
                "duration_ms": response.duration_ms,
                "tool_calls": response.tool_calls,
                "session_id": response.session_id,
                "stop_reason": response.stop_reason,
                "limit_enforcement": response.limit_enforcement,
                "policy": policy,
                "budget": budget,
                "fallback_used": False,
            },
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
