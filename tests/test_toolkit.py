import unittest
import tempfile
from pathlib import Path

from llm_backend_toolkit.errors import ProviderCallError, ToolError
from llm_backend_toolkit.providers import ProviderResponse
from llm_backend_toolkit.toolkit import Toolkit


class FakeProvider:
    def __init__(self, response=None, error=None, cloud=False):
        self.response = response
        self.error = error
        self.cloud = cloud
        self.calls = []

    def invoke(self, prompt, native_images, reasoning_mode, progress_callback=None):
        self.calls.append(
            {"prompt": prompt, "native_images": list(native_images), "reasoning_mode": reasoning_mode}
        )
        if self.error:
            raise self.error
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "thinking",
                    "elapsed_seconds": 1.0,
                    "thinking_active": True,
                    "thinking_chars": 20,
                    "token_events": 3,
                }
            )
            progress_callback(
                {
                    "phase": "generating",
                    "elapsed_seconds": 2.0,
                    "thinking_active": False,
                    "content_delta": "公开回复",
                    "content_chars": 4,
                    "thinking_chars": 20,
                    "token_events": 6,
                }
            )
        return self.response


def base_request(provider="qwen-main-v1"):
    return {
        "provider": provider,
        "task": {
            "goal": "Return JSON",
            "instructions": ["Return only the final result"],
            "inputs": ["7 multiplied by 8"],
            "expected_output": {"format": "json", "required_keys": ["answer"]},
        },
        "privacy": {"cloud_allowed": provider == "qwen3.7-flash"},
    }


