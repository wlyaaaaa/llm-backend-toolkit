from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
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


def _display_text(value: Any, *, max_chars: int = 1_000) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_chars]


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
        request, conversation = self._prepare_continuation(request)
        digest = self.request_digest(request)
        execution = request.get("execution") or {}
        is_agent = str(execution.get("mode") or "direct") == "agent"
        task = request.get("task") or {}
        media = request.get("media") or {}
        has_mutable_references = bool(task.get("sources") or media.get("attachments"))
        explicit_cache_key = str(execution.get("cache_key") or request.get("cache_key") or "").strip()
        cacheable = (not is_agent and not has_mutable_references) or bool(explicit_cache_key)
        initial_poll_ms = self._initial_poll_ms(request)
        job_id = self._new_attempt_id(digest) if force or not cacheable else digest[:24]
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
                "poll_after_ms": initial_poll_ms,
                "recommended_check_utc": _utc_after(initial_poll_ms // 1000),
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
                "backend": str(request.get("backend") or request.get("provider") or "local-default"),
                "provider": str(request.get("provider") or ""),
                "created_utc": _utc_now(),
                "updated_utc": _utc_now(),
                "monitor_until_utc": _utc_after(self._timeout_seconds(request)),
                "cacheable": cacheable,
                "poll_count": 0,
                "initial_poll_ms": initial_poll_ms,
                "conversation_root": (conversation or {}).get("root_job_id") or job_id,
                "conversation_turn": (conversation or {}).get("turn") or 1,
                "conversation_max_turns": (conversation or {}).get("max_turns"),
                "display": {
                    "task_goal": _display_text(task.get("goal")),
                    "execution_mode": "agent" if is_agent else "direct",
                    "reasoning_mode": str(
                        (request.get("reasoning") or {}).get("mode") or "off"
                    ),
                },
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
            "poll_after_ms": initial_poll_ms,
            "recommended_check_utc": _utc_after(initial_poll_ms // 1000),
            "forced": force,
            "cacheable": cacheable,
            "monitor_until_utc": self._read_state(job_id).get("monitor_until_utc"),
            **({"conversation": conversation} if conversation else {}),
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
