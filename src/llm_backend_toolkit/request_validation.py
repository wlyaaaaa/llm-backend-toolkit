from __future__ import annotations

from typing import Any


_OBJECT_SECTIONS = (
    "task",
    "context",
    "reasoning",
    "media",
    "privacy",
    "observability",
    "execution",
    "continuation",
)


def validate_request_object_sections(request: Any) -> None:
    """Reject malformed object sections before routing or job persistence."""
    if not isinstance(request, dict):
        raise ValueError("Request must be a JSON object")
    if "expected_output" in request:
        raise ValueError("Use task.expected_output, not a top-level expected_output field")
    for name in _OBJECT_SECTIONS:
        value = request.get(name)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")

    privacy = request.get("privacy") or {}
    if "cloud_allowed" in privacy and type(privacy["cloud_allowed"]) is not bool:
        raise ValueError("privacy.cloud_allowed must be a JSON boolean")
    context = request.get("context") or {}
    target = context.get("target_tokens")
    if target is not None and (type(target) is not int or target < 16):
        raise ValueError("context.target_tokens must be an integer of at least 16")
    execution = request.get("execution") or {}
    budget = execution.get("budget")
    if budget is not None and not isinstance(budget, dict):
        raise ValueError("execution.budget must be an object")
    if isinstance(budget, dict) and "mode" in budget:
        raise ValueError(
            "Use execution.budget.limit_mode (watchdog_only or bounded), not budget.mode"
        )

    task = request.get("task")
    if isinstance(task, dict):
        expected_output = task.get("expected_output")
        if expected_output is not None and not isinstance(expected_output, dict):
            raise ValueError("task.expected_output must be an object")
        if isinstance(expected_output, dict):
            output_format = expected_output.get("format", "text")
            if not isinstance(output_format, str) or output_format.lower() not in {"text", "json"}:
                raise ValueError("task.expected_output.format must be text or json")
            required_keys = expected_output.get("required_keys", [])
            if not isinstance(required_keys, list) or any(not isinstance(key, str) for key in required_keys):
                raise ValueError("task.expected_output.required_keys must be an array of strings")
