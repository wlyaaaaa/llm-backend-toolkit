import hashlib
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from llm_backend_toolkit.jobs import JobNotRunnableError, JobStore
from llm_backend_toolkit.providers import ProviderResponse
from llm_backend_toolkit.toolkit import Toolkit


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _reference(path: Path, value: bytes, *, reference_id: str) -> dict:
    return {
        "id": reference_id,
        "path": str(path),
        "expected_sha256": _sha256(value),
        "expected_bytes": len(value),
    }


def _request(
    source: dict,
    *,
    attachments: list[dict] | None = None,
    cache_key: str = "synthetic:input-integrity:v1",
) -> dict:
    return {
        "provider": "qwen-main-v1",
        "task": {
            "goal": "Return the synthetic marker.",
            "sources": [source],
            "expected_output": {"format": "text"},
        },
        "media": {
            "mode": "native",
            "attachments": list(attachments or []),
        },
        "privacy": {"cloud_allowed": False},
        "execution": {
            "mode": "direct",
            "cache_key": cache_key,
        },
    }


def _spawn_integrity_submit(
    state_root: str,
    request: dict,
    ready_queue,
    start_event,
    result_queue,
) -> None:
    try:
        root = Path(state_root)

        def record_spawn(job_id: str, _root: Path) -> None:
            with (root / "spawned.log").open("a", encoding="utf-8") as stream:
                stream.write(job_id + "\n")

        store = JobStore(root, spawner=record_spawn)
        ready_queue.put(os.getpid())
        if not start_event.wait(15):
            raise TimeoutError("input-integrity submit barrier timed out")
        receipt = store.submit(request)
        result_queue.put(
            {
                "ok": True,
                "job_id": receipt["job_id"],
                "status": receipt["status"],
                "input_integrity": receipt["input_integrity"],
            }
        )
    except BaseException as error:
        result_queue.put(
            {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
            }
        )


class _ReadingProvider:
    cloud = False
    supports_vision = True

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke(self, prompt, media, reasoning_mode, progress_callback=None):
        self.calls.append(
            {
                "prompt": prompt,
                "media": list(media),
                "media_bytes": [Path(path).read_bytes() for path in media],
            }
        )
        return ProviderResponse(content="SYNTHETIC_OK", model="fixture-model")


