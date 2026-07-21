from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


Spawner = Callable[[str, Path], None]


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


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


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
    ) -> None:
        self.root = Path(root or default_state_root()).expanduser().resolve()
        self.spawner = spawner or _default_spawner
        configured_preview = int(os.environ.get("LLM_TOOLKIT_RESULT_PREVIEW_CHARS", "2000"))
        self.result_preview_chars = max(32, result_preview_chars or configured_preview)

    @staticmethod
    def request_digest(request: dict[str, Any]) -> str:
        canonical = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def submit(self, request: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        digest = self.request_digest(request)
        job_id = self._new_attempt_id(digest) if force else digest[:24]
        job_dir = self._job_dir(job_id)
        state_path = job_dir / "state.json"
        if state_path.is_file():
            state = self.get(job_id)
            if state["job_status"] == "completed":
                return {
                    "status": "cache_hit",
                    "job_id": job_id,
                    "job_status": "completed",
                    "poll_after_ms": 0,
                }
            if state["job_status"] == "stale":
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "job_status": "stale",
                    "poll_after_ms": 0,
                    "error": state["error"],
                    "decision": state["decision"],
                }
            return {
                "status": "running" if state["job_status"] == "running" else "accepted",
                "job_id": job_id,
                "job_status": state["job_status"],
                "poll_after_ms": 2000,
                "monitor_until_utc": state.get("monitor_until_utc"),
            }

        job_dir.mkdir(parents=True, exist_ok=False)
        _atomic_json(job_dir / "request.json", request)
        _atomic_json(
            state_path,
            {
                "schema": "llm-backend-toolkit.job-state.v1",
                "job_id": job_id,
                "request_digest": digest,
                "job_status": "queued",
                "provider": str(request.get("provider") or ""),
                "created_utc": _utc_now(),
                "updated_utc": _utc_now(),
                "monitor_until_utc": _utc_after(self._timeout_seconds(request)),
            },
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
            "poll_after_ms": 2000,
            "forced": force,
            "monitor_until_utc": self._read_state(job_id).get("monitor_until_utc"),
        }

    def claim(self, job_id: str) -> dict[str, Any]:
        job_dir = self._job_dir(job_id)
        request_path = job_dir / "request.json"
        if not request_path.is_file():
            raise FileNotFoundError(f"Job request is unavailable: {job_id}")
        state = self._read_state(job_id)
        state["job_status"] = "running"
        state["updated_utc"] = _utc_now()
        _atomic_json(job_dir / "state.json", state)
        value = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Stored job request is not an object")
        return value

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        job_dir = self._job_dir(job_id)
        _atomic_json(job_dir / "result.json", result)
        self._write_output_artifact(job_dir, result.get("output"))
        state = self._read_state(job_id)
        state["job_status"] = "completed"
        state["result_status"] = str(result.get("status") or "unknown")
        state["updated_utc"] = _utc_now()
        _atomic_json(job_dir / "state.json", state)
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
        output = {
            "status": "ok",
            "job_id": job_id,
            "job_status": effective_status,
            "provider": state.get("provider"),
            "result_status": state.get("result_status"),
            "created_utc": state.get("created_utc"),
            "updated_utc": state.get("updated_utc"),
            "monitor_until_utc": state.get("monitor_until_utc"),
            "poll_after_ms": 0 if effective_status in {"completed", "stale"} else 2000,
        }
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

    @staticmethod
    def _timeout_seconds(request: dict[str, Any]) -> int:
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
        if str(request.get("provider") or "") == "qwen3.7-plus":
            return 300
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
        return compact

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
