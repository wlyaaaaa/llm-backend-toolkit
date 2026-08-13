from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .agent_runners import AgentRunnerError, default_runners
from .backends import BackendRegistry, ResolvedBackend
from .context import ContextOverflow, compact_task
from .errors import MediaError, ProviderCallError, ToolError
from .input_integrity import declaration_scope
from .media import MediaProcessor
from .providers import default_providers
from .sources import SourceLoader
from .workspace_observer import (
    ValidatedWorkspaceRoot,
    WorkspaceSnapshot,
    WorkspaceRootError,
    capture_workspace_snapshot,
    compare_workspace_snapshots,
    is_safe_workspace_relative_path,
    revalidate_workspace_root,
    validate_workspace_root,
)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class Toolkit:
    def __init__(
        self,
        *,
        registry: BackendRegistry | None = None,
        providers: dict[str, Any] | None = None,
        media_processor: MediaProcessor | None = None,
        source_loader: SourceLoader | None = None,
        runners: dict[str, Any] | None = None,
    ) -> None:
        self.registry = registry or BackendRegistry.load()
        self.providers = default_providers(self.registry) if providers is None else providers
        self.media_processor = media_processor or MediaProcessor()
        self.source_loader = source_loader or SourceLoader()
        self.runners = default_runners() if runners is None else runners

    def catalog(self) -> dict[str, Any]:
        return {"status": "ok", "backend_catalog": self.registry.catalog()}

    @staticmethod
    def _emit_progress(
        progress_callback: Callable[[dict[str, Any]], None] | None,
        event: dict[str, Any],
    ) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(event)
        except Exception:
            return

    @staticmethod
    def _workspace_snapshot(
        workspace: ValidatedWorkspaceRoot,
        public_text_allowlist: frozenset[str],
    ) -> WorkspaceSnapshot:
        try:
            return capture_workspace_snapshot(
                workspace,
                public_text_allowlist=public_text_allowlist,
            )
        except Exception:
            return WorkspaceSnapshot(status="unavailable", _files={})

    def _emit_workspace_change(
        self,
        progress_callback: Callable[[dict[str, Any]], None] | None,
        before: WorkspaceSnapshot,
        workspace: ValidatedWorkspaceRoot,
        *,
        phase: str,
        public_text_allowlist: frozenset[str],
    ) -> None:
        after = self._workspace_snapshot(workspace, public_text_allowlist)
        change = compare_workspace_snapshots(before, after)
        if change.changed_files < 1:
            return
        status_labels = {
            "scoped_complete": "限定扫描范围完整",
            "partial_time_limit": "达到时间上限，结果为已观察下限",
            "partial_item_limit": "达到项目上限，结果为已观察下限",
            "partial_depth_limit": "达到深度上限，结果为已观察下限",
            "partial_error": "部分路径不可读取，结果为已观察下限",
            "unavailable": "不可用",
        }
        scan_label = status_labels.get(change.scan_status, "部分完成")
        changes = [
            {
                "relative_path": item.relative_path,
                "change_kind": item.change_kind,
                "lines_added": item.lines_added,
                "lines_deleted": item.lines_deleted,
                "diff_status": item.diff_status,
                **(
                    {"unified_diff": item.unified_diff}
                    if item.unified_diff is not None
                    else {}
                ),
            }
            for item in change.changes
        ]
        self._emit_progress(
            progress_callback,
            {
                "phase": phase,
                "public_event": {
                    "kind": "workspace.change.observed",
                    "summary_zh": (
                        f"运行期间观察到 {change.changed_files} 个文件条目元数据发生变化；"
                        f"扫描状态：{scan_label}。"
                    ),
                    "payload": {
                        "changed_files": change.changed_files,
                        "scan_status": change.scan_status,
                        "provenance": "workspace_before_after",
                        "attribution": "unverified_concurrent_window",
                        "detail_policy": (
                            "caller_public_safe_include"
                            if public_text_allowlist
                            else "count_only"
                        ),
                        "details_included": len(changes),
                        "details_omitted": change.details_omitted,
                        "changes": changes,
                    },
                },
            },
        )

    def invoke(
        self,
        request: dict[str, Any],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        self._emit_progress(progress_callback, {"phase": "preparing"})
        try:
            integrity_declarations = declaration_scope(request)
        except ValueError as exc:
            return self._blocked("invalid_request", str(exc))
        if any(
            declaration["expected_sha256"] is not None
            for declaration in integrity_declarations
        ):
            return self._blocked(
                "invalid_request",
                "Expected input integrity declarations require async submit "
                "and job-worker execution.",
                options=("submit-async-job", "handle-in-codex"),
            )
        try:
            resolved, provider = self._resolve_provider(
                str(request.get("backend") or request.get("provider") or "") or None
            )
        except ValueError as exc:
            return self._blocked("invalid_request", str(exc))

        requested_reasoning_mode = (request.get("reasoning") or {}).get("mode")
        reasoning_mode = str(
            requested_reasoning_mode
            or resolved.config.get("default_reasoning_mode")
            or "off"
        )
        if reasoning_mode not in {"off", "on"}:
            result = self._blocked("invalid_request", f"Unsupported reasoning mode: {reasoning_mode}")
            result["backend"] = self._backend_receipt(resolved)
            return result
        required_reasoning_mode = resolved.config.get("required_reasoning_mode")
        if required_reasoning_mode and reasoning_mode != required_reasoning_mode:
            result = self._blocked(
                "invalid_request",
                f"Backend {resolved.backend_id} requires reasoning.mode={required_reasoning_mode}.",
            )
            result["backend"] = self._backend_receipt(resolved)
            return result

        media_config = request.get("media") or {}
        attachments = list(media_config.get("attachments") or [])
        privacy = request.get("privacy") or {}
        if bool(getattr(provider, "cloud", resolved.config.get("cloud", False))) and not bool(
            privacy.get("cloud_allowed")
        ):
            result = self._blocked(
                "privacy_block",
                "Any cloud backend transfer requires privacy.cloud_allowed=true.",
                options=("use-local-backend", "allow-cloud-explicitly", "handle-in-codex"),
            )
            result["backend"] = self._backend_receipt(resolved)
            return result

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
                progress_callback=progress_callback,
            )
            supplemental = [
                {"attachment_id": item["id"], "kind": item["kind"], "text": item["text"]}
                for item in media.supplemental_text
            ]
            supplemental.extend(sources.inputs)
            compacted = compact_task(request, supplemental)
        except MediaError as exc:
            result = self._from_error("blocked", exc.error)
            result["backend"] = self._backend_receipt(resolved)
            return result
        except ContextOverflow as exc:
            result = self._blocked(
                "context_overflow",
                str(exc),
                options=("increase-context-budget", "repack-in-codex"),
            )
            result["backend"] = self._backend_receipt(resolved)
            return result
        except (TypeError, ValueError) as exc:
            result = self._blocked("invalid_request", str(exc))
            result["backend"] = self._backend_receipt(resolved)
            return result

        if compacted.receipt.get("applied") is True:
            context_payload = {
                "mode": compacted.receipt.get("mode"),
                "applied": True,
                "lossy": bool(compacted.receipt.get("lossy")),
                "duplicates_removed": int(
                    compacted.receipt.get("duplicates_removed") or 0
                ),
                "estimated_tokens_before": int(
                    compacted.receipt.get("estimated_tokens_before") or 0
                ),
                "estimated_tokens_after": int(
                    compacted.receipt.get("estimated_tokens_after") or 0
                ),
                "target_tokens": int(
                    compacted.receipt.get("target_tokens") or 0
                ),
            }
            context_window_tokens = resolved.config.get("context_window_tokens")
            if type(context_window_tokens) is int:
                context_payload["context_window_tokens"] = context_window_tokens
            self._emit_progress(
                progress_callback,
                {
                    "phase": "preparing",
                    "public_event": {
                        "kind": "context.compaction.completed",
                        "summary_zh": "已自动压缩调用前上下文。",
                        "payload": context_payload,
                    },
                },
            )

        execution = request.get("execution") or {}
        execution_mode = str(execution.get("mode") or "direct")
        if execution_mode not in {"direct", "agent"}:
            return self._blocked("invalid_request", f"Unsupported execution mode: {execution_mode}")
        if execution_mode == "agent":
            self._emit_progress(progress_callback, {"phase": "waiting"})
            result = self._invoke_agent(
                request=request,
                resolved=resolved,
                provider=provider,
                execution=execution,
                compacted=compacted,
                media=media,
                sources=sources,
                progress_callback=progress_callback,
            )
            self._emit_progress(
                progress_callback,
                {"phase": "completed" if result.get("status") in {"ok", "partial"} else "failed"},
            )
            return result
        try:
            if progress_callback is None:
                response = provider.invoke(compacted.prompt, media.native_images, reasoning_mode)
            else:
                response = provider.invoke(
                    compacted.prompt,
                    media.native_images,
                    reasoning_mode,
                    progress_callback=progress_callback,
                )
        except ProviderCallError as exc:
            self._emit_progress(progress_callback, {"phase": "failed"})
            result = self._from_error("failed", exc.error)
            result["backend"] = self._backend_receipt(resolved)
            result["provider"] = {"requested": resolved.requested, "actual": resolved.backend_id}
            result["context_receipt"] = compacted.receipt
            result["media_routes"] = media.routes
            result["source_receipt"] = sources.receipt
            result["delegation_receipt"] = self._delegation_receipt(compacted.receipt, sources.receipt)
            return result

        self._emit_progress(progress_callback, {"phase": "validating"})
        output, checks = self._check_output(response.content, (request.get("task") or {}).get("expected_output") or {})
        status = "ok" if all(check["passed"] for check in checks) else "partial"
        result = {
            "status": status,
            "output": output,
            "backend": self._backend_receipt(resolved),
            "provider": {"requested": resolved.requested, "actual": response.model or resolved.backend_id},
            "context_receipt": compacted.receipt,
            "delegation_receipt": self._delegation_receipt(compacted.receipt, sources.receipt),
            "usage": response.usage,
            "checks": checks,
            "uncertainties": [] if status == "ok" else ["One or more deterministic result checks failed."],
            "artifacts": media.artifacts,
            "media_routes": media.routes,
            "source_receipt": sources.receipt,
            "decision": None,
        }
        self._emit_progress(progress_callback, {"phase": "completed"})
        return result

    def _invoke_agent(
        self,
        *,
        request: dict[str, Any],
        resolved: ResolvedBackend,
        provider: Any,
        execution: dict[str, Any],
        compacted: Any,
        media: Any,
        sources: Any,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        requested_workspace = Path(
            str(execution.get("workspace") or "")
        ).expanduser()
        try:
            validated_workspace = validate_workspace_root(requested_workspace)
        except WorkspaceRootError:
            return self._blocked(
                "invalid_request",
                "execution.workspace must be an existing absolute, "
                "non-reparse directory with a stable identity.",
            )
        workspace = validated_workspace.canonical_path
        policy = str(execution.get("policy") or "danger-full-access")
        writable_policies = {"workspace-write", "danger-full-access"}
        if policy not in {"read-only", *writable_policies}:
            return self._blocked("invalid_request", f"Unsupported agent policy: {policy}")
        observability = request.get("observability") or {}
        if not isinstance(observability, dict):
            return self._blocked("invalid_request", "observability must be an object.")
        file_changes = observability.get("file_changes")
        public_text_allowlist: frozenset[str] = frozenset()
        if file_changes is not None:
            if policy not in writable_policies or not isinstance(file_changes, dict):
                return self._blocked(
                    "invalid_request",
                    "observability.file_changes requires a writable agent mode.",
                )
            declared_paths = file_changes.get("include")
            if (
                set(file_changes) != {"mode", "include"}
                or file_changes.get("mode") != "diff"
                or not isinstance(declared_paths, list)
                or not 1 <= len(declared_paths) <= 12
                or any(
                    not is_safe_workspace_relative_path(path)
                    for path in declared_paths
                )
                or len(set(declared_paths)) != len(declared_paths)
            ):
                return self._blocked(
                    "invalid_request",
                    "file_changes requires mode=diff and 1-12 unique safe "
                    "relative paths in include.",
                )
            public_text_allowlist = frozenset(declared_paths)
        requested_runner = str(execution.get("runner") or "").strip()
        runner_name = requested_runner or "data_factory"
        routes = resolved.config.get("agent_routes") or {}
        route = routes.get(runner_name)
        if route is None:
            category = (
                "agent_runner_incompatible" if runner_name in self.runners else "agent_runner_unavailable"
            )
            return self._blocked(
                category,
                f"Runner {runner_name} has no exact profile for backend {resolved.backend_id}.",
                options=("select-compatible-runner", "handle-in-codex"),
            )
        runner_adapter = str(route.get("runner") or "")
        runner = self.runners.get(runner_adapter) or self.runners.get(runner_name)
        if runner is None:
            return self._blocked(
                "agent_runner_unavailable",
                f"Configured agent runner adapter is unavailable: {runner_adapter}",
                options=("inspect-runner", "handle-in-codex"),
            )
        benchmark_route = resolved.config.get("routing_role") == "benchmark_only"
        evidence, provider_status_before = self._route_evidence(route, provider)
        if evidence["evidence_state"] == "stale" or (
            benchmark_route
            and (
                evidence.get("evidence_state") != "current"
                or evidence.get("live_verified") is not True
            )
        ):
            category = (
                "route_evidence_stale"
                if evidence["evidence_state"] == "stale"
                else "route_evidence_unverified"
            )
            result = self._blocked(
                category,
                (
                    "The selected backend no longer matches its accepted model evidence."
                    if category == "route_evidence_stale"
                    else "The benchmark backend did not provide current model evidence."
                ),
                options=("probe-backend", "update-backend-registry", "handle-in-codex"),
            )
            result["backend"] = self._backend_receipt(resolved)
            result["execution_receipt"] = self._route_receipt(
                route, evidence, requested_runner=requested_runner
            )
            return result
        budget_input = execution.get("budget") or {}
        if not isinstance(budget_input, dict):
            return self._blocked("invalid_request", "Agent execution.budget must be an object.")

        # ``completion_driven`` is the product default.  Requests that carry
        # the pre-completion numeric fields without a mode are retained as a
        # narrow backwards-compatible bounded form; new callers must opt in to
        # either legacy mode explicitly.
        requested_limit_mode = budget_input.get("limit_mode")
        if requested_limit_mode is None:
            has_legacy_cutoff = any(
                budget_input.get(name) is not None
                for name in ("timeout_seconds", "max_steps", "max_tool_calls")
            )
            limit_mode = "bounded" if has_legacy_cutoff else "completion_driven"
        else:
            limit_mode = str(requested_limit_mode)
        if limit_mode not in {"completion_driven", "bounded", "watchdog_only"}:
            return self._blocked(
                "invalid_request",
                "Agent budget limit_mode must be completion_driven, bounded, or watchdog_only.",
            )

        try:
            if limit_mode == "completion_driven":
                if any(
                    budget_input.get(name) is not None
                    for name in ("timeout_seconds", "max_steps", "max_tool_calls")
                ):
                    return self._blocked(
                        "invalid_request",
                        "completion_driven cannot declare a total wall, step, or tool-call cutoff.",
                    )
                idle_value = budget_input.get("idle_timeout_seconds", 3600)
                idle_timeout_seconds = int(3600 if idle_value is None else idle_value)
                budget = {
                    "timeout_seconds": None,
                    "idle_timeout_seconds": idle_timeout_seconds,
                    "limit_mode": "completion_driven",
                    "max_steps": None,
                    "max_tool_calls": None,
                }
            else:
                timeout_value = budget_input.get("timeout_seconds", 900)
                timeout_seconds = int(900 if timeout_value is None else timeout_value)
                idle_value = budget_input.get("idle_timeout_seconds")
                idle_timeout_seconds = (
                    int(idle_value) if idle_value is not None else None
                )
                if limit_mode == "watchdog_only":
                    if any(
                        budget_input.get(name) is not None
                        for name in ("max_steps", "max_tool_calls")
                    ):
                        return self._blocked(
                            "invalid_request",
                            "watchdog_only cannot declare max_steps or max_tool_calls.",
                        )
                    budget = {
                        "timeout_seconds": timeout_seconds,
                        "idle_timeout_seconds": idle_timeout_seconds,
                        "limit_mode": "watchdog_only",
                        "max_steps": None,
                        "max_tool_calls": None,
                    }
                else:
                    max_steps_value = budget_input.get("max_steps", 20)
                    max_tool_calls_value = budget_input.get("max_tool_calls", 80)
                    budget = {
                        "timeout_seconds": timeout_seconds,
                        "idle_timeout_seconds": idle_timeout_seconds,
                        "limit_mode": "bounded",
                        "max_steps": int(20 if max_steps_value is None else max_steps_value),
                        "max_tool_calls": int(
                            80 if max_tool_calls_value is None else max_tool_calls_value
                        ),
                    }
        except (TypeError, ValueError):
            return self._blocked("invalid_request", "Agent budget values must be integers.")
        idle_timeout_seconds = budget["idle_timeout_seconds"]
        if (
            idle_timeout_seconds is not None
            and idle_timeout_seconds != 0
            and not 60 <= idle_timeout_seconds <= 604_800
        ):
            return self._blocked(
                "invalid_request",
                "Agent idle_timeout_seconds must be 0 or between 60 and 604800.",
            )
        if budget["timeout_seconds"] is not None and not 30 <= budget["timeout_seconds"] <= 86_400:
            return self._blocked("invalid_request", "Agent timeout_seconds must be between 30 and 86400.")
        if limit_mode == "bounded":
            if not 1 <= budget["max_steps"] <= 200:
                return self._blocked("invalid_request", "Agent max_steps must be between 1 and 200.")
            if not 0 <= budget["max_tool_calls"] <= 10_000:
                return self._blocked("invalid_request", "Agent max_tool_calls must be between 0 and 10000.")
        resolved_execution = dict(execution)
        declared_route_evidence = dict(route.get("evidence") or {})
        resolved_execution.update(
            {
                "workspace": str(workspace.resolve()),
                "policy": policy,
                "budget": budget,
                "native_images": list(media.native_images),
                "profile": route["profile"],
                "model": route["model"],
                "cloud": bool(resolved.config.get("cloud")),
                "_progress_callback": progress_callback,
                "provider_id": declared_route_evidence.get("provider_id"),
                "codex_cli_version": declared_route_evidence.get(
                    "codex_cli_version"
                ),
                "aicli_entry_sha256": declared_route_evidence.get(
                    "aicli_entry_sha256"
                ),
                "aicli_version": declared_route_evidence.get("aicli_version"),
                "profile_fingerprint": declared_route_evidence.get(
                    "profile_fingerprint"
                ),
                "require_runtime_identity": benchmark_route,
                "require_benchmark_preflight": benchmark_route,
                "require_network_proof": benchmark_route,
                "network_policy": "forbidden" if benchmark_route else None,
                "search_policy": "disabled" if benchmark_route else None,
            }
        )
        route_receipt = self._route_receipt(route, evidence, requested_runner=requested_runner)
        prompt = compacted.prompt
        if media.native_images:
            prompt += "\n\nApproved native image paths:\n" + "\n".join(media.native_images)
        workspace_before = (
            self._workspace_snapshot(validated_workspace, public_text_allowlist)
            if policy in writable_policies and callable(progress_callback)
            else None
        )
        workspace_event_phase = "waiting"
        try:
            revalidate_workspace_root(validated_workspace)
        except WorkspaceRootError:
            return self._blocked(
                "invalid_request",
                "execution.workspace changed after validation; the runner was not started.",
            )
        try:
            response = runner.invoke(prompt, resolved_execution)
            workspace_event_phase = "validating"
        except AgentRunnerError as exc:
            workspace_event_phase = "failed"
            provider_status_after = (
                self._provider_status(provider) if benchmark_route else None
            )
            error_status = (
                "blocked"
                if exc.error.category
                in {
                    "agent_budget_exceeded",
                    "agent_budget_unenforced",
                    "agent_network_proof_unavailable",
                    "agent_network_proof_incomplete",
                    "agent_runner_identity_mismatch",
                }
                else "failed"
            )
            result = self._from_error(error_status, exc.error)
            result["backend"] = self._backend_receipt(resolved)
            result["provider"] = {"requested": resolved.requested, "actual": resolved.backend_id}
            result["context_receipt"] = compacted.receipt
            result["delegation_receipt"] = self._delegation_receipt(compacted.receipt, sources.receipt)
            result["media_routes"] = media.routes
            result["source_receipt"] = sources.receipt
            result["execution_receipt"] = {
                "mode": "agent",
                "requested_runner": runner_name,
                "fallback_used": False,
                "policy": policy,
                "budget": budget,
                **route_receipt,
                **exc.receipt,
            }
            if benchmark_route:
                result["execution_receipt"][
                    "local_codex_benchmark_observation"
                ] = self._local_codex_benchmark_observation(
                    resolved=resolved,
                    route=route,
                    runtime=exc.receipt,
                    request=request,
                    budget=budget,
                    policy=policy,
                    workspace=validated_workspace,
                    provider_status_before=provider_status_before,
                    provider_status_after=provider_status_after,
                )
            return result
        finally:
            if workspace_before is not None:
                try:
                    self._emit_workspace_change(
                        progress_callback,
                        workspace_before,
                        validated_workspace,
                        phase=workspace_event_phase,
                        public_text_allowlist=public_text_allowlist,
                    )
                except Exception:
                    pass
        provider_status_after = (
            self._provider_status(provider) if benchmark_route else None
        )
        output, checks = self._check_output(response.content, (request.get("task") or {}).get("expected_output") or {})
        status = "ok" if all(check["passed"] for check in checks) else "partial"
        usage = self._agent_usage(response)
        return {
            "status": status,
            "output": output,
            "backend": self._backend_receipt(resolved),
            "provider": {"requested": resolved.requested, "actual": response.model or resolved.backend_id},
            "context_receipt": compacted.receipt,
            "delegation_receipt": self._delegation_receipt(compacted.receipt, sources.receipt),
            "usage": usage,
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
                "steps": int(getattr(response, "steps", 0) or 0),
                "tool_calls": response.tool_calls,
                "session_id": response.session_id,
                "stop_reason": response.stop_reason,
                "limit_enforcement": response.limit_enforcement,
                "limit_usage": dict(getattr(response, "limit_usage", {}) or {}),
                "limit_hit": str(getattr(response, "limit_hit", "") or "") or None,
                "event_projection": str(getattr(response, "event_projection", "") or ""),
                "machine_event_projection": str(
                    getattr(response, "machine_event_projection", "") or ""
                ),
                "machine_event_status": str(
                    getattr(response, "machine_event_status", "") or ""
                ),
                "machine_event_count": int(
                    getattr(response, "machine_event_count", 0) or 0
                ),
                "observability_level": str(
                    getattr(response, "observability_level", "lifecycle")
                    or "lifecycle"
                ),
                "policy": policy,
                "budget": budget,
                "fallback_used": False,
                **route_receipt,
                **(
                    {
                        "local_codex_benchmark_observation": self._local_codex_benchmark_observation(
                            resolved=resolved,
                            route=route,
                            runtime=response,
                            request=request,
                            budget=budget,
                            policy=policy,
                            workspace=validated_workspace,
                            provider_status_before=provider_status_before,
                            provider_status_after=provider_status_after,
                        )
                    }
                    if benchmark_route
                    else {}
                ),
            },
            "decision": None,
        }

    def status(self, backend_name: str | None) -> dict[str, Any]:
        try:
            resolved, provider = self._resolve_provider(backend_name)
            provider_status = provider.status()
            routes = resolved.config.get("agent_routes") or {}
            if routes:
                route_status: dict[str, Any] = {}
                for route_id in sorted(routes):
                    route = routes[route_id]
                    evidence = self.registry.evaluate_route_evidence(
                        route, provider_status
                    )
                    declared_evidence = route.get("evidence") or {}
                    route_status[route_id] = {
                        "runner": route["runner"],
                        "profile": route["profile"],
                        "model": route["model"],
                        "reasoning_effort": route.get("reasoning_effort"),
                        "evidence_state": evidence["evidence_state"],
                        "receipt_schema": declared_evidence.get("receipt_schema"),
                        "receipt_authority": declared_evidence.get(
                            "receipt_authority"
                        ),
                        "model_digest": declared_evidence.get("model_digest"),
                        "parent_model": declared_evidence.get("parent_model"),
                    }
                provider_status["agent_routes"] = route_status
                provider_status["agent_supported_runners"] = sorted(routes)
            default_route = routes.get("data_factory")
            if default_route:
                evidence = self.registry.evaluate_route_evidence(default_route, provider_status)
                provider_status["agent_default"] = {
                    "runner_alias": "data_factory",
                    "runner": default_route["runner"],
                    "profile": default_route["profile"],
                    "model": default_route["model"],
                    **evidence,
                }
            return {
                "status": "ok",
                "backend": self._backend_receipt(resolved),
                "provider_status": provider_status,
            }
        except ValueError as exc:
            return self._blocked("invalid_request", str(exc))
        except ProviderCallError as exc:
            return self._from_error("failed", exc.error)

    def _resolve_provider(self, name: str | None) -> tuple[ResolvedBackend, Any]:
        resolved = self.registry.resolve(name)
        provider = self.providers.get(resolved.backend_id) or self.providers.get(resolved.requested)
        if provider is None:
            raise ValueError(f"Backend has no provider adapter: {resolved.backend_id}")
        return resolved, provider

    def _route_evidence(
        self, route: dict[str, Any], provider: Any
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        evidence = route.get("evidence") or {}
        needs_observation = bool(evidence.get("live_verified")) and any(
            evidence.get(key) for key in ("model_digest", "parent_model")
        )
        provider_status: dict[str, Any] | None = None
        if needs_observation:
            provider_status = self._provider_status(provider)
        return (
            self.registry.evaluate_route_evidence(route, provider_status),
            provider_status,
        )

    @staticmethod
    def _provider_status(provider: Any) -> dict[str, Any] | None:
        if not hasattr(provider, "status"):
            return None
        try:
            value = provider.status()
        except (ProviderCallError, OSError, ValueError):
            return None
        return dict(value) if isinstance(value, dict) else None

    @staticmethod
    def _local_codex_benchmark_observation(
        *,
        resolved: ResolvedBackend,
        route: dict[str, Any],
        runtime: Any,
        request: dict[str, Any],
        budget: dict[str, Any],
        policy: str,
        workspace: ValidatedWorkspaceRoot,
        provider_status_before: dict[str, Any] | None,
        provider_status_after: dict[str, Any] | None,
    ) -> dict[str, Any]:
        def runtime_value(name: str, default: Any = None) -> Any:
            if isinstance(runtime, dict):
                return runtime.get(name, default)
            return getattr(runtime, name, default)

        limit_usage = dict(runtime_value("limit_usage", {}) or {})
        route_evidence = dict(route.get("evidence") or {})
        try:
            revalidate_workspace_root(workspace)
            workspace_identity_current = True
        except WorkspaceRootError:
            workspace_identity_current = False
        return {
            "schema": "llm-backend-toolkit.local-codex-benchmark-observation.v1",
            "backend": resolved.backend_id,
            "route": {
                "runner": str(route.get("runner") or ""),
                "profile": str(route.get("profile") or ""),
                "model": str(route.get("model") or ""),
                "reasoning_effort": str(route.get("reasoning_effort") or ""),
                "evidence": route_evidence,
            },
            "profile_id": str(runtime_value("profile_id", "") or ""),
            "model_provider": str(runtime_value("model_provider", "") or ""),
            "runtime_identity": dict(
                runtime_value("runtime_identity", {}) or {}
            ),
            "aicli_preflight": dict(
                runtime_value("aicli_preflight", {}) or {}
            ),
            "budget_mode": str(runtime_value("budget_mode", "") or ""),
            "budget": dict(budget),
            "policy": policy,
            "usage": dict(runtime_value("usage", {}) or {}),
            "limit_enforcement": dict(
                runtime_value("limit_enforcement", {}) or {}
            ),
            "process_tree": {
                "cleanup_confirmed": bool(
                    limit_usage.get("cleanup_confirmed", False)
                ),
                "cleanup_method": str(
                    limit_usage.get("cleanup_method") or ""
                ),
            },
            "workspace": {
                "canonical_path": str(workspace.canonical_path),
                "device": int(getattr(workspace, "_device")),
                "inode": int(getattr(workspace, "_inode")),
                "identity_current": workspace_identity_current,
            },
            "task": {"request_sha256": _canonical_digest(request)},
            "provider_before": dict(provider_status_before or {}),
            "provider_after": dict(provider_status_after or {}),
            "fallback_used": False,
        }

    @staticmethod
    def _agent_usage(response: Any) -> dict[str, Any]:
        raw_usage = getattr(response, "usage", {})
        if not isinstance(raw_usage, dict):
            return {}
        normalized: dict[str, Any] = {}
        field_map = {
            "input_tokens": "prompt_tokens",
            "cached_input_tokens": "cached_tokens",
            "output_tokens": "completion_tokens",
            "reasoning_output_tokens": "reasoning_tokens",
        }
        for source, target in field_map.items():
            value = raw_usage.get(source)
            if type(value) is int and value >= 0:
                normalized[target] = value
        for field_name in (
            "current_context_tokens",
            "context_window_tokens",
        ):
            value = raw_usage.get(field_name)
            if type(value) is int and value >= 0:
                normalized[field_name] = value
        upstream_total = raw_usage.get("total_tokens")
        if type(upstream_total) is int and upstream_total >= 0:
            normalized["total_tokens"] = upstream_total
        duration_ms = getattr(response, "duration_ms", None)
        if (
            "completion_tokens" in normalized
            and type(duration_ms) is int
            and duration_ms > 0
        ):
            elapsed_seconds = duration_ms / 1000.0
            normalized.update(
                {
                    "elapsed_seconds": elapsed_seconds,
                    "tps": round(
                        normalized["completion_tokens"] / elapsed_seconds,
                        1,
                    ),
                    "tps_source": "wall_clock_estimate",
                }
            )
        return normalized

    @staticmethod
    def _route_receipt(
        route: dict[str, Any], evidence: dict[str, Any], *, requested_runner: str
    ) -> dict[str, Any]:
        return {
            "resolved_runner": route["runner"],
            "profile": route["profile"],
            "reasoning_effort": route.get("reasoning_effort"),
            "route_basis": evidence["basis"],
            "route_live_verified": evidence["live_verified"],
            "route_evidence_state": evidence["evidence_state"],
            "route_evidence_mismatches": evidence["evidence_mismatches"],
            "default_applied": not bool(requested_runner),
        }

    @staticmethod
    def _backend_receipt(resolved: ResolvedBackend) -> dict[str, Any]:
        receipt = {
            "requested": resolved.requested,
            "resolved": resolved.backend_id,
            "model": resolved.config.get("model"),
            "cloud": bool(resolved.config.get("cloud")),
            "default_applied": resolved.default_applied,
            "alias_applied": resolved.alias_applied,
        }
        context_window_tokens = resolved.config.get("context_window_tokens")
        if type(context_window_tokens) is int:
            receipt["context_window_tokens"] = context_window_tokens
        return receipt

    @staticmethod
    def _delegation_receipt(context: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
        before = int(context.get("estimated_tokens_before") or 0)
        after = int(context.get("estimated_tokens_after") or 0)
        source_chars = sum(int(item.get("source_chars") or 0) for item in sources)
        selected_chars = sum(int(item.get("selected_chars") or 0) for item in sources)
        return {
            "backend_context_tokens_avoided_estimate": max(0, before - after),
            "referenced_source_chars": source_chars,
            "selected_source_chars": selected_chars,
            "referenced_source_tokens_kept_out_of_top_context_estimate": (source_chars + 3) // 4,
            "reasoning_returned": False,
        }

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
    def _blocked(
        cls,
        category: str,
        summary: str,
        *,
        options: tuple[str, ...] = ("inspect-request", "handle-in-codex"),
    ) -> dict[str, Any]:
        return cls._from_error(
            "blocked",
            ToolError(category=category, summary=summary, retryable=False, options=options),
        )
