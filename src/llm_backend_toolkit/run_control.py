"""A worker-local adapter to the existing AICLI cooperative run/abort control.

No arbitrary commands are loaded from stored metadata. The runner supplies the
same executable prefix it already validated. A request is never a stop receipt.
"""
from __future__ import annotations
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable
from .input_integrity import assert_safe_job_path

CONTROL_SCHEMA = "llm-backend-toolkit.runtime-control.v1"


def runtime_cleanup_pending(state: dict[str, Any]) -> bool:
    control = state.get("runtime_control")
    return (isinstance(control, dict) and control.get("schema") == CONTROL_SCHEMA
            and control.get("cleanup_confirmed") is not True)


def read_control(path: Path, expected: dict[str, str]) -> dict[str, Any] | None:
    if not path.exists():
        return None
    assert_safe_job_path(path.parent, path, require_exists=True)
    if path.stat().st_size > 65536:
        raise ValueError("Run control metadata exceeds its bound")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("schema") != "aicli.run-control.v1"
            or not isinstance(value.get("run_id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", value["run_id"])
            or value.get("profile_id") != expected["profile"]
            or value.get("model") != expected["model"]
            or not isinstance(value.get("workspace"), str)
            or os.path.normcase(os.path.abspath(value["workspace"]))
               != os.path.normcase(os.path.abspath(expected["workspace"]))):
        raise ValueError("Run control identity does not match the selected task")
    return value


class CooperativeRunControl:
    def __init__(self, path: Path, expected: dict[str, str], context: dict[str, Any]):
        self.path = path
        self.expected = expected
        self.context = context
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.observation: dict[str, Any] = {
            "schema": CONTROL_SCHEMA, "state": "prepared", "cleanup_confirmed": False,
            "process_tree": "unconfirmed", "gpu_lease": "unconfirmed",
        }
        self.lock = threading.Lock()

    def publish(self, **fields: Any) -> None:
        with self.lock:
            self.observation.update(fields)
            snapshot = dict(self.observation)
        self.context["publish"](snapshot)

    def start(self, call_abort: Callable[[str], dict[str, Any]]) -> None:
        self.context["register"](dict(self.observation))
        def watch():
            while not self.stop.wait(0.5):
                try:
                    if not self.context["cancel_requested"]():
                        continue
                    control = read_control(self.path, self.expected)
                    if control is None:
                        continue
                    self.publish(run_id=control["run_id"], state="abort_requested")
                    # Exactly one dispatch; ambiguous or failed delivery is not retried.
                    result = call_abort(control["run_id"])
                    recovery = result.get("recovery") or {}
                    confirmed = (isinstance(recovery, dict)
                                 and recovery.get("runId") == control["run_id"]
                                 and recovery.get("status") in {"abort_requested", "aborted", "completed"})
                    self.publish(abort_delivery="accepted" if confirmed else "unconfirmed")
                except Exception:
                    self.publish(abort_delivery="unconfirmed")
                return
        self.thread = threading.Thread(target=watch, name="aicli-cooperative-cancel", daemon=True)
        self.thread.start()

    def observe(self, envelope: dict[str, Any], *, requires_gpu: bool) -> None:
        try:
            control = read_control(self.path, self.expected)
        except (ValueError, OSError):
            self.publish(state="unconfirmed")
            return
        run = envelope.get("run") or {}
        recovery = envelope.get("recovery") or {}
        if not isinstance(run, dict) or not isinstance(recovery, dict) or control is None:
            self.publish(state="unconfirmed")
            return
        run_id = run.get("recoveryRunId") or recovery.get("runId")
        if run_id != control["run_id"]:
            self.publish(state="unconfirmed")
            return
        no_start = recovery.get("status") == "aborted" and type(recovery.get("attempts")) is int and recovery["attempts"] == 0
        usage = run.get("limitUsage") or {}
        process_closed = no_start or isinstance(usage, dict) and usage.get("cleanupConfirmed") is True
        broker = run.get("localGpuBrokerSessionSummary") or run.get("localGpuBrokerSession") or {}
        gpu_closed = no_start or not requires_gpu or (
            isinstance(broker, dict) and broker.get("state") == "released"
            and type(broker.get("activeRequests", broker.get("active_requests"))) is int
            and broker.get("activeRequests", broker.get("active_requests")) == 0
        )
        closed = bool(process_closed and gpu_closed)
        self.publish(run_id=run_id, state="confirmed_absent" if closed else "unconfirmed",
                     cleanup_confirmed=closed,
                     process_tree="confirmed_absent" if process_closed else "unconfirmed",
                     gpu_lease=("released" if requires_gpu else "not_applicable") if gpu_closed else "unconfirmed")

    def close(self) -> None:
        self.stop.set()
        # Preserve the exact recovery handle even when the process call timed out.
        # This is a locator, never a proof that the run stopped.
        try:
            identity = read_control(self.path, self.expected)
            if identity is not None:
                self.publish(run_id=identity["run_id"])
        except (ValueError, OSError):
            pass
        if self.thread is not None:
            self.thread.join(timeout=45)
            if self.thread.is_alive():
                self.publish(state="unconfirmed", cleanup_confirmed=False, abort_delivery="unconfirmed")