class JobInputIntegrityTests(unittest.TestCase):
    def test_same_size_replacement_fails_before_running_or_cache_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            original = b"ALPHA-ONE"
            source.write_bytes(original)
            store = JobStore(root / "jobs", spawner=lambda *_: None)
            request = _request(_reference(source, original, reference_id="source-a"))

            receipt = store.submit(request)
            source.write_bytes(b"BRAVO-TWO")

            with self.assertRaisesRegex(ValueError, "integrity"):
                store.claim(receipt["job_id"])

            state = store.get(receipt["job_id"])
            job_dir = root / "jobs" / receipt["job_id"]
            self.assertEqual("failed", state["job_status"])
            self.assertEqual("failed", state["input_integrity"]["status"])
            self.assertTrue(
                state["input_spool_cleanup"]["verified_absent"]
            )
            self.assertFalse((job_dir / "input-spool").exists())
            self.assertFalse((job_dir / "result.json").exists())
            self.assertFalse(any(job_dir.glob("output.*")))
            cache_index = json.loads(
                next((root / "jobs" / ".cache-index").glob("*.json")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotEqual("completed", cache_index["status"])

    def test_missing_and_size_changed_references_fail_closed(self):
        cases = ("missing", "size")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "source.txt"
                original = b"SYNTHETIC"
                source.write_bytes(original)
                store = JobStore(root / "jobs", spawner=lambda *_: None)
                receipt = store.submit(
                    _request(_reference(source, original, reference_id="source-a"))
                )
                if case == "missing":
                    source.unlink()
                else:
                    source.write_bytes(original + b"-CHANGED")

                with self.assertRaisesRegex(ValueError, "integrity"):
                    store.claim(receipt["job_id"])

                state = store.get(receipt["job_id"])
                self.assertEqual("failed", state["job_status"])
                self.assertEqual("failed", state["input_integrity"]["status"])
                self.assertFalse(
                    (root / "jobs" / receipt["job_id"] / "result.json").exists()
                )

    def test_media_same_size_replacement_fails_before_provider_invocation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            image = root / "image.png"
            source_bytes = b"SAFE SOURCE"
            image_bytes = b"IMAGE-ONE"
            source.write_bytes(source_bytes)
            image.write_bytes(image_bytes)
            attachment = {
                **_reference(image, image_bytes, reference_id="image-a"),
                "kind": "image",
                "route": "native",
            }
            store = JobStore(root / "jobs", spawner=lambda *_: None)
            receipt = store.submit(
                _request(
                    _reference(source, source_bytes, reference_id="source-a"),
                    attachments=[attachment],
                )
            )
            image.write_bytes(b"IMAGE-TWO")
            provider = _ReadingProvider()

            with self.assertRaisesRegex(ValueError, "integrity"):
                claimed = store.claim(receipt["job_id"])
                Toolkit(providers={"qwen-main-v1": provider}).invoke(claimed)

            self.assertEqual([], provider.calls)
            self.assertFalse(
                (root / "jobs" / receipt["job_id"] / "result.json").exists()
            )

    def test_integrity_declarations_are_a_validated_pair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            value = b"PAIR"
            source.write_bytes(value)
            store = JobStore(root / "jobs", spawner=lambda *_: None)
            invalid_references = (
                {
                    "id": "source-a",
                    "path": str(source),
                    "expected_sha256": _sha256(value),
                },
                {
                    "id": "source-a",
                    "path": str(source),
                    "expected_bytes": len(value),
                },
                {
                    "id": "source-a",
                    "path": str(source),
                    "expected_sha256": "sha256:not-a-digest",
                    "expected_bytes": len(value),
                },
            )
            for reference in invalid_references:
                with self.subTest(reference=reference), self.assertRaises(ValueError):
                    store.submit(_request(reference))
            self.assertFalse((root / "jobs").exists())

    def test_sources_and_media_are_spooled_and_receipts_are_path_and_body_free(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "PRIVATE-PATH-source.txt"
            image = root / "PRIVATE-PATH-image.png"
            source_bytes = b"SYNTHETIC MARKER: ORBIT-71"
            image_bytes = b"\x89PNG\r\nSYNTHETIC"
            source.write_bytes(source_bytes)
            image.write_bytes(image_bytes)
            attachment = {
                **_reference(image, image_bytes, reference_id="image-a"),
                "kind": "image",
                "route": "native",
            }
            request = _request(
                _reference(source, source_bytes, reference_id="source-a"),
                attachments=[attachment],
                cache_key="SENSITIVE-RAW-CACHE-KEY",
            )
            store = JobStore(root / "jobs", spawner=lambda *_: None)

            accepted = store.submit(request)
            self.assertEqual("pending", accepted["input_integrity"]["status"])
            claimed = store.claim(accepted["job_id"])
            running = store.get(accepted["job_id"])
            source_spool = Path(claimed["task"]["sources"][0]["path"])
            media_spool = Path(claimed["media"]["attachments"][0]["path"])
            expected_spool_root = root / "jobs" / accepted["job_id"] / "input-spool"

            self.assertTrue(source_spool.is_relative_to(expected_spool_root))
            self.assertTrue(media_spool.is_relative_to(expected_spool_root))
            self.assertEqual(source_bytes, source_spool.read_bytes())
            self.assertEqual(image_bytes, media_spool.read_bytes())
            self.assertEqual("verified", running["input_integrity"]["status"])
            self.assertNotIn(
                "expected_sha256", claimed["task"]["sources"][0]
            )
            self.assertNotIn(
                "expected_bytes", claimed["task"]["sources"][0]
            )
            self.assertNotIn(
                "expected_sha256", claimed["media"]["attachments"][0]
            )
            self.assertNotIn(
                "expected_bytes", claimed["media"]["attachments"][0]
            )
            source.unlink()
            image.unlink()

            provider = _ReadingProvider()
            result = Toolkit(providers={"qwen-main-v1": provider}).invoke(claimed)
            store.complete(accepted["job_id"], result)
            completed = store.get(accepted["job_id"], include_result=True)
            hit = store.submit(request)

            self.assertEqual("ok", result["status"])
            self.assertEqual([image_bytes], provider.calls[0]["media_bytes"])
            self.assertEqual("completed", completed["job_status"])
            self.assertEqual("verified", completed["input_integrity"]["status"])
            self.assertEqual("cache_hit", hit["status"])
            self.assertEqual("verified", hit["input_integrity"]["status"])
            self.assertFalse(expected_spool_root.exists())
            self.assertTrue(
                completed["input_spool_cleanup"]["verified_absent"]
            )
            serialized_receipts = json.dumps(
                [accepted, running, completed, hit],
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertNotIn("PRIVATE-PATH", serialized_receipts)
            self.assertNotIn("SYNTHETIC MARKER", serialized_receipts)
            self.assertNotIn("SENSITIVE-RAW-CACHE-KEY", serialized_receipts)
            self.assertIn(_sha256(source_bytes), serialized_receipts)
            self.assertIn(str(len(source_bytes)), serialized_receipts)

    def test_declared_integrity_is_part_of_explicit_cache_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            first_bytes = b"FIRST"
            second_bytes = b"OTHER"
            source.write_bytes(first_bytes)
            store = JobStore(root / "jobs", spawner=lambda *_: None)
            first_request = _request(
                _reference(source, first_bytes, reference_id="source-a")
            )
            first = store.submit(first_request)
            store.claim(first["job_id"])
            store.complete(first["job_id"], {"status": "ok", "output": "done"})

            source.write_bytes(second_bytes)
            second_request = _request(
                _reference(source, second_bytes, reference_id="source-a")
            )
            second = store.submit(second_request)

            self.assertEqual("accepted", second["status"])
            self.assertNotEqual(first["job_id"], second["job_id"])
            self.assertEqual(
                first["cache_identity"]["caller_cache_key_hash"],
                second["cache_identity"]["caller_cache_key_hash"],
            )
            self.assertNotEqual(
                first["cache_identity"]["digest"],
                second["cache_identity"]["digest"],
            )

    def test_failed_integrity_attempt_never_becomes_a_cache_hit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            original = b"EXPECTED"
            source.write_bytes(original)
            store = JobStore(root / "jobs", spawner=lambda *_: None)
            request = _request(_reference(source, original, reference_id="source-a"))
            failed = store.submit(request)
            source.write_bytes(b"MISMATCH")

            with self.assertRaisesRegex(ValueError, "integrity"):
                store.claim(failed["job_id"])

            source.write_bytes(original)
            retried = store.submit(request)
            self.assertEqual("accepted", retried["status"])
            self.assertNotEqual(failed["job_id"], retried["job_id"])

    def test_cross_process_identical_integrity_request_spawns_one_job(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            value = b"CROSS-PROCESS"
            source.write_bytes(value)
            request = _request(_reference(source, value, reference_id="source-a"))
            ready_queue = context.Queue()
            start_event = context.Event()
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_spawn_integrity_submit,
                    args=(
                        str(root / "jobs"),
                        request,
                        ready_queue,
                        start_event,
                        result_queue,
                    ),
                )
                for _ in range(6)
            ]
            for process in processes:
                process.start()
            for _ in processes:
                ready_queue.get(timeout=30)
            start_event.set()
            results = [result_queue.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertFalse(process.is_alive())
                self.assertEqual(0, process.exitcode)

            self.assertTrue(all(result["ok"] for result in results), results)
            self.assertEqual(1, len({result["job_id"] for result in results}))
            self.assertTrue(
                all(
                    result["input_integrity"]["status"] == "pending"
                    for result in results
                )
            )
            spawned = (root / "jobs" / "spawned.log").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(1, len(spawned))

    def test_cancel_before_provider_removes_spool_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            value = b"CANCEL-BEFORE-PROVIDER"
            source.write_bytes(value)
            store = JobStore(root / "jobs", spawner=lambda *_: None)
            request = _request(_reference(source, value, reference_id="source-a"))
            receipt = store.submit(request)
            store.claim(receipt["job_id"])
            spool_root = root / "jobs" / receipt["job_id"] / "input-spool"
            self.assertTrue(spool_root.is_dir())

            cancelled = store.cancel(receipt["job_id"])
            repeated = store.cancel(receipt["job_id"])
            cleanup = store.cleanup_inputs(receipt["job_id"])
            state = store.get(receipt["job_id"])
            retry = store.submit(request)

            self.assertEqual("cancelled", cancelled["job_status"])
            self.assertEqual("cancelled", repeated["job_status"])
            self.assertTrue(cleanup["input_spool_cleanup"]["verified_absent"])
            self.assertEqual("cancelled", state["job_status"])
            self.assertFalse(spool_root.exists())
            self.assertEqual("accepted", retry["status"])
            self.assertNotEqual(receipt["job_id"], retry["job_id"])
            with self.assertRaisesRegex(ValueError, "cancelled"):
                store.claim(receipt["job_id"])

    def test_cancel_during_provider_suppresses_result_and_cache_then_cleans(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            value = b"CANCEL-DURING-PROVIDER"
            source.write_bytes(value)
            store = JobStore(root / "jobs", spawner=lambda *_: None)
            request = _request(_reference(source, value, reference_id="source-a"))
            receipt = store.submit(request)
            store.claim(receipt["job_id"])
            self.assertTrue(store.begin_execution(receipt["job_id"]))
            spool_root = root / "jobs" / receipt["job_id"] / "input-spool"

            requested = store.cancel(receipt["job_id"])
            self.assertEqual("cancellation_requested", requested["job_status"])
            self.assertTrue(spool_root.is_dir())
            store.complete(
                receipt["job_id"],
                {"status": "ok", "output": "MUST-NOT-BE-PUBLISHED"},
            )
            state = store.get(receipt["job_id"], include_result=True)
            retry = store.submit(request)

            self.assertEqual("cancelled", state["job_status"])
            self.assertNotIn("result", state)
            self.assertFalse(
                (root / "jobs" / receipt["job_id"] / "result.json").exists()
            )
            self.assertFalse(spool_root.exists())
            self.assertTrue(
                state["input_spool_cleanup"]["verified_absent"]
            )
            self.assertEqual("accepted", retry["status"])

    def test_cancel_during_spooling_does_not_wait_on_the_job_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            value = b"CANCEL-DURING-SPOOL"
            source.write_bytes(value)
            store = JobStore(root / "jobs", spawner=lambda *_: None)
            request = _request(_reference(source, value, reference_id="source-a"))
            receipt = store.submit(request)
            entered = threading.Event()
            release = threading.Event()
            outcome: list[str] = []

            def slow_prepare(request_value, *, job_dir):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("synthetic release timeout")
                from llm_backend_toolkit.input_integrity import prepare_job_inputs

                return prepare_job_inputs(request_value, job_dir=job_dir)

            def claim_in_thread():
                try:
                    store.claim(receipt["job_id"])
                    outcome.append("claimed")
                except ValueError as error:
                    outcome.append(type(error).__name__)

            with mock.patch(
                "llm_backend_toolkit.jobs.prepare_job_inputs",
                side_effect=slow_prepare,
            ):
                worker = threading.Thread(target=claim_in_thread)
                worker.start()
                self.assertTrue(entered.wait(5))
                cancelled = store.cancel(receipt["job_id"])
                release.set()
                worker.join(timeout=10)

            state = store.get(receipt["job_id"])
            self.assertFalse(worker.is_alive())
            self.assertEqual("cancellation_requested", cancelled["job_status"])
            self.assertEqual(["JobNotRunnableError"], outcome)
            self.assertEqual("cancelled", state["job_status"])
            self.assertTrue(
                state["input_spool_cleanup"]["verified_absent"]
            )

    def test_provider_execution_cannot_begin_before_spooling_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            value = b"PROVIDER-MUST-WAIT"
            source.write_bytes(value)
            store = JobStore(root / "jobs", spawner=lambda *_: None)
            request = _request(_reference(source, value, reference_id="source-a"))
            receipt = store.submit(request)
            entered = threading.Event()
            release = threading.Event()

            def slow_prepare(request_value, *, job_dir):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("synthetic release timeout")
                from llm_backend_toolkit.input_integrity import prepare_job_inputs

                return prepare_job_inputs(request_value, job_dir=job_dir)

            with mock.patch(
                "llm_backend_toolkit.jobs.prepare_job_inputs",
                side_effect=slow_prepare,
            ):
                worker = threading.Thread(
                    target=store.claim,
                    args=(receipt["job_id"],),
                )
                worker.start()
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(
                    JobNotRunnableError, "inputs are not verified"
                ):
                    store.begin_execution(receipt["job_id"])
                release.set()
                worker.join(timeout=10)

            self.assertFalse(worker.is_alive())
            self.assertTrue(store.begin_execution(receipt["job_id"]))

    def test_successful_completion_with_unverified_references_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            value = b"UNVERIFIED-MUST-NOT-PUBLISH"
            source.write_bytes(value)
            request = _request(
                _reference(source, value, reference_id="source-a"),
            )
            request["execution"].pop("cache_key")
            store = JobStore(root / "jobs", spawner=lambda *_: None)
            receipt = store.submit(request)
            job_dir = root / "jobs" / receipt["job_id"]

            with self.assertRaisesRegex(
                ValueError, "verified job input integrity"
            ):
                store.complete(
                    receipt["job_id"],
                    {"status": "ok", "output": "MUST-NOT-BE-PUBLISHED"},
                )

            self.assertFalse((job_dir / "result.json").exists())
            self.assertFalse(any(job_dir.glob("output.*")))

    def test_get_repairs_a_crash_between_terminal_state_and_spool_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            value = b"CRASH-WINDOW"
            source.write_bytes(value)
            store = JobStore(root / "jobs", spawner=lambda *_: None)
            request = _request(_reference(source, value, reference_id="source-a"))
            receipt = store.submit(request)
            store.claim(receipt["job_id"])
            job_dir = root / "jobs" / receipt["job_id"]
            spool_root = job_dir / "input-spool"
            state_path = job_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["job_status"] = "completed"
            state["result_status"] = "ok"
            state["cache_result_eligible"] = False
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.assertTrue(spool_root.is_dir())

            first = store.get(receipt["job_id"])
            second = store.cleanup_inputs(receipt["job_id"])

            self.assertFalse(spool_root.exists())
            self.assertTrue(first["input_spool_cleanup"]["verified_absent"])
            self.assertTrue(second["input_spool_cleanup"]["verified_absent"])

    def test_direct_invoke_with_integrity_declarations_fails_before_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.txt"
            value = b"DIRECT-INVOKE-MUST-BLOCK"
            source.write_bytes(value)
            provider = _ReadingProvider()

            result = Toolkit(providers={"qwen-main-v1": provider}).invoke(
                _request(_reference(source, value, reference_id="source-a"))
            )

            self.assertEqual("blocked", result["status"])
            self.assertEqual("invalid_request", result["error"]["category"])
            self.assertIn("async submit", result["error"]["summary"])
            self.assertEqual([], provider.calls)


if __name__ == "__main__":
    unittest.main()
