import copy
import json
import unittest
from pathlib import Path

from llm_backend_toolkit.acceptance_routes import build_local_codex_benchmark_registry
from llm_backend_toolkit.backends import BackendRegistry


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceRouteTests(unittest.TestCase):
    def test_benchmark_route_is_exact_non_default_and_does_not_mutate_live_registry(self):
        source = json.loads(
            (ROOT / "src" / "llm_backend_toolkit" / "default_backends.json").read_text(
                encoding="utf-8"
            )
        )
        before = copy.deepcopy(source)

        registry = build_local_codex_benchmark_registry(
            source,
            backend_id="benchmark-local-review-27b",
            provider_model="qwen-review-v1",
            route_model="qwen-review-v1",
            profile="codex-ollama-review-27b",
            context_window_tokens=131072,
            model_digest="90a516a548f99c9a68f9915620e00bf1a800a507a9a2c86236a1354ab08e3195",
            parent_model="qwen3.6:27b",
        )

        self.assertIsInstance(registry, BackendRegistry)
        self.assertEqual(before, source)
        self.assertEqual("local-default", registry.default_backend)
        self.assertNotIn("benchmark-local-review-27b", registry.aliases)
        route_backend = registry.resolve("benchmark-local-review-27b")
        self.assertEqual("qwen-review-v1", route_backend.config["model"])
        self.assertFalse(route_backend.config["cloud"])
        self.assertEqual(
            ["codex-cli"],
            sorted(route_backend.config["agent_routes"]),
        )
        route = route_backend.config["agent_routes"]["codex-cli"]
        self.assertEqual("codex-ollama-review-27b", route["profile"])
        self.assertEqual("qwen-review-v1", route["model"])
        self.assertEqual("benchmark_only", route["evidence"]["basis"])
        self.assertTrue(route["evidence"]["live_verified"])
        self.assertEqual(
            "90a516a548f99c9a68f9915620e00bf1a800a507a9a2c86236a1354ab08e3195",
            route["evidence"]["model_digest"],
        )

    def test_invalid_digest_or_non_loopback_endpoint_is_rejected(self):
        source = json.loads(
            (ROOT / "src" / "llm_backend_toolkit" / "default_backends.json").read_text(
                encoding="utf-8"
            )
        )
        with self.assertRaisesRegex(ValueError, "model_digest"):
            build_local_codex_benchmark_registry(
                source,
                backend_id="benchmark-local-review-27b",
                provider_model="qwen-review-v1",
                route_model="qwen-review-v1",
                profile="codex-ollama-review-27b",
                context_window_tokens=131072,
                model_digest="not-a-digest",
                parent_model="qwen3.6:27b",
            )

        with self.assertRaisesRegex(ValueError, "loopback"):
            build_local_codex_benchmark_registry(
                source,
                backend_id="benchmark-local-review-27b",
                provider_model="qwen-review-v1",
                route_model="qwen-review-v1",
                profile="codex-ollama-review-27b",
                context_window_tokens=131072,
                model_digest="a" * 64,
                parent_model="qwen3.6:27b",
                base_url="https://example.com/v1",
            )


if __name__ == "__main__":
    unittest.main()
