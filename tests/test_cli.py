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

    def test_status_accepts_an_arbitrary_backend_id(self):
        args = build_parser().parse_args(["status", "--backend", "future-platform-v2"])
        self.assertEqual("future-platform-v2", args.backend)

    def test_backend_catalog_is_a_first_class_metadata_command(self):
        args = build_parser().parse_args(["backends"])
        self.assertEqual("backends", args.command)


if __name__ == "__main__":
    unittest.main()
