import importlib.util
import json
import sys
import unittest
from pathlib import Path

from llm_backend_toolkit.backends import BackendRegistry


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "benchmarks" / "local_qwen_quality_v1.py"
SPEC = importlib.util.spec_from_file_location("local_qwen_quality_v1", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LocalQwenQualityBenchmarkTests(unittest.TestCase):
    def test_suite_separates_calibration_from_unseen_holdout(self):
        cases = MODULE.build_cases()

        self.assertEqual(8, len([case for case in cases if case.phase == "calibration"]))
        self.assertEqual(14, len([case for case in cases if case.phase == "holdout"]))
        self.assertEqual(len(cases), len({case.case_id for case in cases}))

    def test_official_hybrid_uses_model_specific_thinking_and_precise_presets(self):
        general = next(
            case
            for case in MODULE.build_cases()
            if case.case_id == "cal_versioned_state"
        )
        precise = next(
            case
            for case in MODULE.build_cases()
            if case.case_id == "holdout_python_mutable_default"
        )
        nonthinking = next(
            case
            for case in MODULE.build_cases()
            if case.case_id == "holdout_strict_nested_json"
        )

        dense = MODULE.options_for(
            "qwen-review-v1", general, "official-hybrid", seed=7
        )
        moe = MODULE.options_for(
            "qwen-main-v1", general, "official-hybrid", seed=7
        )
        code = MODULE.options_for(
            "qwen-main-v1", precise, "official-hybrid", seed=7
        )
        fast = MODULE.options_for(
            "qwen-main-v1", nonthinking, "official-hybrid", seed=7
        )

        self.assertEqual(0.0, dense["presence_penalty"])
        self.assertEqual(1.5, moe["presence_penalty"])
        self.assertEqual(0.6, code["temperature"])
        self.assertEqual(0.0, code["presence_penalty"])
        self.assertEqual(0.7, fast["temperature"])
        self.assertEqual(0.8, fast["top_p"])
        self.assertEqual(1.5, fast["presence_penalty"])
        self.assertEqual(7, fast["seed"])

    def test_quality_precise_forces_thinking_with_conservative_sampling(self):
        nonthinking = next(
            case
            for case in MODULE.build_cases()
            if case.case_id == "cal_transport_intent"
        )

        options = MODULE.options_for(
            "qwen-review-v1", nonthinking, "quality-precise", seed=11
        )

        self.assertFalse(MODULE.thinking_for(nonthinking, "current-alias"))
        self.assertTrue(MODULE.thinking_for(nonthinking, "quality-precise"))
        self.assertEqual(0.6, options["temperature"])
        self.assertEqual(0.95, options["top_p"])
        self.assertEqual(0.0, options["presence_penalty"])
        self.assertEqual(32768, options["num_predict"])

    def test_reference_answers_receive_full_score_and_near_misses_do_not(self):
        for case in MODULE.build_cases():
            with self.subTest(case=case.case_id):
                score, _ = case.validator(case.reference_message)
                self.assertEqual(2, score)
                score, _ = case.validator({"content": "WRONG", "tool_calls": []})
                self.assertLess(score, 2)

    def test_loopback_endpoint_rejects_internal_or_remote_targets(self):
        self.assertEqual(
            "http://127.0.0.1:32100",
            MODULE.validate_endpoint("http://127.0.0.1:32100/"),
        )
        for endpoint in (
            "http://127.0.0.1:32101",
            "http://example.com:32100",
            "https://127.0.0.1:32100",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                MODULE.validate_endpoint(endpoint)

    def test_summary_keeps_quality_ahead_of_speed(self):
        rows = [
            {
                "model": "slow-right",
                "preset": "p",
                "result": {
                    "status": "ok",
                    "score": 2,
                    "score_max": 2,
                    "wall_ms": 9000,
                    "eval_count": 10,
                    "eval_duration_ns": 1_000_000_000,
                },
            },
            {
                "model": "fast-wrong",
                "preset": "p",
                "result": {
                    "status": "ok",
                    "score": 1,
                    "score_max": 2,
                    "wall_ms": 100,
                    "eval_count": 10,
                    "eval_duration_ns": 100_000_000,
                },
            },
        ]

        summary = MODULE.summarize(rows)

        self.assertEqual("slow-right", summary["ranking"][0]["model"])
        self.assertEqual(2, summary["by_candidate"]["slow-right|p"]["score"])
        json.dumps(summary)

    def test_candidate_registry_exposes_27b_as_non_primary_direct_crosscheck(self):
        path = (
            ROOT
            / "benchmarks"
            / "local_qwen_quality_v1"
            / "agent-candidate-registry.json"
        )
        registry = BackendRegistry.load(path)
        default = registry.resolve(None)
        candidate = registry.resolve("local-crosscheck-27b")
        model_alias = registry.resolve("qwen-review-v1")

        self.assertEqual("local-default", registry.default_backend)
        self.assertEqual("qwen-main-v1", default.config["model"])
        self.assertEqual("local-crosscheck-27b", model_alias.backend_id)
        self.assertFalse(candidate.config["cloud"])
        self.assertEqual("qwen-review-v1", candidate.config["model"])
        self.assertEqual("crosscheck_only", candidate.config["routing_role"])
        self.assertEqual(
            "crosscheck_available_not_primary",
            candidate.config["evaluation_state"],
        )
        self.assertEqual("on", candidate.config["default_reasoning_mode"])
        self.assertEqual(131_072, candidate.config["context_window_tokens"])
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
            candidate.config["ollama_options"],
        )
        self.assertEqual({}, candidate.config["agent_routes"])


if __name__ == "__main__":
    unittest.main()
