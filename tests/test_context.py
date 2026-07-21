import unittest

from llm_backend_toolkit.context import ContextOverflow, compact_task


class ContextCompactorTests(unittest.TestCase):
    def test_cjk_estimate_is_conservative_and_compaction_honors_token_target(self):
        request = {
            "task": {"goal": "总结", "inputs": ["甲" * 1000]},
            "context": {"mode": "compact", "target_tokens": 160},
        }

        result = compact_task(request)

        self.assertGreater(result.receipt["estimated_tokens_before"], 900)
        self.assertLessEqual(result.receipt["estimated_tokens_after"], 160)
        self.assertTrue(result.receipt["lossy"])

    def test_compact_is_default_deduplicates_and_reports_receipt(self):
        request = {
            "task": {
                "goal": "Return a concise result",
                "instructions": ["Preserve evidence", "Preserve evidence"],
                "inputs": ["alpha", "alpha", "beta"],
                "expected_output": {"format": "text"},
            },
            "context": {"pinned": ["Never invent facts"]},
        }

        compacted = compact_task(request)

        self.assertEqual("compact", compacted.receipt["mode"])
        self.assertTrue(compacted.receipt["executed"])
        self.assertTrue(compacted.receipt["applied"])
        self.assertFalse(compacted.receipt["lossy"])
        self.assertEqual(2, compacted.receipt["duplicates_removed"])
        self.assertIn("Never invent facts", compacted.prompt)
        self.assertEqual(1, compacted.prompt.count("Preserve evidence"))

    def test_long_unpinned_input_is_clipped_with_hash_and_lossy_receipt(self):
        request = {
            "task": {
                "goal": "Summarize",
                "instructions": [],
                "inputs": ["A" * 5000],
                "expected_output": {"format": "text"},
            },
            "context": {"mode": "compact", "target_tokens": 256, "pinned": []},
        }

        compacted = compact_task(request)

        self.assertTrue(compacted.receipt["lossy"])
        self.assertGreater(compacted.receipt["estimated_tokens_before"], compacted.receipt["estimated_tokens_after"])
        self.assertIn("sha256:", compacted.prompt)
        self.assertLessEqual(compacted.receipt["estimated_tokens_after"], 256)

    def test_pinned_content_is_never_silently_clipped(self):
        request = {
            "task": {"goal": "g", "instructions": [], "inputs": [], "expected_output": {}},
            "context": {"mode": "compact", "target_tokens": 16, "pinned": ["P" * 1000]},
        }

        with self.assertRaises(ContextOverflow):
            compact_task(request)

    def test_passthrough_reports_that_compaction_did_not_run(self):
        request = {
            "task": {"goal": "g", "instructions": [], "inputs": ["x"], "expected_output": {}},
            "context": {"mode": "passthrough"},
        }

        compacted = compact_task(request)

        self.assertEqual("passthrough", compacted.receipt["mode"])
        self.assertFalse(compacted.receipt["executed"])
        self.assertFalse(compacted.receipt["applied"])


if __name__ == "__main__":
    unittest.main()
