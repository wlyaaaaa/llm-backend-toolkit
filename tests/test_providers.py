import json
import os
import unittest
from unittest.mock import patch

from llm_backend_toolkit.providers import OllamaProvider, OpenAIChatProvider, Qwen37PlusProvider, provider_from_config


class FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeStreamingHttpResponse:
    def __init__(self, payloads):
        self.payloads = payloads

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(
            [json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n" for payload in self.payloads]
        )


class ProviderContractTests(unittest.TestCase):
    def test_cloud_openai_compatible_backend_rejects_plain_http(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            provider_from_config(
                {
                    "adapter": "openai-chat",
                    "model": "remote-model",
                    "cloud": True,
                    "base_url_default": "http://remote.example/v1",
                    "api_key_env": "REMOTE_API_KEY",
                }
            )

    def test_generic_openai_chat_platform_is_created_from_registry_config(self):
        config = {
            "adapter": "openai-chat",
            "model": "future-model-v2",
            "cloud": True,
            "supports_vision": False,
            "base_url_default": "https://api.future.example/v1",
            "api_key_env": "FUTURE_PLATFORM_KEY",
        }

        with patch.dict(os.environ, {"FUTURE_PLATFORM_KEY": "fixture-key"}):
            provider = provider_from_config(config)

        self.assertIsInstance(provider, OpenAIChatProvider)
        self.assertEqual("future-model-v2", provider.model)
        self.assertEqual("https://api.future.example/v1", provider.base_url)
        self.assertTrue(provider.api_key)

    def test_cloud_adapter_rejects_ollama_options_instead_of_leaking_them(self):
        with self.assertRaisesRegex(ValueError, "ollama_options"):
            provider_from_config(
                {
                    "adapter": "openai-chat",
                    "model": "future-model-v2",
                    "cloud": True,
                    "supports_vision": False,
                    "base_url_default": "https://api.future.example/v1",
                    "api_key_env": "FUTURE_PLATFORM_KEY",
                    "ollama_options": {"temperature": 1.0},
                }
            )

    def test_ollama_adapter_rejects_known_internal_broker_backend(self):
        with self.assertRaisesRegex(ValueError, "managed public endpoint"):
            OllamaProvider(base_url="http://127.0.0.1:32101")

    def test_qwen_adapter_disables_thinking_and_uses_only_qwen37plus(self):
        seen = []

        def fake_urlopen(request, timeout):
            seen.append((request, timeout))
            return FakeHttpResponse(
                {
                    "model": "qwen3.7-plus",
                    "choices": [
                        {
                            "message": {
                                "content": "done",
                                "reasoning_content": "PRIVATE_CLOUD_HIDDEN_TRACE",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 3},
                }
            )

        provider = Qwen37PlusProvider(api_key="fixture-key", base_url="https://example.invalid/v1")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = provider.invoke("task", [], "off")

        payload = json.loads(seen[0][0].data.decode("utf-8"))
        self.assertEqual("qwen3.7-plus", payload["model"])
        self.assertFalse(payload["enable_thinking"])
        self.assertEqual("done", response.content)
        self.assertEqual("", response.reasoning)
        self.assertNotIn("PRIVATE_CLOUD_HIDDEN_TRACE", json.dumps(response.__dict__))

    def test_ollama_adapter_uses_managed_public_endpoint_and_no_thinking(self):
        seen = []

        def fake_urlopen(request, timeout):
            seen.append((request, timeout))
            return FakeHttpResponse(
                {
                    "model": "qwen-main-v1:latest",
                    "message": {"content": "local", "thinking": "not returned by toolkit"},
                    "done_reason": "stop",
                    "prompt_eval_count": 8,
                    "eval_count": 2,
                    "prompt_eval_duration": 300,
                    "eval_duration": 400,
                    "total_duration": 900,
                }
            )

        provider = OllamaProvider(base_url="http://127.0.0.1:32100")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = provider.invoke("task", [], "off")

        request = seen[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual("http://127.0.0.1:32100/api/chat", request.full_url)
        self.assertFalse(payload["think"])
        self.assertFalse(payload["stream"])
        self.assertEqual("0", payload["keep_alive"])
        self.assertNotIn("options", payload)
        self.assertEqual("local", response.content)
        self.assertEqual("", response.reasoning)
        self.assertEqual(
            {
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "prompt_eval_duration_ns": 300,
                "eval_duration_ns": 400,
                "total_duration_ns": 900,
            },
            response.usage,
        )

    def test_hard_reasoning_options_are_sent_only_to_ollama_and_thinking_still_requires_opt_in(self):
        seen = []
        options = {
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 1.5,
            "repeat_penalty": 1.0,
            "num_ctx": 262144,
            "num_predict": 32768,
        }

        def fake_urlopen(request, timeout):
            seen.append((request, timeout))
            return FakeHttpResponse(
                {
                    "model": "qwen-main-v1:latest",
                    "message": {"content": "local", "thinking": "PRIVATE_HIDDEN_TRACE"},
                    "done_reason": "stop",
                }
            )

        provider = provider_from_config(
            {
                "adapter": "ollama",
                "model": "qwen-main-v1",
                "cloud": False,
                "base_url_default": "http://127.0.0.1:32100",
                "ollama_options": options,
            }
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            without_thinking = provider.invoke("task", [], "off")
            with_thinking = provider.invoke("task", [], "on")

        first_payload = json.loads(seen[0][0].data.decode("utf-8"))
        second_payload = json.loads(seen[1][0].data.decode("utf-8"))
        self.assertEqual(options, first_payload["options"])
        self.assertFalse(first_payload["think"])
        self.assertEqual(options, second_payload["options"])
        self.assertTrue(second_payload["think"])
        self.assertEqual("", without_thinking.reasoning)
        self.assertEqual("", with_thinking.reasoning)
        self.assertNotIn("PRIVATE_HIDDEN_TRACE", json.dumps(with_thinking.__dict__))

    def test_ollama_streams_public_content_and_discards_hidden_thinking(self):
        seen = []
        events = []
        hidden_secret = "PRIVATE_HIDDEN_REASONING_MUST_NOT_LEAK"
        chunks = [
            {
                "model": "qwen-main-v1:latest",
                "message": {"thinking": hidden_secret, "content": ""},
                "done": False,
            },
            {
                "model": "qwen-main-v1:latest",
                "message": {"thinking": "more hidden", "content": ""},
                "done": False,
            },
            {
                "model": "qwen-main-v1:latest",
                "message": {"content": "公开"},
                "done": False,
            },
            {
                "model": "qwen-main-v1:latest",
                "message": {"content": "回复"},
                "done": False,
            },
            {
                "model": "qwen-main-v1:latest",
                "message": {"content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 11,
                "eval_count": 7,
                "prompt_eval_duration": 101,
                "eval_duration": 202,
                "total_duration": 404,
            },
        ]

        def fake_urlopen(request, timeout):
            seen.append((request, timeout))
            return FakeStreamingHttpResponse(chunks)

        provider = OllamaProvider(base_url="http://127.0.0.1:32100")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = provider.invoke("task", [], "on", events.append)

        payload = json.loads(seen[0][0].data.decode("utf-8"))
        self.assertTrue(payload["stream"])
        self.assertTrue(payload["think"])
        self.assertEqual("公开回复", response.content)
        self.assertEqual("", response.reasoning)
        self.assertEqual("stop", response.finish_reason)
        self.assertEqual(
            {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "prompt_eval_duration_ns": 101,
                "eval_duration_ns": 202,
                "total_duration_ns": 404,
            },
            response.usage,
        )
        self.assertEqual(
            ["公开", "回复"],
            [event["content_delta"] for event in events if "content_delta" in event],
        )
        self.assertEqual("completed", events[-1]["phase"])
        self.assertFalse(events[-1]["thinking_active"])
        self.assertEqual(len(hidden_secret) + len("more hidden"), events[-1]["thinking_chars"])
        serialized_events = json.dumps(events, ensure_ascii=False)
        self.assertNotIn(hidden_secret, serialized_events)
        self.assertNotIn("more hidden", serialized_events)
        self.assertNotIn(hidden_secret, json.dumps(response.__dict__, ensure_ascii=False))

    def test_ollama_progress_callback_failure_does_not_interrupt_result(self):
        chunks = [
            {
                "model": "qwen-main-v1:latest",
                "message": {"content": "safe"},
                "done": False,
            },
            {
                "model": "qwen-main-v1:latest",
                "message": {},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 1,
                "eval_count": 1,
            },
        ]

        def fail_progress(_event):
            raise RuntimeError("display unavailable")

        provider = OllamaProvider(base_url="http://127.0.0.1:32100")
        with patch("urllib.request.urlopen", return_value=FakeStreamingHttpResponse(chunks)):
            response = provider.invoke("task", [], "off", fail_progress)

        self.assertEqual("safe", response.content)

    def test_ollama_status_reports_version_bound_model_identity(self):
        payloads = {
            "/_gpu_broker/status": {"ok": True, "lease": None, "active_ollama_requests": 0},
            "/api/show": {
                "capabilities": ["completion", "vision"],
                "details": {
                    "parent_model": "qwen3.6:35b",
                    "parameter_size": "36.0B",
                    "quantization_level": "Q4_K_M",
                },
                "model_info": {"qwen35moe.context_length": 262144},
            },
            "/api/tags": {
                "models": [
                    {
                        "name": "qwen-main-v1:latest",
                        "digest": "a" * 64,
                        "modified_at": "2026-07-03T08:14:44-07:00",
                    }
                ]
            },
            "/api/version": {"version": "0.32.1"},
        }

        def fake_urlopen(request, timeout):
            del timeout
            path = request.full_url.removeprefix("http://127.0.0.1:32100")
            return FakeHttpResponse(payloads[path])

        provider = OllamaProvider(base_url="http://127.0.0.1:32100")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            status = provider.status()

        self.assertEqual("a" * 64, status["model"]["digest"])
        self.assertEqual("Q4_K_M", status["model"]["quantization"])
        self.assertEqual(262144, status["model"]["context_length"])
        self.assertEqual("0.32.1", status["runtime"]["ollama_version"])


if __name__ == "__main__":
    unittest.main()
