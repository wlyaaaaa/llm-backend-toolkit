from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .input_integrity import (
    INPUT_SPOOL_CLEANUP_SCHEMA,
    InputIntegrityError,
    assert_safe_job_path,
    cleanup_job_inputs,
    has_active_job_input_lease,
    prepare_job_inputs,
    validate_job_input_lease,
)


WORKER_LEASE_SCHEMA = "llm-backend-toolkit.worker-lease.v1"
CONTROLLED_CANCEL_SCHEMA = "llm-backend-toolkit.controlled-cancel.v1"
CONTROLLED_CANCEL_CLEANUP_SCHEMA = "llm-backend-toolkit.controlled-cancel-cleanup.v1"
_ACTIVE_PHASES = {"input_spooling", "inputs_captured", "provider_running"}
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class JobNotRunnableError(ValueError):
    pass


def _controlled_cancel_pending(state: dict[str, Any]) -> bool:
    controlled = state.get("controlled_cancel")
    if (
        not isinstance(controlled, dict)
        or controlled.get("schema") != CONTROLLED_CANCEL_SCHEMA
    ):
        return False
    cleanup = controlled.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("schema") != CONTROLLED_CANCEL_CLEANUP_SCHEMA:
        return True
    process_tree = cleanup.get("process_tree")
    gpu_lease = cleanup.get("gpu_lease")
    return not (
        isinstance(process_tree, dict)
        and process_tree.get("status") == "confirmed_absent"
        and isinstance(gpu_lease, dict)
        and gpu_lease.get("status") == "released"
    )


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _windows_process_start_token(pid: int) -> str | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    open_process.restype = wintypes.HANDLE
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    get_process_times.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    process_query_limited_information = 0x1000
    handle = open_process(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not get_process_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        ticks = (
            int(creation.dwHighDateTime) << 32
        ) | int(creation.dwLowDateTime)
        return f"windows-filetime:{ticks}"
    finally:
        close_handle(handle)


def _posix_process_start_token(pid: int) -> str | None:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(fields) < 22:
        return None
    return f"proc-starttime:{fields[21]}"


def process_start_token(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_start_token(pid)
    return _posix_process_start_token(pid)


def new_worker_lease(phase: str) -> dict[str, Any]:
    pid = os.getpid()
    start_token = process_start_token(pid)
    if start_token is None:
        raise JobNotRunnableError(
            "Cannot establish a durable worker process identity"
        )
    now = _utc_now()
    return {
        "schema": WORKER_LEASE_SCHEMA,
        "status": "active",
        "lease_id": secrets.token_hex(16),
        "owner_pid": pid,
        "owner_start_token": start_token,
        "acquired_utc": now,
        "heartbeat_utc": now,
        "phase": phase,
    }


def worker_lease_is_alive(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("schema") != WORKER_LEASE_SCHEMA:
        return False
    if value.get("status") != "active":
        return False
    try:
        pid = int(value.get("owner_pid"))
    except (TypeError, ValueError):
        return False
    expected = str(value.get("owner_start_token") or "")
    actual = process_start_token(pid)
    return bool(actual and expected and actual == expected)


def _advance_worker_lease(
    state: dict[str, Any],
    *,
    phase: str,
) -> None:
    lease = dict(state.get("worker_lease") or {})
    if lease.get("schema") != WORKER_LEASE_SCHEMA:
        raise JobNotRunnableError("Worker lease is missing or incompatible")
    if lease.get("status") != "active":
        raise JobNotRunnableError("Worker lease is not active")
    lease["phase"] = phase
    lease["heartbeat_utc"] = _utc_now()
    state["worker_lease"] = lease


def _release_worker_lease(
    state: dict[str, Any],
    *,
    phase: str,
    status: str = "released",
) -> None:
    lease = dict(state.get("worker_lease") or {})
    if not lease:
        return
    lease["phase"] = phase
    lease["heartbeat_utc"] = _utc_now()
    lease["released_utc"] = _utc_now()
    lease["status"] = status
    state["worker_lease"] = lease


def _atomic_job_json(
    job_dir: Path,
    path: Path,
    value: dict[str, Any],
) -> None:
    assert_safe_job_path(job_dir, path, require_exists=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    assert_safe_job_path(job_dir, temporary, require_exists=False)
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    assert_safe_job_path(job_dir, temporary, require_exists=True)
    temporary.replace(path)
    assert_safe_job_path(job_dir, path, require_exists=True)


def _discard_uncommitted_outputs(job_dir: Path) -> None:
    candidates = [job_dir / "result.json", *job_dir.glob("output.*")]
    for path in candidates:
        if not os.path.lexists(path):
            continue
        safe_path = assert_safe_job_path(
            job_dir,
            path,
            require_exists=True,
        )
        if safe_path.is_file():
            safe_path.unlink()


class JobInputLifecycle:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _job_dir(self, job_id: str) -> Path:
        job_dir = self.store._job_dir(job_id)
        assert_safe_job_path(job_dir, job_dir, require_exists=True)
        return job_dir

    def safe_job_dir(self, job_id: str) -> Path:
        return self._job_dir(job_id)

    def persist_state_locked(
        self,
        job_id: str,
        state: dict[str, Any],
    ) -> None:
        self._write_state(job_id, state)

    def assert_provider_completion(
        self,
        job_id: str,
        *,
        state: dict[str, Any],
        reference_count: int,
    ) -> None:
        if reference_count == 0:
            return
        if (
            state.get("job_status") != "running"
            or state.get("worker_phase") != "provider_running"
        ):
            raise JobNotRunnableError(
                "Referenced input completion requires provider_running state"
            )
        lease = state.get("worker_lease")
        if not worker_lease_is_alive(lease):
            raise JobNotRunnableError(
                "Referenced input completion requires a live worker lease"
            )
        if int(lease.get("owner_pid") or 0) != os.getpid():
            raise JobNotRunnableError(
                "Referenced input completion must be published by its worker"
            )
        validate_job_input_lease(
            self._job_dir(job_id),
            expected_reference_count=reference_count,
        )

    def _write_state(
        self,
        job_id: str,
        state: dict[str, Any],
    ) -> None:
        job_dir = self._job_dir(job_id)
        state["updated_utc"] = _utc_now()
        _atomic_job_json(job_dir, job_dir / "state.json", state)

    def _finish_pre_provider_failure(
        self,
        job_id: str,
        *,
        input_integrity: dict[str, Any] | None,
        category: str,
        summary: str,
        retryable: bool,
        decision_options: list[str],
    ) -> None:
        with self.store._job_lock(job_id):
            state = self.store._read_state(job_id)
            if str(state.get("job_status") or "") in {
                "cancelled",
                "cancellation_requested",
            }:
                if _controlled_cancel_pending(state):
                    # The caller requested a controlled cancellation.  Do not
                    # infer process-tree or GPU-lease cleanup from this worker
                    # noticing the request while it exits.
                    self._write_state(job_id, state)
                    return
                state["job_status"] = "cancelled"
                state["result_status"] = "cancelled"
                state["cache_result_eligible"] = False
                state["worker_phase"] = "cancelled"
                _release_worker_lease(state, phase="cancelled")
            else:
                state["job_status"] = "failed"
                state["result_status"] = "failed"
                state["cache_result_eligible"] = False
                state["worker_phase"] = "failed"
                if input_integrity is not None:
                    state["input_integrity"] = input_integrity
                state["error"] = {
                    "category": category,
                    "summary": summary,
                    "retryable": retryable,
                }
                state["decision"] = {
                    "owner": "top_model",
                    "options": decision_options,
                }
                _release_worker_lease(state, phase="failed")
            self._write_state(job_id, state)
            self._cleanup_locked(job_id, state=state)

    def claim(self, job_id: str) -> dict[str, Any]:
        job_dir = self._job_dir(job_id)
        with self.store._job_lock(job_id):
            state = self.store._read_state(job_id)
            job_status = str(state.get("job_status") or "")
            if job_status in {"cancelled", "cancellation_requested"}:
                raise JobNotRunnableError(f"Job is cancelled: {job_id}")
            if job_status != "queued":
                raise JobNotRunnableError(
                    f"Job is already claimed or terminal: {job_id}"
                )
            request_path = assert_safe_job_path(
                job_dir,
                job_dir / "request.json",
                require_exists=True,
            )
            value = json.loads(request_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Stored job request is not an object")
            state["job_status"] = "running"
            state["worker_phase"] = "input_spooling"
            state["worker_lease"] = new_worker_lease("input_spooling")
            state["input_spool_cleanup"] = {
                "schema": INPUT_SPOOL_CLEANUP_SCHEMA,
                "status": "retained_for_running",
                "verified_absent": False,
            }
            self._write_state(job_id, state)
        try:
            prepared, input_integrity = prepare_job_inputs(
                value,
                job_dir=job_dir,
            )
            _atomic_job_json(
                job_dir,
                job_dir / "prepared-request.json",
                prepared,
            )
        except InputIntegrityError as error:
            self._finish_pre_provider_failure(
                job_id,
                input_integrity=error.receipt,
                category="input_integrity_failed",
                summary=error.summary,
                retryable=False,
                decision_options=[
                    "inspect-input-integrity",
                    "submit-corrected-reference",
                ],
            )
            raise
        except BaseException as error:
            self._finish_pre_provider_failure(
                job_id,
                input_integrity=None,
                category="input_preparation_failed",
                summary=(
                    "Input preparation failed before provider execution; "
                    "private exception details were suppressed."
                ),
                retryable=True,
                decision_options=[
                    "retry-with-force",
                    "inspect-job-runtime",
                ],
            )
            if isinstance(error, Exception):
                raise JobNotRunnableError(
                    "Input preparation failed before provider execution"
                ) from error
            raise
        with self.store._job_lock(job_id):
            state = self.store._read_state(job_id)
            if str(state.get("job_status") or "") in {
                "cancelled",
                "cancellation_requested",
            }:
                if _controlled_cancel_pending(state):
                    self._write_state(job_id, state)
                    raise JobNotRunnableError(
                        f"Controlled cancellation cleanup is unconfirmed: {job_id}"
                    )
                state["job_status"] = "cancelled"
                state["result_status"] = "cancelled"
                state["cache_result_eligible"] = False
                state["worker_phase"] = "cancelled"
                _release_worker_lease(state, phase="cancelled")
                self._write_state(job_id, state)
                self._cleanup_locked(job_id, state=state)
                raise JobNotRunnableError(f"Job is cancelled: {job_id}")
            state["job_status"] = "running"
            state["worker_phase"] = "inputs_captured"
            _advance_worker_lease(state, phase="inputs_captured")
            state["input_integrity"] = input_integrity
            state["input_spool_cleanup"] = {
                "schema": INPUT_SPOOL_CLEANUP_SCHEMA,
                "status": (
                    "not_applicable"
                    if input_integrity.get("status") == "not_applicable"
                    else "retained_for_running"
                ),
                "verified_absent": (
                    input_integrity.get("status") == "not_applicable"
                ),
            }
            self._write_state(job_id, state)
            return prepared

    def begin_execution(self, job_id: str) -> bool:
        job_dir = self._job_dir(job_id)
        with self.store._job_lock(job_id):
            state = self.store._read_state(job_id)
            job_status = str(state.get("job_status") or "")
            if job_status in {"cancelled", "cancellation_requested"}:
                if _controlled_cancel_pending(state):
                    return False
                if job_status == "cancellation_requested":
                    state["job_status"] = "cancelled"
                    state["result_status"] = "cancelled"
                state["worker_phase"] = "cancelled"
                _release_worker_lease(state, phase="cancelled")
                self._write_state(job_id, state)
                self._cleanup_locked(job_id, state=state)
                return False
            if job_status != "running":
                raise JobNotRunnableError(f"Job is not runnable: {job_id}")
            if str(state.get("worker_phase") or "") != "inputs_captured":
                raise JobNotRunnableError(
                    f"Job inputs are not verified for provider execution: {job_id}"
                )
            input_integrity = state.get("input_integrity")
            reference_count = (
                int(input_integrity.get("reference_count") or 0)
                if isinstance(input_integrity, dict)
                else 0
            )
            try:
                validate_job_input_lease(
                    job_dir,
                    expected_reference_count=reference_count,
                )
            except ValueError as error:
                state["job_status"] = "failed"
                state["result_status"] = "failed"
                state["cache_result_eligible"] = False
                state["worker_phase"] = "failed"
                state["error"] = {
                    "category": "input_consumption_binding_lost",
                    "summary": str(error),
                    "retryable": False,
                }
                _release_worker_lease(state, phase="failed")
                self._write_state(job_id, state)
                self._cleanup_locked(job_id, state=state)
                raise JobNotRunnableError(
                    f"Protected input consumption binding is unavailable: {job_id}"
                ) from error
            state["worker_phase"] = "provider_running"
            _advance_worker_lease(state, phase="provider_running")
            self._write_state(job_id, state)
            return True

    def cancel(self, job_id: str) -> dict[str, Any]:
        self.recover_if_dead(job_id)
        with self.store._job_lock(job_id):
            state = self.store._read_state(job_id)
            job_status = str(state.get("job_status") or "")
            if job_status in _TERMINAL_STATUSES:
                cleanup = self._cleanup_locked(job_id, state=state)
                return {
                    "status": (
                        "ok"
                        if cleanup.get("verified_absent")
                        else "blocked"
                    ),
                    "job_id": job_id,
                    "job_status": job_status,
                    "input_spool_cleanup": cleanup,
                }
            state["cache_result_eligible"] = False
            if (
                job_status == "queued"
                or state.get("worker_phase") == "inputs_captured"
            ):
                state["job_status"] = "cancelled"
                state["result_status"] = "cancelled"
                state["worker_phase"] = "cancelled"
                _release_worker_lease(state, phase="cancelled")
                self._write_state(job_id, state)
                cleanup = self._cleanup_locked(job_id, state=state)
                return {
                    "status": (
                        "ok"
                        if cleanup.get("verified_absent")
                        else "blocked"
                    ),
                    "job_id": job_id,
                    "job_status": "cancelled",
                    "input_spool_cleanup": cleanup,
                }
            state["job_status"] = "cancellation_requested"
            state["cancellation_requested"] = True
            if isinstance(state.get("worker_lease"), dict):
                _advance_worker_lease(
                    state,
                    phase=str(state.get("worker_phase") or "input_spooling"),
                )
            self._write_state(job_id, state)
            return {
                "status": "accepted",
                "job_id": job_id,
                "job_status": "cancellation_requested",
                "input_spool_cleanup": dict(
                    state.get("input_spool_cleanup") or {}
                ),
            }

    def cleanup(self, job_id: str) -> dict[str, Any]:
        self.recover_if_dead(job_id)
        with self.store._job_lock(job_id):
            state = self.store._read_state(job_id)
            job_status = str(state.get("job_status") or "")
            if job_status not in _TERMINAL_STATUSES:
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "job_status": job_status,
                    "error": {
                        "category": "input_spool_in_use",
                        "summary": (
                            "Input spool cleanup is allowed only after the job "
                            "is completed, failed, or cancelled."
                        ),
                        "retryable": True,
                    },
                    "input_spool_cleanup": dict(
                        state.get("input_spool_cleanup") or {}
                    ),
                }
            cleanup = self._cleanup_locked(job_id, state=state)
            return {
                "status": (
                    "ok"
                    if cleanup.get("verified_absent")
                    else "blocked"
                ),
                "job_id": job_id,
                "job_status": job_status,
                "input_spool_cleanup": cleanup,
            }

    def recover_if_dead(self, job_id: str) -> bool:
        job_dir = self._job_dir(job_id)
        with self.store._job_lock(job_id):
            state = self.store._read_state(job_id)
            job_status = str(state.get("job_status") or "")
            worker_phase = str(state.get("worker_phase") or "")
            if (
                job_status not in {"running", "cancellation_requested"}
                or worker_phase not in _ACTIVE_PHASES
            ):
                return False
            if job_status == "cancellation_requested" and _controlled_cancel_pending(state):
                return False
            if has_active_job_input_lease(job_dir):
                return False
            if worker_lease_is_alive(state.get("worker_lease")):
                return False
            cancelled = job_status == "cancellation_requested"
            state["job_status"] = "cancelled" if cancelled else "failed"
            state["result_status"] = "cancelled" if cancelled else "failed"
            state["cache_result_eligible"] = False
            state["worker_phase"] = "cancelled" if cancelled else "failed"
            if not cancelled:
                state["error"] = {
                    "category": "worker_lease_lost",
                    "summary": (
                        "The durable worker lease owner is no longer alive; "
                        "the input spool was recovered without publishing a result."
                    ),
                    "retryable": True,
                }
                state["decision"] = {
                    "owner": "top_model",
                    "options": ["retry-with-force", "inspect-job"],
                }
            _release_worker_lease(
                state,
                phase=state["worker_phase"],
                status="lost",
            )
            self._write_state(job_id, state)
            _discard_uncommitted_outputs(job_dir)
            self._cleanup_locked(job_id, state=state)
            return True

    def finish_locked(
        self,
        job_id: str,
        *,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        _release_worker_lease(
            state,
            phase=str(state.get("worker_phase") or "terminal"),
        )
        self._write_state(job_id, state)
        return self._cleanup_locked(job_id, state=state)

    def _cleanup_locked(
        self,
        job_id: str,
        *,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        job_dir = self._job_dir(job_id)
        cleanup = cleanup_job_inputs(job_dir)
        state["input_spool_cleanup"] = cleanup
        self._write_state(job_id, state)
        return cleanup
