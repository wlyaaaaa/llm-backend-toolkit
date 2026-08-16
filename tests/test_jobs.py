import hashlib
import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import llm_backend_toolkit.jobs as jobs_module
from llm_backend_toolkit.backends import BackendRegistry
from llm_backend_toolkit.jobs import JobStore
from llm_backend_toolkit.observability import (
    append_event,
    read_conversation_process,
    read_events,
)


def _registry(
    *,
    model: str = "model-v1",
    profile: str = "profile-v1",
    backend_id: str = "local-a",
    default_reasoning_mode: str | None = None,
) -> BackendRegistry:
    backend = {
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
    if default_reasoning_mode is not None:
        backend["default_reasoning_mode"] = default_reasoning_mode
    return BackendRegistry.from_dict(
        {
            "schema": "llm-backend-toolkit.backends.v1",
            "default_backend": backend_id,
            "aliases": {},
            "backends": {backend_id: backend},
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


def _spawn_submit_same_cache_key(
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

        store = JobStore(root, spawner=record_spawn, registry=_registry())
        ready_queue.put(os.getpid())
        if not start_event.wait(15):
            raise TimeoutError("spawn test start barrier timed out")
        receipt = store.submit(request)
        result_queue.put(
            {
                "ok": True,
                "job_id": receipt["job_id"],
                "status": receipt["status"],
            }
        )
    except BaseException as error:
        result_queue.put(
            {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
            }
        )


def _spawn_exit_while_holding_cache_lock(
    state_root: str,
    cache_digest: str,
    acquired_path: str,
) -> None:
    store = JobStore(Path(state_root), spawner=lambda *_: None)
    with store._cache_lock(cache_digest):
        Path(acquired_path).write_text("acquired\n", encoding="utf-8")
        os._exit(0)


def _spawn_hold_cache_lock(
    state_root: str,
    cache_digest: str,
    acquired_queue,
    release_event,
) -> None:
    store = JobStore(Path(state_root), spawner=lambda *_: None)
    with store._cache_lock(cache_digest):
        acquired_queue.put(os.getpid())
        if not release_event.wait(20):
            raise TimeoutError("spawn test release barrier timed out")


class JobStoreTests(unittest.TestCase):
    def test_job_receipts_and_cache_identity_use_the_backend_reasoning_default(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(
                Path(temp),
                spawner=lambda *_: None,
                registry=_registry(default_reasoning_mode="on"),
            )
            request = {
                "backend": "local-a",
                "task": {"goal": "g"},
                "execution": {"cache_key": "same-key"},
            }

            accepted = store.submit(request)
            state = store.get(accepted["job_id"])
            _, default_digest = store._explicit_cache_identity(request, "same-key")
            explicit_off = dict(request)
            explicit_off["reasoning"] = {"mode": "off"}
            _, off_digest = store._explicit_cache_identity(explicit_off, "same-key")

            self.assertEqual("on", state["display"]["reasoning_mode"])
            created = read_events(Path(temp) / accepted["job_id"])[0]
            self.assertEqual("on", created["payload"]["reasoning_mode"])
            self.assertNotEqual(default_digest, off_digest)

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
                    "task_label": "模型生成任务",
                    "execution_mode": "direct",
                    "reasoning_mode": "on",
                    "model": BackendRegistry.load().resolve("qwen-main-v1").config["model"],
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
                    "public_event": {
                        "kind": "agent.output.delta",
                        "summary_zh": "PRIVATE_PUBLIC_OUTPUT",
                        "payload": {"status": "updated"},
                    },
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
            self.assertEqual(0, progress["metrics"]["estimated_output_tokens"])
            self.assertNotIn("public_preview", progress)
            self.assertNotIn("PRIVATE_HIDDEN_TRACE", raw)
            self.assertNotIn('"partial"', raw)
            self.assertNotIn(
                "agent.output.delta",
                [event["kind"] for event in read_events(Path(temp) / receipt["job_id"])],
            )

    def test_progress_recorder_serializes_concurrent_callbacks_without_losing_deltas(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"],
                allow_public_preview=True,
                write_interval_seconds=0.05,
            )
            original_atomic_json = jobs_module._atomic_json
            active = 0
            active_guard = threading.Lock()
            collisions: list[str] = []

            def guarded_atomic_json(path, value):
                nonlocal active
                with active_guard:
                    active += 1
                    if active > 1:
                        collisions.append(str(path))
                try:
                    time.sleep(0.005)
                    original_atomic_json(path, value)
                finally:
                    with active_guard:
                        active -= 1

            barrier = threading.Barrier(8)
            errors: list[Exception] = []

            def emit(index: int) -> None:
                try:
                    barrier.wait(timeout=3)
                    recorder(
                        {
                            "phase": "completed",
                            "content_delta": f"[{index}]",
                        }
                    )
                except Exception as error:  # pragma: no cover - diagnostic capture
                    errors.append(error)

            with patch(
                "llm_backend_toolkit.jobs._atomic_json",
                side_effect=guarded_atomic_json,
            ):
                threads = [threading.Thread(target=emit, args=(index,)) for index in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

            progress = json.loads(
                (Path(temp) / receipt["job_id"] / "progress.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(errors)
            self.assertFalse(collisions)
            for index in range(8):
                self.assertIn(f"[{index}]", progress["public_preview"])

    def test_progress_recorder_replaces_streamed_preview_with_completed_reply(self):
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

            recorder(
                {
                    "phase": "generating",
                    "content_delta": "你好，",
                    "public_event": {
                        "kind": "agent.output.delta",
                        "summary_zh": "你好，",
                        "payload": {"status": "updated"},
                    },
                }
            )
            recorder({"phase": "generating", "content_delta": "这是"})
            recorder(
                {
                    "phase": "completed",
                    "content_replace": "你好，这是公开回复。",
                    "public_event": {
                        "kind": "agent.output.completed",
                        "summary_zh": "你好，这是公开回复。",
                        "payload": {"status": "completed"},
                    },
                }
            )

            progress = json.loads(
                (Path(temp) / receipt["job_id"] / "progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual("你好，这是公开回复。", progress["public_preview"])
            self.assertEqual("completed", progress["phase"])
            completed_events = [
                event
                for event in read_events(Path(temp) / receipt["job_id"])
                if event["kind"] == "agent.output.completed"
            ]
            self.assertEqual(1, len(completed_events))
            self.assertNotIn(
                "agent.output.delta",
                [event["kind"] for event in read_events(Path(temp) / receipt["job_id"])],
            )
            self.assertEqual(
                "你好，这是公开回复。",
                completed_events[0]["summary_zh"],
            )

    def test_progress_recorder_suppresses_a_secret_split_across_deltas(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"],
                allow_public_preview=True,
                write_interval_seconds=0.05,
            )

            recorder({"phase": "generating", "content_delta": "token=abcd"})
            recorder({"phase": "completed", "content_delta": "efgh12345678"})

            progress_path = Path(temp) / receipt["job_id"] / "progress.json"
            raw = progress_path.read_text(encoding="utf-8")
            progress = json.loads(raw)
            self.assertNotIn("public_preview", progress)
            self.assertEqual(0, progress["metrics"]["estimated_output_tokens"])
            self.assertNotIn("token=abcdefgh12345678", raw)

    def test_progress_recorder_keeps_public_reasoning_summaries_separate_and_paged(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"],
                allow_public_preview=True,
                write_interval_seconds=0.05,
            )

            recorder(
                {
                    "phase": "thinking",
                    "reasoning_summary_delta": {
                        "summary_group": 1,
                        "summary_index": 0,
                        "delta": "正在核对",
                    },
                }
            )
            time.sleep(0.06)
            recorder(
                {
                    "phase": "thinking",
                    "reasoning_summary_delta": {
                        "summary_group": 1,
                        "summary_index": 0,
                        "delta": "公开配置。",
                    },
                }
            )

            progress_path = Path(temp) / receipt["job_id"] / "progress.json"
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertNotIn("public_preview", progress)
            summary_events = [
                event
                for event in read_events(Path(temp) / receipt["job_id"])
                if event["kind"] == "agent.reasoning.summary.delta"
            ]
            self.assertEqual(
                ["正在核对", "公开配置。"],
                [event["payload"]["delta"] for event in summary_events],
            )
            self.assertEqual([1, 1], [event["payload"]["summary_group"] for event in summary_events])
            self.assertEqual([0, 0], [event["payload"]["summary_index"] for event in summary_events])
            summary = progress["public_reasoning_summaries"][0]
            self.assertEqual(
                (1, 0, "正在核对公开配置。"),
                (summary["summary_group"], summary["summary_index"], summary["text"]),
            )
            self.assertEqual(summary_events[0]["sequence"], summary["first_sequence"])
            self.assertEqual(summary_events[-1]["sequence"], summary["last_sequence"])

    def test_progress_recorder_blocks_a_secret_split_across_reasoning_summary_deltas(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"],
                allow_public_preview=True,
                write_interval_seconds=0.05,
            )

            for delta in ("sk", "-abcdefghijklmnop"):
                recorder(
                    {
                        "phase": "thinking",
                        "reasoning_summary_delta": {
                            "summary_group": 1,
                            "summary_index": 0,
                            "delta": delta,
                        },
                    }
                )

            raw = (
                Path(temp) / receipt["job_id"] / "progress.json"
            ).read_text(encoding="utf-8")
            progress = json.loads(raw)
            self.assertNotIn("public_reasoning_summaries", progress)
            self.assertNotIn("sk-abcdefghijklmnop", raw)
            self.assertNotIn(
                "agent.reasoning.summary.delta",
                [event["kind"] for event in read_events(Path(temp) / receipt["job_id"])],
            )

    def test_progress_recorder_bounds_the_public_reasoning_summary_segments(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"],
                allow_public_preview=True,
                write_interval_seconds=0.05,
            )

            for _ in range(11):
                recorder(
                    {
                        "phase": "thinking",
                        "reasoning_summary_delta": {
                            "summary_group": 1,
                            "summary_index": 0,
                            "delta": "中" * 4_000,
                        },
                    }
                )
            recorder({"phase": "completed"})

            progress = json.loads(
                (Path(temp) / receipt["job_id"] / "progress.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(4_000, len(progress["public_reasoning_summaries"][0]["text"]))
            self.assertTrue(progress["public_reasoning_summaries_truncated"])

    def test_progress_recorder_keeps_multiple_public_reasoning_summary_segments(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"],
                allow_public_preview=True,
                write_interval_seconds=0.05,
            )

            for group, index, delta in (
                (1, 0, "第一段前半"),
                (1, 0, "后半。"),
                (1, 1, "第二段。"),
                (2, 0, "第三段。"),
            ):
                recorder(
                    {
                        "phase": "thinking",
                        "reasoning_summary_delta": {
                            "summary_group": group,
                            "summary_index": index,
                            "delta": delta,
                        },
                    }
                )
            recorder({"phase": "completed"})

            progress = json.loads(
                (Path(temp) / receipt["job_id"] / "progress.json").read_text(
                    encoding="utf-8"
                )
            )
            summaries = progress["public_reasoning_summaries"]
            self.assertEqual(
                [
                    (1, 0, "第一段前半后半。"),
                    (1, 1, "第二段。"),
                    (2, 0, "第三段。"),
                ],
                [
                    (item["summary_group"], item["summary_index"], item["text"])
                    for item in summaries
                ],
            )
            events_by_key = {}
            for event in read_events(Path(temp) / receipt["job_id"]):
                if event["kind"] == "agent.reasoning.summary.delta":
                    key = (
                        event["payload"]["summary_group"],
                        event["payload"]["summary_index"],
                    )
                    events_by_key.setdefault(key, []).append(event["sequence"])
            for summary in summaries:
                key = (summary["summary_group"], summary["summary_index"])
                self.assertEqual(events_by_key[key][0], summary["first_sequence"])
                self.assertEqual(events_by_key[key][-1], summary["last_sequence"])

    def test_reasoning_nodes_beyond_live_cache_remain_in_full_conversation(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"],
                allow_public_preview=True,
                write_interval_seconds=0.05,
            )

            for group in range(1, 14):
                recorder(
                    {
                        "phase": "thinking",
                        "reasoning_summary_delta": {
                            "summary_group": group,
                            "summary_index": 0,
                            "delta": f"公开摘要 {group}。",
                        },
                    }
                )
                recorder(
                    {
                        "phase": "waiting",
                        "public_event": {
                            "kind": "agent.tool.activity",
                            "summary_zh": "智能体已完成执行命令。",
                            "payload": {
                                "item_type": "command_execution",
                                "status": "completed",
                                "tool_calls": group,
                            },
                        },
                    }
                )
            recorder({"phase": "completed"})

            job_dir = Path(temp) / receipt["job_id"]
            progress = json.loads((job_dir / "progress.json").read_text(encoding="utf-8"))
            process = read_conversation_process(job_dir)

        self.assertEqual(12, len(progress["public_reasoning_summaries"]))
        self.assertTrue(progress["public_reasoning_summaries_truncated"])
        self.assertEqual(13, len(process["thought_nodes"]))
        self.assertEqual(
            [f"公开摘要 {group}。" for group in range(1, 14)],
            [node["text"] for node in process["thought_nodes"]],
        )
        self.assertEqual(13, len(process["activity_segments"]))
        self.assertEqual(
            13,
            sum(
                activity["count"]
                for segment in process["activity_segments"]
                for activity in segment["activities"]
                if activity["type"] == "command"
            ),
        )

    def test_commentary_group_is_split_around_intervening_activity(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"],
                allow_public_preview=True,
                write_interval_seconds=0.05,
            )
            recorder(
                {
                    "phase": "thinking",
                    "commentary_delta": {
                        "commentary_group": 1,
                        "delta": "命令前。",
                    },
                }
            )
            recorder(
                {
                    "phase": "waiting",
                    "public_event": {
                        "kind": "agent.tool.activity",
                        "summary_zh": "智能体已完成执行命令。",
                        "payload": {
                            "item_type": "command_execution",
                            "status": "completed",
                            "tool_calls": 1,
                        },
                    },
                }
            )
            recorder(
                {
                    "phase": "thinking",
                    "commentary_delta": {
                        "commentary_group": 1,
                        "delta": "命令后。",
                    },
                }
            )
            recorder({"phase": "completed"})

            process = read_conversation_process(Path(temp) / receipt["job_id"])

        self.assertEqual(
            ["命令前。", "命令后。"],
            [node["text"] for node in process["thought_nodes"]],
        )
        self.assertEqual(1, len(process["activity_segments"]))
        before, after = process["thought_nodes"]
        activity = process["activity_segments"][0]
        self.assertLess(before["last_sequence"], activity["first_sequence"])
        self.assertLess(activity["last_sequence"], after["first_sequence"])

    def test_commentary_completion_replaces_uninterrupted_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"], allow_public_preview=True
            )
            recorder(
                {
                    "phase": "thinking",
                    "commentary_delta": {
                        "commentary_group": 1,
                        "delta": "旧草稿。",
                    },
                }
            )
            recorder(
                {
                    "phase": "thinking",
                    "commentary_replace": {
                        "commentary_group": 1,
                        "content_replace": "已完成的新结论。",
                    },
                }
            )
            recorder({"phase": "completed"})

            process = read_conversation_process(Path(temp) / receipt["job_id"])

        self.assertEqual(
            ["已完成的新结论。"],
            [node["text"] for node in process["thought_nodes"]],
        )

    def test_commentary_completion_after_activity_becomes_new_part(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"], allow_public_preview=True
            )
            recorder(
                {
                    "phase": "thinking",
                    "commentary_delta": {
                        "commentary_group": 1,
                        "delta": "命令前草稿。",
                    },
                }
            )
            recorder(
                {
                    "phase": "waiting",
                    "public_event": {
                        "kind": "agent.tool.activity",
                        "summary_zh": "智能体已完成执行命令。",
                        "payload": {
                            "item_type": "command_execution",
                            "status": "completed",
                            "tool_calls": 1,
                        },
                    },
                }
            )
            recorder(
                {
                    "phase": "thinking",
                    "commentary_replace": {
                        "commentary_group": 1,
                        "content_replace": "命令后形成的新结论。",
                    },
                }
            )
            recorder({"phase": "completed"})

            process = read_conversation_process(Path(temp) / receipt["job_id"])

        self.assertEqual(
            ["命令前草稿。", "命令后形成的新结论。"],
            [node["text"] for node in process["thought_nodes"]],
        )
        before, after = process["thought_nodes"]
        activity = process["activity_segments"][0]
        self.assertLess(before["last_sequence"], activity["first_sequence"])
        self.assertLess(activity["last_sequence"], after["first_sequence"])

    def test_progress_recorder_propagates_explicit_reasoning_summary_truncation(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(receipt["job_id"], allow_public_preview=True)

            recorder(
                {
                    "phase": "thinking",
                    "reasoning_summary_delta": {
                        "summary_group": 1,
                        "summary_index": 0,
                        "delta": "公开摘要。",
                        "truncated": True,
                    },
                }
            )
            recorder({"phase": "completed"})

            progress = json.loads(
                (Path(temp) / receipt["job_id"] / "progress.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(progress["public_reasoning_summaries_truncated"])
            event = next(
                item
                for item in read_events(Path(temp) / receipt["job_id"])
                if item["kind"] == "agent.reasoning.summary.delta"
            )
            self.assertTrue(event["payload"]["truncated"])

    def test_progress_recorder_freezes_completed_commentary_and_anchors_public_ranges(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"], allow_public_preview=True
            )

            recorder(
                {
                    "phase": "thinking",
                    "commentary_delta": {
                        "commentary_group": 2,
                        "delta": "正在执行公开检查。",
                    },
                    "reasoning_summary_delta": {
                        "summary_group": 1,
                        "summary_index": 0,
                        "delta": "正在核对。",
                    },
                    "reasoning": "PRIVATE_RAW_REASONING_CANARY",
                }
            )
            recorder(
                {
                    "phase": "thinking",
                    "commentary_replace": {
                        "commentary_group": 2,
                        "content_replace": "公开检查已完成。",
                    },
                }
            )
            recorder(
                {
                    "phase": "thinking",
                    "commentary_delta": {
                        "commentary_group": 2,
                        "delta": "不得追加。",
                    },
                }
            )
            for delta in ("sk", "-abcdefghijklmnop"):
                recorder(
                    {
                        "phase": "thinking",
                        "commentary_delta": {
                            "commentary_group": 3,
                            "delta": delta,
                        },
                    }
                )
            recorder({"phase": "completed"})

            job_dir = Path(temp) / receipt["job_id"]
            raw = (job_dir / "progress.json").read_text(encoding="utf-8")
            progress = json.loads(raw)
            commentary_events = [
                event
                for event in read_events(job_dir)
                if event["kind"] in {
                    "agent.commentary.delta",
                    "agent.commentary.completed",
                }
            ]
            self.assertEqual(
                [
                    {
                        "commentary_group": 2,
                        "text": "公开检查已完成。",
                        "first_sequence": commentary_events[0]["sequence"],
                        "last_sequence": commentary_events[1]["sequence"],
                    }
                ],
                progress["public_commentary_segments"],
            )
            self.assertEqual(
                ["agent.commentary.delta", "agent.commentary.completed"],
                [event["kind"] for event in commentary_events],
            )
            summary = progress["public_reasoning_summaries"][0]
            summary_event = next(
                event
                for event in read_events(job_dir)
                if event["kind"] == "agent.reasoning.summary.delta"
            )
            self.assertEqual(summary_event["sequence"], summary["first_sequence"])
            self.assertEqual(summary_event["sequence"], summary["last_sequence"])
            self.assertNotIn("不得追加。", raw)
            self.assertNotIn("PRIVATE_RAW_REASONING_CANARY", raw)
            self.assertNotIn("sk-abcdefghijklmnop", raw)

    def test_progress_recorder_completed_reply_sets_preview_without_deltas(self):
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

            recorder(
                {
                    "phase": "completed",
                    "content_replace": "没有增量时的完整公开回复。",
                }
            )

            progress = json.loads(
                (Path(temp) / receipt["job_id"] / "progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "没有增量时的完整公开回复。",
                progress["public_preview"],
            )
            self.assertEqual("completed", progress["phase"])

    def test_progress_recorder_keeps_a_useful_bounded_public_draft_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit({"backend": "local-default", "task": {"goal": "g"}})
            store.claim(receipt["job_id"])
            recorder = store.progress_recorder(
                receipt["job_id"],
                allow_public_preview=True,
            )
            reply = "中" * 25_000

            recorder({"phase": "completed", "content_replace": reply})

            progress = json.loads(
                (Path(temp) / receipt["job_id"] / "progress.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(reply[:20_000], progress["public_preview"])
            self.assertTrue(progress["public_preview_truncated"])

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
            self.assertEqual("模型生成任务", completed["display"]["task_label"])

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

    def test_agent_job_monitor_deadline_uses_default_watchdog_budget(self):
        request = {
            "provider": "qwen-main-v1",
            "task": {"goal": "g"},
            "execution": {
                "mode": "agent",
                "runner": "data_factory",
                "workspace": "C:/work",
            },
        }

        self.assertEqual(1020, JobStore._timeout_seconds(request))

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

    def test_explicit_cache_identity_v2_hashes_key_and_is_stable_across_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_key = (
                "personalos-model-batch:raw-a:extractor-v1:"
                "schema-v1:model-v1:prompt-v1"
            )
            expected_key_hash = "sha256:" + hashlib.sha256(
                cache_key.encode("utf-8")
            ).hexdigest()
            store = JobStore(
                Path(temp),
                spawner=lambda *_: None,
                registry=_registry(),
            )
            request = _explicit_agent_request(cache_key)

            accepted = store.submit(request)
            persisted = store.get(accepted["job_id"])
            store.claim(accepted["job_id"])
            running = store.submit(request)
            store.complete(
                accepted["job_id"],
                {"status": "ok", "output": {"facts": []}},
            )
            cache_hit = store.submit(request)
            completed = store.get(accepted["job_id"])

            identity = accepted["cache_identity"]
            self.assertEqual(
                "llm-backend-toolkit.explicit-cache-identity.v2",
                identity["schema"],
            )
            self.assertEqual("explicit", identity["mode"])
            self.assertEqual(
                "stdlib-json-sort-compact-utf8-v1",
                identity["canonicalization"],
            )
            self.assertEqual(expected_key_hash, identity["caller_cache_key_hash"])
            self.assertNotIn("caller_cache_key", identity)
            state_text = (
                Path(temp) / accepted["job_id"] / "state.json"
            ).read_text(encoding="utf-8")
            index_text = next(
                (Path(temp) / ".cache-index").glob("*.json")
            ).read_text(encoding="utf-8")
            self.assertNotIn(cache_key, state_text)
            self.assertNotIn(cache_key, index_text)
            for receipt in (
                persisted,
                running,
                cache_hit,
                completed,
            ):
                self.assertEqual(identity, receipt["cache_identity"])
                self.assertNotIn(
                    cache_key,
                    json.dumps(receipt, ensure_ascii=False),
                )
            self.assertEqual("running", running["status"])
            self.assertEqual("cache_hit", cache_hit["status"])

    def test_request_digest_identity_v2_declares_canonicalization_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            request = {
                "provider": "qwen-main-v1",
                "task": {"goal": "canonical request"},
            }
            store = JobStore(Path(temp), spawner=lambda *_: None)

            receipt = store.submit(request)

            self.assertEqual(
                {
                    "schema": (
                        "llm-backend-toolkit.explicit-cache-identity.v2"
                    ),
                    "mode": "request_digest",
                    "digest": (
                        "sha256:" + JobStore.request_digest(request)
                    ),
                    "canonicalization": (
                        "stdlib-json-sort-compact-utf8-v1"
                    ),
                },
                receipt["cache_identity"],
            )

    def test_old_or_missing_explicit_identity_is_readable_but_never_a_v2_hit(self):
        variants = ("v1", "missing")
        for variant in variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temp:
                spawned = []
                store = JobStore(
                    Path(temp),
                    spawner=lambda job_id, _root: spawned.append(job_id),
                    registry=_registry(),
                )
                request = _explicit_agent_request()
                first = store.submit(request)
                state_path = Path(temp) / first["job_id"] / "state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                identity = dict(state["cache_identity"])
                if variant == "v1":
                    state["cache_identity"] = {
                        "schema": (
                            "llm-backend-toolkit.explicit-cache-identity.v1"
                        ),
                        "mode": "explicit",
                        "digest": identity["digest"],
                        "backend": identity["backend"],
                        "model": identity["model"],
                        "route": identity["route"],
                        "profile": identity["profile"],
                    }
                else:
                    state.pop("cache_identity")
                state_path.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                readable = store.get(first["job_id"])
                second = store.submit(request)

                if variant == "v1":
                    self.assertEqual(
                        "llm-backend-toolkit.explicit-cache-identity.v1",
                        readable["cache_identity"]["schema"],
                    )
                else:
                    self.assertNotIn("cache_identity", readable)
                self.assertEqual("accepted", second["status"])
                self.assertNotEqual(first["job_id"], second["job_id"])
                self.assertEqual(
                    "llm-backend-toolkit.explicit-cache-identity.v2",
                    second["cache_identity"]["schema"],
                )
                self.assertEqual(2, len(spawned))

    def test_v1_request_digest_state_is_readable_but_not_a_v2_hit(self):
        with tempfile.TemporaryDirectory() as temp:
            spawned = []
            store = JobStore(
                Path(temp),
                spawner=lambda job_id, _root: spawned.append(job_id),
            )
            request = {
                "provider": "qwen-main-v1",
                "task": {"goal": "legacy request digest"},
            }
            first = store.submit(request)
            state_path = Path(temp) / first["job_id"] / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            identity = dict(state["cache_identity"])
            state["cache_identity"] = {
                "schema": "llm-backend-toolkit.explicit-cache-identity.v1",
                "mode": "request_digest",
                "digest": identity["digest"],
            }
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            readable = store.get(first["job_id"])
            second = store.submit(request)

            self.assertEqual(
                "llm-backend-toolkit.explicit-cache-identity.v1",
                readable["cache_identity"]["schema"],
            )
            self.assertEqual("accepted", second["status"])
            self.assertNotEqual(first["job_id"], second["job_id"])
            self.assertEqual(
                "llm-backend-toolkit.explicit-cache-identity.v2",
                second["cache_identity"]["schema"],
            )
            self.assertEqual(2, len(spawned))

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

    def test_spawn_processes_with_same_explicit_key_create_and_spawn_once(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temp:
            ready_queue = context.Queue()
            start_event = context.Event()
            result_queue = context.Queue()
            request = _explicit_agent_request()
            processes = [
                context.Process(
                    target=_spawn_submit_same_cache_key,
                    args=(
                        temp,
                        request,
                        ready_queue,
                        start_event,
                        result_queue,
                    ),
                )
                for _ in range(12)
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

            self.assertTrue(
                all(result["ok"] for result in results),
                results,
            )
            job_ids = {result["job_id"] for result in results}
            self.assertEqual(1, len(job_ids))
            self.assertTrue(
                all(
                    result["status"] in {"accepted", "running"}
                    for result in results
                )
            )
            spawned = (Path(temp) / "spawned.log").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual([next(iter(job_ids))], spawned)

    def test_process_exit_releases_cache_lock_without_stale_lease(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temp:
            acquired_path = Path(temp) / "acquired.txt"
            cache_digest = "a" * 64
            process = context.Process(
                target=_spawn_exit_while_holding_cache_lock,
                args=(temp, cache_digest, str(acquired_path)),
            )
            process.start()
            process.join(timeout=30)
            self.assertFalse(process.is_alive())
            self.assertEqual(0, process.exitcode)
            self.assertTrue(acquired_path.is_file())

            store = JobStore(Path(temp), spawner=lambda *_: None)
            started = time.monotonic()
            with store._cache_lock(cache_digest):
                pass
            self.assertLess(time.monotonic() - started, 1.0)

    def test_old_lock_file_never_steals_a_live_owner_lock(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temp:
            cache_digest = "b" * 64
            acquired_queue = context.Queue()
            release_event = context.Event()
            process = context.Process(
                target=_spawn_hold_cache_lock,
                args=(
                    temp,
                    cache_digest,
                    acquired_queue,
                    release_event,
                ),
            )
            process.start()
            acquired_queue.get(timeout=30)
            lock_path = (
                Path(temp)
                / ".cache-locks"
                / f"{cache_digest}.lock"
            )
            old = time.time() - 3600
            os.utime(lock_path, (old, old))

            store = JobStore(Path(temp), spawner=lambda *_: None)
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(
                    TimeoutError,
                    "cache identity lock",
                ):
                    with store._cache_lock(cache_digest):
                        pass
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 4.5)
                self.assertLess(elapsed, 7.0)
            finally:
                release_event.set()
                process.join(timeout=30)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=30)
            self.assertEqual(0, process.exitcode)

            started = time.monotonic()
            with store._cache_lock(cache_digest):
                pass
            self.assertLess(time.monotonic() - started, 1.0)

    def test_mutable_file_references_are_not_cached_without_a_content_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            spawned = []
            store = JobStore(Path(temp), spawner=lambda job_id, _root: spawned.append(job_id))
            source = Path(temp) / "mutable-data.jsonl"
            source.write_text('{"value":"synthetic"}\n', encoding="utf-8")
            request = {
                "provider": "qwen-main-v1",
                "task": {
                    "goal": "g",
                    "sources": [{"id": "data", "path": str(source)}],
                },
            }
            first = store.submit(request)
            store.claim(first["job_id"])
            self.assertTrue(store.begin_execution(first["job_id"]))
            store.complete(first["job_id"], {"status": "ok", "output": "done"})

            second = store.submit(request)

            self.assertFalse(first["cacheable"])
            self.assertNotEqual(first["job_id"], second["job_id"])
            self.assertEqual(2, len(spawned))

    def test_event_nested_payloads_use_recursive_positive_schemas(self):
        with tempfile.TemporaryDirectory() as temp:
            job_dir = Path(temp) / ("a" * 24)
            append_event(
                job_dir,
                "run.completed",
                "运行完成。",
                payload={
                    "result_status": "ok",
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 5,
                        "eval_duration_ns": 250_000_000,
                        "tps_source": "eval_duration",
                        "raw_log": "PRIVATE_USAGE_LOG",
                        "details": {
                            "raw_log": "PRIVATE_NESTED_USAGE_LOG",
                            "path": "C:/private/usage.json",
                        },
                    },
                    "checks": [
                        {
                            "id": "C:/private/checks/truth.json",
                            "passed": False,
                            "summary": "PRIVATE_CHECK_SUMMARY",
                            "evidence": {"raw_log": "PRIVATE_CHECK_LOG"},
                        }
                    ],
                    "unknown": {"raw_log": "PRIVATE_TOP_LEVEL_LOG"},
                },
            )
            append_event(
                job_dir,
                "cache.hit",
                "命中缓存。",
                payload={
                    "attempt_id": "attempt-1",
                    "source_job_id": "..\\private\\source-job",
                    "cache_identity": {
                        "schema": "llm-backend-toolkit.explicit-cache-identity.v1",
                        "mode": "explicit",
                        "digest": "sha256:" + ("b" * 64),
                        "backend": "local-a",
                        "model": "model-v1",
                        "caller_cache_key": "PRIVATE_CALLER_KEY",
                        "nested": {"raw_log": "PRIVATE_CACHE_LOG"},
                    },
                },
            )

            completed, cache_hit = read_events(job_dir)

            self.assertEqual(
                {
                    "result_status": "ok",
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 5,
                        "eval_duration_ns": 250_000_000,
                        "tps_source": "eval_duration",
                    },
                    "checks": [
                        {
                            "id": completed["payload"]["checks"][0]["id"],
                            "passed": False,
                        }
                    ],
                },
                completed["payload"],
            )
            self.assertRegex(
                completed["payload"]["checks"][0]["id"],
                r"^opaque-[0-9a-f]{16}$",
            )
            self.assertEqual(
                {
                    "schema": "llm-backend-toolkit.explicit-cache-identity.v1",
                    "mode": "explicit",
                    "digest": "sha256:" + ("b" * 64),
                    "backend": "local-a",
                    "model": "model-v1",
                },
                cache_hit["payload"]["cache_identity"],
            )
            self.assertRegex(
                cache_hit["payload"]["source_job_id"],
                r"^opaque-[0-9a-f]{16}$",
            )
            serialized = json.dumps([completed, cache_hit], ensure_ascii=False)
            for private_value in (
                "PRIVATE_USAGE_LOG",
                "PRIVATE_NESTED_USAGE_LOG",
                "PRIVATE_CHECK_SUMMARY",
                "PRIVATE_CHECK_LOG",
                "PRIVATE_TOP_LEVEL_LOG",
                "PRIVATE_CALLER_KEY",
                "PRIVATE_CACHE_LOG",
                "C:/private",
                "..\\private",
            ):
                self.assertNotIn(private_value, serialized)

    def test_path_shaped_attachment_ids_become_stable_opaque_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            job_dir = Path(temp) / ("c" * 24)
            private_id = "C:\\private\\audio\\meeting.wav"
            first = append_event(
                job_dir,
                "media.asr.started",
                "开始语音识别。",
                payload={"attachment_id": private_id, "kind": "audio"},
            )
            second = append_event(
                job_dir,
                "media.asr.completed",
                "语音识别完成。",
                payload={"attachment_id": private_id, "kind": "audio"},
            )

            first_id = first["payload"]["attachment_id"]
            self.assertRegex(first_id, r"^opaque-[0-9a-f]{16}$")
            self.assertEqual(first_id, second["payload"]["attachment_id"])
            self.assertNotIn(private_id, json.dumps([first, second]))

    def test_event_append_sequence_is_linear_and_only_terminal_events_fsync(self):
        with tempfile.TemporaryDirectory() as temp:
            job_dir = Path(temp) / ("d" * 24)
            with patch(
                "llm_backend_toolkit.observability.read_events",
                side_effect=AssertionError("append_event must not scan the full event log"),
            ), patch("llm_backend_toolkit.observability.os.fsync") as fsync:
                started = time.monotonic()
                for index in range(650):
                    event = append_event(
                        job_dir,
                        "agent.tool.activity",
                        "智能体正在调用工具。",
                        payload={
                            "status": "running",
                            "item_type": "tool_call",
                            "steps": index,
                        },
                    )
                    self.assertEqual(index + 1, event["sequence"])
                elapsed = time.monotonic() - started

                self.assertLess(elapsed, 10.0)
                self.assertEqual(0, fsync.call_count)
                (job_dir / ".events-sequence.json").unlink()
                terminal = append_event(
                    job_dir,
                    "run.completed",
                    "运行完成。",
                    payload={"result_status": "ok"},
                )
                self.assertEqual(651, terminal["sequence"])
                self.assertGreaterEqual(fsync.call_count, 2)

            events = read_events(job_dir, limit=700)
            self.assertEqual(list(range(1, 652)), [event["sequence"] for event in events])
            self.assertTrue((job_dir / ".events-sequence.json").is_file())


if __name__ == "__main__":
    unittest.main()
