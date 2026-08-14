import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_backend_toolkit.cli import (
    _exit_code,
    _probe_request,
    _recorded_invoke,
    build_parser,
    main,
)
from llm_backend_toolkit.jobs import JobStore


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

    def test_invoke_accepts_the_observer_job_store(self):
        args = build_parser().parse_args(
            ["invoke", "--request", "request.json", "--state-dir", "C:/jobs"]
        )
        self.assertEqual("C:/jobs", args.state_dir)

    def test_invoke_records_one_job_before_returning_the_original_result(self):
        class FakeStore:
            instance = None

            def __init__(self, root=None, *, spawner=None):
                self.root = root
                self.spawner = spawner
                self.submitted = []
                self.collected = []
                FakeStore.instance = self

            def submit(self, request, *, force=False):
                self.submitted.append((request, force))
                return {"status": "accepted", "job_id": "recorded-job"}

            def collect(self, job_id, *, full_result=False):
                self.collected.append((job_id, full_result))
                return {
                    "status": "ok",
                    "job_id": job_id,
                    "job_status": "completed",
                    "result": {"status": "ok", "output": "RECORDED_OK"},
                }

        with tempfile.TemporaryDirectory() as temporary:
            request_path = Path(temporary) / "request.json"
            request = {
                "task": {
                    "goal": "Return exactly RECORDED_OK.",
                    "expected_output": {"format": "text"},
                }
            }
            request_path.write_text(json.dumps(request), encoding="utf-8")
            output = io.StringIO()
            with (
                patch("llm_backend_toolkit.cli.JobStore", FakeStore),
                patch("llm_backend_toolkit.cli._execute_job") as execute_job,
                patch("sys.stdout", output),
            ):
                exit_code = main(
                    [
                        "invoke",
                        "--request",
                        str(request_path),
                        "--state-dir",
                        str(Path(temporary) / "jobs"),
                    ]
                )

        self.assertEqual(0, exit_code)
        self.assertEqual({"status": "ok", "output": "RECORDED_OK"}, json.loads(output.getvalue()))
        self.assertIsNotNone(FakeStore.instance.spawner)
        self.assertEqual([(request, True)], FakeStore.instance.submitted)
        execute_job.assert_called_once_with(FakeStore.instance, "recorded-job")
        self.assertEqual([("recorded-job", True)], FakeStore.instance.collected)

    def test_recorded_invoke_persists_progress_and_the_terminal_result(self):
        class FakeToolkit:
            def __init__(self, *, registry=None):
                self.registry = registry

            def invoke(self, request, *, progress_callback=None):
                self.assert_request = request
                progress_callback({"phase": "generating", "content_delta": "RECORDED_"})
                progress_callback({"phase": "generating", "content_delta": "OK"})
                progress_callback({"phase": "completed", "content_replace": "RECORDED_OK"})
                return {"status": "ok", "output": "RECORDED_OK"}

        request = {
            "task": {
                "goal": "Return exactly RECORDED_OK.",
                "expected_output": {"format": "text"},
            }
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "llm_backend_toolkit.cli.Toolkit", FakeToolkit
        ):
            result = _recorded_invoke(request, temporary)
            job_directories = [
                path
                for path in Path(temporary).iterdir()
                if path.is_dir() and (path / "state.json").is_file()
            ]
            self.assertEqual(1, len(job_directories))
            job_id = job_directories[0].name
            persisted = JobStore(temporary).collect(job_id, full_result=True)
            progress = json.loads(
                (job_directories[0] / "progress.json").read_text(encoding="utf-8")
            )

        self.assertEqual({"status": "ok", "output": "RECORDED_OK"}, result)
        self.assertEqual("completed", persisted["job_status"])
        self.assertEqual("RECORDED_OK", persisted["result"]["output"])
        self.assertEqual("RECORDED_OK", progress["public_preview"])

    def test_probe_defers_to_the_selected_backend_reasoning_default(self):
        request = _probe_request(None, "json", None, cloud_allowed=False)

        self.assertNotIn("reasoning", request)

    def test_status_accepts_an_arbitrary_backend_id(self):
        args = build_parser().parse_args(["status", "--backend", "future-platform-v2"])
        self.assertEqual("future-platform-v2", args.backend)

    def test_backend_catalog_is_a_first_class_metadata_command(self):
        args = build_parser().parse_args(["backends"])
        self.assertEqual("backends", args.command)


if __name__ == "__main__":
    unittest.main()
