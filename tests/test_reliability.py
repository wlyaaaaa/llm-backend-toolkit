"""Behavior regressions for the public tool's cross-layer reliability contract."""
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_backend_toolkit.backends import BackendRegistry
from llm_backend_toolkit.context import _clip, compact_task
from llm_backend_toolkit.jobs import JobStore
from llm_backend_toolkit.providers import OllamaProvider, ProviderResponse
from llm_backend_toolkit.sources import SourceLoader
from llm_backend_toolkit.toolkit import Toolkit


def registry(model="model-a", **options):
    backend = {"adapter": "ollama", "model": model, "cloud": False, **options}
    return BackendRegistry.from_dict({
        "schema": "llm-backend-toolkit.backends.v1", "default_backend": "local",
        "backends": {"local": backend},
    })


class ResponseProvider:
    supports_vision = False
    def __init__(self, reason="stop", cloud=False):
        self.reason = reason
        self.cloud = cloud
        self.calls = 0
    def invoke(self, *args, **kwargs):
        self.calls += 1
        return ProviderResponse("PUBLIC_FIXTURE_RESULT", "model-a", self.reason)


class ReliabilityTests(unittest.TestCase):
    def test_short_input_is_not_duplicated_by_clipping(self):
        self.assertEqual("SHORT_PUBLIC_EVIDENCE", _clip("SHORT_PUBLIC_EVIDENCE", 1000))

    def test_cjk_boundary_retains_nearly_all_available_budget(self):
        for size in (16000, 16500, 17000, 20000, 30000):
            with self.subTest(size=size):
                result = compact_task({"task": {"goal": "summarize", "inputs": ["汉" * size]}})
                self.assertGreater(result.prompt.count("汉"), 15000)
                self.assertLessEqual(result.receipt["estimated_tokens_after"], 16384)

    def test_registered_window_prevents_unnecessary_default_clipping(self):
        source = "中文资料" * 10000
        response = ResponseProvider()
        tool = Toolkit(registry=registry(context_window_tokens=262144),
                       providers={"local": response}, runners={})
        result = tool.invoke({"task": {"goal": "summarize", "inputs": [source]}})
        self.assertFalse(result["context_receipt"]["lossy"])
        self.assertEqual(262144, result["context_receipt"]["target_tokens"])

    def test_mixed_short_and_long_inputs_preserve_short_evidence_once(self):
        result = compact_task({"task": {"goal": "summarize", "inputs": ["UNIQUE_PUBLIC_SHORT", "汉" * 40000]}})
        self.assertEqual(1, result.prompt.count("UNIQUE_PUBLIC_SHORT"))
        self.assertGreater(result.prompt.count("汉"), 15000)

    def test_explicit_cloud_authorization_is_a_boolean_not_truthiness(self):
        for value in ("false", "true", 0, 1, [], [False], None):
            with self.subTest(value=value):
                provider = ResponseProvider(cloud=True)
                tool = Toolkit(registry=registry(), providers={"local": provider}, runners={})
                result = tool.invoke({"task": {"goal": "public fixture"}, "privacy": {"cloud_allowed": value}})
                self.assertEqual("blocked", result["status"])
                self.assertEqual("invalid_request", result["error"]["category"])
                self.assertEqual(0, provider.calls)
        for value, expected_calls in ((False, 0), (True, 1)):
            provider = ResponseProvider(cloud=True)
            tool = Toolkit(registry=registry(), providers={"local": provider}, runners={})
            tool.invoke({"task": {"goal": "public fixture"}, "privacy": {"cloud_allowed": value}})
            self.assertEqual(expected_calls, provider.calls)

    def test_incomplete_output_is_partial_and_not_a_completed_progress_event(self):
        for reason in ("length", "content_filter", "", "tool_calls", "response_incomplete"):
            with self.subTest(reason=reason):
                events = []
                tool = Toolkit(registry=registry(), providers={"local": ResponseProvider(reason)}, runners={})
                result = tool.invoke({"task": {"goal": "public fixture"}}, progress_callback=events.append)
                self.assertEqual("partial", result["status"])
                self.assertTrue(result["output"])
                self.assertTrue(result["uncertainties"])
                self.assertNotIn("completed", [event.get("phase") for event in events])

    def test_missing_stream_terminal_keeps_partial_but_never_reports_success(self):
        body = json.dumps({"model": "model-a", "message": {"content": "PARTIAL"}, "done": False}).encode() + b"\n"
        tool = Toolkit(registry=registry(), providers={"local": OllamaProvider(model="model-a")}, runners={})
        events = []
        with patch("urllib.request.urlopen", return_value=io.BytesIO(body)):
            result = tool.invoke({"task": {"goal": "public fixture"}}, progress_callback=events.append)
        self.assertEqual("partial", result["status"])
        self.assertEqual("PARTIAL", result["output"])
        self.assertEqual("stream_incomplete", result["finish_reason"])
        self.assertNotIn("completed", [event.get("phase") for event in events])

    def test_changed_default_model_does_not_reuse_completed_job(self):
        with tempfile.TemporaryDirectory() as directory:
            request = {"task": {"goal": "public fixture"}}
            first = JobStore(Path(directory), registry=registry("model-a"), spawner=lambda *_: None)
            accepted = first.submit(request)
            first.complete(accepted["job_id"], {"status": "ok", "output": "MODEL_A_RESULT"})
            self.assertEqual("cache_hit", first.submit(request)["status"])
            second = JobStore(Path(directory), registry=registry("model-b"), spawner=lambda *_: None)
            replacement = second.submit(request)
            self.assertEqual("accepted", replacement["status"])
            self.assertNotEqual(accepted["job_id"], replacement["job_id"])

    def test_changed_effective_endpoint_does_not_reuse_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory), registry=registry(base_url_env="PUBLIC_TEST_ENDPOINT"), spawner=lambda *_: None)
            request = {"task": {"goal": "public fixture"}}
            with patch.dict(os.environ, {"PUBLIC_TEST_ENDPOINT": "http://127.0.0.1:32100"}):
                first = store.submit(request)
                store.complete(first["job_id"], {"status": "ok", "output": "FIRST"})
            with patch.dict(os.environ, {"PUBLIC_TEST_ENDPOINT": "http://localhost:32100"}):
                second = store.submit(request)
            self.assertNotEqual(first["job_id"], second["job_id"])

    def test_partial_result_is_available_but_never_cache_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory), registry=registry(), spawner=lambda *_: None)
            request = {"task": {"goal": "public fixture"}}
            first = store.submit(request)
            store.complete(first["job_id"], {"status": "partial", "output": "PARTIAL"})
            self.assertEqual("completed", store.get(first["job_id"])["job_status"])
            self.assertEqual("accepted", store.submit(request)["status"])

    def test_excerpt_line_range_matches_actually_retained_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.txt"
            path.write_text("\n".join(f"line {i}: " + "x" * 30 for i in range(1, 30)), encoding="utf-8")
            result = SourceLoader().load([{"id": "public", "path": str(path), "max_chars": 256}], query="line 1")
            for item in result.inputs:
                self.assertEqual(item["line_end"], item["line_start"] + len(item["excerpt"].splitlines()) - 1)
                self.assertTrue(item["excerpt_truncated"])
            self.assertEqual(result.inputs[0]["line_end"], result.receipt[0]["selected_ranges"][0]["line_end"])


    def test_legacy_or_failed_evidence_is_not_current_acceptance(self):
        for evidence in ({"live_verified": True},
                         {"live_verified": True, "capability_acceptance": "failed_max_steps_80"},
                         {"live_verified": True, "capability_acceptance_state": "configured", "model_digest": "old"}):
            with self.subTest(evidence=evidence):
                observed = BackendRegistry.evaluate_route_evidence({"evidence": evidence}, None)
                self.assertFalse(observed["live_verified"])
                self.assertNotEqual("current", observed["evidence_state"])

    def test_direct_preflight_never_loads_materials_or_calls_provider(self):
        from unittest.mock import Mock
        provider, sources, media = ResponseProvider(), Mock(), Mock()
        tool = Toolkit(registry=registry(), providers={"local": provider}, runners={},
                       source_loader=sources, media_processor=media)
        result = tool.preflight({"task": {"goal": "public fixture", "sources": [{"path": "missing.txt"}]}})
        self.assertEqual("ok", result["status"])
        self.assertFalse(result["preflight"]["materials_read"])
        self.assertEqual(0, provider.calls)
        sources.load.assert_not_called()
        media.process.assert_not_called()


    def test_machine_native_images_fail_before_a_process_or_provider_call(self):
        from llm_backend_toolkit.agent_runners import AiCliProfileRunner, AgentRunnerError
        runner = AiCliProfileRunner(name="codex-cli", engine="codex", default_profile="public-profile")
        with patch("llm_backend_toolkit.agent_runners._bounded_process") as process:
            with self.assertRaises(AgentRunnerError) as raised:
                runner._run_command({"workspace": ".", "native_images": ["public-image.png"]})
        self.assertEqual("agent_media_unsupported", raised.exception.error.category)
        process.assert_not_called()

    def test_machine_cannot_claim_unsupported_codex_workspace_isolation(self):
        from llm_backend_toolkit.agent_runners import AiCliProfileRunner, AgentRunnerError
        runner = AiCliProfileRunner(name="codex-cli", engine="codex", default_profile="public-profile")
        with self.assertRaises(AgentRunnerError) as raised:
            runner._run_command({"workspace": ".", "policy": "workspace-write"})
        self.assertEqual("agent_runner_incompatible", raised.exception.error.category)

    def test_preflight_and_invoke_agree_on_required_reasoning(self):
        tool = Toolkit(registry=registry(required_reasoning_mode="on"),
                       providers={"local": ResponseProvider()}, runners={})
        for mode in ("off", "invalid"):
            request = {"task": {"goal": "public fixture"}, "reasoning": {"mode": mode}}
            for result in (tool.preflight(request), tool.invoke(request)):
                self.assertEqual("blocked", result["status"])
                self.assertEqual("invalid_request", result["error"]["category"])

    def test_runtime_identity_exposes_source_install_version_drift(self):
        from llm_backend_toolkit import __version__
        with patch("llm_backend_toolkit.toolkit.version", return_value="0.0.0-stale"):
            identity = Toolkit.runtime_identity()
        self.assertEqual(__version__, identity["source_version"])
        self.assertEqual("0.0.0-stale", identity["version"])
        self.assertFalse(identity["version_matches_source"])


    def test_cli_accepts_utf8_bom_from_windows_pipelines_and_files(self):
        from llm_backend_toolkit.cli import _read_request
        request = {"task": {"goal": "public fixture"}}
        text = json.dumps(request)
        with patch("sys.stdin", io.StringIO("\ufeff" + text)):
            self.assertEqual(request, _read_request("-"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(text, encoding="utf-8-sig")
            self.assertEqual(request, _read_request(str(path)))

class RequestBoundaryReliabilityTests(unittest.TestCase):
    def test_misplaced_or_malformed_output_contract_is_never_ignored(self):
        bad_requests = [
            {"task": {"goal": "public"}, "expected_output": {"format": "json"}},
            {"task": {"goal": "public", "expected_output": {"format": "unsupported"}}},
            {"task": {"goal": "public", "expected_output": {"required_keys": "answer"}}},
            {"task": {"goal": "public", "expected_output": {"required_keys": [1]}}},
        ]
        for request in bad_requests:
            with self.subTest(request=request):
                provider = ResponseProvider()
                tool = Toolkit(registry=registry(), providers={"local": provider}, runners={})
                for result in (tool.invoke(request), tool.preflight(request)):
                    self.assertEqual("blocked", result["status"])
                    self.assertEqual("invalid_request", result["error"]["category"])
                self.assertEqual(0, provider.calls)
                with tempfile.TemporaryDirectory() as directory:
                    spawned = []
                    store = JobStore(Path(directory), registry=registry(), spawner=lambda *args: spawned.append(args))
                    with self.assertRaises(ValueError):
                        store.submit(request)
                    self.assertEqual([], spawned)

    def test_public_agent_example_uses_supported_structured_contract(self):
        from llm_backend_toolkit.agent_runners import AiCliProfileRunner
        request = json.loads((Path(__file__).resolve().parents[1] / "examples/local-agent-request.json").read_text(encoding="utf-8"))
        runner = AiCliProfileRunner(name="codex-cli", engine="codex", default_profile="public")
        command = runner._run_command(request["execution"], prefix=["fixture"])
        self.assertIn("danger-full-access", command)
        self.assertNotIn("workspace-write", command)
        self.assertNotIn("cache_key", request["execution"])


class ProviderBoundaryReliabilityTests(unittest.TestCase):
    def test_malformed_ollama_message_fails_without_echoing_payload(self):
        payloads = [[], {"message": "PRIVATE_INVALID_BODY", "done": True},
                    {"message": {"content": {"PRIVATE_INVALID_BODY": 1}}, "done": True},
                    {"message": {"tool_calls": "PRIVATE_INVALID_BODY"}, "done": True}]
        for streaming in (False, True):
            for payload in payloads:
                with self.subTest(streaming=streaming, payload_type=type(payload).__name__):
                    tool = Toolkit(registry=registry(), providers={"local": OllamaProvider(model="model-a")}, runners={})
                    events = []
                    with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode() + b"\n")):
                        result = tool.invoke({"task": {"goal": "Public fixture"}},
                                             progress_callback=events.append if streaming else None)
                    self.assertEqual("failed", result["status"])
                    self.assertNotIn("PRIVATE_INVALID_BODY", json.dumps(result))
                    self.assertNotIn("completed", [event.get("phase") for event in events])

    def test_malformed_chat_choices_are_structured_errors(self):
        from llm_backend_toolkit.providers import OpenAIChatProvider
        from llm_backend_toolkit.errors import ProviderCallError
        for payload in ([], {}, {"choices": ["invalid"]},
                        {"choices": [{"message": {"content": {"secret": "PRIVATE_INVALID_BODY"}}}]}):
            with self.subTest(payload_type=type(payload).__name__):
                provider = OpenAIChatProvider(model="public", base_url="https://example.invalid", api_key="fixture")
                with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
                    with self.assertRaises(ProviderCallError) as raised:
                        provider.invoke("public", [], "off")
                self.assertEqual("provider_unavailable", raised.exception.error.category)
                self.assertNotIn("PRIVATE_INVALID_BODY", str(raised.exception))

    def test_terminal_length_never_emits_completed_progress(self):
        payload = {"message": {"content": "Public partial"}, "done": True, "done_reason": "length"}
        events = []
        tool = Toolkit(registry=registry(), providers={"local": OllamaProvider(model="model-a")}, runners={})
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode() + b"\n")):
            result = tool.invoke({"task": {"goal": "public"}}, progress_callback=events.append)
        self.assertEqual("partial", result["status"])
        self.assertNotIn("completed", [event.get("phase") for event in events])

    def test_preflight_accepts_extra_fields_but_rejects_required_semantic_drift(self):
        from llm_backend_toolkit.agent_runners import AiCliProfileRunner, AgentRunnerError
        runner = AiCliProfileRunner(name="codex-cli", engine="codex", default_profile="public", entry=__file__)
        execution = {"workspace": str(Path(__file__).resolve().parent), "model": "model-a", "profile": "public"}
        baseline = {"schema": "aicli.machine-run-preflight.v1", "model": "model-a", "profileId": "public",
                    "engine": "codex", "policy": "danger-full-access", "budgetMode": "watchdog_only",
                    "arguments": "structured", "modelInvoked": False, "runtimeCreated": False,
                    "version": "999.0.0", "future_optional_field": {"enabled": True}}
        with patch.object(runner, "_prefix", return_value=["public-fixture"]):
            with patch("llm_backend_toolkit.agent_runners._bounded_process", return_value=(0, json.dumps({"preflight": baseline}), "", 1)):
                self.assertEqual("999.0.0", runner.preflight(execution)["version"])
            for change in ({"engine": "other"}, {"policy": "workspace-write"}, {"modelInvoked": True}, {"runtimeCreated": True}):
                with self.subTest(change=change):
                    with patch("llm_backend_toolkit.agent_runners._bounded_process", return_value=(0, json.dumps({"preflight": {**baseline, **change}}), "", 1)):
                        with self.assertRaises(AgentRunnerError):
                            runner.preflight(execution)


if __name__ == "__main__":
    unittest.main()