class ToolkitTests(unittest.TestCase):
    def test_progress_callback_reports_safe_generation_and_validation_phases(self):
        provider = FakeProvider(
            ProviderResponse(content='{"answer": 56}', model="qwen-main-v1")
        )
        toolkit = Toolkit(providers={"qwen-main-v1": provider})
        events = []

        result = toolkit.invoke(base_request(), progress_callback=events.append)

        self.assertEqual("ok", result["status"])
        self.assertEqual(
            ["preparing", "thinking", "generating", "validating", "completed"],
            [event["phase"] for event in events],
        )
        self.assertEqual("公开回复", events[2]["content_delta"])
        self.assertFalse(
            any(
                (event.get("public_event") or {}).get("kind")
                == "context.compaction.completed"
                for event in events
            )
        )

    def test_applied_context_compaction_emits_a_public_event_and_limit_receipt(self):
        provider = FakeProvider(
            ProviderResponse(content='{"answer": 56}', model="qwen-main-v1")
        )
        toolkit = Toolkit(providers={"qwen-main-v1": provider})
        compacting_request = base_request()
        compacting_request["task"]["instructions"].append(
            compacting_request["task"]["instructions"][0]
        )
        events = []

        result = toolkit.invoke(compacting_request, progress_callback=events.append)

        context_event = next(
            event["public_event"]
            for event in events
            if (event.get("public_event") or {}).get("kind")
            == "context.compaction.completed"
        )
        self.assertEqual("已自动压缩调用前上下文。", context_event["summary_zh"])
        self.assertTrue(context_event["payload"]["applied"])
        self.assertFalse(context_event["payload"]["lossy"])
        self.assertEqual(1, context_event["payload"]["duplicates_removed"])
        self.assertGreater(
            context_event["payload"]["estimated_tokens_before"],
            context_event["payload"]["estimated_tokens_after"],
        )
        self.assertEqual(
            262144,
            context_event["payload"]["context_window_tokens"],
        )
        self.assertEqual(262144, result["backend"]["context_window_tokens"])

    def test_success_returns_result_side_checks_and_no_reasoning(self):
        provider = FakeProvider(
            ProviderResponse(
                content='{"answer": 56}',
                model="qwen-main-v1",
                finish_reason="stop",
                usage={"prompt_tokens": 20, "completion_tokens": 5},
                reasoning="hidden trace",
            )
        )
        toolkit = Toolkit(providers={"qwen-main-v1": provider})

        result = toolkit.invoke(base_request())

        self.assertEqual("ok", result["status"])
        self.assertEqual({"answer": 56}, result["output"])
        self.assertNotIn("reasoning", result)
        self.assertTrue(all(check["passed"] for check in result["checks"]))
        self.assertEqual("on", provider.calls[0]["reasoning_mode"])
        self.assertEqual("compact", result["context_receipt"]["mode"])

    def test_explicit_reasoning_off_overrides_the_local_quality_default(self):
        provider = FakeProvider(
            ProviderResponse(content='{"answer": 56}', model="qwen-main-v1")
        )
        toolkit = Toolkit(providers={"qwen-main-v1": provider})
        request = base_request()
        request["reasoning"] = {"mode": "off"}

        result = toolkit.invoke(request)

        self.assertEqual("ok", result["status"])
        self.assertEqual("off", provider.calls[0]["reasoning_mode"])

    def test_failed_primary_never_calls_an_unrequested_local_provider(self):
        primary = FakeProvider(
            error=ProviderCallError(
                ToolError(
                    category="billing_unavailable",
                    provider_code="Arrearage",
                    summary="billing unavailable",
                    retryable=False,
                )
            ),
            cloud=True,
        )
        local = FakeProvider(ProviderResponse(content="local", model="qwen-main-v1"))
        toolkit = Toolkit(providers={"qwen3.7-flash": primary, "qwen-main-v1": local})

        result = toolkit.invoke(base_request("qwen3.7-flash"))

        self.assertEqual("failed", result["status"])
        self.assertEqual("billing_unavailable", result["error"]["category"])
        self.assertEqual("top_model", result["decision"]["owner"])
        self.assertEqual([], local.calls)

    def test_cloud_media_requires_explicit_permission(self):
        provider = FakeProvider(ProviderResponse(content="ok", model="qwen3.7-flash"), cloud=True)
        toolkit = Toolkit(providers={"qwen3.7-flash": provider})
        request = base_request("qwen3.7-flash")
        request["privacy"]["cloud_allowed"] = False
        request["media"] = {
            "mode": "native",
            "attachments": [{"id": "img", "path": "missing.png", "kind": "image"}],
        }

        result = toolkit.invoke(request)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("privacy_block", result["error"]["category"])
        self.assertEqual([], provider.calls)

    def test_cloud_text_and_source_refs_require_explicit_permission_before_local_reads(self):
        provider = FakeProvider(ProviderResponse(content="ok", model="qwen3.7-flash"), cloud=True)
        toolkit = Toolkit(providers={"qwen3.7-flash": provider})
        request = base_request("qwen3.7-flash")
        request["privacy"]["cloud_allowed"] = False
        request["task"]["sources"] = [{"id": "private", "path": "missing-private-source.txt"}]

        result = toolkit.invoke(request)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("privacy_block", result["error"]["category"])
        self.assertIn("allow-cloud-explicitly", result["decision"]["options"])
        self.assertEqual([], provider.calls)

    def test_invalid_json_is_partial_and_visible_to_the_caller(self):
        provider = FakeProvider(ProviderResponse(content="not-json", model="qwen-main-v1"))
        toolkit = Toolkit(providers={"qwen-main-v1": provider})

        result = toolkit.invoke(base_request())

        self.assertEqual("partial", result["status"])
        self.assertTrue(any(not check["passed"] for check in result["checks"]))
        self.assertEqual("not-json", result["output"])

    def test_source_references_are_loaded_inside_the_tool_and_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "facts.txt"
            source.write_text("irrelevant\n" * 30 + "The launch code is ORBIT-71.\n", encoding="utf-8")
            provider = FakeProvider(ProviderResponse(content='{"code":"ORBIT-71"}', model="qwen-main-v1"))
            toolkit = Toolkit(providers={"qwen-main-v1": provider})
            request = base_request()
            request["task"]["goal"] = "Return the launch code"
            request["task"]["sources"] = [{"id": "facts", "path": str(source), "top_k": 2}]
            request["task"]["expected_output"] = {"format": "json", "required_keys": ["code"]}

            result = toolkit.invoke(request)

            self.assertEqual("ok", result["status"])
            self.assertIn("ORBIT-71", provider.calls[0]["prompt"])
            self.assertEqual("facts", result["source_receipt"][0]["id"])


if __name__ == "__main__":
    unittest.main()
