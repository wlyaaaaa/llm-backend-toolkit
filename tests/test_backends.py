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
                "context_window_tokens": 262144,
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

    def __init__(self, digest="digest-v1", *, cloud=False):
        self.digest = digest
        self.cloud = cloud
        self.calls = []
        self.reasoning_modes = []

    def status(self):
        return {
            "provider": "replacement-local-model",
            "cloud": False,
            "model": {"digest": self.digest, "parent_model": "replacement-family"},
            "live_call_performed": False,
        }

    def invoke(self, prompt, native_images, reasoning_mode):
        self.calls.append(prompt)
        self.reasoning_modes.append(reasoning_mode)
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
    def test_local_crosscheck_27b_is_explicit_direct_only_and_keeps_35b_default(self):
        registry = BackendRegistry.load()

        default = registry.resolve(None)
        crosscheck = registry.resolve("local-crosscheck-27b")
        model_alias = registry.resolve("qwen-review-v1")

        self.assertEqual("local-default", default.backend_id)
        self.assertEqual("qwen-main-v1", default.config["model"])
        self.assertEqual("local-crosscheck-27b", crosscheck.backend_id)
        self.assertEqual("local-crosscheck-27b", model_alias.backend_id)
        self.assertTrue(model_alias.alias_applied)
        self.assertFalse(crosscheck.default_applied)
        self.assertEqual("ollama", crosscheck.config["adapter"])
        self.assertEqual("qwen-review-v1", crosscheck.config["model"])
        self.assertFalse(crosscheck.config["cloud"])
        self.assertTrue(crosscheck.config["supports_vision"])
        self.assertEqual(131_072, crosscheck.config["context_window_tokens"])
        self.assertEqual("on", crosscheck.config["default_reasoning_mode"])
        self.assertEqual("crosscheck_only", crosscheck.config["routing_role"])
        self.assertEqual(
            {
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "repeat_penalty": 1.0,
                "num_ctx": 131_072,
                "num_predict": 32_768,
            },
            crosscheck.config["ollama_options"],
        )
        self.assertEqual("http://127.0.0.1:32100", crosscheck.config["base_url_default"])
        self.assertEqual({}, crosscheck.config["agent_routes"])

        catalog_entry = next(
            item
            for item in registry.catalog()["backends"]
            if item["id"] == "local-crosscheck-27b"
        )
        self.assertFalse(catalog_entry["default"])
        self.assertEqual("crosscheck_only", catalog_entry["routing_role"])
        self.assertEqual([], catalog_entry["agent_routes"])

    def test_local_crosscheck_agent_request_fails_without_invoking_or_falling_back(self):
        registry = BackendRegistry.load()
        default_provider = ReplacementProvider()
        crosscheck_provider = ReplacementProvider()
        runner = ReplacementRunner()
        toolkit = Toolkit(
            registry=registry,
            providers={
                "local-default": default_provider,
                "local-crosscheck-27b": crosscheck_provider,
            },
            runners={"data_factory": runner},
        )

        with tempfile.TemporaryDirectory() as workspace:
            result = toolkit.invoke(
                {
                    "backend": "local-crosscheck-27b",
                    "task": {"goal": "crosscheck"},
                    "execution": {
                        "mode": "agent",
                        "workspace": workspace,
                        "policy": "read-only",
                    },
                }
            )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("agent_runner_incompatible", result["error"]["category"])
        self.assertIn("backend local-crosscheck-27b", result["error"]["summary"])
        self.assertEqual(0, len(default_provider.calls))
        self.assertEqual(0, len(crosscheck_provider.calls))
        self.assertEqual(0, len(runner.calls))

    def test_qwen_flash_is_direct_only_and_does_not_change_the_local_default(self):
        registry = BackendRegistry.load()

        default = registry.resolve(None)
        flash = registry.resolve("qwen3.7-flash")

        self.assertEqual("local-default", default.backend_id)
        self.assertEqual("cloud-qwen-flash", flash.backend_id)
        self.assertTrue(flash.alias_applied)
        self.assertEqual("openai-chat", flash.config["adapter"])
        self.assertEqual("qwen3.7-flash", flash.config["model"])
        self.assertTrue(flash.config["cloud"])
        self.assertTrue(flash.config["supports_vision"])
        self.assertEqual(1_000_000, flash.config["context_window_tokens"])
        self.assertEqual({}, flash.config["agent_routes"])
        with self.assertRaisesRegex(ValueError, "Unknown backend"):
            registry.resolve("qwen3.7-plus")
        with self.assertRaisesRegex(ValueError, "Unknown backend"):
            registry.resolve("cloud-qwen-plus")

    def test_deepseek_v4_flash_is_explicit_direct_only_with_registry_driven_thinking(self):
        registry = BackendRegistry.load()

        default = registry.resolve(None)
        flash = registry.resolve("deepseek-v4-flash")

        self.assertEqual("local-default", default.backend_id)
        self.assertEqual("cloud-deepseek-v4-flash", flash.backend_id)
        self.assertTrue(flash.alias_applied)
        self.assertFalse(flash.default_applied)
        self.assertEqual("openai-chat", flash.config["adapter"])
        self.assertEqual("deepseek-v4-flash", flash.config["model"])
        self.assertTrue(flash.config["cloud"])
        self.assertEqual("direct_only", flash.config["routing_role"])
        self.assertEqual("on", flash.config["default_reasoning_mode"])
        self.assertEqual("https://api.deepseek.com", flash.config["base_url_default"])
        self.assertEqual("DEEPSEEK_API_KEY", flash.config["api_key_env"])
        self.assertEqual(
            {
                "path": ["thinking", "type"],
                "on": "enabled",
                "off": "disabled",
            },
            flash.config["reasoning_request"],
        )
        self.assertEqual({}, flash.config["agent_routes"])
        with self.assertRaisesRegex(ValueError, "Unknown backend"):
            registry.resolve("deepseek-v4-pro")
        with self.assertRaisesRegex(ValueError, "Unknown backend"):
            registry.resolve("cloud-deepseek-v4-pro")

    def test_deepseek_cloud_permission_and_agent_mode_fail_closed_without_fallback(self):
        registry = BackendRegistry.load()
        default_provider = ReplacementProvider()
        deepseek_provider = ReplacementProvider(cloud=True)
        runner = ReplacementRunner()
        toolkit = Toolkit(
            registry=registry,
            providers={
                "local-default": default_provider,
                "cloud-deepseek-v4-flash": deepseek_provider,
            },
            runners={"data_factory": runner},
        )

        privacy_blocked = toolkit.invoke(
            {
                "backend": "cloud-deepseek-v4-flash",
                "task": {"goal": "explicit cloud route"},
            }
        )
        with tempfile.TemporaryDirectory() as workspace:
            agent_blocked = toolkit.invoke(
                {
                    "backend": "cloud-deepseek-v4-flash",
                    "task": {"goal": "must remain direct-only"},
                    "privacy": {"cloud_allowed": True},
                    "execution": {
                        "mode": "agent",
                        "workspace": workspace,
                        "policy": "read-only",
                    },
                }
            )
        direct = toolkit.invoke(
            {
                "backend": "cloud-deepseek-v4-flash",
                "task": {"goal": "omitted reasoning defaults to thinking"},
                "privacy": {"cloud_allowed": True},
                "execution": {"mode": "direct"},
            }
        )

        self.assertEqual("blocked", privacy_blocked["status"])
        self.assertEqual("privacy_block", privacy_blocked["error"]["category"])
        self.assertEqual("blocked", agent_blocked["status"])
        self.assertEqual("agent_runner_incompatible", agent_blocked["error"]["category"])
        self.assertEqual("ok", direct["status"])
        self.assertEqual(0, len(default_provider.calls))
        self.assertEqual(1, len(deepseek_provider.calls))
        self.assertEqual(["on"], deepseek_provider.reasoning_modes)
        self.assertEqual(0, len(runner.calls))

    def test_fast_middle_spark_is_opt_in_agent_only_and_does_not_change_the_local_default(self):
        registry = BackendRegistry.load()

        default = registry.resolve(None)
        spark = registry.resolve("fast-middle-agent")

        self.assertEqual("local-default", default.backend_id)
        self.assertFalse(default.config["cloud"])
        self.assertEqual(
            "max",
            default.config["agent_routes"]["data_factory"]["reasoning_effort"],
        )
        self.assertEqual("fast-middle-agent", spark.backend_id)
        self.assertEqual("agent-only", spark.config["adapter"])
        self.assertTrue(spark.config["cloud"])
        self.assertFalse(spark.config["supports_vision"])
        self.assertEqual("gpt-5.3-codex-spark", spark.config["model"])
        self.assertEqual("latency_crosscheck", spark.config["routing_role"])
        route = spark.config["agent_routes"]["data_factory"]
        self.assertEqual("codex-cli", route["runner"])
        self.assertEqual("codex-spark-xhigh", route["profile"])
        self.assertEqual("gpt-5.3-codex-spark", route["model"])
        self.assertEqual("xhigh", route["reasoning_effort"])
        catalog_roles = {
            item["id"]: item["routing_role"]
            for item in registry.catalog()["backends"]
        }
        self.assertEqual("crosscheck_only", catalog_roles["local-crosscheck-27b"])
        self.assertEqual("latency_crosscheck", catalog_roles["fast-middle-agent"])

    def test_default_registry_uses_the_frozen_quality_profile_and_keeps_hard_role(self):
        registry = BackendRegistry.load()

        default = registry.resolve(None)
        hard = registry.resolve("local-hard-reasoning")

        self.assertEqual("local-default", default.backend_id)
        self.assertEqual("on", default.config["default_reasoning_mode"])
        self.assertEqual(
            {
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "repeat_penalty": 1.0,
                "num_ctx": 262144,
                "num_predict": 32768,
            },
            default.config["ollama_options"],
        )
        self.assertEqual("qwen-main-v1", hard.config["model"])
        self.assertFalse(hard.config["cloud"])
        self.assertEqual("on", hard.config["required_reasoning_mode"])
        self.assertEqual(
            {
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "repeat_penalty": 1.0,
                "num_ctx": 262144,
                "num_predict": 32768,
            },
            hard.config["ollama_options"],
        )
        catalog_entry = next(
            item for item in registry.catalog()["backends"] if item["id"] == "local-hard-reasoning"
        )
        self.assertEqual("on", catalog_entry["required_reasoning_mode"])

    def test_default_backend_and_legacy_alias_resolve_without_code_changes(self):
        registry = BackendRegistry.from_dict(registry_data())

        default = registry.resolve(None)
        legacy = registry.resolve("qwen-main-v1")

        self.assertEqual("local-default", default.backend_id)
        self.assertTrue(default.default_applied)
        self.assertEqual("local-default", legacy.backend_id)
        self.assertEqual("qwen-main-v1", legacy.requested)
        self.assertEqual(262144, default.config["context_window_tokens"])

    def test_registry_rejects_invalid_context_window_tokens(self):
        for invalid_value in (True, 0, 1023, 1_048_577, "262144"):
            with self.subTest(invalid_value=invalid_value):
                data = registry_data()
                data["backends"]["local-default"]["context_window_tokens"] = invalid_value
                with self.assertRaisesRegex(ValueError, "context_window_tokens"):
                    BackendRegistry.from_dict(data)

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

    def test_registry_rejects_ollama_options_for_cloud_or_non_ollama_backends(self):
        data = registry_data()
        data["backends"]["cloud"] = {
            "adapter": "openai-chat",
            "model": "remote",
            "cloud": True,
            "ollama_options": {"temperature": 1.0},
        }

        with self.assertRaisesRegex(ValueError, "local ollama"):
            BackendRegistry.from_dict(data)

    def test_registry_rejects_unknown_or_out_of_range_ollama_options(self):
        for invalid_options in (
            {"seed": 7},
            {"temperature": 3.0},
            {"top_p": "0.95"},
            {"top_k": True},
            {"num_ctx": 0},
            {"num_predict": 0},
        ):
            with self.subTest(invalid_options=invalid_options):
                data = registry_data()
                data["backends"]["local-default"]["ollama_options"] = invalid_options
                with self.assertRaisesRegex(ValueError, "ollama_options"):
                    BackendRegistry.from_dict(data)

    def test_registry_rejects_invalid_required_reasoning_mode(self):
        for invalid_mode in ("auto", True, 1, {}):
            with self.subTest(invalid_mode=invalid_mode):
                data = registry_data()
                data["backends"]["local-default"]["required_reasoning_mode"] = invalid_mode
                with self.assertRaisesRegex(ValueError, "required_reasoning_mode"):
                    BackendRegistry.from_dict(data)

    def test_registry_validates_generic_nested_reasoning_request_mapping(self):
        valid = registry_data()
        valid["backends"]["remote"] = {
            "adapter": "openai-chat",
            "model": "remote-model",
            "cloud": True,
            "base_url_default": "https://api.example.invalid",
            "api_key_env": "REMOTE_API_KEY",
            "reasoning_request": {
                "path": ["thinking", "type"],
                "on": "enabled",
                "off": "disabled",
            },
            "agent_routes": {},
        }
        registry = BackendRegistry.from_dict(valid)

        self.assertEqual(
            ["thinking", "type"],
            registry.resolve("remote").config["reasoning_request"]["path"],
        )

        invalid_mappings = (
            None,
            {},
            {"path": "thinking.type", "on": "enabled", "off": "disabled"},
            {"path": [], "on": "enabled", "off": "disabled"},
            {"path": ["messages", "content"], "on": "enabled", "off": "disabled"},
            {"path": ["thinking", "bad field"], "on": "enabled", "off": "disabled"},
            {"path": ["thinking", "type"], "on": {}, "off": "disabled"},
        )
        for invalid_mapping in invalid_mappings:
            with self.subTest(invalid_mapping=invalid_mapping):
                data = json.loads(json.dumps(valid))
                data["backends"]["remote"]["reasoning_request"] = invalid_mapping
                with self.assertRaisesRegex(ValueError, "reasoning_request"):
                    BackendRegistry.from_dict(data)

    def test_crosscheck_only_role_cannot_be_default_or_define_agent_routes(self):
        with_agent_route = registry_data()
        with_agent_route["backends"]["local-default"]["routing_role"] = "crosscheck_only"
        with self.assertRaisesRegex(ValueError, "crosscheck_only.*agent_routes"):
            BackendRegistry.from_dict(with_agent_route)

        as_default = registry_data()
        as_default["backends"]["local-default"]["routing_role"] = "crosscheck_only"
        as_default["backends"]["local-default"]["agent_routes"] = {}
        with self.assertRaisesRegex(ValueError, "crosscheck_only.*default_backend"):
            BackendRegistry.from_dict(as_default)

    def test_direct_only_role_cannot_define_agent_routes(self):
        data = registry_data()
        remote = json.loads(json.dumps(data["backends"]["local-default"]))
        remote.update(
            {
                "adapter": "openai-chat",
                "model": "remote-model",
                "cloud": True,
                "routing_role": "direct_only",
            }
        )
        data["backends"]["remote"] = remote

        with self.assertRaisesRegex(ValueError, "direct_only.*agent_routes"):
            BackendRegistry.from_dict(data)

    def test_registry_rejects_invalid_routing_role(self):
        for invalid_role in ("", "contains spaces", True, 1, {}):
            with self.subTest(invalid_role=invalid_role):
                data = registry_data()
                data["backends"]["local-default"]["routing_role"] = invalid_role
                with self.assertRaisesRegex(ValueError, "routing_role"):
                    BackendRegistry.from_dict(data)

    def test_backend_required_reasoning_mode_fails_before_provider_invocation(self):
        data = registry_data()
        hard_config = json.loads(json.dumps(data["backends"]["local-default"]))
        hard_config["required_reasoning_mode"] = "on"
        data["backends"]["local-hard-reasoning"] = hard_config
        default_provider = ReplacementProvider()
        hard_provider = ReplacementProvider()
        toolkit = Toolkit(
            registry=BackendRegistry.from_dict(data),
            providers={
                "local-default": default_provider,
                "local-hard-reasoning": hard_provider,
            },
            runners={},
        )

        blocked = toolkit.invoke(
            {
                "backend": "local-hard-reasoning",
                "task": {
                    "goal": "return json",
                    "sources": [
                        {
                            "id": "must-not-be-read",
                            "path": "C:/definitely-not-present/toolkit-required-reasoning.txt",
                        }
                    ],
                    "expected_output": {"format": "json"},
                },
                "reasoning": {"mode": "off"},
            }
        )
        accepted = toolkit.invoke(
            {
                "backend": "local-hard-reasoning",
                "task": {"goal": "return json", "expected_output": {"format": "json"}},
                "reasoning": {"mode": "on"},
            }
        )

        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("invalid_request", blocked["error"]["category"])
        self.assertIn("requires reasoning.mode=on", blocked["error"]["summary"])
        self.assertEqual("local-hard-reasoning", blocked["backend"]["resolved"])
        self.assertEqual("ok", accepted["status"])
        self.assertEqual(1, len(hard_provider.calls))

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
