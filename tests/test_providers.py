import json
import unittest
from unittest.mock import patch

from llm_backend_toolkit.providers import OllamaProvider, Qwen37PlusProvider


class FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ProviderContractTests(unittest.TestCase):
    def test_qwen_adapter_disables_thinking_and_uses_only_qwen37plus(self):
        seen = []

        def fake_urlopen(request, timeout):
            seen.append((request, timeout))
            return FakeHttpResponse(
                {
                    "model": "qwen3.7-plus",
                    "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
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
                }
            )

        provider = OllamaProvider(base_url="http://127.0.0.1:32100")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = provider.invoke("task", [], "off")

        request = seen[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual("http://127.0.0.1:32100/api/chat", request.full_url)
        self.assertFalse(payload["think"])
        self.assertEqual("0", payload["keep_alive"])
        self.assertEqual("local", response.content)


if __name__ == "__main__":
    unittest.main()
