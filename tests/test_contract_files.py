import json
import re
import tomllib
import unittest
from pathlib import Path

from llm_backend_toolkit import __version__


ROOT = Path(__file__).resolve().parents[1]


class ContractFileTests(unittest.TestCase):
    def test_package_versions_match(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(project["project"]["version"], __version__)

    def test_schemas_and_example_are_valid_json(self):
        request_schema = json.loads((ROOT / "schemas" / "request.schema.json").read_text(encoding="utf-8"))
        response_schema = json.loads((ROOT / "schemas" / "response.schema.json").read_text(encoding="utf-8"))
        acceptance_schema = json.loads(
            (ROOT / "schemas" / "aicli-agent-acceptance-receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        registry = json.loads(
            (ROOT / "src" / "llm_backend_toolkit" / "default_backends.json").read_text(
                encoding="utf-8"
            )
        )
        example = json.loads((ROOT / "examples" / "local-request.json").read_text(encoding="utf-8"))
        cloud_direct = json.loads((ROOT / "examples" / "cloud-direct-request.json").read_text(encoding="utf-8"))
        fast_middle = json.loads(
            (ROOT / "examples" / "fast-middle-agent-request.json").read_text(encoding="utf-8")
        )

        self.assertEqual("string", request_schema["properties"]["backend"]["type"])
        self.assertNotIn("enum", request_schema["properties"]["backend"])
        self.assertNotIn("provider", request_schema["required"])
        execution = request_schema["properties"]["execution"]
        self.assertEqual(["direct", "agent"], execution["properties"]["mode"]["enum"])
        self.assertEqual(
            "danger-full-access",
            execution["properties"]["policy"]["default"],
        )
        self.assertEqual(
            ["read-only", "workspace-write", "danger-full-access"],
            execution["properties"]["policy"]["enum"],
        )
        self.assertNotIn("enum", execution["properties"]["runner"])
        self.assertIn("pattern", execution["properties"]["runner"])
        cache_key = execution["properties"]["cache_key"]
        self.assertEqual(512, cache_key["maxLength"])
        self.assertIn("pattern", cache_key)
        self.assertIn("execution_receipt", response_schema["properties"])
        self.assertEqual(
            "aicli.agent.acceptance-receipt.v1",
            acceptance_schema["properties"]["receipt_schema"]["const"],
        )
        self.assertEqual(
            {"requested", "effective", "attested"},
            set(acceptance_schema["required"]) & {"requested", "effective", "attested"},
        )
        self.assertEqual(
            "aicli.agent.acceptance-receipt.v1",
            registry["acceptance_contract"]["receipt_schema"],
        )
        self.assertFalse(
            registry["acceptance_contract"]["stability_required_for_initial_capability"]
        )
        self.assertFalse(
            registry["acceptance_contract"]["stress_required_for_initial_capability"]
        )
        reserved_plus = registry["acceptance_contract"]["reserved_routes"][
            "codex-qwen3-7-plus-paygo"
        ]
        self.assertEqual("qwen3.7-plus", reserved_plus["model"])
        self.assertEqual("unverified", reserved_plus["state"])
        self.assertFalse(reserved_plus["selectable"])
        local_evidence = registry["backends"]["local-default"]["agent_routes"][
            "codex-cli"
        ]["evidence"]
        self.assertEqual("historical", local_evidence["evidence_state"])
        self.assertEqual(
            "codex-ollama-qwen3-8-27b",
            registry["backends"]["local-default"]["agent_routes"]["codex-cli"]["profile"],
        )
        self.assertEqual(
            "aicli-qwen3.8-27b-256k:2026-08-14",
            registry["backends"]["local-default"]["model"],
        )
        self.assertEqual("not_required", local_evidence["stability_evidence"])
        reserved_qwen38 = registry["acceptance_contract"]["reserved_routes"][
            "codex-qwen3-8-max-paygo"
        ]
        self.assertEqual("unverified", reserved_qwen38["state"])
        self.assertFalse(reserved_qwen38["selectable"])
        self.assertEqual(
            "aicli_profile_does_not_activate_toolkit_route",
            reserved_qwen38["reason"],
        )
        self.assertIn("cache_identity", response_schema["properties"])
        cache_identities = response_schema["properties"]["cache_identity"][
            "oneOf"
        ]
        explicit_identity = next(
            value
            for value in cache_identities
            if value["properties"]["mode"]["const"] == "explicit"
            and value["properties"]["schema"]["const"].endswith(".v2")
        )
        request_digest_identity = next(
            value
            for value in cache_identities
            if value["properties"]["mode"]["const"] == "request_digest"
            and value["properties"]["schema"]["const"].endswith(".v2")
        )
        self.assertEqual(
            "llm-backend-toolkit.explicit-cache-identity.v2",
            explicit_identity["properties"]["schema"]["const"],
        )
        self.assertIn(
            "caller_cache_key_hash",
            explicit_identity["required"],
        )
        self.assertEqual(
            set(explicit_identity["required"]),
            set(explicit_identity["properties"]),
        )
        self.assertNotIn(
            "caller_cache_key",
            explicit_identity["properties"],
        )
        self.assertEqual(
            "stdlib-json-sort-compact-utf8-v1",
            explicit_identity["properties"]["canonicalization"]["const"],
        )
        self.assertEqual(
            set(request_digest_identity["required"]),
            set(request_digest_identity["properties"]),
        )
        self.assertEqual(
            "stdlib-json-sort-compact-utf8-v1",
            request_digest_identity["properties"]["canonicalization"][
                "const"
            ],
        )
        legacy_identities = [
            value
            for value in cache_identities
            if value["properties"]["schema"]["const"].endswith(".v1")
        ]
        self.assertEqual(2, len(legacy_identities))
        self.assertTrue(
            all(value.get("deprecated") is True for value in legacy_identities)
        )
        self.assertTrue(
            all(
                set(value["required"]) == set(value["properties"])
                for value in legacy_identities
            )
        )
        self.assertIn("accepted", response_schema["properties"]["status"]["enum"])
        self.assertEqual("local-default", example["backend"])
        self.assertEqual("off", example["reasoning"]["mode"])
        self.assertFalse((ROOT / "examples" / "cloud-agent-request.json").exists())
        self.assertEqual("cloud-qwen-flash", cloud_direct["backend"])
        self.assertTrue(cloud_direct["privacy"]["cloud_allowed"])
        self.assertEqual("direct", cloud_direct["execution"]["mode"])
        self.assertEqual("fast-middle-agent", fast_middle["backend"])
        self.assertTrue(fast_middle["privacy"]["cloud_allowed"])
        self.assertEqual("read-only", fast_middle["execution"]["policy"])
        self.assertEqual("data_factory", fast_middle["execution"]["runner"])

    def test_public_files_do_not_contain_secret_like_values(self):
        forbidden = (
            re.compile(r"(?i)authorization\s*:\s*bearer\s+ghp_[A-Za-z0-9]{20,}"),
            re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
            re.compile(r'"apiKey"\s*:\s*"[^"\n]{8,}"'),
        )
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or ".venv" in path.parts:
                continue
            if path.suffix.lower() not in {".py", ".md", ".json", ".toml", ".txt"} and path.name not in {"LICENSE"}:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden:
                self.assertIsNone(pattern.search(text), f"secret-like token in {path}")

    def test_worker_contract_exposes_only_two_exact_route_shapes(self):
        worker_schema = json.loads(
            (ROOT / "schemas" / "worker-contract.schema.json").read_text(
                encoding="utf-8"
            )
        )
        route_contracts = worker_schema["oneOf"]
        self.assertEqual(
            [
                "legacy-local-default-data-factory",
                "benchmark-only-exact-codex-cli",
            ],
            [contract["title"] for contract in route_contracts],
        )

        local_contract, benchmark_contract = route_contracts
        local_request = local_contract["properties"]["request"]
        self.assertEqual(
            "local-default",
            local_request["properties"]["backend"]["const"],
        )
        self.assertEqual(
            "data_factory",
            local_request["properties"]["execution"]["properties"]["runner"][
                "const"
            ],
        )
        self.assertEqual(
            "local-default",
            local_contract["properties"]["bindings"]["properties"][
                "backend_alias"
            ]["const"],
        )

        benchmark_request = benchmark_contract["properties"]["request"]
        self.assertEqual(
            "local-default",
            benchmark_request["properties"]["backend"]["not"]["const"],
        )
        benchmark_execution = benchmark_request["properties"]["execution"]
        self.assertEqual(
            "codex-cli",
            benchmark_execution["properties"]["runner"]["const"],
        )
        self.assertEqual(
            "workspace-write",
            benchmark_execution["properties"]["policy"]["const"],
        )
        benchmark_budget = benchmark_execution["properties"]["budget"]
        self.assertEqual(
            "watchdog_only",
            benchmark_budget["properties"]["limit_mode"]["const"],
        )
        self.assertEqual(
            7200,
            benchmark_budget["properties"]["timeout_seconds"]["const"],
        )
        benchmark_constraints = benchmark_contract["properties"]["constraints"]
        self.assertEqual(
            {"network", "search"},
            set(benchmark_constraints["required"]),
        )
        self.assertEqual(
            "forbidden",
            benchmark_constraints["properties"]["network"]["const"],
        )
        self.assertEqual(
            "disabled",
            benchmark_constraints["properties"]["search"]["const"],
        )
        benchmark_bindings = benchmark_contract["properties"]["bindings"]
        self.assertIn("fallback_used", benchmark_bindings["required"])
        self.assertFalse(
            benchmark_bindings["properties"]["fallback_used"]["const"]
        )
        self.assertEqual(
            "responses",
            benchmark_bindings["properties"]["wire"]["const"],
        )
        self.assertIn("routing_role=benchmark_only", benchmark_contract["description"])
        self.assertIn("cloud=false", benchmark_contract["description"])

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        worker_doc = (ROOT / "docs" / "local-async-worker-contract.md").read_text(
            encoding="utf-8"
        )
        for text in (readme, worker_doc):
            self.assertIn("benchmark_only", text)
            self.assertIn("codex-cli", text)
            self.assertIn("no fallback", text.lower())

    def test_current_model_docs_do_not_promote_superseded_35b_default(self):
        crosscheck = (ROOT / "docs" / "local-crosscheck-27b.md").read_text(
            encoding="utf-8"
        )
        fast_middle = (ROOT / "docs" / "fast-middle-agent.md").read_text(
            encoding="utf-8"
        )
        benchmark = (ROOT / "docs" / "general-agent-benchmark-v1.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("当前 Qwen3.8 27B 的 `local-default`", crosscheck)
        self.assertNotIn("省略 `backend` 仍解析到 35B `local-default`", crosscheck)
        self.assertIn("默认使用当前本地 Qwen3.8 27B", fast_middle)
        self.assertNotIn("默认使用本地 35B 或更高等级 Codex", fast_middle)
        self.assertIn("HISTORICAL / SUPERSEDED", benchmark)
        self.assertIn("不构成当前默认", benchmark)

        data_factory = (ROOT / "docs" / "agent-data-factory.md").read_text(
            encoding="utf-8"
        )
        investigation = (
            ROOT / "docs" / "local-qwen-four-agent-investigation-2026-07-29.md"
        ).read_text(encoding="utf-8")
        qwen_report = (
            ROOT / "docs" / "qwen3.7-flash-vs-plus-report-2026-07-28.md"
        ).read_text(encoding="utf-8")
        self.assertIn("历史/已被当前 registry superseded", data_factory)
        self.assertIn("历史环境下适合交给当时本地 35B", data_factory)
        self.assertIn("历史结论（不作为当前路由依据）", investigation)
        self.assertIn("历史结论（不作为当前路由依据）", qwen_report)
        self.assertIn("当时默认模型：`local-default`", qwen_report)


if __name__ == "__main__":
    unittest.main()
