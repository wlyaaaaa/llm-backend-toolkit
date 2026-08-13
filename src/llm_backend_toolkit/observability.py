from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .workspace_observer import (
    is_safe_public_text,
    is_safe_workspace_relative_path,
)


EVENT_SCHEMA = "llm-backend-toolkit.run-event.v1"
_EVENT_SEQUENCE_STATE = ".events-sequence.json"
_DURABLE_EVENT_KINDS = frozenset(
    {"run.completed", "run.failed", "handoff.collected"}
)
_LOCK_POLL_SECONDS = 0.01
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_OPAQUE_ID_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}\Z")
_USAGE_NUMBER_FIELDS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cached_input_tokens",
        "estimated_output_tokens",
        "token_events",
        "eval_duration_ns",
        "prompt_eval_duration_ns",
        "total_duration_ns",
        "load_duration_ns",
        "elapsed_seconds",
        "tps",
        "tokens_per_second",
        "current_context_tokens",
        "context_window_tokens",
    }
)
_USAGE_TEXT_FIELDS = frozenset({"tps_source", "tokens_per_second_source"})
_CACHE_IDENTITY_TEXT_FIELDS = frozenset(
    {"schema", "mode", "digest", "backend", "model", "route", "profile"}
)
_EVENT_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "run.started": frozenset({"backend", "execution_mode"}),
}
_WORKSPACE_SCAN_STATUSES = frozenset(
    {
        "scoped_complete",
        "partial_time_limit",
        "partial_item_limit",
        "partial_depth_limit",
        "partial_error",
        "unavailable",
    }
)
_WORKSPACE_CHANGE_KINDS = frozenset(
    {"added", "deleted", "modified", "metadata"}
)
_WORKSPACE_DIFF_STATUSES = frozenset(
    {"available", "truncated", "metadata_only"}
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def notify_observer(job_dir: Path) -> None:
    try:
        directory = Path(job_dir)
        directory.parent.mkdir(parents=True, exist_ok=True)
        (directory.parent / ".observer-generation").touch(exist_ok=True)
    except OSError:
        return


def _process_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(os.fspath(path.resolve()))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


def _try_lock_file(stream) -> bool:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
                error, "winerror", None
            ) in {32, 33}:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _unlock_file(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    process_lock = _process_lock(path)
    if not process_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError(f"Timed out acquiring lock: {path.name}")
    stream = None
    acquired = False
    try:
        stream = path.open("a+b", buffering=0)
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
        while not acquired:
            acquired = _try_lock_file(stream)
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out acquiring lock: {path.name}")
            time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        try:
            if acquired and stream is not None:
                _unlock_file(stream)
        finally:
            if stream is not None:
                stream.close()
            process_lock.release()


def _safe_text(value: Any, *, max_chars: int = 192) -> str | None:
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


def _project_flat_payload(
    payload: dict[str, Any],
    *,
    text_fields: frozenset[str] = frozenset(),
    number_fields: frozenset[str] = frozenset(),
    bool_fields: frozenset[str] = frozenset(),
    id_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for field in text_fields:
        value = _safe_text(payload.get(field))
        if value is not None:
            projected[field] = value
    for field in number_fields:
        value = _safe_number(payload.get(field))
        if value is not None:
            projected[field] = value
    for field in bool_fields:
        value = payload.get(field)
        if isinstance(value, bool):
            projected[field] = value
    for field in id_fields:
        value = _safe_opaque_id(payload.get(field))
        if value is not None:
            projected[field] = value
    return projected


def _project_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _project_flat_payload(
        value,
        text_fields=_USAGE_TEXT_FIELDS,
        number_fields=_USAGE_NUMBER_FIELDS,
    )


def _project_checks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    projected: list[dict[str, Any]] = []
    for item in value[:128]:
        if not isinstance(item, dict):
            continue
        check = _project_flat_payload(
            item,
            bool_fields=frozenset({"passed"}),
            id_fields=frozenset({"id"}),
        )
        if check:
            projected.append(check)
    return projected


def _project_cache_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return _project_flat_payload(value, text_fields=_CACHE_IDENTITY_TEXT_FIELDS)


def _project_workspace_change_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    changed_files = payload.get("changed_files")
    scan_status = payload.get("scan_status")
    detail_policy = payload.get("detail_policy")
    if (
        type(changed_files) is not int
        or not 0 <= changed_files <= 40_000
        or not isinstance(scan_status, str)
        or scan_status not in _WORKSPACE_SCAN_STATUSES
        or payload.get("provenance") != "workspace_before_after"
        or payload.get("attribution") != "unverified_concurrent_window"
        or not isinstance(detail_policy, str)
        or detail_policy not in {
            "count_only",
            "caller_public_safe_include",
        }
    ):
        return {}

    changes: list[dict[str, Any]] = []
    raw_changes = payload.get("changes")
    if isinstance(raw_changes, (list, tuple)):
        for raw in raw_changes[:6]:
            if not isinstance(raw, dict):
                continue
            relative_path = raw.get("relative_path")
            change_kind = raw.get("change_kind")
            diff_status = raw.get("diff_status")
            lines_added = raw.get("lines_added")
            lines_deleted = raw.get("lines_deleted")
            if (
                not is_safe_workspace_relative_path(relative_path)
                or not isinstance(change_kind, str)
                or change_kind not in _WORKSPACE_CHANGE_KINDS
                or not isinstance(diff_status, str)
                or diff_status not in _WORKSPACE_DIFF_STATUSES
                or type(lines_added) is not int
                or type(lines_deleted) is not int
                or not 0 <= lines_added <= 100_000
                or not 0 <= lines_deleted <= 100_000
            ):
                continue
            projected_change = {
                "relative_path": relative_path,
                "change_kind": change_kind,
                "lines_added": lines_added,
                "lines_deleted": lines_deleted,
                "diff_status": diff_status,
            }
            unified_diff = raw.get("unified_diff")
            if (
                diff_status in {"available", "truncated"}
                and is_safe_public_text(unified_diff, max_chars=8_000)
                and str(unified_diff).startswith(f"--- a/{relative_path}\n")
                and f"+++ b/{relative_path}\n" in str(unified_diff)[:600]
            ):
                projected_change["unified_diff"] = unified_diff
            elif diff_status != "metadata_only":
                continue
            changes.append(projected_change)
    if detail_policy == "count_only":
        changes = []

    return {
        "changed_files": changed_files,
        "scan_status": scan_status,
        "provenance": "workspace_before_after",
        "attribution": "unverified_concurrent_window",
        "detail_policy": detail_policy,
        "details_included": len(changes),
        "details_omitted": max(0, changed_files - len(changes)),
        "changes": changes,
    }


def _public_payload(kind: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if kind == "run.created":
        return _project_flat_payload(
            payload,
            text_fields=frozenset(
                {
                    "task_label",
                    "backend",
                    "model",
                    "execution_mode",
                    "reasoning_mode",
                    "reasoning_effort",
                }
            ),
            id_fields=frozenset({"conversation_root"}),
        )
    if kind == "cache.hit":
        projected = _project_flat_payload(
            payload,
            id_fields=frozenset({"attempt_id", "source_job_id"}),
        )
        cache_identity = _project_cache_identity(payload.get("cache_identity"))
        if cache_identity:
            projected["cache_identity"] = cache_identity
        return projected
    if kind in {"run.completed", "run.failed"}:
        projected = _project_flat_payload(
            payload,
            text_fields=frozenset({"result_status"}),
        )
        usage = _project_usage(payload.get("usage"))
        checks = _project_checks(payload.get("checks"))
        if usage:
            projected["usage"] = usage
        if checks:
            projected["checks"] = checks
        return projected
    if kind == "handoff.collected":
        return _project_flat_payload(
            payload,
            bool_fields=frozenset({"full_result"}),
        )
    if kind.startswith("media."):
        return _project_flat_payload(
            payload,
            text_fields=frozenset({"kind", "mode", "route"}),
            id_fields=frozenset({"attachment_id"}),
        )
    if kind == "workspace.change.observed":
        return _project_workspace_change_payload(payload)
    if kind == "context.compaction.completed":
        return _project_flat_payload(
            payload,
            text_fields=frozenset({"mode"}),
            number_fields=frozenset(
                {
                    "duplicates_removed",
                    "estimated_tokens_before",
                    "estimated_tokens_after",
                    "target_tokens",
                    "context_window_tokens",
                }
            ),
            bool_fields=frozenset({"applied", "lossy"}),
        )
    if kind.startswith("agent."):
        return _project_flat_payload(
            payload,
            text_fields=frozenset(
                {
                    "status",
                    "item_type",
                    "command_status",
                    "error_category",
                    "limit",
                    "level",
                }
            ),
            id_fields=frozenset({"error_code"}),
            number_fields=frozenset(
                {
                    "steps",
                    "tool_calls",
                    "events_seen",
                    "current_tokens",
                    "context_window_tokens",
                    "compaction_count",
                    "exit_code",
                    "duration_ms",
                }
            ),
        )
    if kind in _EVENT_PAYLOAD_FIELDS:
        return _project_flat_payload(
            payload,
            text_fields=_EVENT_PAYLOAD_FIELDS[kind],
        )
    if kind in {
        "run.accepted",
        "queue.entered",
        "work.preparing",
        "model.connecting",
        "work.waiting",
        "reasoning.activity",
        "output.started",
        "validation.started",
    }:
        return _project_flat_payload(
            payload,
            text_fields=frozenset({"phase"}),
            number_fields=frozenset(
                {
                    "elapsed_seconds",
                    "content_chars",
                    "thinking_chars",
                    "token_events",
                }
            ),
            bool_fields=frozenset({"thinking_active"}),
        )
    return {}


def read_events(
    job_dir: Path,
    *,
    after_sequence: int = 0,
    limit: int = 500,
    before_sequence: int | None = None,
) -> list[dict[str, Any]]:
    path = Path(job_dir) / "events.jsonl"
    if not path.is_file():
        return []
    bounded_limit = max(1, int(limit))
    events: deque[dict[str, Any]] = deque(maxlen=bounded_limit)
    try:
        stream = path.open("r", encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    with stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(event, dict)
                or event.get("schema") != EVENT_SCHEMA
                or event.get("visibility") != "public"
            ):
                continue
            try:
                sequence = int(event.get("sequence") or 0)
            except (TypeError, ValueError):
                continue
            if sequence <= after_sequence:
                continue
            if before_sequence is not None and sequence >= before_sequence:
                continue
            public_event = dict(event)
            kind = str(public_event.get("kind") or "")
            public_event["summary_zh"] = str(
                public_event.get("summary_zh") or ""
            )[:500]
            public_event["payload"] = _public_payload(
                kind,
                public_event.get("payload"),
            )
            events.append(public_event)
    return list(events)


def _last_valid_event_sequence(path: Path) -> int:
    try:
        stream = path.open("rb")
    except OSError:
        return 0
    with stream:
        try:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            pending = b""
            while position > 0:
                read_size = min(8192, position)
                position -= read_size
                stream.seek(position)
                pending = stream.read(read_size) + pending
                lines = pending.split(b"\n")
                pending = lines[0]
                for line in reversed(lines[1:]):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                        sequence = int(event.get("sequence") or 0)
                    except (UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                        continue
                    if event.get("schema") == EVENT_SCHEMA and sequence > 0:
                        return sequence
            if pending.strip():
                try:
                    event = json.loads(pending)
                    sequence = int(event.get("sequence") or 0)
                except (UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                    return 0
                if event.get("schema") == EVENT_SCHEMA and sequence > 0:
                    return sequence
        except OSError:
            return 0
    return 0


def _event_sequence(directory: Path) -> int:
    events_path = directory / "events.jsonl"
    try:
        events_size = events_path.stat().st_size
    except OSError:
        events_size = 0
    try:
        state = json.loads(
            (directory / _EVENT_SEQUENCE_STATE).read_text(encoding="utf-8")
        )
        sequence = int(state.get("sequence") or 0)
        recorded_size = int(state.get("events_size") or 0)
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        sequence = 0
        recorded_size = -1
    if recorded_size == events_size and (
        (events_size == 0 and sequence == 0) or sequence > 0
    ):
        return sequence
    return _last_valid_event_sequence(events_path)


def _write_event_sequence_state(
    directory: Path,
    *,
    sequence: int,
    events_size: int,
    durable: bool,
) -> None:
    try:
        with (directory / _EVENT_SEQUENCE_STATE).open("w", encoding="utf-8") as stream:
            json.dump(
                {"sequence": sequence, "events_size": events_size},
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if durable:
                stream.flush()
                os.fsync(stream.fileno())
    except OSError:
        # The sidecar is an optimization. A missing or partial file is repaired
        # from the final valid JSONL record on the next append.
        return


def append_event(
    job_dir: Path,
    kind: str,
    summary_zh: str,
    *,
    payload: dict[str, Any] | None = None,
    visibility: str = "public",
) -> dict[str, Any]:
    directory = Path(job_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / ".events.lock"):
        return _append_event_unlocked(
            directory,
            kind,
            summary_zh,
            payload=payload,
            visibility=visibility,
        )


def append_event_once(
    job_dir: Path,
    kind: str,
    summary_zh: str,
    *,
    payload: dict[str, Any] | None = None,
    visibility: str = "public",
) -> dict[str, Any]:
    directory = Path(job_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / ".events.lock"):
        existing = read_events(directory, limit=1_000_000)
        prior = next(
            (event for event in existing if event.get("kind") == kind),
            None,
        )
        if prior is not None:
            return prior
        return _append_event_unlocked(
            directory,
            kind,
            summary_zh,
            payload=payload,
            visibility=visibility,
        )


def _append_event_unlocked(
    directory: Path,
    kind: str,
    summary_zh: str,
    *,
    payload: dict[str, Any] | None,
    visibility: str,
) -> dict[str, Any]:
    sequence = _event_sequence(directory) + 1
    event = {
        "schema": EVENT_SCHEMA,
        "job_id": directory.name,
        "event_id": f"{directory.name}:{sequence}",
        "sequence": sequence,
        "occurred_utc": utc_now(),
        "kind": str(kind),
        "visibility": visibility,
        "summary_zh": str(summary_zh),
        "payload": _public_payload(str(kind), payload or {}),
    }
    encoded = (
        json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    durable = str(kind) in _DURABLE_EVENT_KINDS
    with (directory / "events.jsonl").open("ab", buffering=0) as stream:
        written = stream.write(encoded)
        if written != len(encoded):
            raise OSError("Incomplete event append")
        events_size = stream.tell()
        if durable:
            stream.flush()
            os.fsync(stream.fileno())
    _write_event_sequence_state(
        directory,
        sequence=sequence,
        events_size=events_size,
        durable=durable,
    )
    notify_observer(directory)
    return event


def has_event(job_dir: Path, kind: str) -> bool:
    return any(event.get("kind") == kind for event in read_events(Path(job_dir)))
