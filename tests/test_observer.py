from __future__ import annotations

import json
import math
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from llm_backend_toolkit.jobs import OBSERVER_LOCAL_SCHEMA, JobStore
from llm_backend_toolkit.observer import (
    OBSERVER_MAX_LOCAL_METADATA_BYTES,
    OBSERVER_MAX_OUTPUT_BYTES,
    ObserverStore,
    _elapsed_seconds,
    _runtime_health,
    _state_root_id,
    _stream_updates,
    _with_local_workspace_paths,
    create_observer_server,
    observer_runtime_path,
)
from llm_backend_toolkit.observability import EVENT_SCHEMA, append_event, read_events


def request() -> dict:
    return {
        "backend": "local-default",
        "task": {
            "goal": "用中文返回七乘以八。",
            "expected_output": {"format": "text"},
        },
        "reasoning": {"mode": "on"},
        "privacy": {"cloud_allowed": False},
    }


class VisibleRunEventTests(unittest.TestCase):
    def test_agent_protocol_failure_persists_only_the_safe_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            job_dir = Path(temp) / "job-safe-error"

            append_event(
                job_dir,
                "agent.run.failed",
                "智能体协议验证失败。",
                payload={
                    "status": "failed",
                    "error_category": "protocol_or_process_failure",
                    "error_code": "codex_appserver.thread_start_failed",
                    "raw_stderr": "PRIVATE_RAW_STDERR",
                },
            )

            event = read_events(job_dir)[0]
            self.assertEqual(
                "codex_appserver.thread_start_failed",
                event["payload"]["error_code"],
            )
            self.assertNotIn(
                "PRIVATE_RAW_STDERR",
                json.dumps(event, ensure_ascii=False),
            )

    def test_sse_stream_survives_past_thirty_seconds_until_client_disconnect(self) -> None:
        class StaticStore:
            @staticmethod
            def signature() -> str:
                return "generation:stable"

        class DisconnectingStream:
            def __init__(self) -> None:
                self.writes: list[bytes] = []

            def write(self, value: bytes) -> None:
                self.writes.append(value)
                if len(self.writes) == 3:
                    raise BrokenPipeError

            @staticmethod
            def flush() -> None:
                return

        ticks = iter((0.0, 31.0, 62.0, 93.0))
        stream = DisconnectingStream()

        _stream_updates(
            StaticStore(),
            stream,
            monotonic=lambda: next(ticks),
            sleep=lambda _seconds: None,
        )

        self.assertTrue(stream.writes[0].startswith(b"event: refresh\n"))
        self.assertEqual(b": heartbeat\n\n", stream.writes[1])
        self.assertEqual(b": heartbeat\n\n", stream.writes[2])

    def test_visible_run_is_recorded_before_worker_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            observed: list[dict] = []

            def spawner(job_id: str, root: Path) -> None:
                events = read_events(root / job_id)
                self.assertEqual("run.created", events[0]["kind"])
                self.assertEqual("模型生成任务", events[0]["payload"]["task_label"])
                observed.extend(events)

            receipt = JobStore(Path(temp), spawner=spawner).submit(request(), force=True)

            self.assertEqual("accepted", receipt["status"])
            self.assertTrue(observed)
            state = json.loads(
                (Path(temp) / receipt["job_id"] / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual("recorded", state["visibility"]["status"])

    def test_lifecycle_appends_public_events_without_hidden_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit(request(), force=True)
            job_id = receipt["job_id"]
            store.claim(job_id)
            progress = store.progress_recorder(job_id, allow_public_preview=True)
            progress(
                {
                    "phase": "thinking",
                    "thinking_active": True,
                    "thinking_chars": 42,
                    "reasoning": "PRIVATE_HIDDEN_TRACE",
                }
            )
            progress(
                {
                    "phase": "generating",
                    "content_delta": "答案是 56。",
                    "content_chars": 7,
                }
            )
            store.complete(
                job_id,
                {
                    "status": "ok",
                    "output": "答案是 56。",
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 10,
                        "eval_duration_ns": 500_000_000,
                    },
                },
            )

            events = read_events(Path(temp) / job_id)
            kinds = [event["kind"] for event in events]
            self.assertIn("run.started", kinds)
            self.assertIn("reasoning.activity", kinds)
            self.assertIn("output.started", kinds)
            self.assertIn("run.completed", kinds)
            serialized = json.dumps(events, ensure_ascii=False)
            self.assertNotIn("PRIVATE_HIDDEN_TRACE", serialized)

    def test_public_subsystem_events_and_live_token_estimates_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit(request(), force=True)
            job_id = receipt["job_id"]
            store.claim(job_id)
            progress = store.progress_recorder(job_id, allow_public_preview=True)

            progress(
                {
                    "phase": "preparing",
                    "public_event": {
                        "kind": "media.ocr.started",
                        "summary_zh": "LocalOCR 正在提取图片文字。",
                        "payload": {
                            "attachment_id": "scan",
                            "reasoning": "PRIVATE_MEDIA_TRACE",
                        },
                    },
                }
            )
            progress(
                {
                    "phase": "preparing",
                    "public_event": {
                        "kind": "context.compaction.completed",
                        "summary_zh": "已自动压缩调用前上下文。",
                        "payload": {
                            "mode": "compact",
                            "applied": True,
                            "lossy": False,
                            "duplicates_removed": 1,
                            "estimated_tokens_before": 4200,
                            "estimated_tokens_after": 2048,
                            "target_tokens": 2048,
                            "context_window_tokens": 262144,
                            "reasoning": "PRIVATE_CONTEXT_TRACE",
                        },
                    },
                }
            )
            progress(
                {
                    "phase": "generating",
                    "elapsed_seconds": 2.0,
                    "content_delta": "公开回复",
                    "content_chars": 4,
                    "token_events": 2,
                }
            )

            persisted = json.loads(
                (Path(temp) / job_id / "progress.json").read_text(encoding="utf-8")
            )
            self.assertGreater(persisted["metrics"]["estimated_output_tokens"], 0)
            events = read_events(Path(temp) / job_id)
            media_event = next(
                event for event in events if event["kind"] == "media.ocr.started"
            )
            self.assertEqual("scan", media_event["payload"]["attachment_id"])
            self.assertNotIn("PRIVATE_MEDIA_TRACE", json.dumps(media_event))
            context_event = next(
                event
                for event in events
                if event["kind"] == "context.compaction.completed"
            )
            self.assertEqual(2048, context_event["payload"]["estimated_tokens_after"])
            self.assertEqual(262144, context_event["payload"]["context_window_tokens"])
            self.assertNotIn("PRIVATE_CONTEXT_TRACE", json.dumps(context_event))

    def test_cache_hit_is_a_visible_attempt_on_the_source_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            first = store.submit(request())
            store.claim(first["job_id"])
            store.complete(first["job_id"], {"status": "ok", "output": "56"})

            cached = store.submit(request())

            self.assertEqual("cache_hit", cached["status"])
            self.assertRegex(cached["visibility_attempt_id"], r"^[0-9a-f]{16}$")
            events = read_events(Path(temp) / first["job_id"])
            self.assertEqual("cache.hit", events[-1]["kind"])
            self.assertEqual(
                cached["visibility_attempt_id"],
                events[-1]["payload"]["attempt_id"],
            )

    def test_collect_records_handoff_once_but_plain_get_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit(request(), force=True)
            job_id = receipt["job_id"]
            store.claim(job_id)
            store.complete(job_id, {"status": "ok", "output": "56"})

            store.get(job_id, include_result=True)
            self.assertNotIn(
                "handoff.collected",
                [event["kind"] for event in read_events(Path(temp) / job_id)],
            )
            store.collect(job_id, full_result=False)
            store.collect(job_id, full_result=True)

            collected = [
                event
                for event in read_events(Path(temp) / job_id)
                if event["kind"] == "handoff.collected"
            ]
            self.assertEqual(1, len(collected))

    def test_concurrent_collect_is_cross_thread_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit(request(), force=True)
            job_id = receipt["job_id"]
            store.claim(job_id)
            store.complete(job_id, {"status": "ok", "output": "56"})
            errors: list[Exception] = []

            def collect() -> None:
                try:
                    store.collect(job_id)
                except Exception as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)

            threads = [threading.Thread(target=collect) for _ in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertFalse(errors)
            collected = [
                event
                for event in read_events(Path(temp) / job_id)
                if event["kind"] == "handoff.collected"
            ]
            self.assertEqual(1, len(collected))


class ObserverStoreTests(unittest.TestCase):
    def test_usage_projection_preserves_input_output_and_cached_input_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root, spawner=lambda *_: None)
            job_id = store.submit(request(), force=True)["job_id"]
            store.claim(job_id)
            store.complete(
                job_id,
                {
                    "status": "ok",
                    "output": "完成",
                    "usage": {
                        "input_tokens": 120,
                        "output_tokens": 30,
                        "reasoning_tokens": 7,
                        "cached_input_tokens": 80,
                        "total_tokens": 157,
                    },
                },
            )

            usage = ObserverStore(root).get_run(job_id)["result"]["usage"]

            self.assertEqual(120, usage["input_tokens"])
            self.assertEqual(30, usage["output_tokens"])
            self.assertEqual(7, usage["reasoning_tokens"])
            self.assertEqual(80, usage["cached_input_tokens"])
            self.assertEqual(157, usage["total_tokens"])

    def test_command_activity_detail_preserves_only_safe_status_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root, spawner=lambda *_: None)
            job_id = store.submit(request(), force=True)["job_id"]
            store.claim(job_id)
            recorder = store.progress_recorder(job_id, allow_public_preview=True)
            recorder(
                {
                    "phase": "waiting",
                    "public_event": {
                        "kind": "agent.tool.activity",
                        "summary_zh": "智能体执行命令成功（退出码 0）。",
                        "payload": {
                            "status": "completed",
                            "item_type": "command_execution",
                            "command_status": "succeeded",
                            "tool_calls": 3,
                            "exit_code": 0,
                            "duration_ms": 617,
                            "command": "PRIVATE_COMMAND",
                        },
                    },
                }
            )

            event = next(
                item
                for item in ObserverStore(root).get_run(job_id)["events"]
                if item["kind"] == "agent.tool.activity"
            )

            self.assertEqual("succeeded", event["payload"]["command_status"])
            self.assertEqual(0, event["payload"]["exit_code"])
            self.assertEqual(617, event["payload"]["duration_ms"])
            self.assertNotIn("PRIVATE_COMMAND", json.dumps(event, ensure_ascii=False))

    def test_active_elapsed_advances_past_stale_runner_metric(self) -> None:
        created = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        observed = created + timedelta(seconds=125)
        state = {
            "job_status": "running",
            "created_utc": created.isoformat().replace("+00:00", "Z"),
            "updated_utc": (created + timedelta(seconds=1)).isoformat().replace(
                "+00:00",
                "Z",
            ),
        }

        elapsed = _elapsed_seconds(
            state,
            {"metrics": {"elapsed_seconds": 2.0}},
            now=observed,
        )

        self.assertEqual(125.0, elapsed)

    def test_active_elapsed_ignores_non_finite_runner_metric(self) -> None:
        created = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        elapsed = _elapsed_seconds(
            {
                "job_status": "running",
                "created_utc": created.isoformat().replace("+00:00", "Z"),
            },
            {"metrics": {"elapsed_seconds": float("nan")}},
            now=created + timedelta(seconds=5),
        )

        self.assertEqual(5.0, elapsed)
        self.assertTrue(math.isfinite(elapsed))

    def test_terminal_elapsed_does_not_jump_back_from_live_wall_clock(self) -> None:
        created = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        elapsed = _elapsed_seconds(
            {
                "job_status": "completed",
                "created_utc": created.isoformat().replace("+00:00", "Z"),
                "updated_utc": (created + timedelta(seconds=100))
                .isoformat()
                .replace("+00:00", "Z"),
            },
            {"metrics": {"elapsed_seconds": 2.0}},
            now=created + timedelta(seconds=101),
        )

        self.assertEqual(100.0, elapsed)

    def test_expired_active_run_is_projected_as_stale_and_stops_elapsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root, spawner=lambda *_: None)
            job_id = store.submit(request(), force=True)["job_id"]
            state_path = root / job_id / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            created = datetime.now(timezone.utc) - timedelta(minutes=5)
            deadline = created + timedelta(minutes=2)
            state.update(
                {
                    "job_status": "running",
                    "created_utc": created.isoformat().replace("+00:00", "Z"),
                    "updated_utc": (created + timedelta(seconds=1))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "monitor_until_utc": deadline.isoformat().replace(
                        "+00:00",
                        "Z",
                    ),
                }
            )
            state_path.write_text(
                json.dumps(state, ensure_ascii=False),
                encoding="utf-8",
            )

            observer = ObserverStore(root)
            detail = observer.get_run(job_id)
            summary = observer.list_runs()["runs"][0]

            self.assertEqual("stale", detail["job_status"])
            self.assertEqual("stale", summary["job_status"])
            self.assertAlmostEqual(120.0, detail["performance"]["elapsed_seconds"], places=1)

    def test_large_text_and_json_artifacts_are_bounded_in_store_and_http(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root, spawner=lambda *_: None, result_preview_chars=128)
            cases = (
                (
                    "text",
                    "PUBLIC_TEXT_PREFIX" + ("x" * OBSERVER_MAX_OUTPUT_BYTES) + "PRIVATE_TEXT_TAIL",
                    "output.txt",
                    "PRIVATE_TEXT_TAIL",
                ),
                (
                    "json",
                    {
                        "public": "PUBLIC_JSON_PREFIX",
                        "padding": "x" * OBSERVER_MAX_OUTPUT_BYTES,
                        "tail": "PRIVATE_JSON_TAIL",
                    },
                    "output.json",
                    "PRIVATE_JSON_TAIL",
                ),
            )
            jobs: list[tuple[str, Path, str]] = []
            for _name, output, artifact_name, tail_canary in cases:
                receipt = store.submit(request(), force=True)
                job_id = receipt["job_id"]
                store.claim(job_id)
                store.complete(job_id, {"status": "ok", "output": output})
                artifact = root / job_id / artifact_name
                self.assertGreater(artifact.stat().st_size, OBSERVER_MAX_OUTPUT_BYTES)
                jobs.append((job_id, artifact, tail_canary))

            observer = ObserverStore(root)
            server = create_observer_server(root, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                for job_id, artifact, tail_canary in jobs:
                    direct = observer.get_run(job_id)
                    with urllib.request.urlopen(
                        f"{base}/api/runs/{job_id}",
                        timeout=3,
                    ) as response:
                        http = json.load(response)

                    for projected in (direct, http):
                        output = projected["result"]["output"]
                        self.assertEqual("preview", output["type"])
                        self.assertTrue(output["truncated"])
                        self.assertEqual("observer_size_limit", output["reason"])
                        self.assertEqual(artifact.name, output["source"])
                        self.assertEqual(artifact.stat().st_size, output["bytes"])
                        self.assertNotIn(
                            tail_canary,
                            json.dumps(projected, ensure_ascii=False),
                        )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_oversized_result_json_is_rejected_before_opening(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root, spawner=lambda *_: None)
            receipt = store.submit(request(), force=True)
            job_id = receipt["job_id"]
            store.claim(job_id)
            store.complete(job_id, {"status": "ok", "output": "small"})
            result_path = root / job_id / "result.json"
            tail_canary = "PRIVATE_RESULT_TAIL"
            result_path.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "output": "x" * OBSERVER_MAX_OUTPUT_BYTES + tail_canary,
                    }
                ),
                encoding="utf-8",
            )
            self.assertGreater(result_path.stat().st_size, OBSERVER_MAX_OUTPUT_BYTES)
            original_open = Path.open

            def guarded_open(path: Path, *args, **kwargs):
                if Path(path) == result_path:
                    raise AssertionError("oversized result.json must not be opened")
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", autospec=True, side_effect=guarded_open):
                detail = ObserverStore(root).get_run(job_id)

            output = detail["result"]["output"]
            self.assertEqual("bounded", detail["result"]["status"])
            self.assertEqual("preview", output["type"])
            self.assertEqual("result.json", output["source"])
            self.assertEqual(result_path.stat().st_size, output["bytes"])
            self.assertEqual("observer_size_limit", output["reason"])
            self.assertNotIn(tail_canary, json.dumps(detail, ensure_ascii=False))

    def test_large_history_only_reads_result_and_events_for_requested_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            start = datetime(2026, 7, 25, tzinfo=timezone.utc)
            for index in range(1_000):
                job_id = f"{index:024x}"
                job_dir = root / job_id
                job_dir.mkdir()
                stamp = (
                    start + timedelta(seconds=index)
                ).isoformat().replace("+00:00", "Z")
                (job_dir / "state.json").write_text(
                    json.dumps(
                        {
                            "job_status": "completed",
                            "result_status": "ok",
                            "backend": "local-default",
                            "created_utc": stamp,
                            "updated_utc": stamp,
                            "display": {"task_label": f"历史任务 {index}"},
                        }
                    ),
                    encoding="utf-8",
                )
                (job_dir / "progress.json").write_text("{}", encoding="utf-8")
                (job_dir / "result.json").write_text(
                    json.dumps({"status": "ok", "output": f"result-{index}"}),
                    encoding="utf-8",
                )
                (job_dir / "events.jsonl").write_text("", encoding="utf-8")

            observer_module = __import__(
                "llm_backend_toolkit.observer",
                fromlist=["_read_json"],
            )
            with (
                patch(
                    "llm_backend_toolkit.observer._read_json",
                    wraps=observer_module._read_json,
                ) as json_reader,
                patch(
                    "llm_backend_toolkit.observer.read_events",
                    wraps=observer_module.read_events,
                ) as event_reader,
            ):
                started = time.perf_counter()
                listing = ObserverStore(root).list_runs(limit=25, offset=100)
                elapsed = time.perf_counter() - started

            result_json_reads = [
                call
                for call in json_reader.call_args_list
                if Path(call.args[0]).name == "result.json"
            ]
            self.assertEqual(1_000, listing["total"])
            self.assertEqual(25, len(listing["runs"]))
            self.assertEqual(125, listing["next_offset"])
            self.assertEqual(0, len(result_json_reads))
            self.assertEqual(25, event_reader.call_count)
            self.assertLess(elapsed, 2.0)

    def test_private_prompt_paths_and_unapproved_metadata_never_enter_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            private_canary = r"C:\private\PRIVATE_PROMPT_TOKEN"
            private_request = request()
            private_request["task"]["goal"] = private_canary
            private_request["observability"] = {
                "public_label": "修复缓存身份回执",
            }
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit(private_request, force=True)
            job_id = receipt["job_id"]
            store.claim(job_id)
            progress = store.progress_recorder(
                job_id,
                allow_public_preview=False,
            )
            progress(
                {
                    "phase": "preparing",
                    "public_event": {
                        "kind": "media.ocr.started",
                        "summary_zh": "LocalOCR 正在处理附件。",
                        "payload": {
                            "attachment_id": "scan",
                            "path": private_canary,
                            "command": private_canary,
                            "api_key": private_canary,
                        },
                    },
                }
            )
            store.complete(
                job_id,
                {
                    "status": "ok",
                    "output": "PUBLIC_OUTPUT",
                    "artifacts": [{"path": private_canary}],
                    "command": private_canary,
                    "stdout": private_canary,
                    "usage": {
                        "completion_tokens": 12,
                        "eval_duration_ns": 1_000_000_000,
                        "raw_log": private_canary,
                    },
                    "checks": [
                        {
                            "id": private_canary,
                            "passed": True,
                            "summary": "公开校验通过",
                            "raw_log": private_canary,
                        }
                    ],
                    "source_receipt": [
                        {
                            "id": private_canary,
                            "sha256": "a" * 64,
                            "source_chars": 20,
                            "selected_chars": 10,
                            "selected_ranges": [
                                {
                                    "line_start": 1,
                                    "line_end": 2,
                                    "path": private_canary,
                                }
                            ],
                            "path": private_canary,
                        }
                    ],
                    "media_routes": [
                        {
                            "id": private_canary,
                            "kind": "image",
                            "route": "specialist",
                            "path": private_canary,
                        }
                    ],
                    "execution_receipt": {
                        "runner": "codex-cli",
                        "duration_ms": 123,
                        "session_id": private_canary,
                        "command": private_canary,
                        "budget": {
                            "timeout_seconds": 60,
                            "raw_log": private_canary,
                        },
                        "limit_usage": {
                            "steps": 2,
                            "events_seen": 5,
                            "raw_log": private_canary,
                        },
                    },
                },
            )

            state_text = (Path(temp) / job_id / "state.json").read_text(
                encoding="utf-8"
            )
            events_text = json.dumps(read_events(Path(temp) / job_id), ensure_ascii=False)
            observer = ObserverStore(Path(temp))
            projected = json.dumps(
                {
                    "list": observer.list_runs(),
                    "detail": observer.get_run(job_id),
                },
                ensure_ascii=False,
            )

            self.assertNotIn(private_canary, state_text)
            self.assertNotIn(private_canary, events_text)
            self.assertNotIn(private_canary, projected)
            self.assertNotIn("artifacts", projected)
            self.assertIn("PUBLIC_OUTPUT", projected)
            self.assertIn("公开校验通过", projected)
            self.assertIn("opaque-", projected)
            self.assertIn("completion_tokens", projected)
            self.assertNotIn("raw_log", projected)
            self.assertIn("修复缓存身份回执", projected)

    def test_run_detail_adds_local_absolute_paths_without_persisting_them(self) -> None:
        with (
            tempfile.TemporaryDirectory() as state_temp,
            tempfile.TemporaryDirectory() as workspace_temp,
        ):
            submitted = request()
            submitted["execution"] = {
                "mode": "agent",
                "runner": "data_factory",
                "workspace": workspace_temp,
                "policy": "workspace-write",
            }
            store = JobStore(Path(state_temp), spawner=lambda *_: None)
            job_id = store.submit(submitted, force=True)["job_id"]
            job_dir = Path(state_temp) / job_id
            store.claim(job_id)
            append_event(
                job_dir,
                "workspace.change.observed",
                "检测到 1 个工作区文件变化。",
                payload={
                    "changed_files": 1,
                    "scan_status": "scoped_complete",
                    "provenance": "workspace_before_after",
                    "attribution": "unverified_concurrent_window",
                    "detail_policy": "caller_public_safe_include",
                    "changes": [
                        {
                            "relative_path": "docs/acceptance.md",
                            "change_kind": "modified",
                            "lines_added": 2,
                            "lines_deleted": 1,
                            "diff_status": "available",
                            "unified_diff": (
                                "--- a/docs/acceptance.md\n"
                                "+++ b/docs/acceptance.md\n"
                                "@@ -1 +1,2 @@\n"
                                "-待处理\n"
                                "+已通过\n"
                            ),
                        }
                    ],
                },
            )
            store.complete(job_id, {"status": "ok", "output": "真实编辑已完成。"})

            canonical_workspace = Path(workspace_temp).resolve()
            persisted_events = (job_dir / "events.jsonl").read_text(encoding="utf-8")
            local_metadata = json.loads(
                (job_dir / ".observer-local.json").read_text(encoding="utf-8")
            )
            detail = ObserverStore(Path(state_temp)).get_run(job_id)
            workspace_event = next(
                event
                for event in detail["events"]
                if event["kind"] == "workspace.change.observed"
            )
            change = workspace_event["payload"]["changes"][0]

            self.assertFalse((job_dir / "request.json").exists())
            self.assertEqual(
                {
                    "schema": OBSERVER_LOCAL_SCHEMA,
                    "job_id": job_id,
                    "canonical_workspace": str(canonical_workspace),
                },
                local_metadata,
            )
            self.assertNotIn(str(canonical_workspace), persisted_events)
            for name in ("state.json", "result.json", "progress.json"):
                path = job_dir / name
                if path.is_file():
                    self.assertNotIn(
                        str(canonical_workspace),
                        path.read_text(encoding="utf-8"),
                    )
            self.assertNotIn(
                str(canonical_workspace),
                json.dumps(
                    store.get(
                        job_id,
                        include_result=True,
                        full_result=True,
                    ),
                    ensure_ascii=False,
                ),
            )
            self.assertEqual("docs/acceptance.md", change["relative_path"])
            self.assertEqual(
                str(canonical_workspace / "docs" / "acceptance.md"),
                change["absolute_path"],
            )
            self.assertTrue(
                change["unified_diff"].startswith(
                    "--- a/docs/acceptance.md\n+++ b/docs/acceptance.md\n"
                )
            )

    def test_invalid_observer_local_metadata_fails_closed_after_request_cleanup(self) -> None:
        with (
            tempfile.TemporaryDirectory() as state_temp,
            tempfile.TemporaryDirectory() as workspace_temp,
        ):
            submitted = request()
            submitted["execution"] = {
                "mode": "agent",
                "runner": "data_factory",
                "workspace": workspace_temp,
                "policy": "workspace-write",
            }
            store = JobStore(Path(state_temp), spawner=lambda *_: None)
            job_id = store.submit(submitted, force=True)["job_id"]
            job_dir = Path(state_temp) / job_id
            store.claim(job_id)
            append_event(
                job_dir,
                "workspace.change.observed",
                "检测到 1 个工作区文件变化。",
                payload={
                    "changed_files": 1,
                    "scan_status": "scoped_complete",
                    "provenance": "workspace_before_after",
                    "attribution": "unverified_concurrent_window",
                    "detail_policy": "caller_public_safe_include",
                    "changes": [
                        {
                            "relative_path": "safe/inside.txt",
                            "change_kind": "modified",
                            "lines_added": 1,
                            "lines_deleted": 0,
                            "diff_status": "metadata_only",
                        }
                    ],
                },
            )
            store.complete(job_id, {"status": "ok", "output": "done"})
            self.assertFalse((job_dir / "request.json").exists())
            local_path = job_dir / ".observer-local.json"
            canonical_workspace = str(Path(workspace_temp).resolve())
            invalid_values = (
                {
                    "schema": "wrong-schema",
                    "job_id": job_id,
                    "canonical_workspace": canonical_workspace,
                },
                {
                    "schema": OBSERVER_LOCAL_SCHEMA,
                    "job_id": "0" * 24,
                    "canonical_workspace": canonical_workspace,
                },
                {
                    "schema": OBSERVER_LOCAL_SCHEMA,
                    "job_id": job_id,
                    "canonical_workspace": canonical_workspace,
                    "unexpected": True,
                },
            )
            for invalid in invalid_values:
                with self.subTest(invalid=invalid):
                    local_path.write_text(
                        json.dumps(invalid),
                        encoding="utf-8",
                    )
                    detail = ObserverStore(Path(state_temp)).get_run(job_id)
                    workspace_event = next(
                        event
                        for event in detail["events"]
                        if event["kind"] == "workspace.change.observed"
                    )
                    self.assertNotIn(
                        "absolute_path",
                        workspace_event["payload"]["changes"][0],
                    )

            local_path.write_bytes(b" " * (OBSERVER_MAX_LOCAL_METADATA_BYTES + 1))
            detail = ObserverStore(Path(state_temp)).get_run(job_id)
            workspace_event = next(
                event
                for event in detail["events"]
                if event["kind"] == "workspace.change.observed"
            )
            self.assertNotIn(
                "absolute_path",
                workspace_event["payload"]["changes"][0],
            )

    def test_request_spool_is_only_a_legacy_local_workspace_fallback(self) -> None:
        with (
            tempfile.TemporaryDirectory() as state_temp,
            tempfile.TemporaryDirectory() as workspace_temp,
        ):
            submitted = request()
            submitted["execution"] = {
                "mode": "agent",
                "runner": "data_factory",
                "workspace": workspace_temp,
                "policy": "workspace-write",
            }
            store = JobStore(Path(state_temp), spawner=lambda *_: None)
            job_id = store.submit(submitted, force=True)["job_id"]
            job_dir = Path(state_temp) / job_id
            (job_dir / ".observer-local.json").unlink()
            append_event(
                job_dir,
                "workspace.change.observed",
                "检测到 1 个工作区文件变化。",
                payload={
                    "changed_files": 1,
                    "scan_status": "scoped_complete",
                    "provenance": "workspace_before_after",
                    "attribution": "unverified_concurrent_window",
                    "detail_policy": "caller_public_safe_include",
                    "changes": [
                        {
                            "relative_path": "legacy.txt",
                            "change_kind": "added",
                            "lines_added": 1,
                            "lines_deleted": 0,
                            "diff_status": "metadata_only",
                        }
                    ],
                },
            )

            detail = ObserverStore(Path(state_temp)).get_run(job_id)
            workspace_event = next(
                event
                for event in detail["events"]
                if event["kind"] == "workspace.change.observed"
            )

            self.assertEqual(
                str(Path(workspace_temp).resolve() / "legacy.txt"),
                workspace_event["payload"]["changes"][0]["absolute_path"],
            )

    def test_local_absolute_path_projection_rejects_unsafe_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_temp:
            events = [
                {
                    "kind": "workspace.change.observed",
                    "payload": {
                        "changes": [
                            {"relative_path": "../outside.txt"},
                            {"relative_path": "safe/inside.txt"},
                        ]
                    },
                }
            ]

            projected = _with_local_workspace_paths(
                events,
                Path(workspace_temp).resolve(),
            )
            changes = projected[0]["payload"]["changes"]

            self.assertNotIn("absolute_path", changes[0])
            self.assertEqual(
                str(Path(workspace_temp).resolve() / "safe" / "inside.txt"),
                changes[1]["absolute_path"],
            )
            self.assertNotIn("absolute_path", events[0]["payload"]["changes"][1])

    def test_public_label_is_bounded_and_never_falls_back_to_prompt_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            private_canary = "PRIVATE_PROMPT_MUST_NOT_BECOME_A_TITLE"
            submitted = request()
            submitted["task"]["goal"] = private_canary
            submitted["observability"] = {
                "public_label": "  可见   中文标题  " + ("长" * 200),
            }
            store = JobStore(Path(temp), spawner=lambda *_: None)
            job_id = store.submit(submitted, force=True)["job_id"]

            state = json.loads(
                (Path(temp) / job_id / "state.json").read_text(encoding="utf-8")
            )
            detail = ObserverStore(Path(temp)).get_run(job_id)

            label = state["display"]["task_label"]
            self.assertEqual(80, len(label))
            self.assertTrue(label.startswith("可见 中文标题"))
            self.assertEqual(label, detail["display"]["task_label"])
            self.assertNotIn(private_canary, json.dumps(detail, ensure_ascii=False))

    def test_event_window_keeps_latest_terminal_and_handoff_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit(request(), force=True)
            job_id = receipt["job_id"]
            job_dir = Path(temp) / job_id
            events = []
            for sequence in range(1, 601):
                kind = "agent.tool.activity"
                if sequence == 599:
                    kind = "run.completed"
                elif sequence == 600:
                    kind = "handoff.collected"
                events.append(
                    json.dumps(
                        {
                            "schema": EVENT_SCHEMA,
                            "job_id": job_id,
                            "event_id": f"{job_id}:{sequence}",
                            "sequence": sequence,
                            "occurred_utc": "2026-07-25T12:00:00Z",
                            "kind": kind,
                            "visibility": "public",
                            "summary_zh": f"公开事件 {sequence}",
                            "payload": (
                                {"full_result": False}
                                if kind == "handoff.collected"
                                else {"status": "completed"}
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
            (job_dir / "events.jsonl").write_text(
                "\n".join(events) + "\n",
                encoding="utf-8",
            )

            observed = read_events(job_dir)
            observer = ObserverStore(Path(temp))
            detail = observer.get_run(job_id)
            older = observer.get_event_page(
                job_id,
                limit=160,
                before_sequence=detail["event_page"]["next_before_sequence"],
            )

            self.assertEqual(500, len(observed))
            self.assertEqual(101, observed[0]["sequence"])
            self.assertEqual("handoff.collected", observed[-1]["kind"])
            self.assertEqual("collected", detail["handoff"]["status"])
            self.assertEqual(160, len(detail["events"]))
            self.assertEqual(441, detail["events"][0]["sequence"])
            self.assertTrue(detail["event_page"]["has_earlier"])
            self.assertEqual(441, detail["event_page"]["next_before_sequence"])
            self.assertEqual(440, detail["event_page"]["earlier_count"])
            self.assertEqual(600, detail["event_page"]["latest_sequence"])
            self.assertEqual(160, len(older["events"]))
            self.assertEqual(281, older["events"][0]["sequence"])
            self.assertEqual(440, older["events"][-1]["sequence"])
            self.assertEqual(600, older["event_page"]["latest_sequence"])

    def test_runtime_identity_isolated_for_sibling_state_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root_a = Path(temp) / "a" / "jobs"
            root_b = Path(temp) / "b" / "jobs"
            root_a.mkdir(parents=True)
            root_b.mkdir(parents=True)
            self.assertNotEqual(
                observer_runtime_path(root_a),
                observer_runtime_path(root_b),
            )
            self.assertEqual(root_a, observer_runtime_path(root_a).parent)
            server = create_observer_server(root_a, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                runtime_a = {
                    "schema": "llm-backend-toolkit.observer-runtime.v1",
                    "url": f"http://127.0.0.1:{server.server_port}/",
                    "state_root": str(root_a),
                    "state_root_id": _state_root_id(root_a),
                }
                runtime_b = {
                    **runtime_a,
                    "state_root": str(root_b),
                    "state_root_id": _state_root_id(root_b),
                }
                self.assertTrue(_runtime_health(runtime_a, root_a))
                self.assertFalse(_runtime_health(runtime_b, root_b))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_snapshot_lists_runs_and_computes_tps_without_poll_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit(request(), force=True)
            job_id = receipt["job_id"]
            store.claim(job_id)
            store.complete(
                job_id,
                {
                    "status": "ok",
                    "output": {"answer": 56},
                    "backend": {
                        "model": "qwen-main-v1",
                        "context_window_tokens": 262144,
                    },
                    "context_receipt": {
                        "mode": "compact",
                        "executed": True,
                        "applied": False,
                        "lossy": False,
                        "duplicates_removed": 0,
                        "estimated_tokens_before": 512,
                        "estimated_tokens_after": 384,
                        "target_tokens": 4096,
                    },
                    "usage": {
                        "prompt_tokens": 80,
                        "completion_tokens": 40,
                        "current_context_tokens": 202000,
                        "context_window_tokens": 258400,
                        "eval_duration_ns": 2_000_000_000,
                    },
                    "checks": [{"id": "valid_json", "passed": True}],
                },
            )
            poll_count_before = json.loads(
                (Path(temp) / job_id / "state.json").read_text(encoding="utf-8")
            )["poll_count"]

            observer = ObserverStore(Path(temp))
            listing = observer.list_runs()
            detail = observer.get_run(job_id)

            self.assertEqual(job_id, listing["runs"][0]["job_id"])
            self.assertEqual("qwen-main-v1", detail["model"])
            self.assertEqual(20.0, detail["performance"]["tokens_per_second"])
            self.assertEqual(
                "eval_duration",
                detail["performance"]["tokens_per_second_source"],
            )
            self.assertEqual({"answer": 56}, detail["result"]["output"])
            self.assertEqual(
                262144,
                detail["result"]["backend"]["context_window_tokens"],
            )
            self.assertEqual(
                384,
                detail["result"]["context_receipt"]["estimated_tokens_after"],
            )
            self.assertEqual(202000, detail["context"]["current_tokens"])
            self.assertEqual(258400, detail["context"]["context_window_tokens"])
            self.assertEqual(
                "codex_runtime",
                detail["context"]["current_source"],
            )
            self.assertTrue(detail["events"])
            poll_count_after = json.loads(
                (Path(temp) / job_id / "state.json").read_text(encoding="utf-8")
            )["poll_count"]
            self.assertEqual(poll_count_before, poll_count_after)

    def test_live_context_uses_only_codex_runtime_event_not_prompt_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            job_id = store.submit(request(), force=True)["job_id"]
            store.claim(job_id)
            progress = store.progress_recorder(
                job_id,
                allow_public_preview=True,
                write_interval_seconds=0.05,
            )
            progress(
                {
                    "phase": "waiting",
                    "current_context_tokens": 202000,
                    "context_window_tokens": 258400,
                    "public_event": {
                        "kind": "agent.context.usage.updated",
                        "summary_zh": "Codex 已上报实时上下文占用。",
                        "payload": {
                            "current_tokens": 202000,
                            "context_window_tokens": 258400,
                            "private_trace": "PRIVATE_CONTEXT_TRACE",
                        },
                    },
                }
            )

            detail = ObserverStore(Path(temp)).get_run(job_id)

            self.assertEqual(202000, detail["context"]["current_tokens"])
            self.assertEqual(258400, detail["context"]["context_window_tokens"])
            self.assertEqual("codex_runtime", detail["context"]["current_source"])
            self.assertNotIn(
                "agent.context.usage.updated",
                [event["kind"] for event in detail["events"]],
            )
            serialized = json.dumps(detail, ensure_ascii=False)
            self.assertNotIn("PRIVATE_CONTEXT_TRACE", serialized)
            self.assertNotIn("initial_task_estimate", serialized)

    def test_context_receipt_and_backend_config_never_become_live_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            job_id = store.submit(request(), force=True)["job_id"]
            store.claim(job_id)
            store.complete(
                job_id,
                {
                    "status": "ok",
                    "output": "done",
                    "backend": {
                        "model": "qwen-main-v1",
                        "context_window_tokens": 262144,
                    },
                    "context_receipt": {
                        "estimated_tokens_before": 512,
                        "estimated_tokens_after": 384,
                    },
                },
            )

            detail = ObserverStore(Path(temp)).get_run(job_id)

            self.assertIsNone(detail["context"]["current_tokens"])
            self.assertEqual("unavailable", detail["context"]["current_source"])
            self.assertIsNone(detail["context"]["context_window_tokens"])
            self.assertEqual("unavailable", detail["context"]["window_source"])

    def test_context_usage_is_coalesced_and_latest_value_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            job_id = store.submit(request(), force=True)["job_id"]
            store.claim(job_id)
            progress = store.progress_recorder(
                job_id,
                allow_public_preview=False,
                write_interval_seconds=60,
            )
            for index in range(1000):
                progress(
                    {
                        "phase": "waiting",
                        "current_context_tokens": 200000 + index,
                        "context_window_tokens": 258400,
                        "public_event": {
                            "kind": "agent.context.usage.updated",
                            "summary_zh": "Codex 已上报实时上下文占用。",
                            "payload": {
                                "current_tokens": 200000 + index,
                                "context_window_tokens": 258400,
                            },
                        },
                    }
                )
            progress({"phase": "validating"})

            detail = ObserverStore(Path(temp)).get_run(job_id)
            event_kinds = [event["kind"] for event in detail["events"]]

            self.assertEqual(200999, detail["context"]["current_tokens"])
            self.assertEqual(258400, detail["context"]["context_window_tokens"])
            self.assertNotIn("agent.context.usage.updated", event_kinds)
            self.assertLess(len(event_kinds), 12)

    def test_agent_tps_uses_reported_runner_wall_time_not_job_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit(request(), force=True)
            job_id = receipt["job_id"]
            store.claim(job_id)
            store.complete(
                job_id,
                {
                    "status": "ok",
                    "output": {"answer": 56},
                    "backend": {"model": "qwen-main-v1"},
                    "usage": {
                        "prompt_tokens": 120,
                        "cached_tokens": 32,
                        "completion_tokens": 40,
                        "total_tokens": 160,
                        "elapsed_seconds": 2.0,
                        "tps": 20.0,
                        "tps_source": "wall_clock_estimate",
                    },
                    "checks": [{"id": "valid_json", "passed": True}],
                },
            )

            detail = ObserverStore(Path(temp)).get_run(job_id)

            self.assertEqual(20.0, detail["performance"]["tokens_per_second"])
            self.assertEqual(
                "wall_clock_estimate",
                detail["performance"]["tokens_per_second_source"],
            )
            self.assertEqual(32, detail["result"]["usage"]["cached_tokens"])

    def test_submission_metadata_resolves_model_profile_and_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            agent_request = request()
            agent_request["backend"] = "fast-middle-agent"
            agent_request["privacy"]["cloud_allowed"] = True
            agent_request["execution"] = {
                "mode": "agent",
                "runner": "data_factory",
                "workspace": temp,
            }

            receipt = JobStore(Path(temp) / "jobs", spawner=lambda *_: None).submit(
                agent_request,
                force=True,
            )
            detail = ObserverStore(Path(temp) / "jobs").get_run(receipt["job_id"])

            self.assertEqual("fast-middle-agent", detail["backend"])
            self.assertEqual("gpt-5.3-codex-spark", detail["model"])
            self.assertEqual("codex-spark-xhigh", detail["display"]["profile"])
            self.assertEqual("codex-cli", detail["display"]["runner"])
            self.assertEqual("xhigh", detail["display"]["reasoning_effort"])

    def test_history_is_paginated_and_preserves_conversation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            first = store.submit(request(), force=True)
            store.claim(first["job_id"])
            store.complete(first["job_id"], {"status": "ok", "output": "第一轮"})
            continuation = request()
            continuation["continuation"] = {
                "from_job_id": first["job_id"],
                "max_turns": 3,
            }
            second = store.submit(continuation, force=True)
            independent = store.submit(request(), force=True)

            observer = ObserverStore(Path(temp))
            page_one = observer.list_runs(limit=2, offset=0)
            page_two = observer.list_runs(limit=2, offset=2)
            second_detail = observer.get_run(second["job_id"])

            self.assertEqual(3, page_one["total"])
            self.assertEqual(2, len(page_one["runs"]))
            self.assertEqual(2, page_one["next_offset"])
            self.assertEqual(1, len(page_two["runs"]))
            self.assertIsNone(page_two["next_offset"])
            self.assertEqual(
                first["job_id"],
                second_detail["conversation"]["root_job_id"],
            )
            self.assertEqual(2, second_detail["conversation"]["turn"])
            self.assertNotEqual(
                second_detail["conversation"]["root_job_id"],
                ObserverStore(Path(temp)).get_run(independent["job_id"])[
                    "conversation"
                ]["root_job_id"],
            )

    def test_unchanged_history_reuses_cached_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            for _ in range(5):
                receipt = store.submit(request(), force=True)
                store.claim(receipt["job_id"])
                store.complete(
                    receipt["job_id"],
                    {"status": "ok", "output": "done"},
                )
            observer = ObserverStore(Path(temp))
            observer.list_runs()

            with patch(
                "llm_backend_toolkit.observer._read_json",
                wraps=__import__(
                    "llm_backend_toolkit.observer",
                    fromlist=["_read_json"],
                )._read_json,
            ) as reader:
                observer.list_runs()

            self.assertEqual(0, reader.call_count)

    def test_signature_bootstraps_generation_marker_for_legacy_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = JobStore(root, spawner=lambda *_: None)
            store.submit(request(), force=True)
            marker = root / ".observer-generation"
            marker.unlink()

            signature = ObserverStore(root).signature()

            self.assertTrue(marker.is_file())
            self.assertTrue(signature.startswith("generation:"))

    def test_http_observer_serves_health_runs_detail_and_gui(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp), spawner=lambda *_: None)
            receipt = store.submit(request(), force=True)
            server = create_observer_server(Path(temp), port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=3) as response:
                    health = json.load(response)
                with urllib.request.urlopen(f"{base}/api/runs", timeout=3) as response:
                    runs = json.load(response)
                with urllib.request.urlopen(
                    f"{base}/api/runs?limit=1&offset=0",
                    timeout=3,
                ) as response:
                    first_page = json.load(response)
                with urllib.request.urlopen(
                    f"{base}/api/runs/{receipt['job_id']}", timeout=3
                ) as response:
                    detail = json.load(response)
                with urllib.request.urlopen(
                    f"{base}/api/runs/{receipt['job_id']}/events?limit=160",
                    timeout=3,
                ) as response:
                    event_page = json.load(response)
                with urllib.request.urlopen(f"{base}/", timeout=3) as response:
                    html = response.read().decode("utf-8")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

            self.assertEqual("ok", health["status"])
            self.assertNotIn("state_root", health)
            self.assertNotIn("state_root", runs)
            self.assertEqual(receipt["job_id"], runs["runs"][0]["job_id"])
            self.assertEqual(1, first_page["total"])
            self.assertIsNone(first_page["next_offset"])
            self.assertEqual(receipt["job_id"], detail["job_id"])
            self.assertEqual(receipt["job_id"], event_page["job_id"])
            self.assertIn("events", event_page)
            self.assertIn("模型调用观察台", html)


if __name__ == "__main__":
    unittest.main()
