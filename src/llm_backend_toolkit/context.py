from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable


DEFAULT_TARGET_TOKENS = 16_384
CHARS_PER_TOKEN_ESTIMATE = 4


class ContextOverflow(ValueError):
    pass


@dataclass(frozen=True)
class CompactedContext:
    prompt: str
    receipt: dict[str, Any]


def _canonical(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dedupe(values: Iterable[Any]) -> tuple[list[Any], int]:
    seen: set[str] = set()
    output: list[Any] = []
    removed = 0
    for value in values:
        key = _canonical(value)
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        output.append(value)
    return output, removed


def _lines(values: Iterable[Any]) -> list[str]:
    return [f"- {_canonical(value)}" for value in values]


def _render(
    goal: str,
    pinned: list[Any],
    instructions: list[Any],
    inputs: list[Any],
    expected_output: Any,
    execution_mode: str = "direct",
) -> str:
    sections = ["# Goal", goal.strip()]
    if pinned:
        sections.extend(["", "# Pinned constraints", *_lines(pinned)])
    if instructions:
        sections.extend(["", "# Instructions", *_lines(instructions)])
    if inputs:
        sections.extend(["", "# Inputs", *_lines(inputs)])
    if expected_output not in (None, {}, ""):
        sections.extend(["", "# Expected output", _canonical(expected_output)])
    if execution_mode == "agent":
        response_discipline = (
            "Before and after each major action or tool call, publish a brief public progress update "
            "in the same language as the user. Each update may state only a plan, action, or verified "
            "result. Never expose or guess hidden chain-of-thought or private reasoning, and do not "
            "include secrets, raw tool input/output, or file contents. Keep updates concise and useful. "
            "End with the complete final result."
        )
    else:
        response_discipline = (
            "Return only the final result. Do not expose chain-of-thought. "
            "Report only material uncertainty and evidence needed to judge the result."
        )
    sections.extend(["", "# Response discipline", response_discipline])
    return "\n".join(sections).strip() + "\n"


def _estimate_tokens(text: str) -> int:
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    wide_chars = len(text) - ascii_chars
    return max(1, math.ceil(ascii_chars / CHARS_PER_TOKEN_ESTIMATE + wide_chars))


def _clip(value: Any, budget: int) -> str:
    text = _canonical(value)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    marker = f"\n...[compacted; sha256:{digest}]...\n"
    if budget <= len(marker) + 8:
        return f"[compacted; sha256:{digest}]"
    keep = budget - len(marker)
    head = max(4, keep * 2 // 3)
    tail = max(4, keep - head)
    return text[:head] + marker + text[-tail:]


def compact_task(request: dict[str, Any], supplemental_inputs: list[Any] | None = None) -> CompactedContext:
    task = request.get("task") or {}
    context = request.get("context") or {}
    execution = request.get("execution") or {}
    execution_mode = str(execution.get("mode") or "direct")
    mode = str(context.get("mode") or "compact")
    if mode not in {"compact", "passthrough"}:
        raise ValueError(f"Unsupported context mode: {mode}")

    goal = str(task.get("goal") or "").strip()
    if not goal:
        raise ValueError("task.goal is required")
    pinned = list(context.get("pinned") or [])
    raw_instructions = list(task.get("instructions") or [])
    raw_inputs = list(task.get("inputs") or []) + list(supplemental_inputs or [])
    expected_output = task.get("expected_output") or {}

    before_prompt = _render(
        goal,
        pinned,
        raw_instructions,
        raw_inputs,
        expected_output,
        execution_mode,
    )
    if mode == "passthrough":
        receipt = {
            "mode": mode,
            "executed": False,
            "applied": False,
            "lossy": False,
            "duplicates_removed": 0,
            "estimated_tokens_before": _estimate_tokens(before_prompt),
            "estimated_tokens_after": _estimate_tokens(before_prompt),
            "preserved": ["goal", "pinned", "instructions", "inputs", "expected_output"],
        }
        return CompactedContext(before_prompt, receipt)

    instructions, removed_instructions = _dedupe(raw_instructions)
    inputs, removed_inputs = _dedupe(raw_inputs)
    deduped_inputs = list(inputs)
    duplicates_removed = removed_instructions + removed_inputs
    target_tokens = int(context.get("target_tokens") or DEFAULT_TARGET_TOKENS)
    if target_tokens < 16:
        raise ValueError("context.target_tokens must be at least 16")
    target_chars = target_tokens * CHARS_PER_TOKEN_ESTIMATE

    prompt = _render(
        goal,
        pinned,
        instructions,
        inputs,
        expected_output,
        execution_mode,
    )
    lossy = False
    if _estimate_tokens(prompt) > target_tokens:
        baseline = _render(
            goal,
            pinned,
            instructions,
            [],
            expected_output,
            execution_mode,
        )
        if _estimate_tokens(baseline) > target_tokens:
            raise ContextOverflow("Pinned context and task contract exceed the requested target")
        available = max(96, target_chars - len(baseline) - max(0, len(inputs) - 1) * 4)
        per_item = max(48, available // max(1, len(inputs)))
        inputs = [_clip(value, per_item) for value in inputs]
        prompt = _render(
            goal,
            pinned,
            instructions,
            inputs,
            expected_output,
            execution_mode,
        )
        while _estimate_tokens(prompt) > target_tokens and per_item > 48:
            excess_tokens = _estimate_tokens(prompt) - target_tokens
            per_item = max(
                48,
                per_item - max(8, math.ceil(excess_tokens * CHARS_PER_TOKEN_ESTIMATE / max(1, len(inputs)))),
            )
            inputs = [_clip(value, per_item) for value in deduped_inputs]
            prompt = _render(
                goal,
                pinned,
                instructions,
                inputs,
                expected_output,
                execution_mode,
            )
        if _estimate_tokens(prompt) > target_tokens:
            raise ContextOverflow("Context cannot be compacted to the requested target without dropping inputs")
        lossy = True

    receipt = {
        "mode": mode,
        "executed": True,
        "applied": duplicates_removed > 0 or lossy,
        "lossy": lossy,
        "duplicates_removed": duplicates_removed,
        "estimated_tokens_before": _estimate_tokens(before_prompt),
        "estimated_tokens_after": _estimate_tokens(prompt),
        "target_tokens": target_tokens,
        "preserved": ["goal", "pinned", "instructions", "expected_output"],
    }
    return CompactedContext(prompt, receipt)
