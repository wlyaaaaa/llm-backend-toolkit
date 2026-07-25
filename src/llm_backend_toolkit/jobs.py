from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .backends import BackendRegistry
from .context import _estimate_tokens
from .observability import append_event, append_event_once, notify_observer


Spawner = Callable[[str, Path], None]
EXPLICIT_CACHE_IDENTITY_SCHEMA = "llm-backend-toolkit.explicit-cache-identity.v2"
CACHE_INDEX_SCHEMA = "llm-backend-toolkit.cache-index.v1"
CACHE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/=-]{0,511}$")
CACHEABLE_RESULT_STATUSES = frozenset({"ok", "partial"})
REQUEST_DIGEST_CANONICALIZATION = "stdlib-json-sort-compact-utf8-v1"
_CACHE_LOCK_TIMEOUT_SECONDS = 5.0
_CACHE_LOCK_POLL_SECONDS = 0.01
_PROCESS_CACHE_LOCKS_GUARD = threading.Lock()
_PROCESS_CACHE_LOCKS: dict[str, threading.Lock] = {}


def _process_cache_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(os.fspath(path))
    with _PROCESS_CACHE_LOCKS_GUARD:
        return _PROCESS_CACHE_LOCKS.setdefault(key, threading.Lock())


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


def default_state_root() -> Path:
    configured = os.environ.get("LLM_TOOLKIT_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "llm-backend-toolkit" / "jobs"
    return Path.home() / ".cache" / "llm-backend-toolkit" / "jobs"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_after(seconds: int) -> str:
    return datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def _is_expired(value: Any) -> bool:
    if not value:
        return False
    try:
        deadline = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) >= deadline


def _display_text(value: Any, *, max_chars: int = 1_000) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_chars]


def _observer_task_label(request: dict[str, Any]) -> str:
    observability = request.get("observability") or {}
    public_label = _display_text(
        observability.get("public_label"),
        max_chars=80,
    )
    if public_label:
        return public_label
    execution = request.get("execution") or {}
    if str(execution.get("mode") or "direct") == "agent":
        return "智能体工作任务"
    attachments = list((request.get("media") or {}).get("attachments") or [])
    kinds = {
        str(item.get("kind") or "")
        for item in attachments
        if isinstance(item, dict)
    }
    if "audio" in kinds:
        return "中文音频转写任务"
    if "image" in kinds:
        return "图像理解与文字提取任务"
    return "模型生成任务"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    notify_observer(path.parent)


def _default_spawner(job_id: str, state_root: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "llm_backend_toolkit",
        "_worker",
        "--job-id",
        job_id,
        "--state-dir",
        str(state_root),
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)


