from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .jobs import default_state_root
from .observability import file_lock, read_events, utc_now


OBSERVER_HEALTH_SCHEMA = "llm-backend-toolkit.observer-health.v1"
OBSERVER_LIST_SCHEMA = "llm-backend-toolkit.observer-runs.v1"
OBSERVER_RUNTIME_SCHEMA = "llm-backend-toolkit.observer-runtime.v1"
OBSERVER_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
OBSERVER_MAX_RESULT_BYTES = 2 * 1024 * 1024
_DISPLAY_FIELDS = frozenset(
    {
        "task_label",
        "execution_mode",
        "reasoning_mode",
        "model",
        "runner",
        "profile",
        "reasoning_effort",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "status",
        "output",
        "backend",
        "provider",
        "context_receipt",
        "delegation_receipt",
        "delivery_receipt",
        "source_receipt",
        "usage",
        "checks",
        "media_routes",
        "execution_receipt",
        "cache_identity",
        "error",
    }
)
_OPAQUE_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}\Z")
_USAGE_NUMBER_FIELDS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "estimated_output_tokens",
        "token_events",
        "eval_duration_ns",
        "prompt_eval_duration_ns",
        "total_duration_ns",
        "load_duration_ns",
        "elapsed_seconds",
        "tps",
        "tokens_per_second",
    }
)
def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_bounded_json(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, int | None, bool]:
    try:
        size = path.stat().st_size
    except OSError:
        return None, None, False
    if size > max_bytes:
        return None, size, True
    try:
        with path.open("rb") as stream:
            encoded = stream.read(max_bytes + 1)
        if len(encoded) > max_bytes:
            return None, len(encoded), True
        value = json.loads(encoded)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, size, False
    return (value if isinstance(value, dict) else None), size, False


