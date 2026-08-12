from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ToolError, classify_agent_process_error


PUBLIC_PROGRESS_MAX_CHARS = 500
_PUBLIC_PROGRESS_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|authorization|passwd|password|secret|token)"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9+/_.=-]{8,}"
    ),
)
_PUBLIC_PROGRESS_ALWAYS_UNSAFE_PATH_PATTERNS = (
    re.compile(r"\bfile:(?:/{1,3}|\\\\)", re.IGNORECASE),
    re.compile(
        r"(?<![A-Z0-9_/\\])[A-Z]:[\\/][^\s\x00]*",
        re.IGNORECASE,
    ),
    re.compile(r"\\\\[^\s\\/]+\\[^\s\\/]+"),
    re.compile(
        r"\\Device\\[^\s\\/]+\\[^\s\\/]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"""(?:^|[\s(\[{'\"=:：（【「『])\\(?!\\)[^\s\\/]+\\[^\s\\/]+"""
    ),
)
_PUBLIC_PROGRESS_NON_URL_PATH_PATTERNS = (
    re.compile(r"//[^\s\\/]+/[^\s\\/]+"),
    re.compile(r"(?<![:/])/(?!/)[^\s\\/]+/[^\s\\/]+"),
    re.compile(
        r"""(?:^|[\s(\[{'\"=:：（【「『])/(?!/)[^\s\\/]+(?:/[^\s\\/]+)*"""
    ),
)
_PUBLIC_PROGRESS_URL_PATTERN = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>＜＞]+"
)

_AUDITED_AICLI_VERSION = "0.3.3"
_AUDITED_AICLI_SOURCE_BUNDLE_SHA256 = (
    "sha256:53acdd7b6312c52b679810f16c1b3c59c805ae5fea3c9e9be8bd7002ea6412aa"
)
_AUDITED_AICLI_ENTRY_SHA256 = (
    "sha256:00cf5e0cf8c1ecc6742a8501656efe51d2cf9bfca2d3c222a8b5eb566370249f"
)
_AUDITED_AICLI_NETWORK_COMPONENTS = {
    "Private/ChildProcess.ps1": (
        "sha256:29d78b7dc86ccb1f4be98c7742cdf3fd14e6eae48631fe2b1aa7d150b697acee"
    ),
    "Private/LaunchPlan.ps1": (
        "sha256:5053f0a1c8d3025e3bd092396aa0d290cff8256e288ba99a7fa50bf5fff68fcd"
    ),
    "Support/CodexAppServerBridge.ps1": (
        "sha256:ea9e66e8453a9f0120e9e68a57782a264d7f7e00d3b42004b86f2bca798e45b2"
    ),
}


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
    usage: dict[str, int] = field(default_factory=dict)
    profile_id: str = ""
    model_provider: str = ""
    budget_mode: str = ""
    runtime_identity: dict[str, Any] = field(default_factory=dict)
    aicli_preflight: dict[str, Any] = field(default_factory=dict)


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
    timeout_seconds: int | None,
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


def _safe_aicli_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    usage: dict[str, int] = {}
    for field_name in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "current_context_tokens",
        "context_window_tokens",
    ):
        field_value = value.get(field_name)
        if type(field_value) is int and field_value >= 0:
            usage[field_name] = field_value
    return usage


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


def _is_safe_public_progress_text(value: str) -> bool:
    if any(
        pattern.search(value)
        for pattern in (
            *_PUBLIC_PROGRESS_SECRET_PATTERNS,
            *_PUBLIC_PROGRESS_ALWAYS_UNSAFE_PATH_PATTERNS,
        )
    ):
        return False
    non_url_probe = _PUBLIC_PROGRESS_URL_PATTERN.sub("", value)
    return not any(
        pattern.search(non_url_probe)
        for pattern in _PUBLIC_PROGRESS_NON_URL_PATH_PATTERNS
    )


def _bounded_public_text(value: Any, *, max_chars: int = PUBLIC_PROGRESS_MAX_CHARS) -> str:
    safe_chars: list[str] = []
    for char in str(value or ""):
        if unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            safe_chars.append(" ")
        elif char == "<":
            safe_chars.append("＜")
        elif char == ">":
            safe_chars.append("＞")
        else:
            safe_chars.append(char)
    normalized = " ".join("".join(safe_chars).split())
    if not _is_safe_public_progress_text(normalized):
        return ""
    return normalized[:max_chars].rstrip()


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
                "tools": {
                    "approvalMode": (
                        "yolo"
                        if execution["policy"] in {"workspace-write", "danger-full-access"}
                        else "plan"
                    )
                },
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
        if execution["policy"] in {"workspace-write", "danger-full-access"}:
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
        if not entry_value:
            raise _runner_error("agent_runner_unavailable", "LLM_TOOLKIT_AICLI_ENTRY is not configured.")
        entry = Path(entry_value).expanduser().resolve()
        if not entry.is_file() or entry.suffix.lower() != ".ps1":
            raise _runner_error("agent_runner_unavailable", "aicli entry must be an existing PowerShell script.")
        pwsh = shutil.which("pwsh")
        if not pwsh:
            raise _runner_error("agent_runner_unavailable", "PowerShell 7 is unavailable.")
        return [pwsh, "-NoProfile", "-File", str(entry)]

    @staticmethod
    def _benchmark_source_receipt(entry: Path) -> dict[str, Any]:
        module_root = entry.parent.parent / "src" / "AiCliProfileManager"
        if (
            not module_root.is_dir()
            or module_root.is_symlink()
            or not (module_root / "AiCliProfileManager.psm1").is_file()
        ):
            raise _runner_error(
                "agent_runner_identity_mismatch",
                "The benchmark AICLI module source bundle is unavailable.",
            )
        files = sorted(
            (
                path
                for path in module_root.rglob("*")
                if path.is_file() and path.suffix.lower() in {".ps1", ".psd1", ".psm1"}
            ),
            key=lambda path: path.relative_to(module_root).as_posix(),
        )
        if not files or any(path.is_symlink() for path in files):
            raise _runner_error(
                "agent_runner_identity_mismatch",
                "The benchmark AICLI source bundle is empty or redirected.",
            )
        manifest = bytearray()
        component_sha256: dict[str, str] = {}
        for path in files:
            relative = path.relative_to(module_root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest.extend(f"{relative}\0{digest}\n".encode("utf-8"))
            if relative in _AUDITED_AICLI_NETWORK_COMPONENTS:
                component_sha256[relative] = "sha256:" + digest
        return {
            "entry_sha256": "sha256:" + hashlib.sha256(manifest).hexdigest(),
            "raw_entry_sha256": "sha256:" + hashlib.sha256(entry.read_bytes()).hexdigest(),
            "fingerprint_scope": "module-source-bundle-v1",
            "file_count": len(files),
            "component_sha256": component_sha256,
        }

    def _benchmark_preflight(
        self,
        prefix: list[str],
        workspace: Path,
        execution: dict[str, Any],
    ) -> dict[str, Any]:
        entry = Path(prefix[-1]).resolve()
        try:
            source = self._benchmark_source_receipt(entry)
        except AgentRunnerError:
            raise
        except OSError as exc:
            raise _runner_error(
                "agent_runner_identity_mismatch",
                "The benchmark AICLI source bundle could not be read.",
            ) from exc
        profile_id = str(execution.get("profile") or self.default_profile)
        observations: list[dict[str, Any]] = []
        for arguments in (
            ["version", "--json"],
            ["profile", "show", profile_id, "--json"],
        ):
            try:
                code, stdout, _, _ = _bounded_process(
                    prefix + arguments,
                    cwd=workspace,
                    stdin_text="",
                    timeout_seconds=10,
                    max_output_chars=200_000,
                )
            except AgentRunnerError as exc:
                raise _runner_error(
                    "agent_runner_identity_mismatch",
                    "The benchmark AICLI identity preflight failed closed.",
                    receipt={"aicli_preflight": source},
                ) from exc
            values = _json_values(stdout)
            observations.append(values[-1] if code == 0 and values else {})
        version_record, profile_record = observations
        capabilities = version_record.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        profile = profile_record.get("profile")
        profile = profile if isinstance(profile, dict) else {}
        models = profile.get("models")
        models = models if isinstance(models, dict) else {}
        preflight = {
            "schema": "llm-backend-toolkit.aicli-benchmark-preflight.v1",
            **source,
            "version": str(version_record.get("version") or ""),
            "machine_event_projection": str(
                capabilities.get("machineEventProjection") or ""
            ),
            "profile": {
                "id": str(profile.get("id") or ""),
                "engine": str(profile.get("engine") or ""),
                "provider": str(profile.get("provider") or ""),
                "provider_id": str(profile.get("codexProviderId") or ""),
                "model": str(models.get("primary") or ""),
                "fingerprint": str(profile.get("profileFingerprint") or ""),
            },
        }

        def normalized_digest(value: Any) -> str:
            return str(value or "").lower().removeprefix("sha256:")

        expected_profile = {
            "id": profile_id,
            "engine": "codex",
            "provider": "ollama",
            "provider_id": str(execution.get("provider_id") or ""),
            "model": str(execution.get("model") or ""),
            "fingerprint": str(execution.get("profile_fingerprint") or ""),
        }
        mismatches: list[str] = []
        if normalized_digest(source["entry_sha256"]) != normalized_digest(
            execution.get("aicli_entry_sha256")
        ):
            mismatches.append("aicli_entry_sha256")
        if source["entry_sha256"] != _AUDITED_AICLI_SOURCE_BUNDLE_SHA256:
            mismatches.append("aicli_network_source_bundle")
        if source["raw_entry_sha256"] != _AUDITED_AICLI_ENTRY_SHA256:
            mismatches.append("aicli_raw_entry_sha256")
        if source.get("component_sha256") != _AUDITED_AICLI_NETWORK_COMPONENTS:
            mismatches.append("aicli_network_source_components")
        if preflight["version"] != str(execution.get("aicli_version") or ""):
            mismatches.append("aicli_version")
        if preflight["version"] != _AUDITED_AICLI_VERSION:
            mismatches.append("aicli_network_source_version")
        if preflight["machine_event_projection"] != "aicli.machine-event.v1":
            mismatches.append("machine_event_projection")
        for profile_field, expected in expected_profile.items():
            if not expected or preflight["profile"][profile_field] != expected:
                mismatches.append(f"profile.{profile_field}")
        if (
            self.engine != "codex"
            or execution.get("policy") != "workspace-write"
            or execution.get("network_policy") != "forbidden"
            or execution.get("search_policy") != "disabled"
            or execution.get("require_network_proof") is not True
            or execution.get("cloud") is True
        ):
            mismatches.append("network_request_contract")
        if mismatches:
            raise _runner_error(
                "agent_runner_identity_mismatch",
                "The benchmark AICLI source, version, or profile identity drifted.",
                receipt={
                    "aicli_preflight": preflight,
                    "identity_mismatches": mismatches,
                },
            )
        source_contract = {
            "schema": "llm-backend-toolkit.aicli-network-source-contract.v1",
            "aicli_version": preflight["version"],
            "source_bundle_sha256": source["entry_sha256"],
            "entry_sha256": source["raw_entry_sha256"],
            "component_sha256": dict(source["component_sha256"]),
            "outer_launcher_network_flag": "--sandbox-state-disable-network",
            "outer_launcher_applies_when_policy_is_not": "danger-full-access",
            "runtime_sandbox_boundary": "outer-codex",
            "runtime_sandbox_type": "externalSandbox",
            "turn_network_access": "restricted",
            "terminal_event": "turn.completed",
        }
        request_contract = {
            "engine": "codex",
            "profile": profile_id,
            "profile_fingerprint": str(
                execution.get("profile_fingerprint") or ""
            ),
            "model": str(execution.get("model") or ""),
            "provider_id": str(execution.get("provider_id") or ""),
            "sandbox_policy": "workspace-write",
            "network": "forbidden",
            "search": "disabled",
            "runtime_permission": {
                "approval_policy": "never",
                "requested_policy": "workspace-write",
                "sandbox_boundary": "outer-codex",
                "sandbox_type": "externalSandbox",
            },
        }
        preflight["network_proof"] = {
            "policy": "forbidden",
            "search": "disabled",
            "status": "agent_network_proof_pending_prelaunch",
            "evidence_kind": "aicli-audited-source-request-contract",
            "source_contract": source_contract,
            "source_contract_sha256": _canonical_digest(source_contract),
            "request_contract": request_contract,
            "request_contract_sha256": _canonical_digest(request_contract),
            "source_bundle_sha256": source["entry_sha256"],
            "entry_sha256": source["raw_entry_sha256"],
        }
        self._machine_events_supported = True
        return preflight

    @staticmethod
    def _runtime_network_proof(
        *,
        preflight: dict[str, Any],
        run: dict[str, Any],
        machine_events: list[dict[str, Any]],
        runtime_identity: dict[str, Any],
        identity_mismatches: list[str],
        outer_exit_code: int,
        child_exit_code: int,
    ) -> tuple[dict[str, Any], list[str]]:
        pending = preflight.get("network_proof")
        pending = dict(pending) if isinstance(pending, dict) else {}
        source_contract = pending.get("source_contract")
        source_contract = (
            dict(source_contract) if isinstance(source_contract, dict) else {}
        )
        request_contract = pending.get("request_contract")
        request_contract = (
            dict(request_contract) if isinstance(request_contract, dict) else {}
        )
        permission = runtime_identity.get("permission")
        permission = dict(permission) if isinstance(permission, dict) else {}
        limit_usage = run.get("limitUsage")
        limit_usage = dict(limit_usage) if isinstance(limit_usage, dict) else {}
        limit_enforcement = run.get("limitEnforcement")
        limit_enforcement = (
            dict(limit_enforcement) if isinstance(limit_enforcement, dict) else {}
        )
        machine_count_value = run.get("machineEventCount")
        machine_count = machine_count_value if type(machine_count_value) is int else 0
        terminal_events = [
            event for event in machine_events if event.get("kind") == "turn.completed"
        ]
        terminal_event = terminal_events[-1] if terminal_events else {}
        terminal_sequence_value = terminal_event.get("sequence")
        terminal_sequence = (
            terminal_sequence_value if type(terminal_sequence_value) is int else 0
        )
        runtime_events = [
            event for event in machine_events if event.get("kind") == "runtime.identity"
        ]
        runtime_event = runtime_events[0] if len(runtime_events) == 1 else {}
        expected_runtime_event = {
            "model": runtime_identity.get("model"),
            "provider_id": runtime_identity.get("model_provider"),
            "cli_version": runtime_identity.get("cli_version"),
            "approval_policy": permission.get("approval_policy"),
            "sandbox_policy": permission.get("requested_policy"),
            "sandbox_boundary": permission.get("sandbox_boundary"),
            "sandbox_type": permission.get("sandbox_type"),
        }
        failures: list[str] = []
        if pending.get("status") != "agent_network_proof_pending_prelaunch":
            failures.append("preflight_status")
        if (
            not source_contract
            or pending.get("source_contract_sha256")
            != _canonical_digest(source_contract)
            or source_contract.get("source_bundle_sha256")
            != _AUDITED_AICLI_SOURCE_BUNDLE_SHA256
            or source_contract.get("entry_sha256") != _AUDITED_AICLI_ENTRY_SHA256
            or source_contract.get("component_sha256")
            != _AUDITED_AICLI_NETWORK_COMPONENTS
        ):
            failures.append("source_contract")
        if (
            not request_contract
            or pending.get("request_contract_sha256")
            != _canonical_digest(request_contract)
            or request_contract.get("sandbox_policy") != "workspace-write"
            or request_contract.get("network") != "forbidden"
            or request_contract.get("search") != "disabled"
        ):
            failures.append("request_contract")
        if identity_mismatches:
            failures.append("runtime_identity")
        if (
            run.get("profileId") != request_contract.get("profile")
            or run.get("model") != request_contract.get("model")
            or run.get("modelProvider") != request_contract.get("provider_id")
        ):
            failures.append("process_identity")
        if run.get("sandboxPolicy") != request_contract.get("sandbox_policy"):
            failures.append("sandbox_policy")
        if (
            run.get("machineEventProjection") != "aicli.machine-event.v1"
            or run.get("machineEventStatus") != "ok"
            or machine_count <= 0
            or machine_count != len(machine_events)
            or [event.get("sequence") for event in machine_events]
            != list(range(1, machine_count + 1))
            or any(
                event.get("schema") != "aicli.machine-event.v1"
                for event in machine_events
            )
        ):
            failures.append("machine_event_stream")
        if any(
            runtime_event.get(field) != expected
            for field, expected in expected_runtime_event.items()
        ):
            failures.append("runtime_identity_event")
        if (
            len(terminal_events) != 1
            or terminal_event.get("status") != "completed"
            or terminal_sequence != machine_count
        ):
            failures.append("terminal_event")
        if (
            outer_exit_code != 0
            or child_exit_code != 0
            or run.get("timedOut") is True
            or bool(run.get("limitHit"))
        ):
            failures.append("process_terminal")
        if (
            limit_usage.get("cleanupConfirmed") is not True
            or not str(limit_usage.get("cleanupMethod") or "")
        ):
            failures.append("cleanup")

        process_receipt = {
            "profile_id": str(run.get("profileId") or ""),
            "model": str(run.get("model") or ""),
            "model_provider": str(run.get("modelProvider") or ""),
            "sandbox_policy": str(run.get("sandboxPolicy") or ""),
            "runtime_identity": runtime_identity,
            "outer_exit_code": outer_exit_code,
            "child_exit_code": child_exit_code,
            "timed_out": run.get("timedOut") is True,
            "budget_mode": str(run.get("budgetMode") or ""),
            "limit_enforcement": limit_enforcement,
            "limit_usage": limit_usage,
            "limit_hit": run.get("limitHit"),
            "event_projection": str(run.get("eventProjection") or ""),
            "machine_event_projection": str(
                run.get("machineEventProjection") or ""
            ),
            "machine_event_status": str(run.get("machineEventStatus") or ""),
            "machine_event_count": machine_count,
        }
        machine_event_receipt = {
            "schema": "llm-backend-toolkit.aicli-machine-event-receipt.v1",
            "projection": str(run.get("machineEventProjection") or ""),
            "status": str(run.get("machineEventStatus") or ""),
            "count": machine_count,
            "sequences": [event.get("sequence") for event in machine_events],
            "kinds": [str(event.get("kind") or "") for event in machine_events],
            "runtime_identity": {
                field: runtime_event.get(field)
                for field in expected_runtime_event
            },
            "terminal": {
                "kind": str(terminal_event.get("kind") or ""),
                "status": str(terminal_event.get("status") or ""),
                "sequence": terminal_sequence,
            },
        }
        proof = {
            **pending,
            "status": "incomplete_postrun" if failures else "enforced",
            "evidence_kind": "aicli-runtime-bound-source-contract",
            "enforcement": "unproven" if failures else "network-denied",
            "sandbox_policy": str(run.get("sandboxPolicy") or ""),
            "sandbox_boundary": str(permission.get("sandbox_boundary") or ""),
            "sandbox_type": str(permission.get("sandbox_type") or ""),
            "runtime_identity_sha256": _canonical_digest(runtime_identity),
            "process_receipt": process_receipt,
            "process_receipt_sha256": _canonical_digest(process_receipt),
            "machine_event_receipt": machine_event_receipt,
            "machine_event_stream_sha256": _canonical_digest(
                machine_event_receipt
            ),
            "terminal_event": str(terminal_event.get("kind") or ""),
            "terminal_sequence": terminal_sequence,
            "machine_event_count": machine_count,
            "cleanup_confirmed": limit_usage.get("cleanupConfirmed") is True,
            "cleanup_method": str(limit_usage.get("cleanupMethod") or ""),
        }
        if failures:
            proof["failure_codes"] = sorted(set(failures))
        return proof, failures

    def _require_machine_events(self, prefix: list[str], workspace: Path) -> None:
        if self._machine_events_supported is True:
            return
        if self._machine_events_supported is False:
            raise _runner_error(
                "agent_runner_incompatible",
                "Codex requires aicli machineEventProjection aicli.machine-event.v1.",
            )
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
            supported = bool(
                code == 0
                and isinstance(capabilities, dict)
                and capabilities.get("machineEventProjection")
                == "aicli.machine-event.v1"
            )
        except AgentRunnerError as exc:
            self._machine_events_supported = False
            raise _runner_error(
                "agent_runner_incompatible",
                "Codex aicli capability probing failed closed.",
            ) from exc
        self._machine_events_supported = supported
        if not supported:
            raise _runner_error(
                "agent_runner_incompatible",
                "Codex requires aicli machineEventProjection aicli.machine-event.v1.",
            )

    @staticmethod
    def _emit_machine_event(callback: Any, event: dict[str, Any]) -> None:
        if not callable(callback):
            return
        kind = str(event.get("kind") or "")
        status = str(event.get("status") or "")
        item_type = str(event.get("item_type") or "")
        command_status = None
        safe_exit_code = None
        safe_duration_ms = None
        if kind == "tool.activity" and item_type == "command_execution":
            raw_command_status = event.get("command_status")
            if raw_command_status is not None:
                if (
                    type(raw_command_status) is not str
                    or raw_command_status
                    not in {"in_progress", "succeeded", "failed", "declined"}
                ):
                    return
                command_status = raw_command_status
            raw_exit_code = event.get("exit_code")
            if (
                type(raw_exit_code) is int
                and -(2**31) <= raw_exit_code <= (2**31) - 1
            ):
                safe_exit_code = raw_exit_code
            raw_duration_ms = event.get("duration_ms")
            if (
                type(raw_duration_ms) is int
                and 0 <= raw_duration_ms <= (2**63) - 1
            ):
                safe_duration_ms = raw_duration_ms
        public_text = ""
        if kind in {"output.delta", "output.completed"}:
            public_text = _bounded_public_text(event.get("public_text"))
            if not public_text:
                return
        phase = "waiting"
        if kind == "reasoning.activity":
            phase = "thinking"
            summary = (
                "智能体正在进行内部分析；只展示活动状态，不展示隐藏思维正文。"
            )
        elif kind == "tool.activity":
            if item_type == "command_execution" and command_status:
                summary = {
                    "in_progress": "智能体正在执行命令。",
                    "succeeded": "智能体执行命令成功。",
                    "failed": "智能体执行命令失败。",
                    "declined": "智能体的命令执行已被拒绝。",
                }[command_status]
                details = []
                if command_status != "in_progress":
                    if safe_exit_code is not None:
                        details.append(f"退出码 {safe_exit_code}")
                    if safe_duration_ms is not None:
                        details.append(f"耗时 {safe_duration_ms} 毫秒")
                if details:
                    summary = f"{summary[:-1]}（{'，'.join(details)}）。"
            else:
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
                summary = (
                    f"智能体正在{activity}。"
                    if status != "completed"
                    else f"智能体已完成{activity}。"
                )
        elif kind == "planning.activity":
            summary = "智能体正在更新公开工作计划。"
        elif kind in {"output.delta", "output.completed"}:
            phase = "generating"
            summary = public_text
        elif kind == "turn.completed":
            phase = "validating"
            summary = "智能体本轮工作完成，正在整理结果与回执。"
        elif kind == "context.usage.updated":
            summary = "Codex 已上报实时上下文占用。"
        elif kind == "context.compaction.completed":
            phase = "thinking"
            summary = "Codex 已自动压缩上下文，继续执行当前任务。"
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
                "error_code",
                "limit",
                "current_tokens",
                "context_window_tokens",
                "compaction_count",
            )
            if event.get(key) is not None
        }
        if command_status is not None:
            allowed_payload["command_status"] = command_status
        if safe_exit_code is not None:
            allowed_payload["exit_code"] = safe_exit_code
        if safe_duration_ms is not None:
            allowed_payload["duration_ms"] = safe_duration_ms
        progress: dict[str, Any] = {
            "phase": phase,
            "public_event": {
                "kind": f"agent.{kind}",
                "summary_zh": summary,
                "payload": allowed_payload,
            },
        }
        if kind == "output.delta":
            progress["content_delta"] = public_text
        elif kind == "output.completed":
            progress["content_replace"] = public_text
        if kind == "context.usage.updated":
            progress["current_context_tokens"] = event.get("current_tokens")
            progress["context_window_tokens"] = event.get(
                "context_window_tokens"
            )
        try:
            callback(progress)
        except Exception:
            return

    def invoke(self, prompt: str, execution: dict[str, Any]) -> AgentResponse:
        workspace = Path(execution["workspace"])
        budget = execution["budget"]
        if not isinstance(budget, dict):
            raise _runner_error("invalid_request", "Agent execution budget must be an object.")
        requested_limit_mode = budget.get("limit_mode")
        if requested_limit_mode is None:
            has_legacy_cutoff = any(
                budget.get(name) is not None
                for name in ("timeout_seconds", "max_steps", "max_tool_calls")
            )
            limit_mode = "bounded" if has_legacy_cutoff else "completion_driven"
        else:
            limit_mode = str(requested_limit_mode)
        if limit_mode not in {"completion_driven", "bounded", "watchdog_only"}:
            raise _runner_error(
                "invalid_request",
                "Agent budget limit_mode is unsupported.",
            )
        try:
            idle_value = budget.get("idle_timeout_seconds")
            idle_timeout_seconds = int(3600 if idle_value is None else idle_value)
        except (TypeError, ValueError) as exc:
            raise _runner_error(
                "invalid_request",
                "Agent idle_timeout_seconds must be an integer.",
            ) from exc
        if idle_timeout_seconds != 0 and not 60 <= idle_timeout_seconds <= 604_800:
            raise _runner_error(
                "invalid_request",
                "Agent idle_timeout_seconds must be 0 or between 60 and 604800.",
            )
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
                "--dangerously-skip-permissions",
            ]
            if limit_mode == "bounded":
                native.extend(["--max-turns", str(budget["max_steps"])])
        elif self.engine == "opencode":
            native = []
            for path in native_images:
                native.extend(["--file", path])
        elif self.engine == "qwen-code":
            native = []
        else:
            raise _runner_error("agent_runner_unavailable", f"Unsupported aicli agent engine: {self.engine}")
        prefix = self._prefix()
        aicli_preflight: dict[str, Any] = {}
        if execution.get("require_benchmark_preflight") is True:
            aicli_preflight = self._benchmark_preflight(
                prefix,
                workspace,
                execution,
            )
        progress_callback = execution.get("_progress_callback")
        captured_machine_events: list[dict[str, Any]] = []

        def capture_machine_event(event: dict[str, Any]) -> None:
            captured_machine_events.append(
                json.loads(json.dumps(event, ensure_ascii=False, sort_keys=True))
            )
            if callable(progress_callback):
                self._emit_machine_event(progress_callback, event)

        machine_events_supported = False
        if self.engine == "codex":
            self._require_machine_events(prefix, workspace)
            machine_events_supported = True
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
            "--max-output-chars", "1000000",
        ]
        watchdog_only = limit_mode == "watchdog_only"
        completion_driven = limit_mode == "completion_driven"
        if completion_driven:
            command.extend(["--idle-timeout-seconds", str(idle_timeout_seconds)])
        elif watchdog_only:
            command.extend(
                ["--timeout-seconds", str(budget["timeout_seconds"]), "--watchdog-only"]
            )
        else:
            command.extend(
                [
                    "--timeout-seconds",
                    str(budget["timeout_seconds"]),
                    "--max-steps",
                    str(budget["max_steps"]),
                    "--max-tool-calls",
                    str(budget["max_tool_calls"]),
                ]
            )
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
                timeout_seconds=(
                    None
                    if completion_driven
                    else int(budget["timeout_seconds"]) + 15
                ),
                event_file=event_path,
                on_event=(
                    capture_machine_event
                    if machine_events_supported
                    and (
                        execution.get("require_network_proof") is True
                        or callable(progress_callback)
                    )
                    else None
                ),
            )
        finally:
            if event_temp is not None:
                event_temp.cleanup()
        envelopes = _json_values(stdout)
        if not envelopes:
            raise _runner_error("agent_failed", f"aicli returned no JSON envelope: {stderr.strip()[:500]}", retryable=True)
        envelope = envelopes[-1]
        run = envelope.get("run") or {}
        effective_model = str(run.get("model") or "")
        if execution.get("cloud") and not effective_model:
            raise _runner_error(
                "agent_model_unverified",
                "aicli did not report the effective model for this cloud agent run.",
                receipt={"requested_model": model},
            )
        if effective_model and effective_model != model:
            raise _runner_error(
                "agent_model_mismatch",
                "aicli reported a different effective model than the requested agent model.",
                receipt={
                    "requested_model": model,
                    "effective_model": effective_model,
                },
            )
        routed_model = effective_model or model
        profile_id = str(run.get("profileId") or "")
        model_provider = str(run.get("modelProvider") or "")
        runtime_identity_value = run.get("runtimeIdentity")
        runtime_identity = (
            dict(runtime_identity_value)
            if isinstance(runtime_identity_value, dict)
            else {}
        )
        identity_mismatches: list[str] = []
        if bool(execution.get("require_runtime_identity")):
            expected_provider_id = str(execution.get("provider_id") or "")
            expected_cli_version = str(execution.get("codex_cli_version") or "")
            permission = runtime_identity.get("permission") or {}
            if not isinstance(permission, dict):
                permission = {}
            if profile_id != profile:
                identity_mismatches.append("profile_id")
            if not expected_provider_id or model_provider != expected_provider_id:
                identity_mismatches.append("model_provider")
            if runtime_identity.get("model") != routed_model:
                identity_mismatches.append("runtime_identity.model")
            if runtime_identity.get("model_provider") != expected_provider_id:
                identity_mismatches.append("runtime_identity.model_provider")
            if (
                not expected_cli_version
                or runtime_identity.get("cli_version") != expected_cli_version
            ):
                identity_mismatches.append("runtime_identity.cli_version")
            if permission.get("approval_policy") != "never":
                identity_mismatches.append(
                    "runtime_identity.permission.approval_policy"
                )
            if permission.get("requested_policy") != execution.get("policy"):
                identity_mismatches.append(
                    "runtime_identity.permission.requested_policy"
                )
            if permission.get("sandbox_boundary") != "outer-codex":
                identity_mismatches.append(
                    "runtime_identity.permission.sandbox_boundary"
                )
            if permission.get("sandbox_type") != "externalSandbox":
                identity_mismatches.append(
                    "runtime_identity.permission.sandbox_type"
                )
        child_stdout = str(run.get("stdout") or "")
        child_values = _json_values(child_stdout)
        usage = _safe_aicli_usage(run.get("usage"))
        limit_enforcement = dict(run.get("limitEnforcement") or {})
        budget_mode = str(run.get("budgetMode") or "")
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
            "model": routed_model,
            "exit_code": child_code,
            "duration_ms": int(run.get("durationMs") or duration_ms),
            "steps": steps,
            "tool_calls": tool_calls,
            "session_id": session_id,
            "stop_reason": limit_hit or ("failed" if code != 0 or child_code != 0 else "completed"),
            "limit_hit": limit_hit or None,
            "limit_enforcement": limit_enforcement,
            "budget_mode": budget_mode,
            "profile_id": profile_id,
            "model_provider": model_provider,
            "sandbox_policy": str(run.get("sandboxPolicy") or ""),
            "runtime_identity": runtime_identity,
            "aicli_preflight": aicli_preflight,
            "limit_usage": {
                "steps": steps,
                "tool_calls": tool_calls,
                "events_seen": int(limit_usage.get("eventsSeen") or 0),
                "protocol": str(limit_usage.get("protocol") or ""),
                "step_definition": str(limit_usage.get("stepDefinition") or ""),
                "cleanup_confirmed": bool(limit_usage.get("cleanupConfirmed", False)),
                "cleanup_method": str(limit_usage.get("cleanupMethod") or ""),
            },
            "event_projection": str(run.get("eventProjection") or ""),
            "machine_event_projection": str(
                run.get("machineEventProjection") or ""
            ),
            "machine_event_status": str(run.get("machineEventStatus") or ""),
            "machine_event_count": int(run.get("machineEventCount") or 0),
            "error_code": str(run.get("errorCode") or ""),
            "observability_level": (
                "live_safe_events"
                if machine_events_supported
                and str(run.get("machineEventStatus") or "") == "ok"
                else "lifecycle"
            ),
        }
        if execution.get("require_network_proof") is True:
            network_proof, network_failures = self._runtime_network_proof(
                preflight=aicli_preflight,
                run=run,
                machine_events=captured_machine_events,
                runtime_identity=runtime_identity,
                identity_mismatches=identity_mismatches,
                outer_exit_code=code,
                child_exit_code=child_code,
            )
            aicli_preflight["network_proof"] = network_proof
            receipt["aicli_preflight"] = aicli_preflight
            if network_failures:
                receipt["stop_reason"] = "network_proof_incomplete"
                receipt["identity_mismatches"] = identity_mismatches
                receipt["benchmark_qualification"] = {
                    "state": "infra-invalid",
                    "scored": False,
                    "ranking_eligible": False,
                }
                raise _runner_error(
                    "agent_network_proof_incomplete",
                    "The completed AICLI process did not provide a bound network-denied runtime receipt.",
                    receipt=receipt,
                )
        elif identity_mismatches:
            receipt["identity_mismatches"] = identity_mismatches
            raise _runner_error(
                "agent_runtime_identity_mismatch",
                "aicli did not prove the exact local Codex runtime identity.",
                receipt=receipt,
            )
        if any(
            limit_enforcement.get(name) == "failed-closed"
            for name in ("timeout", "idleTimeout", "maxSteps", "maxToolCalls")
        ):
            receipt["stop_reason"] = "budget_unenforced"
            raise _runner_error(
                "agent_budget_unenforced",
                "The agent boundary failed closed before proving every declared hard limit.",
                receipt=receipt,
            )
        if bool(run.get("timedOut")) or limit_hit in {
            "timeout",
            "idleTimeout",
            "idle_timeout",
        }:
            normalized_limit_hit = (
                "idle_timeout"
                if completion_driven and limit_hit in {"", "timeout", "idleTimeout", "idle_timeout"}
                else (limit_hit or "timeout")
            )
            receipt["stop_reason"] = normalized_limit_hit
            receipt["limit_hit"] = normalized_limit_hit
            raise _runner_error(
                "agent_timeout",
                (
                    "Agent idle activity lease expired."
                    if completion_driven
                    else "Agent exceeded its hard wall-clock budget."
                ),
                retryable=True,
                receipt=receipt,
            )
        if limit_hit:
            if completion_driven and limit_hit in {"maxSteps", "maxToolCalls"}:
                receipt["stop_reason"] = "budget_unenforced"
                raise _runner_error(
                    "agent_budget_unenforced",
                    "A completion-driven run reported an unexpected hard step/tool limit.",
                    receipt=receipt,
                )
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
        if completion_driven:
            expected_enforcement = {
                "timeout": "not-configured",
                "idleTimeout": (
                    "disabled" if idle_timeout_seconds == 0 else "renewable"
                ),
                "maxSteps": "not-configured",
                "maxToolCalls": "not-configured",
            }
        elif watchdog_only:
            expected_enforcement = {
                "timeout": "hard",
                "maxSteps": "not-configured",
                "maxToolCalls": "not-configured",
            }
        else:
            expected_enforcement = {
                "timeout": "hard",
                "maxSteps": "hard",
                "maxToolCalls": "hard",
            }
        expected_budget_mode = {
            "completion_driven": "completion-driven",
            "watchdog_only": "watchdog-only",
        }.get(limit_mode)
        if expected_budget_mode is not None and budget_mode != expected_budget_mode:
            receipt["stop_reason"] = "budget_unenforced"
            raise _runner_error(
                "agent_budget_unenforced",
                "The agent boundary did not confirm the selected budget semantics.",
                receipt=receipt,
            )
        if any(
            limit_enforcement.get(name) != expected
            for name, expected in expected_enforcement.items()
        ):
            receipt["stop_reason"] = "budget_unenforced"
            raise _runner_error(
                "agent_budget_unenforced",
                "The selected agent runner did not prove the declared budget semantics.",
                receipt=receipt,
            )
        if not final:
            raise _runner_error("agent_failed", f"{self.name} returned no final answer.", retryable=True)
        return AgentResponse(
            content=final,
            runner=self.name,
            model=routed_model,
            exit_code=child_code,
            duration_ms=int(run.get("durationMs") or duration_ms),
            usage=usage,
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
            profile_id=profile_id,
            model_provider=model_provider,
            budget_mode=budget_mode,
            runtime_identity=runtime_identity,
            aicli_preflight=aicli_preflight,
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
