import unittest

from llm_backend_toolkit.cli import _exit_code, build_parser


class CliContractTests(unittest.TestCase):
    def test_async_receipts_are_successful_cli_outcomes(self):
        self.assertEqual(0, _exit_code({"status": "accepted"}))
        self.assertEqual(0, _exit_code({"status": "cache_hit"}))

    def test_probe_accepts_job_store_options(self):
        args = build_parser().parse_args(
            [
                "probe",
                "--case",
                "json",
                "--state-dir",
                "C:/jobs",
                "--force",
                "--cloud-allowed",
            ]
        )
        self.assertEqual("C:/jobs", args.state_dir)
        self.assertTrue(args.force)
        self.assertTrue(args.cloud_allowed)


if __name__ == "__main__":
    unittest.main()
