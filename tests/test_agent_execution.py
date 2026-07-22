import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_backend_toolkit.agent_runners import (
    AgentResponse,
    AgentRunnerError,
    AiCliProfileRunner,
    OpenCodeRunner,
    QwenCodeRunner,
    _json_values,
    default_runners,
)
from llm_backend_toolkit.providers import ProviderResponse
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
                    "stdout": '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
                    "limitEnforcement": {"timeout": "hard", "maxSteps": "not-enforced"},
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
            self.assertEqual("hard", response.limit_enforcement["timeout"])

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

    def test_agent_mode_is_local_only_and_requires_a_real_workspace(self):
        toolkit = Toolkit(providers={"qwen-main-v1": FakeProvider()}, runners={})
        request = agent_request(Path("Z:/definitely-missing-agent-workspace"))

        result = toolkit.invoke(request)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("invalid_request", result["error"]["category"])


if __name__ == "__main__":
    unittest.main()
