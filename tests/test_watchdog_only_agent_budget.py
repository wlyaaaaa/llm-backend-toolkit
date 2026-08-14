import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_backend_toolkit.agent_runners import (
    AgentResponse,
    AgentRunnerError,
    AiCliProfileRunner,
)
from llm_backend_toolkit.providers import ProviderResponse
from llm_backend_toolkit.toolkit import Toolkit


class _Provider:
    cloud = False
    supports_vision = True

    def invoke(self, prompt, native_images, reasoning_mode):
        return ProviderResponse(content='{"answer": 56}', model="qwen-main-v1")


class _Runner:
    def __init__(self):
        self.calls = []

    def invoke(self, prompt, execution):
        self.calls.append(execution)
        mode = execution["budget"]["limit_mode"]
        limit_enforcement = (
            {
                "timeout": "not-configured",
                "idleTimeout": "renewable",
                "maxSteps": "not-configured",
                "maxToolCalls": "not-configured",
            }
            if mode == "completion_driven"
            else {
                "timeout": "hard",
                "idleTimeout": "not-configured",
                "maxSteps": "not-configured",
                "maxToolCalls": "not-configured",
            }
        )
        return AgentResponse(
            content='{"answer": 56}',
            runner="codex-cli",
            model="qwen-main-v1",
            exit_code=0,
            duration_ms=100,
            tool_calls=3,
            session_id="session-1",
            stop_reason="completed",
            limit_enforcement=limit_enforcement,
        )


def _request(workspace: Path) -> dict:
    return {
        "provider": "qwen-main-v1",
        "task": {
            "goal": "Complete the supplied benchmark workspace.",
            "expected_output": {"format": "json", "required_keys": ["answer"]},
        },
        "context": {"mode": "compact", "target_tokens": 1024},
        "execution": {
            "mode": "agent",
            "runner": "codex-cli",
            "workspace": str(workspace),
            "policy": "workspace-write",
            "budget": {
                "idle_timeout_seconds": 3600,
                "limit_mode": "completion_driven",
            },
        },
    }