class JobStore:
    def __init__(
        self,
        root: Path | str | None = None,
        *,
        spawner: Spawner | None = None,
        result_preview_chars: int | None = None,
        registry: BackendRegistry | None = None,
    ) -> None:
        self.root = Path(root or default_state_root()).expanduser().resolve()
        self.spawner = spawner or _default_spawner
        configured_preview = int(os.environ.get("LLM_TOOLKIT_RESULT_PREVIEW_CHARS", "2000"))
        self.result_preview_chars = max(32, result_preview_chars or configured_preview)
        self.registry = registry

    @staticmethod
    def request_digest(request: dict[str, Any]) -> str:
        canonical = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def submit(self, request: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        request, conversation = self._prepare_continuation(request)
        request_digest = self.request_digest(request)
        execution = request.get("execution") or {}
        is_agent = str(execution.get("mode") or "direct") == "agent"
        task = request.get("task") or {}
        media = request.get("media") or {}
        has_mutable_references = bool(task.get("sources") or media.get("attachments"))
        explicit_cache_key = self._validated_explicit_cache_key(request)
        cacheable = (not is_agent and not has_mutable_references) or bool(explicit_cache_key)
        if explicit_cache_key:
            cache_identity, cache_digest = self._explicit_cache_identity(
                request, explicit_cache_key
            )
        else:
            cache_digest = request_digest
            cache_identity = {
                "schema": EXPLICIT_CACHE_IDENTITY_SCHEMA,
                "mode": "request_digest",
                "digest": f"sha256:{cache_digest}",
                "canonicalization": REQUEST_DIGEST_CANONICALIZATION,
            }
        initial_poll_ms = self._initial_poll_ms(request)
        if force or not cacheable:
            job_id = self._new_attempt_id(cache_digest)
            self._create_job(
                job_id=job_id,
                request=request,
                request_digest=request_digest,
                cache_identity=cache_identity,
                cacheable=cacheable,
                conversation=conversation,
                initial_poll_ms=initial_poll_ms,
            )
        else:
            with self._cache_lock(cache_digest):
                existing = self._find_cached_submission(
                    cache_digest=cache_digest,
                    cache_identity=cache_identity,
                    initial_poll_ms=initial_poll_ms,
                )
                if existing is not None:
                    return existing
                primary_job_id = cache_digest[:24]
                if self._job_dir(primary_job_id).exists():
                    job_id = self._new_attempt_id(cache_digest)
                else:
                    job_id = primary_job_id
                self._create_job(
                    job_id=job_id,
                    request=request,
                    request_digest=request_digest,
                    cache_identity=cache_identity,
                    cacheable=True,
                    conversation=conversation,
                    initial_poll_ms=initial_poll_ms,
                )
                self._write_cache_index(
                    cache_digest,
                    job_id,
                    cache_identity,
                    status="active",
                )
        try:
            self.spawner(job_id, self.root)
        except Exception:
            self.fail(job_id, "Background worker could not be started")
            raise
        return {
            "status": "accepted",
            "job_id": job_id,
            "job_status": "queued",
            "poll_after_ms": initial_poll_ms,
            "recommended_check_utc": _utc_after(initial_poll_ms // 1000),
            "forced": force,
            "cacheable": cacheable,
            "cache_identity": cache_identity,
            "visibility": {
                "status": "recorded",
                "event_log": str(self._job_dir(job_id) / "events.jsonl"),
            },
            "monitor_until_utc": self._read_state(job_id).get("monitor_until_utc"),
            **({"conversation": conversation} if conversation else {}),
        }

    @staticmethod
    def _validated_explicit_cache_key(request: dict[str, Any]) -> str | None:
        execution = request.get("execution")
        if not isinstance(execution, dict) or "cache_key" not in execution:
            return None
        value = execution.get("cache_key")
        if not isinstance(value, str) or not CACHE_KEY_PATTERN.fullmatch(value):
            raise ValueError(
                "execution.cache_key must be 1-512 safe identity characters "
                "without whitespace"
            )
        return value

    def _explicit_cache_identity(
        self,
        request: dict[str, Any],
        cache_key: str,
    ) -> tuple[dict[str, Any], str]:
        registry = self.registry or BackendRegistry.load()
        requested_backend = str(
            request.get("backend") or request.get("provider") or ""
        ).strip()
        resolved = registry.resolve(requested_backend or None)
        config = dict(resolved.config)
        execution = request.get("execution") or {}
        execution_mode = str(execution.get("mode") or "direct")
        route_id = ""
        route: dict[str, Any] = {}
        if execution_mode == "agent":
            route_id = str(execution.get("runner") or "data_factory")
            selected = (config.get("agent_routes") or {}).get(route_id)
            if isinstance(selected, dict):
                route = dict(selected)
        privacy = request.get("privacy")
        privacy_value = dict(privacy) if isinstance(privacy, dict) else {}
        privacy_value["cloud_allowed"] = bool(privacy_value.get("cloud_allowed"))
        expected = ((request.get("task") or {}).get("expected_output") or {})
        expected_value = dict(expected) if isinstance(expected, dict) else {}
        expected_value.setdefault("format", "text")
        media = request.get("media")
        media_value = media if isinstance(media, dict) else {}
        attachment_protocol = []
        for attachment in media_value.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            attachment_protocol.append(
                {
                    "kind": str(attachment.get("kind") or ""),
                    "route": str(
                        attachment.get("route")
                        or media_value.get("mode")
                        or "auto"
                    ),
                    "purpose": str(attachment.get("purpose") or ""),
                }
            )
        config_fingerprint = self.request_digest(config)
        route_fingerprint = self.request_digest(route) if route else ""
        caller_cache_key_hash = hashlib.sha256(
            cache_key.encode("utf-8")
        ).hexdigest()
        scope = {
            "schema": EXPLICIT_CACHE_IDENTITY_SCHEMA,
            "canonicalization": REQUEST_DIGEST_CANONICALIZATION,
            "request_protocol": "llm-backend-toolkit.request.v1",
            "caller_cache_key_hash": f"sha256:{caller_cache_key_hash}",
            "backend": {
                "id": resolved.backend_id,
                "adapter": str(config.get("adapter") or ""),
                "model": str(config.get("model") or ""),
                "cloud": bool(config.get("cloud")),
                "config_fingerprint": f"sha256:{config_fingerprint}",
            },
            "execution": {
                "mode": execution_mode,
                "policy": str(execution.get("policy") or "read-only"),
                "route_id": route_id or None,
                "runner": str(route.get("runner") or "") or None,
                "profile": str(route.get("profile") or "") or None,
                "model": str(route.get("model") or "") or None,
                "route_fingerprint": (
                    f"sha256:{route_fingerprint}" if route_fingerprint else None
                ),
            },
            "privacy": privacy_value,
            "reasoning": {
                "mode": str((request.get("reasoning") or {}).get("mode") or "off")
            },
            "task_protocol": {
                "type": str((request.get("task") or {}).get("type") or "generation"),
                "expected_output": expected_value,
            },
            "media_protocol": {
                "mode": str(media_value.get("mode") or "auto"),
                "attachments": attachment_protocol,
            },
        }
        digest = self.request_digest(scope)
        receipt = {
            "schema": EXPLICIT_CACHE_IDENTITY_SCHEMA,
            "mode": "explicit",
            "digest": f"sha256:{digest}",
            "canonicalization": REQUEST_DIGEST_CANONICALIZATION,
            "caller_cache_key_hash": f"sha256:{caller_cache_key_hash}",
            "backend": resolved.backend_id,
            "model": str(config.get("model") or ""),
            "route": route_id or None,
            "profile": str(route.get("profile") or "") or None,
        }
        return receipt, digest

    def _create_job(
        self,
        *,
        job_id: str,
        request: dict[str, Any],
        request_digest: str,
        cache_identity: dict[str, Any],
        cacheable: bool,
        conversation: dict[str, Any] | None,
        initial_poll_ms: int,
    ) -> None:
        execution = request.get("execution") or {}
        is_agent = str(execution.get("mode") or "direct") == "agent"
        requested_backend = str(
            request.get("backend")
            or request.get("provider")
            or "local-default"
        )
        display_metadata: dict[str, Any] = {
            "task_label": _observer_task_label(request),
            "execution_mode": "agent" if is_agent else "direct",
            "reasoning_mode": str(
                (request.get("reasoning") or {}).get("mode") or "off"
            ),
        }
        resolved_backend = requested_backend
        try:
            registry = self.registry or BackendRegistry.load()
            resolved = registry.resolve(requested_backend)
            resolved_backend = resolved.backend_id
            display_metadata["model"] = str(resolved.config.get("model") or "")
            if is_agent:
                route_id = str(execution.get("runner") or "data_factory")
                route = (resolved.config.get("agent_routes") or {}).get(route_id)
                if isinstance(route, dict):
                    display_metadata.update(
                        {
                            "runner": str(route.get("runner") or route_id),
                            "profile": str(route.get("profile") or ""),
                            "model": str(
                                route.get("model")
                                or display_metadata.get("model")
                                or ""
                            ),
                            "reasoning_effort": str(
                                route.get("reasoning_effort") or ""
                            ),
                        }
                    )
        except (OSError, UnicodeError, ValueError, TypeError):
            # A visibility record must still exist when the worker will later
            # fail closed on an invalid or unavailable registry.
            pass
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        _atomic_json(job_dir / "request.json", request)
        created_utc = _utc_now()
        _atomic_json(
            job_dir / "state.json",
            {
                "schema": "llm-backend-toolkit.job-state.v1",
                "job_id": job_id,
                "request_digest": request_digest,
                "cache_identity": cache_identity,
                "job_status": "queued",
                "backend": resolved_backend,
                "model": str(display_metadata.get("model") or ""),
                "provider": str(request.get("provider") or ""),
                "created_utc": created_utc,
                "updated_utc": created_utc,
                "monitor_until_utc": _utc_after(self._timeout_seconds(request)),
                "cacheable": cacheable,
                "cache_result_eligible": False,
                "poll_count": 0,
                "initial_poll_ms": initial_poll_ms,
                "conversation_root": (conversation or {}).get("root_job_id")
                or job_id,
                "conversation_turn": (conversation or {}).get("turn") or 1,
                "conversation_max_turns": (conversation or {}).get("max_turns"),
                "display": display_metadata,
                "visibility": {
                    "status": "recorded",
                    "event_schema": "llm-backend-toolkit.run-event.v1",
                },
            },
        )
        append_event(
            job_dir,
            "run.created",
            "Codex 已创建可见模型子运行，正在等待执行。",
            payload={
                "task_label": _observer_task_label(request),
                "backend": resolved_backend,
                "model": str(display_metadata.get("model") or ""),
                "execution_mode": "agent" if is_agent else "direct",
                "reasoning_mode": str(
                    (request.get("reasoning") or {}).get("mode") or "off"
                ),
                "reasoning_effort": str(
                    display_metadata.get("reasoning_effort") or ""
                ),
                "conversation_root": (conversation or {}).get("root_job_id")
                or job_id,
            },
        )

    def _find_cached_submission(
        self,
        *,
        cache_digest: str,
        cache_identity: dict[str, Any],
        initial_poll_ms: int,
    ) -> dict[str, Any] | None:
        indexed = self._read_cache_index(cache_digest)
        if indexed is not None:
            indexed_job_id = str(indexed.get("job_id") or "")
            try:
                indexed_state = self._read_state(indexed_job_id)
            except (FileNotFoundError, ValueError):
                indexed_state = None
            if indexed_state is not None and self._state_matches_cache(
                indexed_state,
                cache_identity=cache_identity,
            ):
                receipt = self._existing_submission_receipt(
                    indexed_job_id,
                    indexed_state,
                    initial_poll_ms=initial_poll_ms,
                )
                if receipt is not None:
                    return receipt

        primary_job_id = cache_digest[:24]
        try:
            primary_state = self._read_state(primary_job_id)
        except (FileNotFoundError, ValueError):
            return None
        if not self._state_matches_cache(
            primary_state,
            cache_identity=cache_identity,
        ):
            return None
        return self._existing_submission_receipt(
            primary_job_id,
            primary_state,
            initial_poll_ms=initial_poll_ms,
        )

    @staticmethod
    def _state_matches_cache(
        state: dict[str, Any],
        *,
        cache_identity: dict[str, Any],
    ) -> bool:
        stored = state.get("cache_identity")
        if isinstance(stored, dict):
            return stored == cache_identity
        return False

    def _existing_submission_receipt(
        self,
        job_id: str,
        state: dict[str, Any],
        *,
        initial_poll_ms: int,
    ) -> dict[str, Any] | None:
        effective = self.get(job_id)
        job_status = str(effective.get("job_status") or "")
        cache_identity = dict(state.get("cache_identity") or {})
        if job_status == "completed":
            if not self._cache_result_is_eligible(job_id, state):
                return None
            attempt_id = secrets.token_hex(8)
            append_event(
                self._job_dir(job_id),
                "cache.hit",
                "本次调用复用了这一子运行的已校验结果。",
                payload={
                    "attempt_id": attempt_id,
                    "source_job_id": job_id,
                    "cache_identity": cache_identity,
                },
            )
            receipt = {
                "status": "cache_hit",
                "job_id": job_id,
                "job_status": "completed",
                "poll_after_ms": 0,
                "visibility_attempt_id": attempt_id,
            }
            if cache_identity:
                receipt["cache_identity"] = cache_identity
            return receipt
        if job_status == "stale":
            receipt = {
                "status": "blocked",
                "job_id": job_id,
                "job_status": "stale",
                "poll_after_ms": 0,
                "error": effective["error"],
                "decision": effective["decision"],
            }
            if cache_identity:
                receipt["cache_identity"] = cache_identity
            return receipt
        if job_status not in {"queued", "running"}:
            return None
        poll_after_ms = max(
            initial_poll_ms,
            int(state.get("initial_poll_ms") or initial_poll_ms),
        )
        receipt = {
            "status": "running" if job_status == "running" else "accepted",
            "job_id": job_id,
            "job_status": job_status,
            "poll_after_ms": poll_after_ms,
            "recommended_check_utc": _utc_after(poll_after_ms // 1000),
            "monitor_until_utc": state.get("monitor_until_utc"),
        }
        if cache_identity:
            receipt["cache_identity"] = cache_identity
        return receipt

    def _cache_result_is_eligible(
        self,
        job_id: str,
        state: dict[str, Any],
    ) -> bool:
        if not bool(state.get("cacheable", True)):
            return False
        if "cache_result_eligible" in state:
            return bool(state.get("cache_result_eligible"))
        result_status = str(state.get("result_status") or "")
        if result_status:
            return result_status in CACHEABLE_RESULT_STATUSES
        result_path = self._job_dir(job_id) / "result.json"
        if not result_path.is_file():
            return False
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        return (
            isinstance(result, dict)
            and str(result.get("status") or "") in CACHEABLE_RESULT_STATUSES
        )

    @contextmanager
    def _cache_lock(self, cache_digest: str):
        lock_root = self.root / ".cache-locks"
        try:
            lock_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            if not lock_root.is_dir():
                raise
        lock_path = lock_root / f"{cache_digest}.lock"
        deadline = time.monotonic() + _CACHE_LOCK_TIMEOUT_SECONDS
        process_lock = _process_cache_lock(lock_path)
        remaining = max(0.0, deadline - time.monotonic())
        if not process_lock.acquire(timeout=remaining):
            raise TimeoutError("Timed out acquiring the cache identity lock")
        stream = None
        acquired = False
        try:
            stream = lock_path.open("a+b", buffering=0)
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
            while not acquired:
                acquired = _try_lock_file(stream)
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Timed out acquiring the cache identity lock"
                    )
                time.sleep(_CACHE_LOCK_POLL_SECONDS)
            yield
        finally:
            try:
                if acquired and stream is not None:
                    _unlock_file(stream)
            finally:
                if stream is not None:
                    stream.close()
                process_lock.release()

    def _cache_index_path(self, cache_digest: str) -> Path:
        return self.root / ".cache-index" / f"{cache_digest}.json"

    def _read_cache_index(self, cache_digest: str) -> dict[str, Any] | None:
        path = self._cache_index_path(cache_digest)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(value, dict)
            or value.get("schema") != CACHE_INDEX_SCHEMA
            or value.get("cache_digest") != f"sha256:{cache_digest}"
        ):
            return None
        return value

    def _write_cache_index(
        self,
        cache_digest: str,
        job_id: str,
        cache_identity: dict[str, Any],
        *,
        status: str,
    ) -> None:
        _atomic_json(
            self._cache_index_path(cache_digest),
            {
                "schema": CACHE_INDEX_SCHEMA,
                "cache_digest": f"sha256:{cache_digest}",
                "cache_identity": cache_identity,
                "job_id": job_id,
                "status": status,
                "updated_utc": _utc_now(),
            },
        )

    def claim(self, job_id: str) -> dict[str, Any]:
        job_dir = self._job_dir(job_id)
        request_path = job_dir / "request.json"
        if not request_path.is_file():
            raise FileNotFoundError(f"Job request is unavailable: {job_id}")
        state = self._read_state(job_id)
        state["job_status"] = "running"
        state["updated_utc"] = _utc_now()
        _atomic_json(job_dir / "state.json", state)
        append_event(
            job_dir,
            "run.started",
            "模型子运行已经启动。",
            payload={
                "backend": state.get("backend"),
                "execution_mode": (state.get("display") or {}).get("execution_mode"),
            },
        )
        value = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Stored job request is not an object")
        return value

    def progress_recorder(
        self,
        job_id: str,
        *,
        allow_public_preview: bool,
        preview_chars: int = 1_500,
        write_interval_seconds: float = 0.25,
    ) -> Callable[[dict[str, Any]], None]:
        job_dir = self._job_dir(job_id)
        if not (job_dir / "state.json").is_file():
            raise FileNotFoundError(f"Unknown job: {job_id}")
        preview_limit = max(200, preview_chars)
        interval = max(0.05, write_interval_seconds)
        public_preview = ""
        events: list[dict[str, Any]] = []
        last_phase = ""
        last_write = 0.0
        metrics: dict[str, Any] = {
            "elapsed_seconds": 0.0,
            "content_chars": 0,
            "estimated_output_tokens": 0,
            "thinking_active": False,
            "thinking_chars": 0,
            "token_events": 0,
        }

        summaries = {
            "accepted": "我已接到任务，正在准备执行。",
            "queued": "任务正在等待本地 GPU。",
            "preparing": "我正在整理输入、来源和输出约束。",
            "connecting": "我正在连接本地模型并取得 GPU 运行通道。",
            "waiting": "模型已经开始工作，正在等待下一段可公开输出。",
            "thinking": "模型仍在内部分析，暂时没有可公开的结论。",
            "generating": "模型已经开始形成公开回复。",
            "validating": "生成结束，正在检查结构、必填字段和结果完整性。",
            "completed": "结果已经写入耐久任务目录，可以继续验收或接管。",
            "failed": "执行遇到问题，已保留可接管状态。",
        }
        allowed_phases = set(summaries)

        def record(event: dict[str, Any]) -> None:
            nonlocal public_preview, last_phase, last_write
            phase = str(event.get("phase") or "waiting")
            if phase not in allowed_phases:
                phase = "waiting"
            delta = str(event.get("content_delta") or "")
            if allow_public_preview and delta and len(public_preview) < preview_limit:
                public_preview = (public_preview + delta)[:preview_limit]
            if delta:
                metrics["estimated_output_tokens"] += _estimate_tokens(delta)
            if "elapsed_seconds" in event:
                metrics["elapsed_seconds"] = round(float(event.get("elapsed_seconds") or 0.0), 3)
            if "content_chars" in event:
                metrics["content_chars"] = max(0, int(event.get("content_chars") or 0))
            if "thinking_active" in event:
                metrics["thinking_active"] = bool(event.get("thinking_active"))
            if "thinking_chars" in event:
                metrics["thinking_chars"] = max(0, int(event.get("thinking_chars") or 0))
            if "token_events" in event:
                metrics["token_events"] = max(0, int(event.get("token_events") or 0))

            public_event = event.get("public_event")
            if isinstance(public_event, dict):
                kind = str(public_event.get("kind") or "")
                summary = _display_text(
                    public_event.get("summary_zh"),
                    max_chars=500,
                )
                if re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", kind) and summary:
                    payload_value = public_event.get("payload")
                    append_event(
                        job_dir,
                        kind,
                        summary,
                        payload=(
                            dict(payload_value)
                            if isinstance(payload_value, dict)
                            else {}
                        ),
                    )

            now = time.monotonic()
            updated_utc = _utc_now()
            phase_changed = phase != last_phase
            if phase_changed:
                events.append(
                    {
                        "phase": phase,
                        "summary": summaries[phase],
                        "updated_utc": updated_utc,
                    }
                )
                events[:] = events[-8:]
                last_phase = phase
                observable = {
                    "accepted": (
                        "run.accepted",
                        "模型执行器已经接收任务。",
                    ),
                    "queued": (
                        "queue.entered",
                        "任务正在等待本地 GPU 运行通道。",
                    ),
                    "preparing": (
                        "work.preparing",
                        "正在整理输入、来源与输出约束。",
                    ),
                    "connecting": (
                        "model.connecting",
                        "正在连接实际模型与 GPU Broker。",
                    ),
                    "waiting": (
                        "work.waiting",
                        "模型已经开始工作，等待下一项可公开事件。",
                    ),
                    "thinking": (
                        "reasoning.activity",
                        "深度推理处于活动状态，暂无可验证公开结论。",
                    ),
                    "generating": (
                        "output.started",
                        "模型开始生成公开输出。",
                    ),
                    "validating": (
                        "validation.started",
                        "公开输出生成结束，正在执行结果校验。",
                    ),
                }.get(phase)
                if observable is not None:
                    kind, summary = observable
                    append_event(
                        job_dir,
                        kind,
                        summary,
                        payload={
                            "phase": phase,
                            "elapsed_seconds": metrics["elapsed_seconds"],
                            "content_chars": metrics["content_chars"],
                            "thinking_active": metrics["thinking_active"],
                            "thinking_chars": metrics["thinking_chars"],
                            "token_events": metrics["token_events"],
                        },
                    )

            payload: dict[str, Any] = {
                "schema": "llm-backend-toolkit.progress.v1",
                "job_id": job_id,
                "phase": phase,
                "summary": summaries[phase],
                "updated_utc": updated_utc,
                "events": events,
                "metrics": dict(metrics),
            }
            if public_preview:
                payload["public_preview"] = public_preview

            should_write = (
                phase_changed
                or phase in {"completed", "failed"}
                or (now - last_write) >= interval
            )
            if should_write:
                _atomic_json(job_dir / "progress.json", payload)
                last_write = now

        return record

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        job_dir = self._job_dir(job_id)
        _atomic_json(job_dir / "result.json", result)
        self._write_output_artifact(job_dir, result.get("output"))
        state = self._read_state(job_id)
        state["job_status"] = "completed"
        state["result_status"] = str(result.get("status") or "unknown")
        state["cache_result_eligible"] = bool(state.get("cacheable")) and (
            state["result_status"] in CACHEABLE_RESULT_STATUSES
        )
        state["updated_utc"] = _utc_now()
        _atomic_json(job_dir / "state.json", state)
        completed_ok = state["result_status"] in CACHEABLE_RESULT_STATUSES
        append_event(
            job_dir,
            "run.completed" if completed_ok else "run.failed",
            (
                "模型子运行已经完成，结果与校验回执可供 Codex 取回。"
                if completed_ok
                else "模型子运行未成功完成，失败回执已保留。"
            ),
            payload={
                "result_status": state["result_status"],
                "usage": result.get("usage") or {},
                "checks": [
                    {
                        "id": check.get("id"),
                        "passed": bool(check.get("passed")),
                    }
                    for check in (result.get("checks") or [])
                    if isinstance(check, dict)
                ],
            },
        )
        cache_identity = state.get("cache_identity")
        if bool(state.get("cacheable")) and isinstance(cache_identity, dict):
            digest_value = str(cache_identity.get("digest") or "")
            if digest_value.startswith("sha256:"):
                cache_digest = digest_value.removeprefix("sha256:")
                if len(cache_digest) == 64 and all(
                    char in "0123456789abcdef" for char in cache_digest
                ):
                    with self._cache_lock(cache_digest):
                        self._write_cache_index(
                            cache_digest,
                            job_id,
                            cache_identity,
                            status=(
                                "completed"
                                if state["cache_result_eligible"]
                                else "ineligible"
                            ),
                        )
        request_path = job_dir / "request.json"
        if request_path.exists():
            request_path.unlink()

    def fail(self, job_id: str, summary: str) -> None:
        self.complete(
            job_id,
            {
                "status": "failed",
                "error": {
                    "category": "worker_failed",
                    "summary": summary,
                    "retryable": True,
                },
                "decision": {"owner": "top_model", "options": ["retry", "handle-in-codex"]},
            },
        )

    def get(
        self,
        job_id: str,
        *,
        include_result: bool = False,
        full_result: bool = False,
    ) -> dict[str, Any]:
        state = self._read_state(job_id)
        stale = state.get("job_status") != "completed" and _is_expired(state.get("monitor_until_utc"))
        effective_status = "stale" if stale else state.get("job_status")
        poll_after_ms = 0
        if effective_status not in {"completed", "stale"}:
            state["poll_count"] = int(state.get("poll_count") or 0) + 1
            poll_after_ms = min(
                self._initial_poll_ms_from_state(state) * (2 ** min(state["poll_count"], 3)),
                300_000,
            )
            state["updated_utc"] = _utc_now()
            _atomic_json(self._job_dir(job_id) / "state.json", state)
        output = {
            "status": "ok",
            "job_id": job_id,
            "job_status": effective_status,
            "backend": state.get("backend") or state.get("provider"),
            "provider": state.get("provider"),
            "result_status": state.get("result_status"),
            "created_utc": state.get("created_utc"),
            "updated_utc": state.get("updated_utc"),
            "monitor_until_utc": state.get("monitor_until_utc"),
            "poll_after_ms": poll_after_ms,
            "conversation": {
                "root_job_id": state.get("conversation_root") or job_id,
                "turn": int(state.get("conversation_turn") or 1),
                "max_turns": state.get("conversation_max_turns"),
            },
            "display": dict(state.get("display") or {}),
        }
        if isinstance(state.get("cache_identity"), dict):
            output["cache_identity"] = dict(state["cache_identity"])
        if poll_after_ms:
            output["recommended_check_utc"] = _utc_after(poll_after_ms // 1000)
        if stale:
            output["error"] = {
                "category": "job_stale",
                "summary": "The job exceeded its monitoring deadline; do not keep polling it.",
                "retryable": True,
            }
            output["decision"] = {
                "owner": "top_model",
                "options": ["inspect-job", "retry-with-force", "handle-in-codex"],
            }
        result_path = self._job_dir(job_id) / "result.json"
        if include_result and state.get("job_status") == "completed" and result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not full_result:
                result = self._compact_result_view(self._job_dir(job_id), result)
            output["result"] = result
        return output

    def collect(
        self,
        job_id: str,
        *,
        full_result: bool = False,
    ) -> dict[str, Any]:
        output = self.get(
            job_id,
            include_result=True,
            full_result=full_result,
        )
        if (
            output.get("job_status") == "completed"
            and "result" in output
        ):
            append_event_once(
                self._job_dir(job_id),
                "handoff.collected",
                "Codex 已取回这一模型子运行的结果。",
                payload={"full_result": bool(full_result)},
            )
        return output

    @staticmethod
    def _timeout_seconds(request: dict[str, Any]) -> int:
        execution = request.get("execution") or {}
        if str(execution.get("mode") or "direct") == "agent":
            budget = execution.get("budget") or {}
            try:
                wall = int(budget.get("timeout_seconds") or 900)
            except (TypeError, ValueError):
                wall = 900
            return max(150, min(86_520, wall + 120))
        media = request.get("media") or {}
        attachments = list(media.get("attachments") or [])
        exact_image_purposes = {"exact_text", "table", "formula", "scan", "layout", "coordinates"}
        specialist = any(
            str(item.get("kind") or "") == "audio"
            or str(item.get("route") or media.get("mode") or "auto") == "specialist"
            or (
                str(item.get("kind") or "") == "image"
                and str(item.get("purpose") or "") in exact_image_purposes
            )
            for item in attachments
        )
        if specialist:
            return 5_400
        return 1_500

    def _write_output_artifact(self, job_dir: Path, value: Any) -> None:
        if value is None:
            return
        text, suffix = self._artifact_payload(value)
        if len(text) <= self.result_preview_chars:
            return
        (job_dir / f"output{suffix}").write_text(text, encoding="utf-8")

    def _compact_result_view(self, job_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        value = result.get("output")
        if value is None:
            return result
        text, suffix = self._artifact_payload(value)
        if len(text) <= self.result_preview_chars:
            return result
        artifact = job_dir / f"output{suffix}"
        artifact_bytes = artifact.read_bytes()
        digest = hashlib.sha256(artifact_bytes).hexdigest()
        compact = dict(result)
        compact["output"] = {
            "type": "artifact",
            "path": str(artifact),
            "sha256": digest,
            "chars": len(text),
            "preview": text[: self.result_preview_chars],
        }
        compact["delivery_receipt"] = {
            "full_chars": len(text),
            "preview_chars": min(len(text), self.result_preview_chars),
            "estimated_top_model_tokens_avoided": max(
                0, (len(text) - self.result_preview_chars + 3) // 4
            ),
            "full_result_returned": False,
        }
        return compact

    def _prepare_continuation(
        self, request: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        prepared = json.loads(json.dumps(request, ensure_ascii=False))
        continuation = prepared.get("continuation") or {}
        parent_id = str(continuation.get("from_job_id") or "")
        if not parent_id:
            return prepared, None
        parent_state = self._read_state(parent_id)
        if parent_state.get("job_status") != "completed":
            raise ValueError("Continuation requires a completed parent job")
        requested_max = int(continuation.get("max_turns") or parent_state.get("conversation_max_turns") or 3)
        inherited_max = parent_state.get("conversation_max_turns")
        max_turns = min(requested_max, int(inherited_max)) if inherited_max else requested_max
        if not 2 <= max_turns <= 8:
            raise ValueError("Continuation max_turns must be between 2 and 8")
        turn = int(parent_state.get("conversation_turn") or 1) + 1
        if turn > max_turns:
            raise ValueError("Continuation maximum turn limit reached")
        parent = self.get(parent_id, include_result=True)
        result = parent.get("result") or {}
        output = result.get("output")
        if isinstance(output, dict) and output.get("type") == "artifact":
            preview = str(output.get("preview") or "")
        elif isinstance(output, str):
            preview = output[: self.result_preview_chars]
        else:
            preview = json.dumps(output, ensure_ascii=False, separators=(",", ":"))[: self.result_preview_chars]
        task = prepared.setdefault("task", {})
        inputs = list(task.get("inputs") or [])
        inputs.append(
            {
                "type": "previous_result",
                "job_id": parent_id,
                "result_status": parent_state.get("result_status"),
                "output_preview": preview,
            }
        )
        task["inputs"] = inputs
        conversation = {
            "root_job_id": parent_state.get("conversation_root") or parent_id,
            "from_job_id": parent_id,
            "turn": turn,
            "max_turns": max_turns,
            "portable_compact_context": True,
        }
        return prepared, conversation

    @staticmethod
    def _initial_poll_ms(request: dict[str, Any]) -> int:
        execution = request.get("execution") or {}
        if str(execution.get("mode") or "direct") == "agent":
            return 60_000
        media = request.get("media") or {}
        if media.get("attachments"):
            return 60_000
        return 30_000

    @staticmethod
    def _initial_poll_ms_from_state(state: dict[str, Any]) -> int:
        return max(30_000, int(state.get("initial_poll_ms") or 30_000))

    @staticmethod
    def _artifact_payload(value: Any) -> tuple[str, str]:
        if isinstance(value, str):
            return value, ".txt"
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n", ".json"

    def _new_attempt_id(self, digest: str) -> str:
        for _ in range(16):
            candidate = digest[:16] + secrets.token_hex(4)
            if not self._job_dir(candidate).exists():
                return candidate
        raise FileExistsError("Could not allocate a unique forced-attempt job ID")

    def _read_state(self, job_id: str) -> dict[str, Any]:
        state_path = self._job_dir(job_id) / "state.json"
        if not state_path.is_file():
            raise FileNotFoundError(f"Unknown job: {job_id}")
        value = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Stored job state is not an object")
        return value

    def _job_dir(self, job_id: str) -> Path:
        if len(job_id) != 24 or any(char not in "0123456789abcdef" for char in job_id):
            raise ValueError("Invalid job ID")
        return self.root / job_id
