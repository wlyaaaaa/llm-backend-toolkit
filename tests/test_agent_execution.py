import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from llm_backend_toolkit.agent_runners import (
    AgentResponse,
    AgentRunnerError,
    AiCliProfileRunner,
    OpenCodeRunner,
    QwenCodeRunner,
    _bounded_process,
    _json_values,
    default_runners,
)
from llm_backend_toolkit.providers import ProviderResponse, Qwen37PlusProvider
from llm_backend_toolkit.toolkit import Toolkit
from llm_backend_toolkit.errors import ToolError


class FakeProvider:
    cloud = False
    supports_vision = True

    def __init__(self):
        self.calls = []

    def invoke(self, prompt, native_images, reasoning_mode):
        self.calls.append(prompt)
        return ProviderResponse(content='{"wrong": true}', model='direct')


class FakeCloudProvider(FakeProvider):
    cloud = True


class FakeRunner:
    def __init__(self, response=None):
        self.response = response or AgentResponse(
            content='{"answer": 56}',
            runner='qwen-code',
            model='qwen-main-v1',
            exit_code=0,
            duration_ms=321,
            tool_calls=4,
            session_id='session-1',
            stop_reason='completed',
        )
        self.calls = []

    def invoke(self, prompt, execution):
        self.calls.append({"prompt": prompt, "execution": execution})
        return self.response


class FailingRunner:
    def invoke(self, prompt, execution):
        raise AgentRunnerError(
            ToolError(category="agent_failed", summary="failed", retryable=True, options=("handle-in-codex",)),
            {"runner": "claude-code", "exit_code": 1, "duration_ms": 101700, "tool_calls": 7},
        )


def agent_request(workspace):
    return {
        "provider": "qwen-main-v1",
        "task": {
            "goal": "Clean the supplied records",
            "instructions": ["Return strict JSON"],
            "inputs": ["record-a", "record-a"],
            "expected_output": {"format": "json", "required_keys": ["answer"]},
        },
        "context": {"mode": "compact", "target_tokens": 1024},
        "execution": {
            "mode": "agent",
            "runner": "data_factory",
            "workspace": str(workspace),
            "policy": "workspace-write",
            "budget": {"timeout_seconds": 600, "max_steps": 20, "max_tool_calls": 80},
        },
    }


