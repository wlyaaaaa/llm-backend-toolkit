import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from llm_backend_toolkit.backends import BackendRegistry
from llm_backend_toolkit.jobs import JobStore


def _registry(
    *,
    model: str = "model-v1",
    profile: str = "profile-v1",
    backend_id: str = "local-a",
) -> BackendRegistry:
    return BackendRegistry.from_dict(
        {
            "schema": "llm-backend-toolkit.backends.v1",
            "default_backend": backend_id,
            "aliases": {},
            "backends": {
                backend_id: {
                    "adapter": "ollama",
                    "model": model,
                    "cloud": False,
                    "supports_vision": True,
                    "agent_routes": {
                        "data_factory": {
                            "runner": "codex-cli",
                            "profile": profile,
                            "model": model,
                            "evidence": {
                                "basis": "synthetic-test",
                                "live_verified": True,
                                "model_digest": f"digest-{model}",
                            },
                        }
                    },
                }
            },
        }
    )


def _explicit_agent_request(
    cache_key: str = "personalos-model-batch:raw-a:extractor-v1:schema-v1:model-v1:prompt-v1",
) -> dict:
    return {
        "backend": "local-a",
        "task": {
            "goal": "derive",
            "expected_output": {"format": "json", "required_keys": ["facts"]},
        },
        "context": {"mode": "compact", "target_tokens": 4096},
        "reasoning": {"mode": "off"},
        "privacy": {"cloud_allowed": False},
        "execution": {
            "mode": "agent",
            "runner": "data_factory",
            "workspace": "C:/staging/one",
            "policy": "read-only",
            "cache_key": cache_key,
            "budget": {"timeout_seconds": 900, "max_steps": 20, "max_tool_calls": 80},
        },
    }


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
            self.assertGreaterEqual(receipt["poll_after_ms"], 30_000)
            state = store.get(receipt["job_id"])
            self.assertEqual("queued", state["job_status"])
            self.assertEqual(
                {
                    "task_goal": "g",
                    "execution_mode": "direct",
                    "reasoning_mode": "off",
                },
                state["display"],
            )

    def test_agent_polling_uses_slow_initial_advice_and_exponential_backoff(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            request = {
                "backend": "local-default",
                "task": {"goal": "g"},
                "execution": {"mode": "agent", "workspace": "C:/staging"},
            }

            receipt = store.submit(request)
            first = store.get(receipt["job_id"])
            second = store.get(receipt["job_id"])

            self.assertGreaterEqual(receipt["poll_after_ms"], 60_000)
            self.assertGreater(first["poll_after_ms"], receipt["poll_after_ms"])
            self.assertGreater(second["poll_after_ms"], first["poll_after_ms"])
            self.assertIn("recommended_check_utc", receipt)

    def test_compact_continuation_carries_only_previous_result_and_is_turn_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None, result_preview_chars=80)
            first = store.submit({"backend": "local-default", "task": {"goal": "first"}})
            store.complete(first["job_id"], {"status": "ok", "output": "A" * 500})

            second = store.submit(
                {
                    "backend": "local-default",
                    "task": {"goal": "follow up", "inputs": []},
                    "continuation": {"from_job_id": first["job_id"], "max_turns": 2},
                }
            )
            claimed = store.claim(second["job_id"])

            self.assertEqual(2, second["conversation"]["turn"])
            carried = claimed["task"]["inputs"][-1]
            self.assertEqual("previous_result", carried["type"])
            self.assertEqual(first["job_id"], carried["job_id"])
            self.assertLessEqual(len(carried["output_preview"]), 80)

            store.complete(second["job_id"], {"status": "ok", "output": "done"})
            with self.assertRaisesRegex(ValueError, "maximum turn"):
                store.submit(
                    {
                        "backend": "local-default",
                        "task": {"goal": "too far"},
                        "continuation": {"from_job_id": second["job_id"], "max_turns": 2},
                    }
                )

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

    def test_progress_recorder_is_human_readable_and_never_persists_hidden_reasoning(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"],
                allow_public_preview=False,
                write_interval_seconds=0.05,
            )

            recorder({"phase": "accepted"})
            recorder(
                {
                    "phase": "thinking",
                    "elapsed_seconds": 1.5,
                    "thinking_active": True,
                    "thinking_chars": 42,
                    "token_events": 7,
                    "content_delta": '{"partial":"do not show"}',
                    "reasoning": "PRIVATE_HIDDEN_TRACE",
                }
            )

            progress_path = Path(temp) / receipt["job_id"] / "progress.json"
            raw = progress_path.read_text(encoding="utf-8")
            progress = json.loads(raw)

            self.assertEqual("thinking", progress["phase"])
            self.assertIn("内部分析", progress["summary"])
            self.assertEqual(42, progress["metrics"]["thinking_chars"])
            self.assertEqual(7, progress["metrics"]["token_events"])
            self.assertNotIn("public_preview", progress)
            self.assertNotIn("PRIVATE_HIDDEN_TRACE", raw)
            self.assertNotIn('"partial"', raw)

    def test_progress_recorder_accumulates_only_bounded_public_reply_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"],
                allow_public_preview=True,
                preview_chars=200,
                write_interval_seconds=0.05,
            )

            recorder({"phase": "generating", "content_delta": "你好，"})
            recorder({"phase": "completed", "content_delta": "这是公开回复。"})

            progress = json.loads(
                (Path(temp) / receipt["job_id"] / "progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual("你好，这是公开回复。", progress["public_preview"])
            self.assertEqual("completed", progress["phase"])

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
            self.assertEqual("g", completed["display"]["task_goal"])

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
            self.assertGreater(compact["result"]["delivery_receipt"]["estimated_top_model_tokens_avoided"], 0)
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

    def test_agent_job_monitor_deadline_uses_the_explicit_wall_budget(self):
        request = {
            "provider": "qwen-main-v1",
            "task": {"goal": "g"},
            "execution": {
                "mode": "agent",
                "runner": "data_factory",
                "workspace": "C:/work",
                "budget": {"timeout_seconds": 1800},
            },
        }

        self.assertEqual(1920, JobStore._timeout_seconds(request))

    def test_mutable_agent_workspace_is_not_cached_without_an_explicit_cache_key(self):
        with tempfile.TemporaryDirectory() as temp:
            spawned = []
            store = JobStore(Path(temp), spawner=lambda job_id, _root: spawned.append(job_id))
            request = {
                "provider": "qwen-main-v1",
                "task": {"goal": "g"},
                "execution": {"mode": "agent", "workspace": "C:/staging"},
            }
            first = store.submit(request)
            store.complete(first["job_id"], {"status": "ok", "output": "done"})

            second = store.submit(request)

            self.assertFalse(first["cacheable"])
            self.assertNotEqual(first["job_id"], second["job_id"])
            self.assertEqual(2, len(spawned))

    def test_agent_cache_requires_a_caller_supplied_input_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            spawned = []
            store = JobStore(Path(temp), spawner=lambda job_id, _root: spawned.append(job_id))
            request = {
                "provider": "qwen-main-v1",
                "task": {"goal": "g"},
                "execution": {"mode": "agent", "workspace": "C:/staging", "cache_key": "raw-sha256:abc"},
            }
            first = store.submit(request)
            store.complete(first["job_id"], {"status": "ok", "output": "done"})

            second = store.submit(request)

            self.assertTrue(first["cacheable"])
            self.assertEqual("cache_hit", second["status"])
            self.assertEqual(1, len(spawned))

    def test_explicit_cache_key_ignores_budget_target_tokens_and_workspace_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            spawned = []
            store = JobStore(
                Path(temp),
                spawner=lambda job_id, _root: spawned.append(job_id),
                registry=_registry(),
            )
            first_request = _explicit_agent_request()
            first = store.submit(first_request)
            store.complete(first["job_id"], {"status": "ok", "output": {"facts": []}})

            second_request = _explicit_agent_request()
            second_request["context"]["target_tokens"] = 16_384
            second_request["execution"]["workspace"] = "D:/different-work-metadata"
            second_request["execution"]["budget"] = {
                "timeout_seconds": 3600,
                "max_steps": 100,
                "max_tool_calls": 500,
            }
            second = store.submit(second_request)

            self.assertEqual("cache_hit", second["status"])
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertEqual("explicit", second["cache_identity"]["mode"])
            self.assertEqual(1, len(spawned))
            state = store.get(first["job_id"])
            self.assertEqual("explicit", state["cache_identity"]["mode"])

    def test_explicit_cache_key_change_invalidates_raw_extractor_schema_model_or_prompt_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            spawned = []
            store = JobStore(
                Path(temp),
                spawner=lambda job_id, _root: spawned.append(job_id),
                registry=_registry(),
            )
            base = store.submit(_explicit_agent_request())
            store.complete(base["job_id"], {"status": "ok", "output": {"facts": []}})

            changed_keys = (
                "personalos-model-batch:raw-b:extractor-v1:schema-v1:model-v1:prompt-v1",
                "personalos-model-batch:raw-a:extractor-v2:schema-v1:model-v1:prompt-v1",
                "personalos-model-batch:raw-a:extractor-v1:schema-v2:model-v1:prompt-v1",
                "personalos-model-batch:raw-a:extractor-v1:schema-v1:model-v2:prompt-v1",
                "personalos-model-batch:raw-a:extractor-v1:schema-v1:model-v1:prompt-v2",
            )
            for cache_key in changed_keys:
                receipt = store.submit(_explicit_agent_request(cache_key))
                self.assertEqual("accepted", receipt["status"])
                self.assertNotEqual(base["job_id"], receipt["job_id"])

            self.assertEqual(1 + len(changed_keys), len(spawned))

    def test_explicit_cache_key_is_still_isolated_by_backend_profile_and_model_fingerprint(self):
        variants = (
            (_registry(backend_id="local-b"), "local-b"),
            (_registry(profile="profile-v2"), "local-a"),
            (_registry(model="model-v2"), "local-a"),
        )
        for changed_registry, changed_backend in variants:
            with self.subTest(
                changed_backend=changed_backend,
                changed_model=changed_registry.backends[changed_backend]["model"],
                changed_profile=changed_registry.backends[changed_backend]["agent_routes"][
                    "data_factory"
                ]["profile"],
            ):
                with tempfile.TemporaryDirectory() as temp:
                    first_store = JobStore(Path(temp), spawner=lambda *_: None, registry=_registry())
                    first_request = _explicit_agent_request()
                    first = first_store.submit(first_request)
                    first_store.complete(first["job_id"], {"status": "ok", "output": {"facts": []}})

                    changed_store = JobStore(
                        Path(temp), spawner=lambda *_: None, registry=changed_registry
                    )
                    changed_request = _explicit_agent_request()
                    changed_request["backend"] = changed_backend
                    second = changed_store.submit(changed_request)

                    self.assertEqual("accepted", second["status"])
                    self.assertNotEqual(first["job_id"], second["job_id"])

    def test_explicit_cache_key_is_isolated_by_privacy_and_output_protocol(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None, registry=_registry())
            first = store.submit(_explicit_agent_request())
            store.complete(first["job_id"], {"status": "ok", "output": {"facts": []}})

            privacy_request = _explicit_agent_request()
            privacy_request["privacy"]["cloud_allowed"] = True
            privacy = store.submit(privacy_request)

            protocol_request = _explicit_agent_request()
            protocol_request["task"]["expected_output"] = {"format": "text"}
            protocol = store.submit(protocol_request)

            self.assertNotEqual(first["job_id"], privacy["job_id"])
            self.assertNotEqual(first["job_id"], protocol["job_id"])

    def test_explicit_cache_key_must_be_in_execution_and_pass_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None, registry=_registry())
            misplaced = _explicit_agent_request()
            misplaced["cache_key"] = misplaced["execution"].pop("cache_key")
            first = store.submit(misplaced)
            store.complete(first["job_id"], {"status": "ok", "output": {"facts": []}})
            second = store.submit(misplaced)

            self.assertFalse(first["cacheable"])
            self.assertNotEqual(first["job_id"], second["job_id"])

            for invalid in ("", " leading-space", "trailing-space ", "contains space", "x" * 513):
                invalid_request = _explicit_agent_request(invalid)
                with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    ValueError, "execution.cache_key"
                ):
                    store.submit(invalid_request)

    def test_failed_cancelled_and_non_cacheable_jobs_never_hit_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None, registry=_registry())
            request = _explicit_agent_request()
            failed = store.submit(request)
            store.complete(failed["job_id"], {"status": "failed", "output": None})
            retried = store.submit(request)

            self.assertEqual("accepted", retried["status"])
            self.assertNotEqual(failed["job_id"], retried["job_id"])
            store.complete(retried["job_id"], {"status": "ok", "output": {"facts": []}})
            recovered_hit = store.submit(request)
            self.assertEqual("cache_hit", recovered_hit["status"])
            self.assertEqual(retried["job_id"], recovered_hit["job_id"])

            cancelled_request = _explicit_agent_request(
                "personalos-model-batch:cancelled-attempt"
            )
            cancelled = store.submit(cancelled_request)
            state_path = Path(temp) / cancelled["job_id"] / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["job_status"] = "cancelled"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            after_cancel = store.submit(cancelled_request)

            self.assertEqual("accepted", after_cancel["status"])
            self.assertNotEqual(cancelled["job_id"], after_cancel["job_id"])

    def test_concurrent_submit_with_same_explicit_key_spawns_one_stable_job(self):
        with tempfile.TemporaryDirectory() as temp:
            spawned = []
            spawned_lock = threading.Lock()

            def spawn(job_id, _root):
                with spawned_lock:
                    spawned.append(job_id)

            store = JobStore(Path(temp), spawner=spawn, registry=_registry())
            request = _explicit_agent_request()
            with ThreadPoolExecutor(max_workers=8) as pool:
                receipts = list(pool.map(lambda _: store.submit(request), range(16)))

            self.assertEqual(1, len(spawned))
            self.assertEqual({spawned[0]}, {receipt["job_id"] for receipt in receipts})
            self.assertTrue(
                all(receipt["status"] in {"accepted", "running"} for receipt in receipts)
            )

    def test_mutable_file_references_are_not_cached_without_a_content_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            spawned = []
            store = JobStore(Path(temp), spawner=lambda job_id, _root: spawned.append(job_id))
            request = {
                "provider": "qwen-main-v1",
                "task": {"goal": "g", "sources": [{"id": "data", "path": "C:/mutable/data.jsonl"}]},
            }
            first = store.submit(request)
            store.complete(first["job_id"], {"status": "ok", "output": "done"})

            second = store.submit(request)

            self.assertFalse(first["cacheable"])
            self.assertNotEqual(first["job_id"], second["job_id"])
            self.assertEqual(2, len(spawned))


if __name__ == "__main__":
    unittest.main()
