from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolError:
    category: str
    provider_code: str = ""
    summary: str = ""
    retryable: bool = False
    decision_owner: str = "top_model"
    options: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "provider_code": self.provider_code or None,
            "summary": self.summary,
            "retryable": self.retryable,
        }

    def decision(self) -> dict[str, Any]:
        return {"owner": self.decision_owner, "options": list(self.options)}


class ProviderCallError(RuntimeError):
    def __init__(self, error: ToolError):
        super().__init__(error.summary or error.category)
        self.error = error


class MediaError(RuntimeError):
    def __init__(self, error: ToolError):
        super().__init__(error.summary or error.category)
        self.error = error


def _provider_fields(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    error = payload.get("error")
    source = error if isinstance(error, dict) else payload
    code = str(source.get("code") or source.get("type") or payload.get("reason") or "")
    message = str(source.get("message") or payload.get("message") or payload.get("reason") or "")
    return code, message


def classify_provider_error(status: int | None, payload: Any) -> ToolError:
    code, message = _provider_fields(payload)
    combined = f"{code} {message}".lower()

    if code in {"Arrearage", "AllocationQuota.FreeTierOnly"} or "insufficient balance" in combined:
        return ToolError(
            category="billing_unavailable",
            provider_code=code,
            summary="The requested cloud provider is unavailable because of its billing state.",
            retryable=False,
            options=("invoke:local-default", "handle-in-codex", "report-billing-action"),
        )
    if status == 401 or any(token in combined for token in ("invalid_api_key", "invalid access token")):
        return ToolError(
            category="authentication_failed",
            provider_code=code,
            summary="The requested provider rejected its credential.",
            retryable=False,
            options=("repair-credential", "handle-in-codex"),
        )
    if status == 403:
        return ToolError(
            category="permission_denied",
            provider_code=code,
            summary="The requested provider denied access.",
            retryable=False,
            options=("inspect-provider-access", "handle-in-codex"),
        )
    if status == 429 or any(
        token in combined
        for token in (
            "rate limit",
            "rate_limit_exceeded",
            "usage limit",
            "insufficient_quota",
            "quota exhausted",
        )
    ):
        return ToolError(
            category="rate_limited",
            provider_code=code,
            summary="The requested provider is rate limited.",
            retryable=True,
            options=("invoke:local-default", "retry-later", "handle-in-codex"),
        )
    if code in {"DataInspectionFailed", "data_inspection_failed"} or "content" in combined and "reject" in combined:
        return ToolError(
            category="content_rejected",
            provider_code=code,
            summary="The requested provider rejected the content.",
            retryable=False,
            options=("inspect-input", "handle-in-codex"),
        )
    if status == 409 and any(token in combined for token in ("gpu_lease_active", "ollama_request_active")):
        return ToolError(
            category="gpu_busy",
            provider_code=code,
            summary="The local GPU is occupied by another managed workload.",
            retryable=True,
            options=("retry-later", "handle-in-codex"),
        )
    if status is not None and status >= 500:
        return ToolError(
            category="provider_unavailable",
            provider_code=code,
            summary="The requested provider is temporarily unavailable.",
            retryable=True,
            options=("retry-later", "handle-in-codex"),
        )
    if status is not None and status >= 400:
        return ToolError(
            category="invalid_request",
            provider_code=code,
            summary="The requested provider rejected the request.",
            retryable=False,
            options=("inspect-request", "handle-in-codex"),
        )
    return ToolError(
        category="provider_unavailable",
        provider_code=code,
        summary="The requested provider call failed before a usable response was returned.",
        retryable=True,
        options=("retry-later", "handle-in-codex"),
    )


def classify_agent_process_error(detail: str) -> ToolError:
    """Normalize provider failures surfaced through an agent CLI process."""
    combined = str(detail or "").lower()
    if any(
        token in combined
        for token in ("arrearage", "allocationquota.freetieronly", "insufficient balance")
    ):
        return ToolError(
            category="billing_unavailable",
            summary="The requested cloud provider is unavailable because of its billing state.",
            retryable=False,
            options=("invoke:local-default", "handle-in-codex", "report-billing-action"),
        )
    if any(token in combined for token in ("invalid_api_key", "invalid api key", "http 401", "status 401")):
        return ToolError(
            category="authentication_failed",
            summary="The requested provider rejected its credential.",
            retryable=False,
            options=("repair-credential", "handle-in-codex"),
        )
    if any(
        token in combined
        for token in (
            "rate limit",
            "rate_limit_exceeded",
            "http 429",
            "status 429",
            "usage limit",
            "insufficient_quota",
            "quota exhausted",
        )
    ):
        return ToolError(
            category="rate_limited",
            summary="The requested provider is rate limited.",
            retryable=True,
            options=("invoke:local-default", "retry-later", "handle-in-codex"),
        )
    return ToolError(
        category="agent_failed",
        summary="The selected agent process failed before returning a usable result.",
        retryable=True,
        options=("inspect-agent-run", "handle-in-codex"),
    )
