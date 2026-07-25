from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ToolError, classify_agent_process_error


@dataclass(frozen=True)
class AgentResponse:
    content: str
    runner: str
    model: str
    exit_code: int
    duration_ms: int
    tool_calls: int = 0
    session_id: str = ""
    stop_reason: str = ""
    limit_enforcement: dict[str, str] = field(default_factory=dict)
    steps: int = 0
    limit_usage: dict[str, Any] = field(default_factory=dict)
    limit_hit: str = ""
    event_projection: str = ""
    machine_event_projection: str = ""
    machine_event_status: str = ""
    machine_event_count: int = 0
    observability_level: str = "lifecycle"


class AgentRunnerError(Exception):
    def __init__(self, error: ToolError, receipt: dict[str, Any] | None = None) -> None:
        super().__init__(error.summary)
        self.error = error
        self.receipt = receipt or {}


def _runner_error(
    category: str,
    summary: str,
    *,
    retryable: bool = False,
    receipt: dict[str, Any] | None = None,
) -> AgentRunnerError:
    return AgentRunnerError(
        ToolError(
            category=category,
            summary=summary,
            retryable=retryable,
            options=("inspect-agent-run", "handle-in-codex"),
        ),
        receipt,
    )


def _bounded_process(
    command: list[str],
    *,
    cwd: Path,
    stdin_text: str,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    max_output_chars: int = 1_000_000,
    event_file: Path | None = None,
    on_event: Any | None = None,
) -> tuple[int, str, str, int]:
    started = time.monotonic()
    event_stop: threading.Event | None = None
    event_thread: threading.Thread | None = None

    def finish_event_tail() -> None:
        if event_stop is None or event_thread is None:
            return
        event_stop.set()
        event_thread.join(timeout=2)

    try:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            shell=False,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        if event_file is not None and callable(on_event):
            event_stop = threading.Event()
            event_thread = threading.Thread(
                target=_tail_machine_events,
                args=(Path(event_file), on_event, event_stop),
                name="aicli-public-event-tail",
                daemon=True,
            )
            event_thread.start()
        stdout, stderr = process.communicate(stdin_text, timeout=timeout_seconds)
        finish_event_tail()
    except subprocess.TimeoutExpired as exc:
        cleanup_confirmed = False
        if "process" in locals():
            if os.name == "nt":
                try:
                    killed = subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=10,
                        shell=False,
                        check=False,
                    )
                    if killed.returncode == 0:
                        process.wait(timeout=5)
                        cleanup_confirmed = process.poll() is not None
                except (OSError, subprocess.SubprocessError):
                    cleanup_confirmed = False
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.wait(timeout=5)
                    cleanup_confirmed = process.poll() is not None
                except (OSError, subprocess.SubprocessError):
                    cleanup_confirmed = False
            if not cleanup_confirmed:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
            elif cleanup_confirmed:
                try:
                    process.communicate(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
        finish_event_tail()
        if not cleanup_confirmed:
            raise _runner_error(
                "agent_budget_unenforced",
                "Agent wall budget expired, but complete process-tree cleanup could not be confirmed.",
                receipt={
                    "stop_reason": "cleanup_unconfirmed",
                    "limit_hit": "timeout",
                    "cleanup_confirmed": False,
                    "limit_enforcement": {"timeout": "failed-closed"},
                },
            ) from exc
        raise _runner_error(
            "agent_timeout",
            f"Agent exceeded its {timeout_seconds}-second wall budget.",
            retryable=True,
            receipt={
                "stop_reason": "timeout",
                "limit_hit": "timeout",
                "cleanup_confirmed": True,
                "limit_enforcement": {"timeout": "hard"},
            },
        ) from exc
    except OSError as exc:
        finish_event_tail()
        raise _runner_error("agent_runner_unavailable", f"Agent process could not start: {type(exc).__name__}") from exc
    duration_ms = int((time.monotonic() - started) * 1000)
    return process.returncode, stdout[:max_output_chars], stderr[:max_output_chars], duration_ms


def _tail_machine_events(
    path: Path,
    callback: Any,
    stop: threading.Event,
) -> None:
    offset = 0
    pending = b""
    expected_sequence = 1
    while True:
        data = b""
        try:
            if path.is_file():
                with path.open("rb") as stream:
                    stream.seek(offset)
                    data = stream.read(1_048_576)
                    offset = stream.tell()
        except OSError:
            data = b""
        if data:
            pending += data
            lines = pending.split(b"\n")
            pending = lines.pop()
            for encoded in lines:
                if not encoded or len(encoded) > 65_536:
                    continue
                try:
                    event = json.loads(encoded.decode("utf-8"))
                    sequence = int(event.get("sequence") or 0)
                except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                    continue
                if (
                    not isinstance(event, dict)
                    or event.get("schema") != "aicli.machine-event.v1"
                    or sequence != expected_sequence
                ):
                    continue
                expected_sequence += 1
                try:
                    callback(event)
                except Exception:
                    continue
            continue
        if stop.is_set():
            return
        stop.wait(0.05)


def _qwen_command_prefix(executable: str) -> list[str]:
    path = Path(executable).expanduser().resolve()
    if path.suffix.lower() == ".js":
        node = shutil.which("node")
        if node:
            return [node, str(path)]
    if path.suffix.lower() in {".cmd", ".ps1", ""}:
        node_modules = path.parent.parent if path.parent.name.lower() == ".bin" else path.parent / "node_modules"
        entry = node_modules / "@qwen-code" / "qwen-code" / "cli-entry.js"
        node = shutil.which("node")
        if node and entry.is_file():
            return [node, str(entry)]
    if path.suffix.lower() in {".exe", ".com"}:
        return [str(path)]
    raise _runner_error("agent_runner_unavailable", "Qwen Code executable could not be resolved without a shell.")


def _opencode_command_prefix(executable: str) -> list[str]:
    path = Path(executable).expanduser().resolve()
    if path.suffix.lower() in {".ps1", ".cmd", ".bat"}:
        native = path.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if native.is_file():
            return [str(native)]
    if path.suffix.lower() in {".exe", ".com"}:
        return [str(path)]
    raise _runner_error("agent_runner_unavailable", "OpenCode executable could not be resolved without a shell.")


def _json_values(text: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    stripped = text.strip()
    if not stripped:
        return values
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    except json.JSONDecodeError:
        pass
    for line in stripped.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_extract_text(item) for item in value]
        return "".join(part for part in parts if part)
    if not isinstance(value, dict):
        return ""
    for key in ("text", "content", "result", "response", "message", "output"):
        if key in value:
            text = _extract_text(value[key])
            if text:
                return text
    return ""


class QwenCodeRunner:
    name = "qwen-code"

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or os.environ.get("LLM_TOOLKIT_QWEN_CODE") or shutil.which("qwen")

    def invoke(self, prompt: str, execution: dict[str, Any]) -> AgentResponse:
        raise _runner_error(
            "unsafe_direct_runner_disabled",
            "Direct Qwen Code execution is disabled; use the sandboxed aicli profile runner.",
        )
        if not self.executable:
            raise _runner_error("agent_runner_unavailable", "Qwen Code is not installed or configured.")
        workspace = Path(execution["workspace"])
        budget = execution["budget"]
        model = str(execution.get("model") or "qwen-main-v1")
        base_url = str(os.environ.get("LLM_TOOLKIT_OLLAMA_BASE_URL") or "http://127.0.0.1:32100").rstrip("/")
        with tempfile.TemporaryDirectory(prefix="llm-toolkit-qwen-") as temp:
            qwen_home = Path(temp)
            settings = {
                "env": {"LLM_TOOLKIT_LOCAL_KEY": "ollama"},
                "modelProviders": {
                    "openai": [
                        {
                            "id": model,
                            "name": model,
                            "envKey": "LLM_TOOLKIT_LOCAL_KEY",
                            "baseUrl": f"{base_url}/v1",
                            "generationConfig": {
                                "timeout": int(budget["timeout_seconds"]) * 1000,
                                "maxRetries": 0,
                                "contextWindowSize": 262144,
                            },
                        }
                    ]
                },
                "security": {"auth": {"selectedType": "openai"}},
                "model": {
                    "name": model,
                    "maxSessionTurns": int(budget["max_steps"]),
                    "maxToolCalls": int(budget["max_tool_calls"]),
                },
                "tools": {"approvalMode": "yolo" if execution["policy"] == "workspace-write" else "plan"},
            }
            (qwen_home / "settings.json").write_text(
                json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            env = dict(os.environ)
            env["QWEN_HOME"] = str(qwen_home)
            env["LLM_TOOLKIT_LOCAL_KEY"] = "ollama"
            command = _qwen_command_prefix(self.executable) + ["-p", "", "--output-format", "json"]
            code, stdout, stderr, duration_ms = _bounded_process(
                command,
                cwd=workspace,
                stdin_text=prompt,
                timeout_seconds=int(budget["timeout_seconds"]),
                env=env,
            )
        values = _json_values(stdout)
        final = _extract_text(values[-1]) if values else stdout.strip()
        if code != 0:
            raise _runner_error("agent_failed", f"Qwen Code exited with code {code}: {stderr.strip()[:500]}", retryable=True)
        if not final:
            raise _runner_error("agent_failed", "Qwen Code returned no final answer.", retryable=True)
        last = values[-1] if values else {}
        stats = last.get("stats") or {}
        tool_stats = stats.get("tools") or {}
        return AgentResponse(
            content=final,
            runner=self.name,
            model=model,
            exit_code=code,
            duration_ms=duration_ms,
            tool_calls=int(tool_stats.get("totalCalls") or last.get("tool_calls") or last.get("toolCalls") or 0),
            session_id=str(last.get("session_id") or last.get("sessionId") or ""),
            stop_reason=str(last.get("stop_reason") or last.get("stopReason") or "completed"),
        )


class OpenCodeRunner:
    name = "opencode"

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or os.environ.get("LLM_TOOLKIT_OPENCODE") or shutil.which("opencode")

    def invoke(self, prompt: str, execution: dict[str, Any]) -> AgentResponse:
        raise _runner_error(
            "unsafe_direct_runner_disabled",
            "Direct OpenCode execution is disabled; use the sandboxed aicli profile runner.",
        )
        if not self.executable:
            raise _runner_error("agent_runner_unavailable", "OpenCode is not installed or configured.")
        workspace = Path(execution["workspace"])
        budget = execution["budget"]
        model_ref = str(
            execution.get("model_ref")
            or os.environ.get("LLM_TOOLKIT_OPENCODE_MODEL")
            or "ollama5090d/qwen-main-v1"
        )
        command = _opencode_command_prefix(self.executable) + [
            "run",
            "--pure",
            "--format",
            "json",
            "--model",
            model_ref,
            "--dir",
            str(workspace),
        ]
        if execution["policy"] == "workspace-write":
            command.append("--auto")
        command.append(prompt)
        code, stdout, stderr, duration_ms = _bounded_process(
            command,
            cwd=workspace,
            stdin_text="",
            timeout_seconds=int(budget["timeout_seconds"]),
        )
        values = _json_values(stdout)
        messages: list[str] = []
        tool_calls = 0
        session_id = ""
        for value in values:
            session_id = session_id or str(value.get("sessionID") or value.get("session_id") or "")
            event_type = str(value.get("type") or "")
            part = value.get("part") or value.get("item") or value
            if "tool" in event_type or (isinstance(part, dict) and "tool" in str(part.get("type") or "")):
                tool_calls += 1
            if isinstance(part, dict) and str(part.get("type") or "") in {"text", "agent_message"}:
                text = _extract_text(part)
                if text:
                    messages.append(text)
        final = messages[-1] if messages else (_extract_text(values[-1]) if values else stdout.strip())
        if code != 0:
            raise _runner_error("agent_failed", f"OpenCode exited with code {code}: {stderr.strip()[:500]}", retryable=True)
        if not final:
            raise _runner_error("agent_failed", "OpenCode returned no final answer.", retryable=True)
        return AgentResponse(
            content=final,
            runner=self.name,
            model=model_ref,
            exit_code=code,
            duration_ms=duration_ms,
            tool_calls=tool_calls,
            session_id=session_id,
            stop_reason="completed",
        )


class AiCliProfileRunner:
    def __init__(self, *, name: str, engine: str, default_profile: str, entry: str | None = None) -> None:
        self.name = name
        self.engine = engine
        self.default_profile = default_profile
        self.entry = entry or os.environ.get("LLM_TOOLKIT_AICLI_ENTRY")
        self._machine_events_supported: bool | None = None

    def _prefix(self) -> list[str]:
        entry_value = self.entry
        if not entry_value and os.name == "nt":
            local = os.environ.get("LOCALAPPDATA")
            if local:
                installed = Path(local) / "aicli" / "bin" / "aicli.ps1"
                if installed.is_file():
                    entry_value = str(installed)
        if not entry_value:
            raise _runner_error("agent_runner_unavailable", "LLM_TOOLKIT_AICLI_ENTRY is not configured.")
        entry = Path(entry_value).expanduser().resolve()
        if not entry.is_file() or entry.suffix.lower() != ".ps1":
            raise _runner_error("agent_runner_unavailable", "aicli entry must be an existing PowerShell script.")
        pwsh = shutil.which("pwsh")
        if not pwsh:
            raise _runner_error("agent_runner_unavailable", "PowerShell 7 is unavailable.")
        return [pwsh, "-NoProfile", "-File", str(entry)]

    def _supports_machine_events(self, prefix: list[str], workspace: Path) -> bool:
        if self._machine_events_supported is not None:
            return self._machine_events_supported
        try:
            code, stdout, _, _ = _bounded_process(
                prefix + ["version", "--json"],
                cwd=workspace,
                stdin_text="",
                timeout_seconds=10,
                max_output_chars=100_000,
            )
            values = _json_values(stdout)
            capabilities = values[-1].get("capabilities") if values else {}
            self._machine_events_supported = bool(
                code == 0
                and isinstance(capabilities, dict)
                and capabilities.get("machineEventProjection")
                == "aicli.machine-event.v1"
            )
        except AgentRunnerError:
            self._machine_events_supported = False
        return self._machine_events_supported

    @staticmethod
    def _emit_machine_event(callback: Any, event: dict[str, Any]) -> None:
        if not callable(callback):
            return
        kind = str(event.get("kind") or "")
        status = str(event.get("status") or "")
        item_type = str(event.get("item_type") or "")
        phase = "waiting"
        if kind == "reasoning.activity":
            phase = "thinking"
            summary = (
                "智能体正在进行内部分析；只展示活动状态，不展示隐藏思维正文。"
            )
        elif kind == "tool.activity":
            labels = {
                "file_change": "编辑文件",
                "command_execution": "执行命令",
                "mcp_tool_call": "调用 MCP 工具",
                "web_search": "查询公开资料",
                "computer_use": "操作计算机",
                "tool_call": "调用工具",
                "dynamic_tool_call": "调用动态工具",
            }
            activity = labels.get(item_type, "调用工具")
            summary = f"智能体正在{activity}。" if status != "completed" else f"智能体已完成{activity}。"
        elif kind == "planning.activity":
            summary = "智能体正在更新公开工作计划。"
        elif kind == "output.completed":
            phase = "generating"
            summary = "智能体已形成一段公开输出。"
        elif kind == "turn.completed":
            phase = "validating"
            summary = "智能体本轮工作完成，正在整理结果与回执。"
        elif kind in {"turn.failed", "run.failed", "limit.hit"}:
            phase = "failed"
            summary = "智能体运行未成功完成，安全失败状态已保留。"
        elif kind == "thread.started":
            summary = "AICLI 已建立可观察的原生智能体线程。"
        elif kind == "turn.started":
            summary = "原生智能体已开始处理本轮任务。"
        else:
            return
        allowed_payload = {
            key: event.get(key)
            for key in (
                "status",
                "item_type",
                "steps",
                "tool_calls",
                "events_seen",
                "error_category",
                "limit",
            )
            if event.get(key) is not None
        }
        progress: dict[str, Any] = {
            "phase": phase,
            "public_event": {
                "kind": f"agent.{kind}",
                "summary_zh": summary,
                "payload": allowed_payload,
            },
        }
        if kind == "output.completed":
            progress["content_delta"] = str(event.get("public_text") or "")
        try:
            callback(progress)
        except Exception:
            return

    def invoke(self, prompt: str, execution: dict[str, Any]) -> AgentResponse:
        workspace = Path(execution["workspace"])
        budget = execution["budget"]
        model = str(execution.get("model") or "qwen-main-v1")
        profile = str(execution.get("profile") or self.default_profile)
        native_images = [str(path) for path in execution.get("native_images") or []]
        if self.engine == "codex":
            native = [
                "exec", "--json", "--ephemeral", "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check", "--disable", "plugins", "--model", model,
            ]
            for path in native_images:
                native.extend(["--image", path])
            native.append("-")
        elif self.engine == "claude":
            native = [
                "--model", model, "-p", "--output-format", "json",
                "--max-turns", str(budget["max_steps"]), "--dangerously-skip-permissions",
            ]
        elif self.engine == "opencode":
            native = []
            for path in native_images:
                native.extend(["--file", path])
        elif self.engine == "qwen-code":
            native = []
        else:
            raise _runner_error("agent_runner_unavailable", f"Unsupported aicli agent engine: {self.engine}")
        prefix = self._prefix()
        progress_callback = execution.get("_progress_callback")
        machine_events_supported = (
            self.engine == "codex"
            and self._supports_machine_events(prefix, workspace)
        )
        if callable(progress_callback):
            try:
                progress_callback(
                    {
                        "phase": "waiting",
                        "public_event": {
                            "kind": "agent.observability",
                            "summary_zh": (
                                "AICLI 实时安全工作事件已接入观察台。"
                                if machine_events_supported
                                else "当前 AICLI 仅提供生命周期可见性；结果仍会完整交付。"
                            ),
                            "payload": {
                                "level": (
                                    "live_safe_events"
                                    if machine_events_supported
                                    else "lifecycle"
                                )
                            },
                        },
                    }
                )
            except Exception:
                pass
        command = prefix + [
            "run", profile, "--project", str(workspace), "--stdin", "--json",
            "--sandbox-policy", str(execution["policy"]),
            "--timeout-seconds", str(budget["timeout_seconds"]),
            "--max-steps", str(budget["max_steps"]),
            "--max-tool-calls", str(budget["max_tool_calls"]),
            "--max-output-chars", "1000000",
        ]
        event_temp: tempfile.TemporaryDirectory[str] | None = None
        event_path: Path | None = None
        if machine_events_supported:
            event_temp = tempfile.TemporaryDirectory(
                prefix="llm-toolkit-aicli-events-"
            )
            event_path = Path(event_temp.name) / "events.jsonl"
            event_path.touch()
            command.extend(["--event-file", str(event_path)])
        command.extend(["--", *native])
        try:
            code, stdout, stderr, duration_ms = _bounded_process(
                command,
                cwd=workspace,
                stdin_text=prompt,
                timeout_seconds=int(budget["timeout_seconds"]) + 15,
                event_file=event_path,
                on_event=(
                    lambda event: self._emit_machine_event(
                        progress_callback,
                        event,
                    )
                )
                if callable(progress_callback)
                else None,
            )
        finally:
            if event_temp is not None:
                event_temp.cleanup()
        envelopes = _json_values(stdout)
        if not envelopes:
            raise _runner_error("agent_failed", f"aicli returned no JSON envelope: {stderr.strip()[:500]}", retryable=True)
        envelope = envelopes[-1]
        run = envelope.get("run") or {}
        child_stdout = str(run.get("stdout") or "")
        child_values = _json_values(child_stdout)
        limit_enforcement = dict(run.get("limitEnforcement") or {})
        limit_usage = dict(run.get("limitUsage") or {})
        limit_hit = str(run.get("limitHit") or "")
        steps = int(limit_usage.get("steps") or 0)
        reported_tool_calls = int(limit_usage.get("toolCalls") or 0)
        if self.engine == "codex":
            messages: list[str] = []
            tool_calls = 0
            session_id = ""
            for value in child_values:
                if value.get("type") == "thread.started":
                    session_id = str(value.get("thread_id") or "")
                item = value.get("item") or {}
                item_type = str(item.get("type") or "") if isinstance(item, dict) else ""
                if item_type == "agent_message":
                    text = _extract_text(item)
                    if text:
                        messages.append(text)
                elif item_type in {
                    "command_execution", "file_change", "mcp_tool_call", "tool_call",
                    "dynamic_tool_call", "web_search", "computer_use",
                }:
                    tool_calls += 1
            final = messages[-1] if messages else ""
        elif self.engine == "opencode":
            messages: list[str] = []
            tool_calls = 0
            session_id = ""
            for value in child_values:
                session_id = session_id or str(value.get("sessionID") or value.get("session_id") or "")
                event_type = str(value.get("type") or "")
                part = value.get("part") or value.get("item") or value
                if "tool" in event_type or (isinstance(part, dict) and "tool" in str(part.get("type") or "")):
                    tool_calls += 1
                if isinstance(part, dict) and str(part.get("type") or "") in {"text", "agent_message"}:
                    text = _extract_text(part)
                    if text:
                        messages.append(text)
            final = messages[-1] if messages else (_extract_text(child_values[-1]) if child_values else "")
        else:
            last = child_values[-1] if child_values else {}
            final = _extract_text(last) if last else child_stdout.strip()
            stats = last.get("stats") or {} if isinstance(last, dict) else {}
            tool_stats = stats.get("tools") or {} if isinstance(stats, dict) else {}
            tool_calls = int(
                tool_stats.get("totalCalls")
                or (last.get("num_turns") if isinstance(last, dict) else 0)
                or (last.get("tool_calls") if isinstance(last, dict) else 0)
                or 0
            )
            session_id = str(last.get("session_id") or "") if isinstance(last, dict) else ""
        if reported_tool_calls or "toolCalls" in limit_usage:
            tool_calls = reported_tool_calls
        child_code = int(run.get("exitCode") if run.get("exitCode") is not None else code)
        receipt = {
            "runner": self.name,
            "model": model,
            "exit_code": child_code,
            "duration_ms": int(run.get("durationMs") or duration_ms),
            "steps": steps,
            "tool_calls": tool_calls,
            "session_id": session_id,
            "stop_reason": limit_hit or ("failed" if code != 0 or child_code != 0 else "completed"),
            "limit_hit": limit_hit or None,
            "limit_enforcement": limit_enforcement,
            "limit_usage": {
                "steps": steps,
                "tool_calls": tool_calls,
                "events_seen": int(limit_usage.get("eventsSeen") or 0),
                "protocol": str(limit_usage.get("protocol") or ""),
                "step_definition": str(limit_usage.get("stepDefinition") or ""),
                "cleanup_confirmed": bool(limit_usage.get("cleanupConfirmed", True)),
                "cleanup_method": str(limit_usage.get("cleanupMethod") or ""),
            },
            "event_projection": str(run.get("eventProjection") or ""),
            "machine_event_projection": str(
                run.get("machineEventProjection") or ""
            ),
            "machine_event_status": str(run.get("machineEventStatus") or ""),
            "machine_event_count": int(run.get("machineEventCount") or 0),
            "observability_level": (
                "live_safe_events"
                if machine_events_supported
                and str(run.get("machineEventStatus") or "") == "ok"
                else "lifecycle"
            ),
        }
        if any(
            limit_enforcement.get(name) == "failed-closed"
            for name in ("timeout", "maxSteps", "maxToolCalls")
        ):
            receipt["stop_reason"] = "budget_unenforced"
            raise _runner_error(
                "agent_budget_unenforced",
                "The agent boundary failed closed before proving every declared hard limit.",
                receipt=receipt,
            )
        if bool(run.get("timedOut")) or limit_hit == "timeout":
            receipt["stop_reason"] = "timeout"
            receipt["limit_hit"] = "timeout"
            raise _runner_error(
                "agent_timeout",
                "Agent exceeded its hard wall-clock budget.",
                retryable=True,
                receipt=receipt,
            )
        if limit_hit:
            raise _runner_error(
                "agent_budget_exceeded",
                f"Agent stopped after exceeding its hard {limit_hit} budget.",
                receipt=receipt,
            )
        if code != 0 or child_code != 0:
            detail = "\n".join(
                part for part in (str(run.get("stderr") or ""), stderr, child_stdout) if part
            ).strip()[-2000:]
            error = classify_agent_process_error(detail)
            raise AgentRunnerError(error, receipt)
        if any(limit_enforcement.get(name) != "hard" for name in ("timeout", "maxSteps", "maxToolCalls")):
            receipt["stop_reason"] = "budget_unenforced"
            raise _runner_error(
                "agent_budget_unenforced",
                "The selected agent runner did not prove every declared budget as a hard limit.",
                receipt=receipt,
            )
        if not final:
            raise _runner_error("agent_failed", f"{self.name} returned no final answer.", retryable=True)
        return AgentResponse(
            content=final,
            runner=self.name,
            model=model,
            exit_code=child_code,
            duration_ms=int(run.get("durationMs") or duration_ms),
            tool_calls=tool_calls,
            session_id=session_id,
            stop_reason="completed",
            limit_enforcement=limit_enforcement,
            steps=steps,
            limit_usage=receipt["limit_usage"],
            limit_hit="",
            event_projection=receipt["event_projection"],
            machine_event_projection=receipt["machine_event_projection"],
            machine_event_status=receipt["machine_event_status"],
            machine_event_count=receipt["machine_event_count"],
            observability_level=receipt["observability_level"],
        )


def default_runners() -> dict[str, Any]:
    qwen = AiCliProfileRunner(name="qwen-code", engine="qwen-code", default_profile="qwen-code-ollama-main")
    opencode = AiCliProfileRunner(name="opencode", engine="opencode", default_profile="opencode-ollama-main")
    codex = AiCliProfileRunner(name="codex-cli", engine="codex", default_profile="codex-ollama-main")
    claude = AiCliProfileRunner(name="claude-code", engine="claude", default_profile="claude-ollama-main")
    # data_factory is a stable product alias. The concrete target is changed only
    # after a checked-in bake-off, never by request-time heuristics or fallback.
    return {
        "data_factory": codex,
        "qwen-code": qwen,
        "opencode": opencode,
        "codex-cli": codex,
        "claude-code": claude,
    }