class CompletionDrivenBudgetTests(unittest.TestCase):
    def test_explicit_completion_driven_keeps_legacy_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = _Runner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": _Provider()},
                runners={"codex-cli": runner},
            )

            result = toolkit.invoke(_request(Path(temp)))

        self.assertEqual("ok", result["status"])
        self.assertEqual(
            {
                "timeout_seconds": None,
                "idle_timeout_seconds": 3600,
                "limit_mode": "completion_driven",
                "max_steps": None,
                "max_tool_calls": None,
            },
            runner.calls[0]["budget"],
        )
        self.assertEqual(
            "completion_driven",
            result["execution_receipt"]["budget"]["limit_mode"],
        )

    def test_omitted_budget_uses_aicli_compatible_watchdog_default(self):
        with tempfile.TemporaryDirectory() as temp:
            request = _request(Path(temp))
            del request["execution"]["budget"]
            runner = _Runner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": _Provider()},
                runners={"codex-cli": runner},
            )

            result = toolkit.invoke(request)

        self.assertEqual("ok", result["status"])
        self.assertEqual("watchdog_only", runner.calls[0]["budget"]["limit_mode"])
        self.assertEqual(900, runner.calls[0]["budget"]["timeout_seconds"])
        self.assertIsNone(runner.calls[0]["budget"]["max_steps"])
        self.assertIsNone(runner.calls[0]["budget"]["max_tool_calls"])

    def test_completion_driven_idle_zero_explicitly_disables_the_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            request = _request(Path(temp))
            request["execution"]["budget"]["idle_timeout_seconds"] = 0
            runner = _Runner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": _Provider()},
                runners={"codex-cli": runner},
            )

            result = toolkit.invoke(request)

        self.assertEqual("ok", result["status"])
        self.assertEqual(0, runner.calls[0]["budget"]["idle_timeout_seconds"])
        self.assertIsNone(runner.calls[0]["budget"]["timeout_seconds"])
        self.assertIsNone(runner.calls[0]["budget"]["max_steps"])
        self.assertIsNone(runner.calls[0]["budget"]["max_tool_calls"])

    def test_unknown_limit_mode_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            request = _request(Path(temp))
            request["execution"]["budget"]["limit_mode"] = "unlimited_magic"
            toolkit = Toolkit(
                providers={"qwen-main-v1": _Provider()},
                runners={"codex-cli": _Runner()},
            )

            result = toolkit.invoke(request)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("invalid_request", result["error"]["category"])

    def test_aicli_runner_rejects_legacy_completion_driven_before_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# test stub\n", encoding="utf-8")
            child_events = "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": '{"answer": 56}'},
                        }
                    ),
                ]
            )
            run_envelope = {
                "run": {
                    "engine": "codex",
                    "model": "qwen-main-v1",
                    "exitCode": 0,
                    "stdout": child_events,
                    "stderr": "",
                    "durationMs": 120,
                    "timedOut": False,
                    "budgetMode": "completion-driven",
                    "limitEnforcement": {
                        "timeout": "not-configured",
                        "idleTimeout": "renewable",
                        "maxSteps": "not-configured",
                        "maxToolCalls": "not-configured",
                    },
                    "limitUsage": {
                        "steps": 7,
                        "toolCalls": 3,
                        "eventsSeen": 12,
                        "protocol": "codex-app-server",
                        "cleanupConfirmed": True,
                    },
                    "machineEventProjection": "aicli.machine-event.v1",
                    "machineEventStatus": "ok",
                }
            }
            calls = []
            process_calls = []

            def bounded(command, **kwargs):
                calls.append(command)
                process_calls.append(kwargs)
                if command[-2:] == ["version", "--json"]:
                    return (
                        0,
                        json.dumps(
                            {
                                "capabilities": {
                                    "machineEventProjection": "aicli.machine-event.v1"
                                }
                            }
                        ),
                        "",
                        1,
                    )
                return 0, json.dumps(run_envelope), "", 120

            execution = {
                "workspace": str(root),
                "model": "qwen-main-v1",
                "profile": "codex-ollama-main",
                "policy": "workspace-write",
                "native_images": [],
                "budget": {
                    "timeout_seconds": None,
                    "idle_timeout_seconds": 3600,
                    "limit_mode": "completion_driven",
                    "max_steps": None,
                    "max_tool_calls": None,
                },
            }
            with patch(
                "llm_backend_toolkit.agent_runners.shutil.which",
                return_value="pwsh",
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=bounded,
            ):
                with self.assertRaises(AgentRunnerError) as caught:
                    AiCliProfileRunner(
                        name="codex-cli",
                        engine="codex",
                        default_profile="codex-ollama-main",
                        entry=str(entry),
                    ).invoke("task", execution)

        self.assertEqual("agent_runner_incompatible", caught.exception.error.category)
        self.assertEqual([], calls)
        self.assertEqual([], process_calls)

    def test_aicli_runner_materializes_default_watchdog_wire_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# test stub\n", encoding="utf-8")
            child_events = "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": '{"answer": 56}'},
                        }
                    ),
                ]
            )
            run_envelope = {
                "run": {
                    "engine": "codex",
                    "profileId": "codex-ollama-main",
                    "model": "qwen-main-v1",
                    "exitCode": 0,
                    "stdout": child_events,
                    "stderr": "",
                    "durationMs": 120,
                    "timedOut": False,
                    "budgetMode": "watchdog-only",
                    "limitEnforcement": {
                        "timeout": "hard",
                        "idleTimeout": "not-configured",
                        "maxSteps": "not-configured",
                        "maxToolCalls": "not-configured",
                    },
                    "limitUsage": {
                        "steps": 4,
                        "toolCalls": 0,
                        "eventsSeen": 5,
                        "protocol": "codex-app-server",
                        "cleanupConfirmed": True,
                    },
                    "machineEventProjection": "aicli.machine-event.v1",
                    "machineEventStatus": "ok",
                    "machineEventCount": 0,
                }
            }
            calls = []
            process_calls = []

            def bounded(command, **kwargs):
                calls.append(command)
                process_calls.append(kwargs)
                if command[-2:] == ["version", "--json"]:
                    return (
                        0,
                        json.dumps(
                            {
                                "capabilities": {
                                    "machineEventProjection": "aicli.machine-event.v1"
                                }
                            }
                        ),
                        "",
                        1,
                    )
                return 0, json.dumps(run_envelope), "", 120

            execution = {
                "workspace": str(root),
                "model": "qwen-main-v1",
                "profile": "codex-ollama-main",
                "policy": "workspace-write",
                "native_images": [],
                "budget": {
                    "timeout_seconds": 900,
                    "idle_timeout_seconds": None,
                    "limit_mode": "watchdog_only",
                    "max_steps": None,
                    "max_tool_calls": None,
                },
            }
            with patch(
                "llm_backend_toolkit.agent_runners.shutil.which",
                return_value="pwsh",
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=bounded,
            ):
                response = AiCliProfileRunner(
                    name="codex-cli",
                    engine="codex",
                    default_profile="codex-ollama-main",
                    entry=str(entry),
                ).invoke("task", execution)

        self.assertEqual(2, len(calls))
        run_command = calls[1]
        self.assertIn("--watchdog-only", run_command)
        timeout_index = run_command.index("--timeout-seconds")
        self.assertEqual("900", run_command[timeout_index + 1])
        self.assertNotIn("--idle-timeout-seconds", run_command)
        self.assertNotIn("--max-steps", run_command)
        self.assertNotIn("--max-tool-calls", run_command)
        self.assertEqual(915, process_calls[1]["timeout_seconds"])
        self.assertEqual("watchdog-only", response.budget_mode)
        self.assertEqual("hard", response.limit_enforcement["timeout"])
        self.assertEqual(
            "not-configured", response.limit_enforcement["maxSteps"]
        )
        self.assertEqual(
            "not-configured", response.limit_enforcement["maxToolCalls"]
        )

    def test_aicli_runner_rejects_legacy_completion_driven_idle_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# test stub\n", encoding="utf-8")
            child_events = json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": '{"answer": 56}'},
                }
            )
            run_envelope = {
                "run": {
                    "engine": "codex",
                    "model": "qwen-main-v1",
                    "exitCode": 0,
                    "stdout": child_events,
                    "stderr": "",
                    "durationMs": 120,
                    "timedOut": False,
                    "budgetMode": "completion-driven",
                    "limitEnforcement": {
                        "timeout": "not-configured",
                        "idleTimeout": "disabled",
                        "maxSteps": "not-configured",
                        "maxToolCalls": "not-configured",
                    },
                    "limitUsage": {"steps": 0, "toolCalls": 0},
                    "machineEventProjection": "aicli.machine-event.v1",
                    "machineEventStatus": "ok",
                }
            }
            calls = []

            def bounded(command, **kwargs):
                calls.append((command, kwargs))
                if command[-2:] == ["version", "--json"]:
                    return (
                        0,
                        json.dumps(
                            {"capabilities": {"machineEventProjection": "aicli.machine-event.v1"}}
                        ),
                        "",
                        1,
                    )
                return 0, json.dumps(run_envelope), "", 120

            execution = {
                "workspace": str(root),
                "model": "qwen-main-v1",
                "profile": "codex-ollama-main",
                "policy": "workspace-write",
                "native_images": [],
                "budget": {
                    "timeout_seconds": None,
                    "idle_timeout_seconds": 0,
                    "limit_mode": "completion_driven",
                    "max_steps": None,
                    "max_tool_calls": None,
                },
            }
            with patch(
                "llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process", side_effect=bounded
            ):
                with self.assertRaises(AgentRunnerError) as caught:
                    AiCliProfileRunner(
                        name="codex-cli",
                        engine="codex",
                        default_profile="codex-ollama-main",
                        entry=str(entry),
                    ).invoke("task", execution)

        self.assertEqual("agent_runner_incompatible", caught.exception.error.category)
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
