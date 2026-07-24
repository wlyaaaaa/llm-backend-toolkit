import unittest

from llm_backend_toolkit.errors import classify_agent_process_error, classify_provider_error


class ErrorClassificationTests(unittest.TestCase):
    def test_billing_codes_are_normalized_without_selecting_a_fallback(self):
        for status, code in ((400, "Arrearage"), (403, "AllocationQuota.FreeTierOnly")):
            with self.subTest(code=code):
                error = classify_provider_error(status, {"error": {"code": code, "message": "billing"}})
                self.assertEqual("billing_unavailable", error.category)
                self.assertFalse(error.retryable)
                self.assertEqual("top_model", error.decision_owner)
                self.assertIn("invoke:local-default", error.options)

    def test_auth_and_content_errors_do_not_masquerade_as_billing(self):
        auth = classify_provider_error(401, {"error": {"code": "invalid_api_key"}})
        content = classify_provider_error(400, {"error": {"code": "DataInspectionFailed"}})

        self.assertEqual("authentication_failed", auth.category)
        self.assertEqual("content_rejected", content.category)

    def test_gpu_conflict_is_reported_as_busy(self):
        error = classify_provider_error(409, {"reason": "gpu_lease_active", "owner": "localocr"})

        self.assertEqual("gpu_busy", error.category)
        self.assertTrue(error.retryable)

    def test_spark_quota_errors_offer_a_visible_local_handoff_without_auto_fallback(self):
        for detail in (
            "HTTP 429 rate limit exceeded",
            "usage limit reached for gpt-5.3-codex-spark",
            "insufficient_quota",
            "RATE_LIMIT_EXCEEDED",
        ):
            with self.subTest(detail=detail):
                error = classify_agent_process_error(detail)
                self.assertEqual("rate_limited", error.category)
                self.assertEqual("top_model", error.decision_owner)
                self.assertIn("invoke:local-default", error.options)
                self.assertIn("retry-later", error.options)


if __name__ == "__main__":
    unittest.main()
