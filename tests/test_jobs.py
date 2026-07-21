import json
import tempfile
import time
import unittest
from pathlib import Path

from llm_backend_toolkit.jobs import JobStore


class JobStoreTests(unittest.TestCase):
    def test_submit_returns_immediately_and_spawns_one_background_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            spawned = []
            store = JobStore(Path(temp), spawner=lambda job_id, root: spawned.append((job_id, root)))
            request = {"provider": "qwen-main-v1", "task": {"goal": "g"}}

            started = time.monotonic()
            receipt = store.submit(request)
            elapsed = time.monotonic() - started

            self.assertEqual("accepted", receipt["status"])
            self.assertLess(elapsed, 1.0)
            self.assertTrue(receipt["monitor_until_utc"].endswith("Z"))
            self.assertEqual(1, len(spawned))
            self.assertEqual(receipt["job_id"], spawned[0][0])
            state = store.get(receipt["job_id"])
            self.assertEqual("queued", state["job_status"])

    def test_completed_identical_request_is_a_cache_hit(self):
        with tempfile.TemporaryDirectory() as temp:
            spawned = []
            store = JobStore(Path(temp), spawner=lambda job_id, root: spawned.append(job_id))
            request = {"provider": "qwen-main-v1", "task": {"goal": "g"}}
            first = store.submit(request)
            store.complete(first["job_id"], {"status": "ok", "output": "done"})

            second = store.submit(request)

            self.assertEqual("cache_hit", second["status"])
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertEqual(1, len(spawned))

    def test_worker_claim_is_atomic_and_request_is_removed_after_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"provider": "qwen-main-v1", "task": {"goal": "g"}})

            request = store.claim(receipt["job_id"])
            store.complete(receipt["job_id"], {"status": "ok", "output": "done"})

            self.assertEqual("g", request["task"]["goal"])
            job_dir = Path(temp) / receipt["job_id"]
            self.assertFalse((job_dir / "request.json").exists())
            result = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual("done", result["output"])

    def test_get_returns_compact_state_and_result_only_after_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"provider": "qwen-main-v1", "task": {"goal": "g"}})

            queued = store.get(receipt["job_id"])
            store.complete(receipt["job_id"], {"status": "ok", "output": {"answer": 56}})
            completed = store.get(receipt["job_id"], include_result=True)

            self.assertNotIn("request", queued)
            self.assertEqual("completed", completed["job_status"])
            self.assertEqual({"answer": 56}, completed["result"]["output"])

    def test_long_output_is_externalized_from_the_default_result_view(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None, result_preview_chars=80)
            receipt = store.submit({"provider": "qwen-main-v1", "task": {"goal": "g"}})
            store.complete(receipt["job_id"], {"status": "ok", "output": "Z" * 1000, "checks": []})

            compact = store.get(receipt["job_id"], include_result=True)
            full = store.get(receipt["job_id"], include_result=True, full_result=True)

            output = compact["result"]["output"]
            self.assertEqual("artifact", output["type"])
            self.assertEqual(1000, output["chars"])
            self.assertLessEqual(len(output["preview"]), 80)
            self.assertTrue(Path(output["path"]).is_file())
            self.assertEqual("Z" * 1000, full["result"]["output"])

    def test_force_submit_creates_a_new_attempt_after_a_failed_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            spawned = []
            store = JobStore(Path(temp), spawner=lambda job_id, _root: spawned.append(job_id))
            request = {"provider": "qwen-main-v1", "task": {"goal": "g"}}
            first = store.submit(request)
            store.complete(first["job_id"], {"status": "failed"})

            second = store.submit(request, force=True)

            self.assertEqual("accepted", second["status"])
            self.assertNotEqual(first["job_id"], second["job_id"])
            self.assertEqual(2, len(spawned))

    def test_expired_job_stops_polling_and_requires_a_top_model_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            request = {"provider": "qwen-main-v1", "task": {"goal": "g"}}
            receipt = store.submit(request)
            state_path = Path(temp) / receipt["job_id"] / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["monitor_until_utc"] = "2000-01-01T00:00:00Z"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            expired = store.get(receipt["job_id"])
            duplicate = store.submit(request)

            self.assertEqual("stale", expired["job_status"])
            self.assertEqual(0, expired["poll_after_ms"])
            self.assertEqual("top_model", expired["decision"]["owner"])
            self.assertEqual("blocked", duplicate["status"])
            self.assertIn("retry-with-force", duplicate["decision"]["options"])


if __name__ == "__main__":
    unittest.main()