class AgentExecutionTests(unittest.TestCase):
    def test_qwen_json_array_is_parsed_without_returning_event_trace(self):
        values = _json_values('[{"type":"tool","content":"hidden"},{"result":"FINAL_ONLY","stats":{"tools":{"totalCalls":3}}}]')
        self.assertEqual(2, len(values))
        self.assertEqual("FINAL_ONLY", values[-1]["result"])

    def test_all_default_agents_use_the_sandboxed_aicli_machine_boundary(self):
        runners = default_runners()
        for name in ("qwen-code", "opencode", "codex-cli", "claude-code"):
            self.assertIsInstance(runners[name], AiCliProfileRunner)
        self.assertIs(runners["data_factory"], runners["codex-cli"])

    def test_cloud_status_exposes_the_unverified_codex_default_without_a_live_call(self):
        toolkit = Toolkit(
            providers={"qwen3.7-plus": Qwen37PlusProvider(api_key="configured-for-status")},
            runners={},
        )

        result = toolkit.status("qwen3.7-plus")

        self.assertEqual("ok", result["status"])
        status = result["provider_status"]
        self.assertFalse(status["live_call_performed"])
        route = status["agent_default"]
        self.assertEqual("data_factory", route["runner_alias"])
        self.assertEqual("codex-cli", route["runner"])
        self.assertEqual("codex-qwen-paygo", route["profile"])
        self.assertEqual("qwen3.7-plus", route["model"])
        self.assertFalse(route["live_verified"])

    def test_legacy_direct_agent_runners_are_fail_closed(self):
        for runner in (QwenCodeRunner(executable="qwen"), OpenCodeRunner(executable="opencode")):
            with self.assertRaises(AgentRunnerError) as raised:
                runner.invoke("task", {"workspace": ".", "budget": {}})
            self.assertEqual("unsafe_direct_runner_disabled", raised.exception.error.category)

    def test_codex_agent_attaches_approved_native_images(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            image = root / "input.png"
            image.write_bytes(b"png")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-ollama-main", entry=str(entry)
            )
            execution = {
                "workspace": str(root),
                "model": "qwen-main-v1",
                "policy": "workspace-write",
                "native_images": [str(image)],
                "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 4},
            }
            envelope = {
                "run": {
                    "exitCode": 0,
                    "durationMs": 12,
                    "stdout": "\n".join(
                        [
                            '{"type":"thread.started","thread_id":"thread-1"}',
                            '{"type":"turn.started","turn_id":"turn-1"}',
                            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
                        ]
                    ),
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "hard",
                        "maxToolCalls": "hard",
                    },
                    "limitUsage": {
                        "steps": 1,
                        "toolCalls": 0,
                        "eventsSeen": 3,
                        "protocol": "codex-jsonl",
                        "stepDefinition": "distinct-thread-item-v1",
                        "cleanupConfirmed": True,
                        "cleanupMethod": "none",
                    },
                    "eventProjection": "codex-public-v1",
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                return_value=(0, __import__("json").dumps(envelope), "", 12),
            ) as bounded:
                response = runner.invoke("task", execution)

            command = bounded.call_args.args[0]
            self.assertEqual("done", response.content)
            self.assertIn("--image", command)
            self.assertIn(str(image), command)
            self.assertIn("--model", command)
            self.assertEqual("qwen-main-v1", command[command.index("--model") + 1])
            self.assertEqual("hard", response.limit_enforcement["timeout"])
            self.assertEqual("hard", response.limit_enforcement["maxSteps"])
            self.assertEqual("hard", response.limit_enforcement["maxToolCalls"])
            self.assertEqual(1, response.steps)
            self.assertEqual(0, response.tool_calls)
            self.assertEqual("codex-jsonl", response.limit_usage["protocol"])
            self.assertEqual("distinct-thread-item-v1", response.limit_usage["step_definition"])
            self.assertTrue(response.limit_usage["cleanup_confirmed"])
            self.assertEqual("", response.limit_hit)
            self.assertEqual("codex-public-v1", response.event_projection)

    def test_codex_agent_rejects_a_success_envelope_without_hard_event_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-ollama-main", entry=str(entry)
            )
            envelope = {
                "run": {
                    "exitCode": 0,
                    "durationMs": 12,
                    "stdout": '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "not-enforced",
                        "maxToolCalls": "not-enforced",
                    },
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                return_value=(0, __import__("json").dumps(envelope), "", 12),
            ):
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke(
                        "task",
                        {
                            "workspace": str(root),
                            "model": "qwen-main-v1",
                            "policy": "workspace-write",
                            "native_images": [],
                            "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 4},
                        },
                    )

            self.assertEqual("agent_budget_unenforced", raised.exception.error.category)
            self.assertEqual("not-enforced", raised.exception.receipt["limit_enforcement"]["maxSteps"])

    def test_codex_agent_returns_a_bounded_limit_receipt_without_hidden_reasoning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-ollama-main", entry=str(entry)
            )
            envelope = {
                "run": {
                    "exitCode": 75,
                    "durationMs": 22,
                    "stdout": "\n".join(
                        [
                            '{"type":"thread.started","thread_id":"thread-1"}',
                            '{"type":"item.completed","item":{"type":"agent_message","text":"safe public progress"}}',
                        ]
                    ),
                    "stderr": "Agent exceeded max_tool_calls=1.",
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "hard",
                        "maxToolCalls": "hard",
                    },
                    "limitUsage": {
                        "steps": 1,
                        "toolCalls": 2,
                        "eventsSeen": 5,
                        "protocol": "codex-jsonl",
                    },
                    "limitHit": "maxToolCalls",
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                return_value=(1, __import__("json").dumps(envelope), "", 22),
            ):
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke(
                        "task",
                        {
                            "workspace": str(root),
                            "model": "qwen-main-v1",
                            "policy": "workspace-write",
                            "native_images": [],
                            "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 1},
                        },
                    )

            self.assertEqual("agent_budget_exceeded", raised.exception.error.category)
            self.assertEqual("maxToolCalls", raised.exception.receipt["limit_hit"])
            self.assertEqual(2, raised.exception.receipt["tool_calls"])
            self.assertNotIn("reasoning", __import__("json").dumps(raised.exception.receipt).lower())

    def test_codex_event_protocol_failure_is_reported_as_budget_unenforced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-ollama-main", entry=str(entry)
            )
            envelope = {
                "run": {
                    "exitCode": 74,
                    "durationMs": 8,
                    "stdout": "",
                    "stderr": "Codex emitted a non-JSON event line.",
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "failed-closed",
                        "maxToolCalls": "failed-closed",
                    },
                    "limitHit": "maxToolCalls",
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                return_value=(1, __import__("json").dumps(envelope), "", 8),
            ):
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke(
                        "task",
                        {
                            "workspace": str(root),
                            "model": "qwen-main-v1",
                            "policy": "workspace-write",
                            "native_images": [],
                            "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 4},
                        },
                    )

            self.assertEqual("agent_budget_unenforced", raised.exception.error.category)
            self.assertEqual("budget_unenforced", raised.exception.receipt["stop_reason"])

    def test_aicli_wall_timeout_is_normalized_as_a_hard_timeout_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-ollama-main", entry=str(entry)
            )
            envelope = {
                "run": {
                    "exitCode": 3,
                    "durationMs": 30000,
                    "stdout": "",
                    "stderr": "Child process exceeded the configured wall timeout.",
                    "timedOut": True,
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "hard",
                        "maxToolCalls": "hard",
                    },
                    "limitHit": "timeout",
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                return_value=(1, __import__("json").dumps(envelope), "", 30000),
            ):
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke(
                        "task",
                        {
                            "workspace": str(root),
                            "model": "qwen-main-v1",
                            "policy": "workspace-write",
                            "native_images": [],
                            "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 4},
                        },
                    )

            self.assertEqual("agent_timeout", raised.exception.error.category)
            self.assertEqual("timeout", raised.exception.receipt["limit_hit"])
            self.assertEqual("hard", raised.exception.receipt["limit_enforcement"]["timeout"])

    def test_toolkit_maps_a_hard_agent_budget_hit_to_blocked(self):
        class BudgetRunner:
            def invoke(self, prompt, execution):
                raise AgentRunnerError(
                    ToolError(
                        category="agent_budget_exceeded",
                        summary="hard limit reached",
                        retryable=False,
                        options=("increase-budget", "handle-in-codex"),
                    ),
                    {
                        "runner": "codex-cli",
                        "exit_code": 75,
                        "duration_ms": 22,
                        "steps": 1,
                        "tool_calls": 2,
                        "stop_reason": "maxToolCalls",
                        "limit_hit": "maxToolCalls",
                        "limit_enforcement": {
                            "timeout": "hard",
                            "maxSteps": "hard",
                            "maxToolCalls": "hard",
                        },
                    },
                )

        with tempfile.TemporaryDirectory() as temp:
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": BudgetRunner()},
            )
            result = toolkit.invoke(agent_request(Path(temp)))

        self.assertEqual("blocked", result["status"])
        self.assertEqual("agent_budget_exceeded", result["error"]["category"])
        self.assertEqual("maxToolCalls", result["execution_receipt"]["limit_hit"])

    @patch("llm_backend_toolkit.agent_runners.os.name", "nt")
    @patch("llm_backend_toolkit.agent_runners.subprocess.run")
    @patch("llm_backend_toolkit.agent_runners.subprocess.Popen")
    def test_outer_timeout_kills_the_complete_windows_process_tree(self, popen, native_run):
        process = MagicMock()
        process.pid = 24680
        process.wait.return_value = 0
        process.poll.return_value = 1
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["agent"], timeout=1),
            ("", ""),
        ]
        native_run.return_value.returncode = 0
        popen.return_value = process

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AgentRunnerError) as raised:
                _bounded_process(
                    ["agent"],
                    cwd=Path(temp),
                    stdin_text="task",
                    timeout_seconds=1,
                )

        self.assertEqual("agent_timeout", raised.exception.error.category)
        native_run.assert_called_once()
        self.assertEqual(
            ["taskkill", "/PID", "24680", "/T", "/F"],
            native_run.call_args.args[0],
        )

    @patch("llm_backend_toolkit.agent_runners.os.name", "nt")
    @patch("llm_backend_toolkit.agent_runners.subprocess.run")
    @patch("llm_backend_toolkit.agent_runners.subprocess.Popen")
    def test_outer_timeout_fails_closed_when_windows_tree_cleanup_is_unconfirmed(self, popen, native_run):
        process = MagicMock()
        process.pid = 13579
        process.wait.side_effect = subprocess.TimeoutExpired(cmd=["agent"], timeout=5)
        process.poll.return_value = None
        process.communicate.side_effect = subprocess.TimeoutExpired(cmd=["agent"], timeout=1)
        native_run.return_value.returncode = 1
        popen.return_value = process

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AgentRunnerError) as raised:
                _bounded_process(
                    ["agent"],
                    cwd=Path(temp),
                    stdin_text="task",
                    timeout_seconds=1,
                )

        self.assertEqual("agent_budget_unenforced", raised.exception.error.category)
        self.assertFalse(raised.exception.receipt["cleanup_confirmed"])

    def test_cloud_agent_defaults_to_codex_and_pins_exact_plus_model(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner(
                AgentResponse(
                    content='{"answer": 56}',
                    runner="codex-cli",
                    model="qwen3.7-plus",
                    exit_code=0,
                    duration_ms=456,
                )
            )
            toolkit = Toolkit(
                providers={"qwen3.7-plus": FakeCloudProvider()},
                runners={"data_factory": runner},
            )
            request = agent_request(Path(temp))
            request["provider"] = "qwen3.7-plus"
            request["privacy"] = {"cloud_allowed": True}
            del request["execution"]["runner"]

            result = toolkit.invoke(request)

            self.assertEqual("ok", result["status"])
            execution = runner.calls[0]["execution"]
            self.assertEqual("codex-qwen-paygo", execution["profile"])
            self.assertEqual("qwen3.7-plus", execution["model"])
            receipt = result["execution_receipt"]
            self.assertEqual("data_factory", receipt["requested_runner"])
            self.assertEqual("codex-qwen-paygo", receipt["profile"])
            self.assertTrue(receipt["default_applied"])
            self.assertFalse(receipt["route_live_verified"])
            self.assertEqual("official_codex_responses_plus_local_sibling_bakeoff", receipt["route_basis"])

    def test_fast_middle_spark_pins_its_exact_profile_model_and_xhigh_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner(
                AgentResponse(
                    content='{"answer": 56}',
                    runner="codex-cli",
                    model="gpt-5.3-codex-spark",
                    exit_code=0,
                    duration_ms=321,
                )
            )
            toolkit = Toolkit(runners={"codex-cli": runner, "data_factory": runner})
            request = agent_request(Path(temp))
            request["backend"] = "fast-middle-agent"
            request["privacy"] = {"cloud_allowed": True}

            result = toolkit.invoke(request)

            self.assertEqual("ok", result["status"])
            execution = runner.calls[0]["execution"]
            self.assertEqual("codex-spark-xhigh", execution["profile"])
            self.assertEqual("gpt-5.3-codex-spark", execution["model"])
            receipt = result["execution_receipt"]
            self.assertEqual("xhigh", receipt["reasoning_effort"])
            self.assertTrue(receipt["route_live_verified"])
            self.assertEqual(
                "aicli_0.3.1_toolkit_live_and_synthetic_acceptance_2026-07-24",
                receipt["route_basis"],
            )
            self.assertFalse(receipt["fallback_used"])
            self.assertFalse(result["backend"]["default_applied"])

    def test_cloud_agent_rejects_a_runner_without_an_exact_cloud_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen3.7-plus": FakeCloudProvider()},
                runners={"qwen-code": runner},
            )
            request = agent_request(Path(temp))
            request["provider"] = "qwen3.7-plus"
            request["privacy"] = {"cloud_allowed": True}
            request["execution"]["runner"] = "qwen-code"

            result = toolkit.invoke(request)

            self.assertEqual("blocked", result["status"])
            self.assertEqual("agent_runner_incompatible", result["error"]["category"])
            self.assertEqual([], runner.calls)
            self.assertEqual("top_model", result["decision"]["owner"])

    def test_agent_mode_uses_compacted_prompt_and_never_calls_direct_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = FakeProvider()
            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": provider},
                runners={"data_factory": runner},
            )

            result = toolkit.invoke(agent_request(Path(temp)))

            self.assertEqual("ok", result["status"])
            self.assertEqual({"answer": 56}, result["output"])
            self.assertEqual([], provider.calls)
            self.assertEqual(1, len(runner.calls))
            self.assertIn("record-a", runner.calls[0]["prompt"])
            self.assertEqual("compact", result["context_receipt"]["mode"])
            self.assertEqual("qwen-code", result["execution_receipt"]["runner"])
            self.assertEqual(4, result["execution_receipt"]["tool_calls"])
            self.assertNotIn("reasoning", result)

    def test_zero_tool_call_budget_is_preserved_instead_of_replaced_by_the_default(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": runner},
            )
            request = agent_request(Path(temp))
            request["execution"]["budget"]["max_tool_calls"] = 0

            result = toolkit.invoke(request)

            self.assertEqual("ok", result["status"])
            self.assertEqual(0, runner.calls[0]["execution"]["budget"]["max_tool_calls"])
            self.assertEqual(0, result["execution_receipt"]["budget"]["max_tool_calls"])

    def test_agent_mode_never_falls_back_to_an_unrequested_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            other = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"opencode": other},
            )
            request = agent_request(Path(temp))
            request["execution"]["runner"] = "missing-runner"

            result = toolkit.invoke(request)

            self.assertEqual("blocked", result["status"])
            self.assertEqual("agent_runner_unavailable", result["error"]["category"])
            self.assertEqual([], other.calls)
            self.assertEqual("top_model", result["decision"]["owner"])

    def test_failed_agent_returns_result_side_timing_without_an_event_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": FailingRunner()},
            )

            result = toolkit.invoke(agent_request(Path(temp)))

            self.assertEqual("failed", result["status"])
            self.assertEqual(101700, result["execution_receipt"]["duration_ms"])
            self.assertEqual(1, result["execution_receipt"]["exit_code"])
            self.assertNotIn("events", result["execution_receipt"])

    def test_agent_mode_requires_a_real_workspace(self):
        toolkit = Toolkit(providers={"qwen-main-v1": FakeProvider()}, runners={})
        request = agent_request(Path("Z:/definitely-missing-agent-workspace"))

        result = toolkit.invoke(request)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("invalid_request", result["error"]["category"])

    def test_cloud_agent_billing_failure_returns_the_decision_to_the_top_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-qwen-paygo", entry=str(entry)
            )
            execution = {
                "workspace": str(root),
                "model": "qwen3.7-plus",
                "profile": "codex-qwen-paygo",
                "policy": "workspace-write",
                "native_images": [],
                "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 4},
            }
            envelope = {
                "run": {
                    "exitCode": 1,
                    "durationMs": 12,
                    "stdout": "",
                    "stderr": "HTTP 400: Arrearage",
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                return_value=(1, __import__("json").dumps(envelope), "", 12),
            ):
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke("task", execution)

            self.assertEqual("billing_unavailable", raised.exception.error.category)
            self.assertEqual("top_model", raised.exception.error.decision_owner)
            self.assertIn("invoke:local-default", raised.exception.error.options)


if __name__ == "__main__":
    unittest.main()
