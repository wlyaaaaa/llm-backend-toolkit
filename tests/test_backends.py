import json
import tempfile
import unittest
from pathlib import Path

from llm_backend_toolkit.backends import BackendRegistry
from llm_backend_toolkit.providers import ProviderResponse
from llm_backend_toolkit.toolkit import Toolkit


def registry_data(*, expected_digest="digest-v1", live_verified=True):
    return {
        "schema": "llm-backend-toolkit.backends.v1",
        "default_backend": "local-default",
        "aliases": {"qwen-main-v1": "local-default"},
        "backends": {
            "local-default": {
                "adapter": "ollama",
                "model": "replacement-local-model",
                "cloud": False,
                "supports_vision": True,
                "agent_routes": {
                    "data_factory": {
                        "runner": "codex-cli",
                        "profile": "codex-ollama-main",
                        "model": "replacement-local-model",
                        "evidence": {
                            "basis": "version-bound-test",
                            "live_verified": live_verified,
                            "model_digest": expected_digest,
                        },
                    }
                },
            }
        },
    }


class ReplacementProvider:
    cloud = False
    supports_vision = True

    def __init__(self, digest="digest-v1"):
        self.digest = digest
        self.calls = []

    def status(self):
        return {
            "provider": "replacement-local-model",
            "cloud": False,
            "model": {"digest": self.digest, "parent_model": "replacement-family"},
            "live_call_performed": False,
        }

    def invoke(self, prompt, native_images, reasoning_mode):
        self.calls.append(prompt)
        return ProviderResponse(content='{"ok": true}', model="replacement-local-model")


class ReplacementRunner:
    def __init__(self):
        self.calls = []

    def invoke(self, prompt, execution):
        self.calls.append(execution)
        return type(
            "Response",
            (),
            {
                "content": '{"ok": true}',
                "runner": "codex-cli",
                "model": execution["model"],
                "exit_code": 0,
                "duration_ms": 1,
                "tool_calls": 0,
                "session_id": None,
                "stop_reason": "completed",
                "limit_enforcement": {},
            },
        )()


class BackendRegistryTests(unittest.TestCase):
    def test_default_backend_and_legacy_alias_resolve_without_code_changes(self):
        registry = BackendRegistry.from_dict(registry_data())

        default = registry.resolve(None)
        legacy = registry.resolve("qwen-main-v1")

        self.assertEqual("local-default", default.backend_id)
        self.assertTrue(default.default_applied)
        self.assertEqual("local-default", legacy.backend_id)
        self.assertEqual("qwen-main-v1", legacy.requested)

    def test_external_registry_can_be_loaded_from_a_json_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "backends.json"
            path.write_text(json.dumps(registry_data()), encoding="utf-8")
            registry = BackendRegistry.load(path)

        self.assertEqual("replacement-local-model", registry.resolve(None).config["model"])

    def test_registry_rejects_inline_credentials(self):
        data = registry_data()
        data["backends"]["local-default"]["api_key"] = "must-not-live-here"

        with self.assertRaisesRegex(ValueError, "environment variable"):
            BackendRegistry.from_dict(data)

    def test_replaced_model_digest_invalidates_old_live_evidence(self):
        registry = BackendRegistry.from_dict(registry_data(expected_digest="old-digest"))
        toolkit = Toolkit(
            registry=registry,
            providers={"local-default": ReplacementProvider("new-digest")},
            runners={},
        )

        result = toolkit.status(None)

        route = result["provider_status"]["agent_default"]
        self.assertFalse(route["live_verified"])
        self.assertEqual("stale", route["evidence_state"])
        self.assertIn("model_digest", route["evidence_mismatches"])

    def test_omitted_backend_uses_the_local_default_for_direct_work(self):
        registry = BackendRegistry.from_dict(registry_data())
        provider = ReplacementProvider()
        toolkit = Toolkit(registry=registry, providers={"local-default": provider}, runners={})

        result = toolkit.invoke({"task": {"goal": "return json", "expected_output": {"format": "json"}}})

        self.assertEqual("ok", result["status"])
        self.assertEqual("local-default", result["backend"]["resolved"])
        self.assertTrue(result["backend"]["default_applied"])
        self.assertEqual(1, len(provider.calls))

    def test_catalog_exposes_safe_routing_metadata_without_credentials(self):
        registry = BackendRegistry.from_dict(registry_data())

        catalog = registry.catalog()

        self.assertEqual("local-default", catalog["default_backend"])
        backend = catalog["backends"][0]
        self.assertEqual("replacement-local-model", backend["model"])
        self.assertNotIn("credential", str(catalog).lower())

    def test_custom_route_name_can_target_an_existing_runner_adapter(self):
        data = registry_data()
        route = data["backends"]["local-default"]["agent_routes"].pop("data_factory")
        data["backends"]["local-default"]["agent_routes"]["bulk-clean"] = route
        runner = ReplacementRunner()
        toolkit = Toolkit(
            registry=BackendRegistry.from_dict(data),
            providers={"local-default": ReplacementProvider()},
            runners={"codex-cli": runner},
        )

        with tempfile.TemporaryDirectory() as workspace:
            result = toolkit.invoke(
                {
                    "task": {"goal": "clean", "expected_output": {"format": "json"}},
                    "execution": {
                        "mode": "agent",
                        "runner": "bulk-clean",
                        "workspace": workspace,
                        "policy": "read-only",
                    },
                }
            )

        self.assertEqual("ok", result["status"])
        self.assertEqual("codex-cli", result["execution_receipt"]["resolved_runner"])
        self.assertEqual(1, len(runner.calls))


if __name__ == "__main__":
    unittest.main()