def _read_result_json(
    job_dir: Path,
) -> tuple[dict[str, Any] | None, int | None, bool]:
    return _read_bounded_json(
        job_dir / "result.json",
        max_bytes=OBSERVER_MAX_RESULT_BYTES,
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse_utc(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _elapsed_seconds(state: dict[str, Any], progress: dict[str, Any] | None) -> float:
    if progress:
        metrics = progress.get("metrics") or {}
        try:
            measured = float(metrics.get("elapsed_seconds") or 0.0)
        except (TypeError, ValueError):
            measured = 0.0
        if measured > 0:
            return round(measured, 3)
    created = _parse_utc(state.get("created_utc"))
    updated = _parse_utc(state.get("updated_utc"))
    if created is None:
        return 0.0
    end = (
        datetime.now(timezone.utc)
        if state.get("job_status") in {"queued", "running"}
        else updated or datetime.now(timezone.utc)
    )
    return round(max(0.0, (end - created).total_seconds()), 3)


def _bounded_output(
    *,
    source: str,
    size_bytes: int,
    fallback: Any = None,
) -> dict[str, Any]:
    artifact = (
        fallback
        if isinstance(fallback, dict) and fallback.get("type") == "artifact"
        else {}
    )
    return {
        "type": "preview",
        "preview": str(artifact.get("preview") or "")[:20_000],
        "chars": artifact.get("chars"),
        "sha256": artifact.get("sha256"),
        "bytes": size_bytes,
        "source": source,
        "reason": "observer_size_limit",
        "truncated": True,
    }


def _full_output(job_dir: Path, result: dict[str, Any]) -> Any:
    for name in ("output.json", "output.txt"):
        path = job_dir / name
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
            if size > OBSERVER_MAX_OUTPUT_BYTES:
                return _bounded_output(
                    source=name,
                    size_bytes=size,
                    fallback=result.get("output"),
                )
            with path.open("rb") as stream:
                encoded = stream.read(OBSERVER_MAX_OUTPUT_BYTES + 1)
            if len(encoded) > OBSERVER_MAX_OUTPUT_BYTES:
                return _bounded_output(
                    source=name,
                    size_bytes=len(encoded),
                    fallback=result.get("output"),
                )
            text = encoded.decode("utf-8")
            return json.loads(text) if name.endswith(".json") else text
        except (OSError, UnicodeError, json.JSONDecodeError):
            break
    return result.get("output")


def _safe_display(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"task_label": "历史模型任务"}
    display: dict[str, Any] = {}
    for key in _DISPLAY_FIELDS:
        item = value.get(key)
        if item is None:
            continue
        if isinstance(item, (bool, int, float)):
            display[key] = item
        elif isinstance(item, str):
            display[key] = " ".join(item.split())[:256]
    display.setdefault("task_label", "历史模型任务")
    return display


def _safe_text(value: Any, *, max_chars: int = 500) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = " ".join(str(value).split()).strip()
    return text[:max_chars] if text else None


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _safe_opaque_id(value: Any) -> str | None:
    text = _safe_text(value, max_chars=512)
    if text is None:
        return None
    looks_like_path = (
        "/" in text
        or "\\" in text
        or re.match(r"\A[A-Za-z]:", text) is not None
    )
    if not looks_like_path and _OPAQUE_ID_PATTERN.fullmatch(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"opaque-{digest}"


def _project_flat(
    value: Any,
    *,
    text_fields: frozenset[str] = frozenset(),
    number_fields: frozenset[str] = frozenset(),
    bool_fields: frozenset[str] = frozenset(),
    id_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    projected: dict[str, Any] = {}
    for field in text_fields:
        item = _safe_text(value.get(field))
        if item is not None:
            projected[field] = item
    for field in number_fields:
        item = _safe_number(value.get(field))
        if item is not None:
            projected[field] = item
    for field in bool_fields:
        item = value.get(field)
        if isinstance(item, bool):
            projected[field] = item
    for field in id_fields:
        item = _safe_opaque_id(value.get(field))
        if item is not None:
            projected[field] = item
    return projected


def _project_usage(value: Any) -> dict[str, Any]:
    return _project_flat(
        value,
        text_fields=frozenset(
            {"tps_source", "tokens_per_second_source"}
        ),
        number_fields=_USAGE_NUMBER_FIELDS,
    )


def _project_checks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    projected: list[dict[str, Any]] = []
    for item in value[:128]:
        check = _project_flat(
            item,
            text_fields=frozenset({"summary"}),
            bool_fields=frozenset({"passed"}),
            id_fields=frozenset({"id"}),
        )
        if check:
            projected.append(check)
    return projected


def _project_source_receipt(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    projected: list[dict[str, Any]] = []
    for item in value[:128]:
        source = _project_flat(
            item,
            text_fields=frozenset({"sha256"}),
            number_fields=frozenset({"source_chars", "selected_chars"}),
            id_fields=frozenset({"id"}),
        )
        ranges: list[dict[str, Any]] = []
        raw_ranges = item.get("selected_ranges") if isinstance(item, dict) else None
        if isinstance(raw_ranges, (list, tuple)):
            for raw_range in raw_ranges[:128]:
                selected_range = _project_flat(
                    raw_range,
                    number_fields=frozenset({"line_start", "line_end"}),
                )
                if selected_range:
                    ranges.append(selected_range)
        if ranges:
            source["selected_ranges"] = ranges
        if source:
            projected.append(source)
    return projected


def _project_media_routes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    projected: list[dict[str, Any]] = []
    for item in value[:128]:
        route = _project_flat(
            item,
            text_fields=frozenset({"kind", "route"}),
            id_fields=frozenset({"id"}),
        )
        if route:
            projected.append(route)
    return projected


def _project_execution_receipt(value: Any) -> dict[str, Any]:
    projected = _project_flat(
        value,
        text_fields=frozenset(
            {
                "mode",
                "requested_runner",
                "runner",
                "model",
                "stop_reason",
                "limit_hit",
                "event_projection",
                "machine_event_projection",
                "machine_event_status",
                "observability_level",
                "policy",
                "resolved_runner",
                "profile",
                "reasoning_effort",
                "route_basis",
                "route_evidence_state",
            }
        ),
        number_fields=frozenset(
            {
                "exit_code",
                "duration_ms",
                "steps",
                "tool_calls",
                "machine_event_count",
            }
        ),
        bool_fields=frozenset(
            {
                "fallback_used",
                "route_live_verified",
                "default_applied",
            }
        ),
        id_fields=frozenset({"session_id"}),
    )
    if isinstance(value, dict):
        budget = _project_flat(
            value.get("budget"),
            number_fields=frozenset(
                {"timeout_seconds", "max_steps", "max_tool_calls"}
            ),
        )
        if budget:
            projected["budget"] = budget
        enforcement = _project_flat(
            value.get("limit_enforcement"),
            text_fields=frozenset({"timeout", "maxSteps", "maxToolCalls"}),
        )
        if enforcement:
            projected["limit_enforcement"] = enforcement
        usage = _project_flat(
            value.get("limit_usage"),
            text_fields=frozenset(
                {"protocol", "step_definition", "cleanup_method"}
            ),
            number_fields=frozenset(
                {"steps", "tool_calls", "events_seen"}
            ),
            bool_fields=frozenset({"cleanup_confirmed"}),
        )
        if usage:
            projected["limit_usage"] = usage
        mismatches = value.get("route_evidence_mismatches")
        if isinstance(mismatches, (list, tuple)):
            safe_mismatches = [
                item
                for raw in mismatches[:32]
                if (item := _safe_text(raw, max_chars=192)) is not None
            ]
            if safe_mismatches:
                projected["route_evidence_mismatches"] = safe_mismatches
    return projected


def _project_error(value: Any) -> dict[str, Any]:
    projected = _project_flat(
        value,
        text_fields=frozenset({"category"}),
        bool_fields=frozenset({"retryable"}),
    )
    if isinstance(value, dict):
        options = value.get("options")
        if isinstance(options, (list, tuple)):
            safe_options = [
                item
                for raw in options[:32]
                if (item := _safe_text(raw, max_chars=192)) is not None
            ]
            if safe_options:
                projected["options"] = safe_options
    return projected


def _public_output(value: Any) -> Any:
    if isinstance(value, dict) and value.get("type") == "artifact":
        return {
            "type": "preview",
            "preview": str(value.get("preview") or "")[:20_000],
            "chars": value.get("chars"),
            "sha256": value.get("sha256"),
            "truncated": True,
        }
    return value


def _project_result(result: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key in _RESULT_FIELDS:
        if key not in result:
            continue
        if key == "output":
            projected[key] = _public_output(result.get(key))
        elif key == "status":
            status = _safe_text(result.get(key), max_chars=64)
            if status is not None:
                projected[key] = status
        elif key in {"backend", "provider"}:
            value = _project_flat(
                result.get(key),
                text_fields=frozenset(
                    {"requested", "resolved", "actual", "model"}
                ),
                bool_fields=frozenset(
                    {"cloud", "default_applied", "alias_applied"}
                ),
            )
            if value:
                projected[key] = value
        elif key == "context_receipt":
            value = _project_flat(
                result.get(key),
                text_fields=frozenset({"mode"}),
                number_fields=frozenset(
                    {
                        "duplicates_removed",
                        "estimated_tokens_before",
                        "estimated_tokens_after",
                        "target_tokens",
                    }
                ),
                bool_fields=frozenset(
                    {"executed", "applied", "lossy"}
                ),
            )
            if isinstance(result.get(key), dict):
                preserved = result[key].get("preserved")
                if isinstance(preserved, (list, tuple)):
                    safe_preserved = [
                        item
                        for raw in preserved[:32]
                        if (item := _safe_text(raw, max_chars=64)) is not None
                    ]
                    if safe_preserved:
                        value["preserved"] = safe_preserved
            if value:
                projected[key] = value
        elif key == "delegation_receipt":
            value = _project_flat(
                result.get(key),
                number_fields=frozenset(
                    {
                        "backend_context_tokens_avoided_estimate",
                        "referenced_source_chars",
                        "selected_source_chars",
                        "referenced_source_tokens_kept_out_of_top_context_estimate",
                    }
                ),
                bool_fields=frozenset({"reasoning_returned"}),
            )
            if value:
                projected[key] = value
        elif key == "delivery_receipt":
            value = _project_flat(
                result.get(key),
                text_fields=frozenset({"status"}),
                number_fields=frozenset(
                    {
                        "full_chars",
                        "preview_chars",
                        "estimated_top_model_tokens_avoided",
                    }
                ),
                bool_fields=frozenset({"full_result_returned"}),
            )
            if value:
                projected[key] = value
        elif key == "usage":
            value = _project_usage(result.get(key))
            if value:
                projected[key] = value
        elif key == "checks":
            value = _project_checks(result.get(key))
            if value:
                projected[key] = value
        elif key == "source_receipt":
            value = _project_source_receipt(result.get(key))
            if value:
                projected[key] = value
        elif key == "media_routes":
            value = _project_media_routes(result.get(key))
            if value:
                projected[key] = value
        elif key == "execution_receipt":
            value = _project_execution_receipt(result.get(key))
            if value:
                projected[key] = value
        elif key == "cache_identity":
            value = _project_flat(
                result.get(key),
                text_fields=frozenset(
                    {
                        "schema",
                        "mode",
                        "digest",
                        "backend",
                        "model",
                        "route",
                        "profile",
                    }
                ),
            )
            if value:
                projected[key] = value
        elif key == "error":
            value = _project_error(result.get(key))
            if value:
                projected[key] = value
        else:
            continue
    return projected


def _project_progress(progress: dict[str, Any]) -> dict[str, Any]:
    projected = _project_flat(
        progress,
        text_fields=frozenset(
            {"schema", "phase", "summary", "updated_utc"}
        ),
        id_fields=frozenset({"job_id"}),
    )
    events = progress.get("events")
    if isinstance(events, (list, tuple)):
        safe_events = [
            item
            for raw in events[-8:]
            if (
                item := _project_flat(
                    raw,
                    text_fields=frozenset(
                        {"phase", "summary", "updated_utc"}
                    ),
                )
            )
        ]
        if safe_events:
            projected["events"] = safe_events
    metrics = _project_flat(
        progress.get("metrics"),
        number_fields=frozenset(
            {
                "elapsed_seconds",
                "content_chars",
                "thinking_chars",
                "token_events",
                "estimated_output_tokens",
            }
        ),
        bool_fields=frozenset({"thinking_active"}),
    )
    if metrics:
        projected["metrics"] = metrics
    preview = progress.get("public_preview")
    if isinstance(preview, str) and preview:
        projected["public_preview"] = preview[:20_000]
    return projected


def _state_root_id(root: Path | str) -> str:
    canonical = os.path.normcase(os.fspath(Path(root).expanduser().resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _model_name(result: dict[str, Any], state: dict[str, Any]) -> str:
    provider = result.get("provider") or {}
    backend = result.get("backend") or {}
    return str(
        provider.get("actual")
        or backend.get("model")
        or state.get("model")
        or state.get("backend")
        or "unknown"
    )


def _performance(
    result: dict[str, Any],
    progress: dict[str, Any] | None,
    elapsed_seconds: float,
) -> dict[str, Any]:
    usage = result.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    duration_ns = usage.get("eval_duration_ns")
    runner_elapsed_seconds = usage.get("elapsed_seconds")
    tokens_per_second: float | None = None
    tokens_per_second_source = "unavailable"
    try:
        if completion_tokens is not None and float(duration_ns or 0) > 0:
            tokens_per_second = round(
                float(completion_tokens) / (float(duration_ns) / 1_000_000_000),
                1,
            )
            tokens_per_second_source = "eval_duration"
        elif (
            completion_tokens is not None
            and float(runner_elapsed_seconds or 0) > 0
        ):
            tokens_per_second = round(
                float(completion_tokens) / float(runner_elapsed_seconds),
                1,
            )
            tokens_per_second_source = "wall_clock_estimate"
        elif completion_tokens is not None and elapsed_seconds > 0:
            tokens_per_second = round(float(completion_tokens) / elapsed_seconds, 1)
            tokens_per_second_source = "wall_clock_estimate"
        elif progress:
            metrics = progress.get("metrics") or {}
            estimated_tokens = float(metrics.get("estimated_output_tokens") or 0)
            measured = float(metrics.get("elapsed_seconds") or 0)
            if estimated_tokens > 0 and measured > 0:
                tokens_per_second = round(estimated_tokens / measured, 1)
                tokens_per_second_source = "public_content_estimate"
    except (TypeError, ValueError, ZeroDivisionError):
        tokens_per_second = None
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens_per_second": tokens_per_second,
        "tokens_per_second_source": tokens_per_second_source,
        "elapsed_seconds": elapsed_seconds,
    }


class ObserverStore:
    def __init__(self, state_root: Path | str | None = None) -> None:
        self.root = Path(state_root or default_state_root()).expanduser().resolve()
        self._summary_cache: dict[
            str,
            tuple[tuple[tuple[int, int], ...], dict[str, Any]],
        ] = {}
        self._state_cache: dict[
            str,
            tuple[tuple[int, int], dict[str, Any]],
        ] = {}
        self._cache_lock = threading.RLock()

    @staticmethod
    def _state_signature(directory: Path) -> tuple[int, int]:
        try:
            stat = (directory / "state.json").stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return 0, 0

    @staticmethod
    def _job_signature(directory: Path) -> tuple[tuple[int, int], ...]:
        values: list[tuple[int, int]] = []
        for name in ("state.json", "progress.json", "result.json", "events.jsonl"):
            try:
                stat = (directory / name).stat()
                values.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                values.append((0, 0))
        return tuple(values)

    def _run_index(
        self,
        directory: Path,
    ) -> tuple[dict[str, Any], tuple[int, int]] | None:
        signature = self._state_signature(directory)
        if signature == (0, 0):
            return None
        with self._cache_lock:
            cached = self._state_cache.get(directory.name)
            if cached is not None and cached[0] == signature:
                return dict(cached[1]), signature
        state = _read_json(directory / "state.json")
        if state is None:
            return None
        with self._cache_lock:
            self._state_cache[directory.name] = (signature, dict(state))
        return state, signature

    def _run_summary(
        self,
        directory: Path,
        *,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        signature = self._job_signature(directory)
        with self._cache_lock:
            cached = self._summary_cache.get(directory.name)
            if (
                cached is not None
                and cached[0] == signature
                and cached[1].get("job_status") not in {"queued", "running"}
            ):
                return dict(cached[1])
        state = state or _read_json(directory / "state.json")
        if state is None:
            return None
        progress = _read_json(directory / "progress.json") or {}
        result, _result_bytes, result_bounded = _read_result_json(directory)
        result = {} if result_bounded else result or {}
        elapsed = _elapsed_seconds(state, progress)
        display = _safe_display(state.get("display"))
        events = read_events(directory)
        value = {
            "job_id": directory.name,
            "job_status": state.get("job_status"),
            "result_status": state.get("result_status"),
            "backend": state.get("backend") or state.get("provider"),
            "model": _model_name(result, state),
            "created_utc": state.get("created_utc"),
            "updated_utc": state.get("updated_utc"),
            "display": display,
            "phase": progress.get("phase") or state.get("job_status"),
            "summary_zh": progress.get("summary")
            or (events[-1].get("summary_zh") if events else ""),
            "performance": _performance(result, progress, elapsed),
            "handoff": self._handoff(events),
            "conversation": {
                "root_job_id": state.get("conversation_root") or directory.name,
                "turn": int(state.get("conversation_turn") or 1),
                "max_turns": state.get("conversation_max_turns"),
            },
        }
        with self._cache_lock:
            self._summary_cache[directory.name] = (signature, dict(value))
        return value

    def signature(self) -> str:
        if not self.root.is_dir():
            return "empty"
        marker = self.root / ".observer-generation"
        try:
            marker_stat = marker.stat()
            return f"generation:{marker_stat.st_mtime_ns}:{marker_stat.st_size}"
        except OSError:
            try:
                marker.touch(exist_ok=True)
                marker_stat = marker.stat()
                return (
                    f"generation:{marker_stat.st_mtime_ns}:"
                    f"{marker_stat.st_size}"
                )
            except OSError:
                pass
        parts: list[str] = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or len(directory.name) != 24:
                continue
            for name in ("state.json", "progress.json", "result.json", "events.jsonl"):
                path = directory / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                parts.append(f"{directory.name}:{name}:{stat.st_mtime_ns}:{stat.st_size}")
        return "|".join(sorted(parts))

    def list_runs(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        candidates: list[tuple[str, str, Path, dict[str, Any]]] = []
        seen: set[str] = set()
        if self.root.is_dir():
            for directory in self.root.iterdir():
                if (
                    not directory.is_dir()
                    or len(directory.name) != 24
                    or any(char not in "0123456789abcdef" for char in directory.name)
                ):
                    continue
                seen.add(directory.name)
                indexed = self._run_index(directory)
                if indexed is None:
                    continue
                state, _state_signature = indexed
                candidates.append(
                    (
                        str(state.get("updated_utc") or ""),
                        directory.name,
                        directory,
                        state,
                    )
                )
        with self._cache_lock:
            for job_id in set(self._summary_cache) - seen:
                self._summary_cache.pop(job_id, None)
            for job_id in set(self._state_cache) - seen:
                self._state_cache.pop(job_id, None)
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        total = len(candidates)
        bounded_limit = max(1, min(limit, 500))
        bounded_offset = max(0, min(offset, total))
        selected = candidates[bounded_offset : bounded_offset + bounded_limit]
        page = [
            summary
            for _updated, _job_id, directory, state in selected
            if (summary := self._run_summary(directory, state=state)) is not None
        ]
        next_offset = bounded_offset + len(page)
        if next_offset >= total:
            next_offset = None
        return {
            "schema": OBSERVER_LIST_SCHEMA,
            "status": "ok",
            "observed_utc": utc_now(),
            "total": total,
            "offset": bounded_offset,
            "limit": bounded_limit,
            "next_offset": next_offset,
            "runs": page,
        }

    def get_run(self, job_id: str) -> dict[str, Any]:
        if len(job_id) != 24 or any(char not in "0123456789abcdef" for char in job_id):
            raise ValueError("Invalid job ID")
        directory = self.root / job_id
        state = _read_json(directory / "state.json")
        if state is None:
            raise FileNotFoundError(f"Unknown job: {job_id}")
        progress = _read_json(directory / "progress.json") or {}
        result, result_bytes, result_bounded = _read_result_json(directory)
        if result_bounded:
            output = _full_output(directory, {})
            result = {
                "status": "bounded",
                "output": (
                    output
                    if output is not None
                    else _bounded_output(
                        source="result.json",
                        size_bytes=int(result_bytes or 0),
                    )
                ),
            }
        elif result:
            result = dict(result)
            result["output"] = _full_output(directory, result)
            result = _project_result(result)
        else:
            result = {}
        events = read_events(directory)
        elapsed = _elapsed_seconds(state, progress)
        return {
            "schema": "llm-backend-toolkit.observer-run.v1",
            "job_id": job_id,
            "job_status": state.get("job_status"),
            "result_status": state.get("result_status"),
            "backend": state.get("backend") or state.get("provider"),
            "model": _model_name(result, state),
            "created_utc": state.get("created_utc"),
            "updated_utc": state.get("updated_utc"),
            "monitor_until_utc": state.get("monitor_until_utc"),
            "display": _safe_display(state.get("display")),
            "conversation": {
                "root_job_id": state.get("conversation_root") or job_id,
                "turn": int(state.get("conversation_turn") or 1),
                "max_turns": state.get("conversation_max_turns"),
            },
            "progress": _project_progress(progress),
            "performance": _performance(result, progress, elapsed),
            "events": events,
            "handoff": self._handoff(events),
            "result": result,
        }

    @staticmethod
    def _handoff(events: list[dict[str, Any]]) -> dict[str, Any]:
        disposition = next(
            (
                event
                for event in reversed(events)
                if event.get("kind") == "handoff.disposition"
            ),
            None,
        )
        collected = next(
            (
                event
                for event in reversed(events)
                if event.get("kind") == "handoff.collected"
            ),
            None,
        )
        if disposition:
            return {
                "status": str(
                    (disposition.get("payload") or {}).get("disposition") or "reported"
                ),
                "updated_utc": disposition.get("occurred_utc"),
            }
        if collected:
            return {
                "status": "collected",
                "updated_utc": collected.get("occurred_utc"),
            }
        return {"status": "not_collected", "updated_utc": None}


class _ObserverServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _stream_updates(
    store: ObserverStore,
    stream: Any,
    *,
    monotonic: Any = None,
    sleep: Any = None,
) -> None:
    clock = monotonic or time.monotonic
    pause = sleep or time.sleep
    previous = ""
    heartbeat = clock()
    try:
        while True:
            signature = store.signature()
            now = clock()
            if signature != previous:
                payload = json.dumps({"signature": signature}, ensure_ascii=False)
                stream.write(
                    f"event: refresh\ndata: {payload}\n\n".encode("utf-8")
                )
                stream.flush()
                previous = signature
                heartbeat = now
            elif now - heartbeat >= 10:
                stream.write(b": heartbeat\n\n")
                stream.flush()
                heartbeat = now
            pause(0.5)
    except (BrokenPipeError, ConnectionResetError, OSError):
        return


def _asset_root() -> Path:
    return Path(__file__).with_name("observer_ui")


def create_observer_server(
    state_root: Path | str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    asset_root: Path | None = None,
) -> ThreadingHTTPServer:
    store = ObserverStore(state_root)
    assets = Path(asset_root or _asset_root()).resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "LlmBackendObserver/1"

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

        def _host_allowed(self) -> bool:
            host_value = str(self.headers.get("Host") or "").split(":", 1)[0].lower()
            return host_value in {"127.0.0.1", "localhost", "[::1]"}

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            cache_control: str = "no-store",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, value: dict[str, Any]) -> None:
            self._send_bytes(
                status,
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _serve_asset(self, name: str) -> None:
            candidate = (assets / name).resolve()
            try:
                candidate.relative_to(assets)
            except ValueError:
                self._send_json(404, {"status": "not_found"})
                return
            if not candidate.is_file():
                self._send_json(404, {"status": "not_found"})
                return
            mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            cache = "no-cache" if candidate.name == "index.html" else "public, max-age=300"
            self._send_bytes(200, candidate.read_bytes(), f"{mime}; charset=utf-8", cache_control=cache)

        def _serve_stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            _stream_updates(store, self.wfile)

        def do_GET(self) -> None:
            if not self._host_allowed():
                self._send_json(421, {"status": "blocked", "reason": "invalid_host"})
                return
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/api/health":
                self._send_json(
                    200,
                    {
                        "schema": OBSERVER_HEALTH_SCHEMA,
                        "status": "ok",
                        "observed_utc": utc_now(),
                        "state_root_id": _state_root_id(store.root),
                    },
                )
                return
            if path == "/api/runs":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = int((query.get("limit") or ["100"])[0])
                except ValueError:
                    limit = 100
                try:
                    offset = int((query.get("offset") or ["0"])[0])
                except ValueError:
                    offset = 0
                self._send_json(
                    200,
                    store.list_runs(limit=limit, offset=offset),
                )
                return
            if path.startswith("/api/runs/"):
                job_id = path.removeprefix("/api/runs/")
                try:
                    detail = store.get_run(job_id)
                except (FileNotFoundError, ValueError):
                    self._send_json(404, {"status": "not_found"})
                    return
                self._send_json(200, detail)
                return
            if path == "/api/stream":
                self._serve_stream()
                return
            if path in {"/", "/index.html"}:
                self._serve_asset("index.html")
                return
            if path.startswith("/assets/"):
                self._serve_asset(path.removeprefix("/assets/"))
                return
            self._send_json(404, {"status": "not_found"})

    return _ObserverServer((host, port), Handler)


def observer_runtime_path(state_root: Path | str | None = None) -> Path:
    root = Path(state_root or default_state_root()).expanduser().resolve()
    return root / ".observer-runtime.json"


def _runtime_health(
    runtime: dict[str, Any] | None,
    expected_root: Path | str,
) -> bool:
    if not runtime or runtime.get("schema") != OBSERVER_RUNTIME_SCHEMA:
        return False
    root = Path(expected_root).expanduser().resolve()
    try:
        runtime_root = Path(str(runtime.get("state_root") or "")).expanduser().resolve()
    except (OSError, ValueError):
        return False
    if os.path.normcase(os.fspath(runtime_root)) != os.path.normcase(os.fspath(root)):
        return False
    expected_id = _state_root_id(root)
    if runtime.get("state_root_id") != expected_id:
        return False
    url = str(runtime.get("url") or "").rstrip("/")
    if not url.startswith("http://127.0.0.1:"):
        return False
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=0.5) as response:
            value = json.load(response)
        return (
            value.get("status") == "ok"
            and value.get("state_root_id") == expected_id
        )
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return False


def ensure_observer(
    state_root: Path | str | None = None,
    *,
    timeout_seconds: float = 5.0,
    open_browser: bool = False,
) -> dict[str, Any]:
    root = Path(state_root or default_state_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    runtime_path = observer_runtime_path(root)
    with file_lock(root / ".observer-start.lock", timeout_seconds=timeout_seconds):
        runtime = _read_json(runtime_path)
        service_status = "already_running"
        if not _runtime_health(runtime, root):
            command = [
                sys.executable,
                "-m",
                "llm_backend_toolkit",
                "_observer",
                "--state-dir",
                str(root),
                "--runtime-file",
                str(runtime_path),
            ]
            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "close_fds": True,
            }
            if os.name == "nt":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NO_WINDOW
                )
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(command, **kwargs)
            deadline = time.monotonic() + max(1.0, timeout_seconds)
            runtime = None
            while time.monotonic() < deadline:
                runtime = _read_json(runtime_path)
                if _runtime_health(runtime, root):
                    break
                time.sleep(0.1)
            if not _runtime_health(runtime, root):
                raise RuntimeError("Observer service did not become ready")
            service_status = "started"
    url = str((runtime or {}).get("url") or "")
    if open_browser:
        webbrowser.open(url, new=1)
    return {
        "status": "ok",
        "service_status": service_status,
        "url": url,
        "pid": (runtime or {}).get("pid"),
        "state_root": str(root),
        "runtime_file": str(runtime_path),
    }


def run_observer(
    state_root: Path | str | None,
    *,
    runtime_file: Path | str | None = None,
) -> None:
    root = Path(state_root or default_state_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    runtime_path = Path(runtime_file or observer_runtime_path(root)).resolve()
    with file_lock(root / ".observer-instance.lock", timeout_seconds=0.2):
        server = create_observer_server(root, port=0)
        runtime = {
            "schema": OBSERVER_RUNTIME_SCHEMA,
            "pid": os.getpid(),
            "host": "127.0.0.1",
            "port": server.server_port,
            "url": f"http://127.0.0.1:{server.server_port}/",
            "state_root": str(root),
            "state_root_id": _state_root_id(root),
            "started_utc": utc_now(),
        }
        _atomic_json(runtime_path, runtime)
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            server.server_close()
            current = _read_json(runtime_path)
            if current and current.get("pid") == os.getpid():
                runtime_path.unlink(missing_ok=True)
