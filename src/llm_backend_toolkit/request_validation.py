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
    for name in _OBJECT_SECTIONS:
        value = request.get(name)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")

    task = request.get("task")
    if isinstance(task, dict):
        expected_output = task.get("expected_output")
        if expected_output is not None and not isinstance(expected_output, dict):
            raise ValueError("task.expected_output must be an object")
