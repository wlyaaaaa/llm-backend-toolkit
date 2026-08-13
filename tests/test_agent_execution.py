import json
import os
import sys
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import llm_backend_toolkit.workspace_observer as workspace_observer
from llm_backend_toolkit.agent_runners import (
    AgentResponse,
    AgentRunnerError,
    AiCliProfileRunner,
    OpenCodeRunner,
    QwenCodeRunner,
    _bounded_process,
    _json_values,
    default_runners,
)
from llm_backend_toolkit.providers import OpenAIChatProvider, ProviderResponse
from llm_backend_toolkit.toolkit import Toolkit
from llm_backend_toolkit.errors import ToolError
from llm_backend_toolkit.observability import append_event
from llm_backend_toolkit.workspace_observer import (
    WorkspaceSnapshot,
    WorkspaceRootError,
    _capture_small_public_text,
    _is_reparse_point,
    capture_workspace_snapshot,
    compare_workspace_snapshots,
    validate_workspace_root,
)


class FakeProvider:
    cloud = False
    supports_vision = True

    def __init__(self):
        self.calls = []

    def invoke(self, prompt, native_images, reasoning_mode):
        self.calls.append(prompt)
        return ProviderResponse(content='{"wrong": true}', model='direct')


class FakeCloudProvider(FakeProvider):
    cloud = True


class FakeRunner:
    def __init__(self, response=None):
        self.response = response or AgentResponse(
            content='{"answer": 56}',
            runner='qwen-code',
            model='qwen-main-v1',
            exit_code=0,
            duration_ms=321,
            tool_calls=4,
            session_id='session-1',
            stop_reason='completed',
        )
        self.calls = []

    def invoke(self, prompt, execution):
        self.calls.append({"prompt": prompt, "execution": execution})
        return self.response


class FailingRunner:
    def invoke(self, prompt, execution):
        raise AgentRunnerError(
            ToolError(category="agent_failed", summary="failed", retryable=True, options=("handle-in-codex",)),
            {"runner": "claude-code", "exit_code": 1, "duration_ms": 101700, "tool_calls": 7},
        )


def agent_request(workspace):
    return {
        "provider": "qwen-main-v1",
        "task": {
            "goal": "Clean the supplied records",
            "instructions": ["Return strict JSON"],
            "inputs": ["record-a", "record-a"],
            "expected_output": {"format": "json", "required_keys": ["answer"]},
        },
        "context": {"mode": "compact", "target_tokens": 1024},
        "execution": {
            "mode": "agent",
            "runner": "data_factory",
            "workspace": str(workspace),
            "policy": "workspace-write",
            "budget": {"timeout_seconds": 600, "max_steps": 20, "max_tool_calls": 80},
        },
    }


def after_codex_machine_event_probe(run_result):
    def bounded(command, **kwargs):
        if command[-2:] == ["version", "--json"]:
            return (
                0,
                json.dumps(
                    {
                        "capabilities": {
                            "machineEventProjection": "aicli.machine-event.v1"
                        }
                    }
                ),
                "",
                1,
            )
        return run_result

    return bounded


class AgentExecutionTests(unittest.TestCase):
    def test_codex_runner_does_not_discover_a_legacy_localappdata_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy_entry = root / "aicli" / "bin" / "aicli.ps1"
            legacy_entry.parent.mkdir(parents=True)
            legacy_entry.write_text("# legacy stub\n", encoding="utf-8")
            execution = {
                "workspace": str(root),
                "model": "qwen-main-v1",
                "policy": "workspace-write",
                "native_images": [],
                "budget": {
                    "timeout_seconds": 30,
                    "max_steps": 4,
                    "max_tool_calls": 4,
                },
            }
            with patch.dict(
                os.environ,
                {
                    "LLM_TOOLKIT_AICLI_ENTRY": "",
                    "LOCALAPPDATA": str(root),
                },
                clear=False,
            ), patch(
                "llm_backend_toolkit.agent_runners.shutil.which",
                return_value="pwsh",
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                return_value=(
                    0,
                    json.dumps(
                        {
                            "capabilities": {
                                "machineEventProjection": "aicli.machine-event.v1"
                            }
                        }
                    ),
                    "",
                    1,
                ),
            ) as bounded:
                runner = AiCliProfileRunner(
                    name="codex-cli",
                    engine="codex",
                    default_profile="codex-ollama-main",
                )

                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke("task", execution)

            self.assertEqual(
                "agent_runner_unavailable",
                raised.exception.error.category,
            )
            bounded.assert_not_called()

    def test_codex_runner_missing_or_wrong_machine_event_capability_fails_before_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            for capabilities in (
                {},
                {"machineEventProjection": "aicli.machine-event.v0"},
            ):
                with self.subTest(capabilities=capabilities):
                    runner = AiCliProfileRunner(
                        name="codex-cli",
                        engine="codex",
                        default_profile="codex-ollama-main",
                        entry=str(entry),
                    )
                    bounded_result = (
                        0,
                        json.dumps({"capabilities": capabilities}),
                        "",
                        1,
                    )
                    with patch(
                        "llm_backend_toolkit.agent_runners.shutil.which",
                        return_value="pwsh",
                    ), patch(
                        "llm_backend_toolkit.agent_runners._bounded_process",
                        return_value=bounded_result,
                    ) as bounded:
                        with self.assertRaises(AgentRunnerError) as raised:
                            runner.invoke("task", self._codex_execution(root))

                    self.assertEqual(
                        "agent_runner_incompatible",
                        raised.exception.error.category,
                    )
                    self.assertEqual(1, bounded.call_count)
                    self.assertEqual(
                        ["version", "--json"],
                        bounded.call_args.args[0][-2:],
                    )

    def test_codex_runner_failed_capability_probe_fails_before_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli",
                engine="codex",
                default_profile="codex-ollama-main",
                entry=str(entry),
            )
            with patch(
                "llm_backend_toolkit.agent_runners.shutil.which",
                return_value="pwsh",
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                return_value=(1, "", "probe failed", 1),
            ) as bounded:
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke("task", self._codex_execution(root))

            self.assertEqual(
                "agent_runner_incompatible",
                raised.exception.error.category,
            )
            self.assertEqual(1, bounded.call_count)
            self.assertEqual(
                ["version", "--json"],
                bounded.call_args.args[0][-2:],
            )

    def test_codex_runner_timed_out_capability_probe_fails_before_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli",
                engine="codex",
                default_profile="codex-ollama-main",
                entry=str(entry),
            )
            probe_timeout = AgentRunnerError(
                ToolError(
                    category="agent_timeout",
                    summary="probe timed out",
                    retryable=True,
                    options=("handle-in-codex",),
                )
            )
            with patch(
                "llm_backend_toolkit.agent_runners.shutil.which",
                return_value="pwsh",
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=probe_timeout,
            ) as bounded:
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke("task", self._codex_execution(root))

            self.assertEqual(
                "agent_runner_incompatible",
                raised.exception.error.category,
            )
            self.assertEqual(1, bounded.call_count)
            self.assertEqual(
                ["version", "--json"],
                bounded.call_args.args[0][-2:],
            )

    @staticmethod
    def _codex_execution(root):
        return {
            "workspace": str(root),
            "model": "qwen-main-v1",
            "policy": "workspace-write",
            "native_images": [],
            "budget": {
                "timeout_seconds": 30,
                "max_steps": 4,
                "max_tool_calls": 4,
            },
        }

    def test_workspace_metadata_change_emits_safe_completed_file_event(self):
        class WritingRunner(FakeRunner):
            def invoke(self, prompt, execution):
                Path(execution["workspace"], "acceptance.md").write_text(
                    "状态：已通过\n证据：真实工作区事件\n",
                    encoding="utf-8",
                )
                return super().invoke(prompt, execution)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "acceptance.md").write_text(
                "状态：待处理\n",
                encoding="utf-8",
            )
            progress = []
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": WritingRunner()},
            )
            request = agent_request(root)
            request["observability"] = {
                "file_changes": {
                    "mode": "diff",
                    "include": ["acceptance.md"],
                }
            }

            result = toolkit.invoke(
                request,
                progress_callback=progress.append,
            )

        self.assertEqual("ok", result["status"])
        file_events = [
            event["public_event"]
            for event in progress
            if (event.get("public_event") or {}).get("kind")
            == "workspace.change.observed"
        ]
        self.assertEqual(1, len(file_events))
        payload = file_events[0]["payload"]
        self.assertEqual(1, payload["changed_files"])
        self.assertEqual("scoped_complete", payload["scan_status"])
        self.assertEqual("workspace_before_after", payload["provenance"])
        self.assertEqual(
            "unverified_concurrent_window",
            payload["attribution"],
        )
        self.assertEqual(1, payload["details_included"])
        self.assertEqual("acceptance.md", payload["changes"][0]["relative_path"])
        self.assertEqual("modified", payload["changes"][0]["change_kind"])
        self.assertIn("-状态：待处理", payload["changes"][0]["unified_diff"])
        self.assertIn("+状态：已通过", payload["changes"][0]["unified_diff"])
        self.assertEqual(
            "validating",
            next(
                event["phase"]
                for event in progress
                if (event.get("public_event") or {}).get("kind")
                == "workspace.change.observed"
            ),
        )
        serialized = json.dumps(file_events, ensure_ascii=False)
        self.assertNotIn(str(root), serialized)

    def test_unchanged_workspace_does_not_emit_a_file_change_event(self):
        with tempfile.TemporaryDirectory() as temp:
            progress = []
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": FakeRunner()},
            )

            result = toolkit.invoke(
                agent_request(Path(temp)),
                progress_callback=progress.append,
            )

        self.assertEqual("ok", result["status"])
        self.assertFalse(
            any(
                (event.get("public_event") or {}).get("kind")
                == "workspace.change.observed"
                for event in progress
            )
        )

    def test_default_workspace_observation_is_count_only_and_never_reads_files(self):
        class WritingRunner(FakeRunner):
            def invoke(self, prompt, execution):
                Path(execution["workspace"], "ordinary.md").write_text(
                    "new public-looking text",
                    encoding="utf-8",
                )
                return super().invoke(prompt, execution)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ordinary.md").write_text("before", encoding="utf-8")
            progress = []
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": WritingRunner()},
            )
            with patch(
                "llm_backend_toolkit.workspace_observer.os.read",
                side_effect=AssertionError("count-only mode must not read file bodies"),
            ):
                result = toolkit.invoke(
                    agent_request(root),
                    progress_callback=progress.append,
                )

        self.assertEqual("ok", result["status"])
        event = next(
            item["public_event"]
            for item in progress
            if (item.get("public_event") or {}).get("kind")
            == "workspace.change.observed"
        )
        self.assertEqual("count_only", event["payload"]["detail_policy"])
        self.assertEqual([], event["payload"]["changes"])
        self.assertEqual(1, event["payload"]["details_omitted"])
        self.assertNotIn("ordinary.md", json.dumps(event, ensure_ascii=False))

    def test_explicit_diff_still_omits_secret_like_file_names(self):
        class WritingRunner(FakeRunner):
            def invoke(self, prompt, execution):
                Path(execution["workspace"], "api-token.txt").write_text(
                    "changed",
                    encoding="utf-8",
                )
                return super().invoke(prompt, execution)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "api-token.txt").write_text("before", encoding="utf-8")
            request = agent_request(root)
            request["observability"] = {
                "file_changes": {
                    "mode": "diff",
                    "include": ["api-token.txt"],
                }
            }
            progress = []
            result = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": WritingRunner()},
            ).invoke(request, progress_callback=progress.append)

        self.assertEqual("ok", result["status"])
        event = next(
            item["public_event"]
            for item in progress
            if (item.get("public_event") or {}).get("kind")
            == "workspace.change.observed"
        )
        self.assertEqual(1, event["payload"]["changed_files"])
        self.assertEqual([], event["payload"]["changes"])
        self.assertNotIn("api-token", json.dumps(event, ensure_ascii=False))

    def test_explicit_diff_omits_secret_or_absolute_path_content(self):
        for unsafe_text in (
            "api_" + "key=" + ("a" * 16) + "\n",
            "local_path=C:\\Users\\private\\input.txt\n",
            "/opt/company/private.txt\n",
            "source=/data/customer.csv\n",
            "path:/srv/customer.csv\n",
            "file:///opt/company/private.txt\n",
            "//server/share/private.txt\n",
            r"\\server\share\private.txt" + "\n",
            r"\Device\HarddiskVolume3\Users\private\x.txt" + "\n",
        ):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                target = root / "notes.md"
                target.write_text("safe before\n", encoding="utf-8")
                before = capture_workspace_snapshot(
                    root,
                    public_text_allowlist=frozenset({"notes.md"}),
                )
                target.write_text(unsafe_text, encoding="utf-8")
                after = capture_workspace_snapshot(
                    root,
                    public_text_allowlist=frozenset({"notes.md"}),
                )
                change = compare_workspace_snapshots(before, after)

            self.assertEqual(1, change.changed_files)
            self.assertEqual((), change.changes)
            self.assertEqual(1, change.details_omitted)

    def test_explicit_diff_rejects_secret_like_parent_components_without_body_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secret_directory = root / "secrets"
            secret_directory.mkdir()
            target = secret_directory / "config.json"
            target.write_text('{"state":"before"}\n', encoding="utf-8")
            allowlist = frozenset({"secrets/config.json"})
            with patch(
                "llm_backend_toolkit.workspace_observer.os.read",
                side_effect=AssertionError(
                    "secret-like path components must prevent body reads"
                ),
            ) as body_read:
                before = capture_workspace_snapshot(
                    root,
                    public_text_allowlist=allowlist,
                )
            body_read.assert_not_called()

            target.write_text('{"state":"after"}\n', encoding="utf-8")
            with patch(
                "llm_backend_toolkit.workspace_observer.os.read",
                side_effect=AssertionError(
                    "secret-like path components must prevent body reads"
                ),
            ) as body_read:
                after = capture_workspace_snapshot(
                    root,
                    public_text_allowlist=allowlist,
                )
            body_read.assert_not_called()

            change = compare_workspace_snapshots(before, after)

        self.assertEqual({}, before._texts)
        self.assertEqual({}, after._texts)
        self.assertEqual(1, change.changed_files)
        self.assertEqual((), change.changes)
        self.assertEqual(1, change.details_omitted)

    def test_explicit_diff_rejects_unsafe_or_empty_include_before_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": runner},
            )
            invalid_policies = (
                {"mode": "diff", "include": []},
                {"mode": "diff", "include": ["../escape.md"]},
                {"mode": "diff", "include": ["C:/absolute.md"]},
                {
                    "mode": "diff",
                    "include": ["safe.md"],
                    "unexpected": True,
                },
            )
            for file_changes in invalid_policies:
                request = agent_request(Path(temp))
                request["observability"] = {
                    "file_changes": file_changes
                }
                result = toolkit.invoke(request)
                self.assertEqual("blocked", result["status"])
                self.assertEqual("invalid_request", result["error"]["category"])

        self.assertEqual([], runner.calls)

    def test_failed_agent_still_emits_observed_file_change(self):
        class WritingFailingRunner(FailingRunner):
            def invoke(self, prompt, execution):
                Path(execution["workspace"], "changed-before-failure.txt").write_text(
                    "durable change",
                    encoding="utf-8",
                )
                return super().invoke(prompt, execution)

        with tempfile.TemporaryDirectory() as temp:
            progress = []
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": WritingFailingRunner()},
            )
            request = agent_request(Path(temp))
            request["observability"] = {
                "file_changes": {
                    "mode": "diff",
                    "include": ["changed-before-failure.txt"],
                }
            }

            result = toolkit.invoke(
                request,
                progress_callback=progress.append,
            )

        self.assertEqual("failed", result["status"])
        file_event = next(
            event["public_event"]
            for event in progress
            if (event.get("public_event") or {}).get("kind")
            == "workspace.change.observed"
        )
        self.assertEqual(1, file_event["payload"]["changed_files"])
        self.assertEqual("scoped_complete", file_event["payload"]["scan_status"])
        self.assertEqual(
            "failed",
            next(
                event["phase"]
                for event in progress
                if event.get("public_event") == file_event
            ),
        )
        self.assertEqual(
            "added",
            file_event["payload"]["changes"][0]["change_kind"],
        )

    def test_workspace_snapshot_is_bounded_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "workspace")
            outside = Path(temp, "outside")
            root.mkdir()
            outside.mkdir()
            outside_file = outside / "never-follow-this-name.txt"
            outside_file.write_text("before", encoding="utf-8")
            link = root / "linked-outside"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symlinks are unavailable: {exc}")

            public_paths = frozenset({"linked-outside/never-follow-this-name.txt"})
            before = capture_workspace_snapshot(
                root,
                public_text_allowlist=public_paths,
            )
            outside_file.write_text("after", encoding="utf-8")
            after = capture_workspace_snapshot(
                root,
                public_text_allowlist=public_paths,
            )
            change = compare_workspace_snapshots(before, after)

            self.assertEqual("scoped_complete", before.status)
            self.assertEqual("scoped_complete", after.status)
            self.assertEqual(0, change.changed_files)
            self.assertEqual({}, before._texts)
            self.assertEqual({}, after._texts)
            for index in range(10):
                (root / f"{index:02}.txt").write_text(str(index), encoding="utf-8")
            bounded = capture_workspace_snapshot(root, max_entries=2)
            self.assertEqual("partial_item_limit", bounded.status)
            self.assertLessEqual(len(bounded._files), 2)

    def test_workspace_root_symlink_is_blocked_before_runner_or_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            actual = parent / "actual"
            link = parent / "workspace-link"
            actual.mkdir()
            try:
                link.symlink_to(actual, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symlinks are unavailable: {exc}")

            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": runner},
            )
            with patch(
                "llm_backend_toolkit.toolkit.capture_workspace_snapshot",
                side_effect=AssertionError(
                    "invalid workspace roots must not reach the observer"
                ),
            ) as snapshot:
                result = toolkit.invoke(
                    agent_request(link),
                    progress_callback=lambda event: None,
                )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("invalid_request", result["error"]["category"])
        self.assertEqual([], runner.calls)
        snapshot.assert_not_called()

    def test_runner_and_observer_share_validated_canonical_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            child = root / "child"
            child.mkdir()
            requested = child / ".."
            canonical = root.resolve()
            observed_roots = []

            def record_snapshot(workspace, *, public_text_allowlist):
                observed_roots.append(
                    Path(getattr(workspace, "canonical_path", workspace))
                )
                return WorkspaceSnapshot("scoped_complete", {})

            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": runner},
            )
            with patch(
                "llm_backend_toolkit.toolkit.capture_workspace_snapshot",
                side_effect=record_snapshot,
            ):
                result = toolkit.invoke(
                    agent_request(requested),
                    progress_callback=lambda event: None,
                )

        self.assertEqual("ok", result["status"])
        self.assertEqual(str(canonical), runner.calls[0]["execution"]["workspace"])
        self.assertEqual([canonical, canonical], observed_roots)

    def test_snapshot_rejects_replaced_validated_root_before_scanning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "workspace")
            retired = Path(temp, "retired")
            root.mkdir()
            (root / "inside.txt").write_text("inside", encoding="utf-8")
            validated = validate_workspace_root(root)
            root.rename(retired)
            root.mkdir()
            (root / "outside.txt").write_text("outside", encoding="utf-8")

            with patch(
                "llm_backend_toolkit.workspace_observer.os.scandir",
                side_effect=AssertionError(
                    "a replaced canonical root must fail before scanning"
                ),
            ) as scandir:
                with self.assertRaises(WorkspaceRootError):
                    capture_workspace_snapshot(
                        validated,
                        public_text_allowlist=frozenset({"outside.txt"}),
                    )

        scandir.assert_not_called()

    def test_internal_directory_replacement_after_precheck_cannot_reach_outside(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "workspace")
            child = root / "child"
            retired = root / "retired"
            outside = Path(temp, "outside")
            child.mkdir(parents=True)
            outside.mkdir()
            (child / "inside.md").write_text("inside", encoding="utf-8")
            (outside / "public.md").write_text(
                "OUTSIDE_PRIVATE",
                encoding="utf-8",
            )
            probe = Path(temp, "symlink-probe")
            try:
                probe.symlink_to(outside, target_is_directory=True)
                probe.unlink()
            except OSError as exc:
                self.skipTest(f"Directory symlinks are unavailable: {exc}")

            validated = validate_workspace_root(root)
            original_safe_stat = workspace_observer._safe_directory_stat
            swapped = False

            def replace_after_precheck(path, *, expected_identity=None):
                nonlocal swapped
                file_stat = original_safe_stat(
                    path,
                    expected_identity=expected_identity,
                )
                if Path(path) == child and not swapped:
                    swapped = True
                    child.rename(retired)
                    child.symlink_to(outside, target_is_directory=True)
                return file_stat

            try:
                with patch(
                    "llm_backend_toolkit.workspace_observer._safe_directory_stat",
                    side_effect=replace_after_precheck,
                ), patch(
                    "llm_backend_toolkit.workspace_observer._capture_small_public_text",
                    side_effect=AssertionError(
                        "outside file bodies must remain unreachable"
                    ),
                ) as capture:
                    with self.assertRaises(WorkspaceRootError):
                        capture_workspace_snapshot(
                            validated,
                            public_text_allowlist=frozenset(
                                {"child/public.md"}
                            ),
                        )
            finally:
                if child.is_symlink():
                    child.unlink()

        self.assertTrue(swapped)
        capture.assert_not_called()

    def test_internal_directory_replacement_before_body_open_revalidates_ancestors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "workspace")
            child = root / "child"
            retired = root / "retired"
            outside = Path(temp, "outside")
            child.mkdir(parents=True)
            outside.mkdir()
            inside_file = child / "public.md"
            inside_file.write_text("inside", encoding="utf-8")
            (outside / "public.md").write_text(
                "OUTSIDE_PRIVATE",
                encoding="utf-8",
            )
            probe = Path(temp, "symlink-probe")
            try:
                probe.symlink_to(outside, target_is_directory=True)
                probe.unlink()
            except OSError as exc:
                self.skipTest(f"Directory symlinks are unavailable: {exc}")

            validated = validate_workspace_root(root)
            original_lstat = os.lstat
            swapped = False

            def replace_before_file_stat(path):
                nonlocal swapped
                if Path(path) == inside_file and not swapped:
                    swapped = True
                    child.rename(retired)
                    child.symlink_to(outside, target_is_directory=True)
                return original_lstat(path)

            try:
                with patch(
                    "llm_backend_toolkit.workspace_observer.os.lstat",
                    side_effect=replace_before_file_stat,
                ), patch(
                    "llm_backend_toolkit.workspace_observer.os.read",
                    side_effect=AssertionError(
                        "ancestor replacement must fail before body open"
                    ),
                ) as body_read:
                    with self.assertRaises(WorkspaceRootError):
                        capture_workspace_snapshot(
                            validated,
                            public_text_allowlist=frozenset(
                                {"child/public.md"}
                            ),
                        )
            finally:
                if child.is_symlink():
                    child.unlink()

        self.assertTrue(swapped)
        body_read.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows handle containment")
    def test_scandir_swap_restore_cannot_add_outside_relative_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "workspace")
            child = root / "child"
            retired = root / "retired"
            outside = Path(temp, "outside")
            child.mkdir(parents=True)
            outside.mkdir()
            (child / "inside.md").write_text("INSIDE", encoding="utf-8")
            outside_name = "outside-only.md"
            (outside / outside_name).write_text(
                "OUTSIDE_PRIVATE",
                encoding="utf-8",
            )
            validated = validate_workspace_root(root)
            original_scandir = os.scandir
            raced = False

            def swap_call_restore(call):
                child.rename(retired)
                child.symlink_to(outside, target_is_directory=True)
                try:
                    return call()
                finally:
                    child.unlink()
                    retired.rename(child)

            def racing_scandir(path):
                nonlocal raced
                if Path(path) == child and not raced:
                    raced = True
                    return swap_call_restore(lambda: original_scandir(path))
                return original_scandir(path)

            with patch(
                "llm_backend_toolkit.workspace_observer.os.scandir",
                side_effect=racing_scandir,
            ), patch(
                "llm_backend_toolkit.workspace_observer.os.read",
                side_effect=AssertionError(
                    "outside enumeration must never lead to a body read"
                ),
            ) as body_read:
                snapshot = capture_workspace_snapshot(
                    validated,
                    public_text_allowlist=frozenset(
                        {f"child/{outside_name}"}
                    ),
                )

        self.assertTrue(raced)
        self.assertNotIn(f"child/{outside_name}", snapshot._files)
        self.assertNotIn(f"child/{outside_name}", snapshot._texts)
        self.assertNotIn(
            "OUTSIDE_PRIVATE",
            json.dumps(snapshot._texts, ensure_ascii=False),
        )
        body_read.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows handle containment")
    def test_lstat_swap_restore_uses_exact_internal_handle_metadata_and_body(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "workspace")
            child = root / "child"
            retired = root / "retired"
            outside = Path(temp, "outside")
            child.mkdir(parents=True)
            outside.mkdir()
            inside_file = child / "public.md"
            inside_file.write_text("INSIDE_PUBLIC", encoding="utf-8")
            (outside / "public.md").write_text(
                "OUTSIDE_PRIVATE",
                encoding="utf-8",
            )
            validated = validate_workspace_root(root)
            original_lstat = os.lstat
            raced = False

            def swap_call_restore(call):
                child.rename(retired)
                child.symlink_to(outside, target_is_directory=True)
                try:
                    return call()
                finally:
                    child.unlink()
                    retired.rename(child)

            def racing_lstat(path):
                nonlocal raced
                if Path(path) == inside_file and not raced:
                    raced = True
                    return swap_call_restore(lambda: original_lstat(path))
                return original_lstat(path)

            with patch(
                "llm_backend_toolkit.workspace_observer.os.lstat",
                side_effect=racing_lstat,
            ):
                snapshot = capture_workspace_snapshot(
                    validated,
                    public_text_allowlist=frozenset(
                        {"child/public.md"}
                    ),
                )

        self.assertTrue(raced)
        self.assertIn("child/public.md", snapshot._files)
        self.assertEqual(
            "INSIDE_PUBLIC",
            snapshot._texts["child/public.md"],
        )
        self.assertNotIn(
            "OUTSIDE_PRIVATE",
            json.dumps(snapshot._texts, ensure_ascii=False),
        )

    @unittest.skipUnless(os.name == "nt", "Windows handle containment")
    def test_open_swap_restore_is_blocked_by_ancestor_guards_before_body_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "workspace")
            child = root / "child"
            retired = root / "retired"
            outside = Path(temp, "outside")
            child.mkdir(parents=True)
            outside.mkdir()
            inside_file = child / "public.md"
            inside_file.write_text("INSIDE_PUBLIC", encoding="utf-8")
            (outside / "public.md").write_text(
                "OUTSIDE_PRIVATE",
                encoding="utf-8",
            )
            validated = validate_workspace_root(root)
            original_open = os.open
            attempted = False

            def racing_open(path, flags, *args, **kwargs):
                nonlocal attempted
                if Path(path) != inside_file:
                    return original_open(path, flags, *args, **kwargs)
                attempted = True
                moved = False
                linked = False
                try:
                    child.rename(retired)
                    moved = True
                    child.symlink_to(outside, target_is_directory=True)
                    linked = True
                    return original_open(path, flags, *args, **kwargs)
                finally:
                    if linked:
                        child.unlink()
                    if moved:
                        retired.rename(child)

            with patch(
                "llm_backend_toolkit.workspace_observer.os.open",
                side_effect=racing_open,
            ), patch(
                "llm_backend_toolkit.workspace_observer.os.read",
                side_effect=AssertionError(
                    "a swapped file handle must be rejected before body read"
                ),
            ) as body_read:
                snapshot = capture_workspace_snapshot(
                    validated,
                    public_text_allowlist=frozenset(
                        {"child/public.md"}
                    ),
                )

        self.assertTrue(attempted)
        self.assertNotIn("child/public.md", snapshot._files)
        self.assertNotIn("child/public.md", snapshot._texts)
        body_read.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows handle containment")
    def test_root_ordinary_directory_swap_restore_rejects_the_opened_handle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "workspace")
            retired = Path(temp, "retired")
            attacker = Path(temp, "attacker")
            root.mkdir()
            inside_file = root / "public.md"
            inside_file.write_text("INSIDE_PUBLIC", encoding="utf-8")
            attacker.mkdir()
            (attacker / "public.md").write_text(
                "OUTSIDE_PRIVATE",
                encoding="utf-8",
            )
            validated = validate_workspace_root(root)
            original_open = os.open
            attempted = False
            swap_blocked = False
            restore_blocked = False

            def racing_open(path, flags, *args, **kwargs):
                nonlocal attempted, swap_blocked, restore_blocked
                if Path(path) != inside_file:
                    return original_open(path, flags, *args, **kwargs)
                attempted = True
                try:
                    root.rename(retired)
                except OSError:
                    swap_blocked = True
                    raise
                attacker.rename(root)
                descriptor = original_open(path, flags, *args, **kwargs)
                try:
                    root.rename(attacker)
                except OSError:
                    restore_blocked = True
                    os.close(descriptor)
                    descriptor = None
                    try:
                        root.rename(attacker)
                    finally:
                        retired.rename(root)
                    raise
                retired.rename(root)
                return descriptor

            with patch(
                "llm_backend_toolkit.workspace_observer.os.open",
                side_effect=racing_open,
            ), patch(
                "llm_backend_toolkit.workspace_observer.os.read",
                side_effect=AssertionError(
                    "an outside root handle must be rejected before body read"
                ),
            ) as body_read:
                snapshot = capture_workspace_snapshot(
                    validated,
                    public_text_allowlist=frozenset({"public.md"}),
                )

        self.assertTrue(attempted)
        self.assertTrue(swap_blocked or restore_blocked)
        self.assertNotIn("public.md", snapshot._files)
        self.assertNotIn("public.md", snapshot._texts)
        body_read.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows handle containment")
    def test_root_junction_swap_restore_rejects_outside_final_handle_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "workspace")
            retired = Path(temp, "retired")
            outside = Path(temp, "outside")
            root.mkdir()
            outside.mkdir()
            inside_file = root / "public.md"
            inside_file.write_text("INSIDE_PUBLIC", encoding="utf-8")
            (outside / "public.md").write_text(
                "OUTSIDE_PRIVATE",
                encoding="utf-8",
            )
            probe = Path(temp, "symlink-probe")
            try:
                probe.symlink_to(outside, target_is_directory=True)
                probe.unlink()
            except OSError as exc:
                self.skipTest(f"Directory symlinks are unavailable: {exc}")

            validated = validate_workspace_root(root)
            original_open = os.open
            raced = False

            def racing_open(path, flags, *args, **kwargs):
                nonlocal raced
                if Path(path) != inside_file:
                    return original_open(path, flags, *args, **kwargs)
                root.rename(retired)
                root.symlink_to(outside, target_is_directory=True)
                try:
                    descriptor = original_open(path, flags, *args, **kwargs)
                    raced = True
                finally:
                    root.unlink()
                    retired.rename(root)
                return descriptor

            with patch(
                "llm_backend_toolkit.workspace_observer.os.open",
                side_effect=racing_open,
            ), patch(
                "llm_backend_toolkit.workspace_observer.os.read",
                side_effect=AssertionError(
                    "an outside final handle must be rejected before body read"
                ),
            ) as body_read:
                snapshot = capture_workspace_snapshot(
                    validated,
                    public_text_allowlist=frozenset({"public.md"}),
                )

        self.assertTrue(raced)
        self.assertNotIn("public.md", snapshot._files)
        self.assertNotIn("public.md", snapshot._texts)
        body_read.assert_not_called()

    def test_workspace_replaced_during_initial_snapshot_is_blocked_before_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "workspace")
            retired = Path(temp, "retired")
            root.mkdir()
            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": runner},
            )

            def replace_root(workspace, *, public_text_allowlist):
                root.rename(retired)
                root.mkdir()
                return WorkspaceSnapshot("unavailable", {})

            with patch(
                "llm_backend_toolkit.toolkit.capture_workspace_snapshot",
                side_effect=replace_root,
            ) as snapshot:
                result = toolkit.invoke(
                    agent_request(root),
                    progress_callback=lambda event: None,
                )

        self.assertEqual("blocked", result["status"])
        self.assertEqual("invalid_request", result["error"]["category"])
        self.assertEqual([], runner.calls)
        self.assertEqual(1, snapshot.call_count)

    def test_windows_reparse_metadata_is_always_excluded(self):
        class ReparseStat:
            st_file_attributes = 0x400

        self.assertTrue(_is_reparse_point(ReparseStat()))

    def test_replaced_file_identity_is_rejected_before_any_body_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            observed_path = root / "observed.md"
            replacement_path = root / "replacement.md"
            observed_path.write_text("inside", encoding="utf-8")
            replacement_path.write_text("outside", encoding="utf-8")
            observed = os.lstat(observed_path)
            replacement_descriptor = os.open(
                replacement_path,
                os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0),
            )
            with patch(
                "llm_backend_toolkit.workspace_observer.os.open",
                return_value=replacement_descriptor,
            ), patch(
                "llm_backend_toolkit.workspace_observer.os.read",
                side_effect=AssertionError("mismatched file body must not be read"),
            ) as body_read:
                text, used = _capture_small_public_text(
                    str(observed_path),
                    "observed.md",
                    observed,
                    remaining_bytes=32 * 1024,
                )

        self.assertIsNone(text)
        self.assertEqual(0, used)
        body_read.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_target_is_not_scanned_or_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp, "workspace")
            outside = Path(temp, "outside")
            link = root / "outside-junction"
            root.mkdir()
            outside.mkdir()
            outside_file = outside / "outside.md"
            outside_file.write_text("before", encoding="utf-8")
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest(f"Cannot create junction: {created.stderr}")
            try:
                before = capture_workspace_snapshot(
                    root,
                    public_text_allowlist=frozenset(
                        {"outside-junction/outside.md"}
                    ),
                )
                outside_file.write_text("after", encoding="utf-8")
                after = capture_workspace_snapshot(
                    root,
                    public_text_allowlist=frozenset(
                        {"outside-junction/outside.md"}
                    ),
                )
                change = compare_workspace_snapshots(before, after)
                self.assertEqual(0, change.changed_files)
                self.assertEqual({}, before._texts)
                self.assertEqual({}, after._texts)
            finally:
                if link.exists():
                    link.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_root_junction_is_blocked_before_runner_or_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            actual = parent / "actual"
            link = parent / "workspace-junction"
            actual.mkdir()
            (actual / "outside.md").write_text("outside", encoding="utf-8")
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(actual)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest(f"Cannot create junction: {created.stderr}")
            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": runner},
            )
            try:
                with patch(
                    "llm_backend_toolkit.toolkit.capture_workspace_snapshot",
                    side_effect=AssertionError(
                        "root junctions must fail before observer access"
                    ),
                ) as snapshot:
                    result = toolkit.invoke(
                        agent_request(link),
                        progress_callback=lambda event: None,
                    )
            finally:
                if link.exists():
                    link.rmdir()

        self.assertEqual("blocked", result["status"])
        self.assertEqual("invalid_request", result["error"]["category"])
        self.assertEqual([], runner.calls)
        snapshot.assert_not_called()

    def test_read_only_agent_does_not_scan_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            request = agent_request(Path(temp))
            request["execution"]["policy"] = "read-only"
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": FakeRunner()},
            )

            with patch(
                "llm_backend_toolkit.toolkit.capture_workspace_snapshot",
                side_effect=AssertionError("read-only runs must not scan"),
            ) as snapshot:
                result = toolkit.invoke(
                    request,
                    progress_callback=lambda event: None,
                )

        self.assertEqual("ok", result["status"])
        snapshot.assert_not_called()

    def test_agent_policy_defaults_to_codex_full_access(self):
        with tempfile.TemporaryDirectory() as temp:
            request = agent_request(Path(temp))
            del request["execution"]["policy"]
            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": runner},
            )

            result = toolkit.invoke(request)

        self.assertEqual("ok", result["status"])
        self.assertEqual(
            "danger-full-access",
            result["execution_receipt"]["policy"],
        )
        self.assertEqual(
            "danger-full-access",
            runner.calls[0]["execution"]["policy"],
        )

    def test_snapshot_and_progress_callback_failures_do_not_mask_agent_result(self):
        with tempfile.TemporaryDirectory() as temp:
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": FakeRunner()},
            )

            with patch(
                "llm_backend_toolkit.toolkit.capture_workspace_snapshot",
                side_effect=RuntimeError("snapshot unavailable"),
            ):
                result = toolkit.invoke(
                    agent_request(Path(temp)),
                    progress_callback=lambda event: (_ for _ in ()).throw(
                        RuntimeError("observer unavailable")
                    ),
                )

        self.assertEqual("ok", result["status"])

    def test_workspace_event_projection_is_positive_bounded_and_type_safe(self):
        diff = (
            "--- a/acceptance.md\n"
            "+++ b/acceptance.md\n"
            "@@ -1 +1 @@\n"
            "-待处理\n"
            "+已通过\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            event = append_event(
                Path(temp),
                "workspace.change.observed",
                "运行期间观察到 1 个文件条目元数据发生变化。",
                payload={
                    "changed_files": 1,
                    "scan_status": "scoped_complete",
                    "provenance": "workspace_before_after",
                    "attribution": "unverified_concurrent_window",
                    "detail_policy": "caller_public_safe_include",
                    "changes": [
                        {
                            "relative_path": "acceptance.md",
                            "change_kind": "modified",
                            "lines_added": 1,
                            "lines_deleted": 1,
                            "diff_status": "available",
                            "unified_diff": diff,
                            "absolute_path": "C:\\private\\acceptance.md",
                        }
                    ],
                },
            )

        self.assertEqual(1, event["payload"]["changed_files"])
        self.assertEqual("scoped_complete", event["payload"]["scan_status"])
        self.assertEqual("acceptance.md", event["payload"]["changes"][0]["relative_path"])
        self.assertEqual(diff, event["payload"]["changes"][0]["unified_diff"])
        self.assertNotIn("absolute_path", event["payload"]["changes"][0])

        for malformed in ([], {}):
            with tempfile.TemporaryDirectory() as temp:
                invalid = append_event(
                    Path(temp),
                    "workspace.change.observed",
                    "invalid",
                    payload={
                        "changed_files": 1,
                        "scan_status": malformed,
                        "provenance": "workspace_before_after",
                        "attribution": "unverified_concurrent_window",
                        "detail_policy": "count_only",
                    },
                )
            self.assertEqual({}, invalid["payload"])

    def test_workspace_change_count_supports_two_disjoint_bounded_snapshots(self):
        metadata = (0, 1, 1, 1, 1)
        before = WorkspaceSnapshot(
            "scoped_complete",
            {f"a-{index}.bin": metadata for index in range(20_000)},
        )
        after = WorkspaceSnapshot(
            "scoped_complete",
            {f"b-{index}.bin": metadata for index in range(20_000)},
        )

        change = compare_workspace_snapshots(before, after)

        self.assertEqual(40_000, change.changed_files)
        with tempfile.TemporaryDirectory() as temp:
            event = append_event(
                Path(temp),
                "workspace.change.observed",
                "运行期间观察到 40000 个文件条目元数据发生变化。",
                payload={
                    "changed_files": change.changed_files,
                    "scan_status": change.scan_status,
                    "provenance": "workspace_before_after",
                    "attribution": "unverified_concurrent_window",
                    "detail_policy": "count_only",
                    "changes": [],
                },
            )
        self.assertEqual(40_000, event["payload"]["changed_files"])

    def test_bounded_process_tails_complete_machine_event_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            event_file = root / "events.jsonl"
            event_file.touch()
            script = root / "emit_events.py"
            script.write_text(
                "import json, pathlib, sys, time\n"
                "path = pathlib.Path(sys.argv[1])\n"
                "with path.open('a', encoding='utf-8') as stream:\n"
                "    for sequence, kind in ((1, 'turn.started'), (2, 'turn.completed')):\n"
                "        stream.write(json.dumps({'schema':'aicli.machine-event.v1','sequence':sequence,'kind':kind}) + '\\n')\n"
                "        stream.flush()\n"
                "        time.sleep(0.08)\n",
                encoding="utf-8",
            )
            observed = []

            code, _, _, _ = _bounded_process(
                [sys.executable, str(script), str(event_file)],
                cwd=root,
                stdin_text="",
                timeout_seconds=5,
                event_file=event_file,
                on_event=observed.append,
            )

            self.assertEqual(0, code)
            self.assertEqual(
                ["turn.started", "turn.completed"],
                [event["kind"] for event in observed],
            )

    def test_aicli_machine_events_become_safe_chinese_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli",
                engine="codex",
                default_profile="codex-ollama-main",
                entry=str(entry),
            )
            progress = []
            envelope = {
                "run": {
                    "exitCode": 0,
                    "durationMs": 50,
                    "stdout": '{"type":"item.completed","item":{"type":"agent_message","text":"完成"}}',
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "hard",
                        "maxToolCalls": "hard",
                    },
                    "limitUsage": {
                        "steps": 2,
                        "toolCalls": 1,
                        "eventsSeen": 6,
                        "protocol": "codex-jsonl",
                        "cleanupConfirmed": True,
                    },
                    "eventProjection": "codex-public-v1",
                    "machineEventProjection": "aicli.machine-event.v1",
                    "machineEventStatus": "ok",
                    "machineEventCount": 4,
                }
            }

            def bounded(command, **kwargs):
                if command[-2:] == ["version", "--json"]:
                    return (
                        0,
                        json.dumps(
                            {
                                "capabilities": {
                                    "machineEventProjection": "aicli.machine-event.v1"
                                }
                            }
                        ),
                        "",
                        2,
                    )
                self.assertIn("--event-file", command)
                self.assertIsNotNone(kwargs.get("event_file"))
                callback = kwargs["on_event"]
                callback(
                    {
                        "schema": "aicli.machine-event.v1",
                        "sequence": 1,
                        "kind": "reasoning.activity",
                        "status": "started",
                        "reasoning": "PRIVATE_REASONING",
                    }
                )
                callback(
                    {
                        "schema": "aicli.machine-event.v1",
                        "sequence": 2,
                        "kind": "tool.activity",
                        "status": "started",
                        "item_type": "file_change",
                        "command": "PRIVATE_COMMAND",
                    }
                )
                callback(
                    {
                        "schema": "aicli.machine-event.v1",
                        "sequence": 3,
                        "kind": "output.completed",
                        "status": "completed",
                        "public_text": "公开阶段结果",
                    }
                )
                callback(
                    {
                        "schema": "aicli.machine-event.v1",
                        "sequence": 4,
                        "kind": "context.usage.updated",
                        "status": "updated",
                        "current_tokens": 202000,
                        "context_window_tokens": 258400,
                        "private_trace": "PRIVATE_CONTEXT_TRACE",
                    }
                )
                callback(
                    {
                        "schema": "aicli.machine-event.v1",
                        "sequence": 5,
                        "kind": "context.compaction.completed",
                        "status": "completed",
                        "compaction_count": 1,
                        "replacement_history": "PRIVATE_COMPACTED_HISTORY",
                    }
                )
                return 0, json.dumps(envelope, ensure_ascii=False), "", 50

            execution = {
                "workspace": str(root),
                "model": "qwen-main-v1",
                "policy": "workspace-write",
                "native_images": [],
                "budget": {
                    "timeout_seconds": 30,
                    "max_steps": 4,
                    "max_tool_calls": 4,
                },
                "_progress_callback": progress.append,
            }
            with patch(
                "llm_backend_toolkit.agent_runners.shutil.which",
                return_value="pwsh",
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=bounded,
            ):
                response = runner.invoke("task", execution)

            self.assertEqual("完成", response.content)
            self.assertEqual("live_safe_events", response.observability_level)
            kinds = [
                event["public_event"]["kind"]
                for event in progress
                if "public_event" in event
            ]
            self.assertIn("agent.reasoning.activity", kinds)
            self.assertIn("agent.tool.activity", kinds)
            self.assertIn("agent.output.completed", kinds)
            self.assertIn("agent.context.usage.updated", kinds)
            self.assertIn("agent.context.compaction.completed", kinds)
            output_event = next(
                event
                for event in progress
                if (event.get("public_event") or {}).get("kind")
                == "agent.output.completed"
            )
            self.assertEqual(
                "公开阶段结果",
                output_event["public_event"]["summary_zh"],
            )
            self.assertEqual("公开阶段结果", output_event["content_replace"])
            self.assertNotIn("content_delta", output_event)
            serialized = json.dumps(progress, ensure_ascii=False)
            self.assertNotIn("PRIVATE_REASONING", serialized)
            self.assertNotIn("PRIVATE_COMMAND", serialized)
            self.assertNotIn("PRIVATE_CONTEXT_TRACE", serialized)
            self.assertNotIn("PRIVATE_COMPACTED_HISTORY", serialized)
            context_event = next(
                event
                for event in progress
                if (event.get("public_event") or {}).get("kind")
                == "agent.context.usage.updated"
            )
            self.assertEqual(
                202000,
                context_event["public_event"]["payload"]["current_tokens"],
            )
            self.assertEqual(
                258400,
                context_event["public_event"]["payload"][
                    "context_window_tokens"
                ],
            )
            self.assertIn("公开阶段结果", serialized)

    def test_aicli_command_execution_events_are_safe_and_specific(self):
        progress = []
        cases = (
            (
                {
                    "kind": "tool.activity",
                    "status": "started",
                    "item_type": "command_execution",
                    "command_status": "in_progress",
                    "command": "PRIVATE_STARTED_COMMAND",
                },
                "智能体正在执行命令。",
                {},
            ),
            (
                {
                    "kind": "tool.activity",
                    "status": "completed",
                    "item_type": "command_execution",
                    "command_status": "succeeded",
                    "exit_code": 0,
                    "duration_ms": 617,
                    "cwd": "PRIVATE_SUCCEEDED_CWD",
                },
                "智能体执行命令成功（退出码 0，耗时 617 毫秒）。",
                {"exit_code": 0, "duration_ms": 617},
            ),
            (
                {
                    "kind": "tool.activity",
                    "status": "completed",
                    "item_type": "command_execution",
                    "command_status": "failed",
                    "exit_code": 9,
                    "duration_ms": 731,
                    "output": "PRIVATE_FAILED_OUTPUT",
                },
                "智能体执行命令失败（退出码 9，耗时 731 毫秒）。",
                {"exit_code": 9, "duration_ms": 731},
            ),
            (
                {
                    "kind": "tool.activity",
                    "status": "completed",
                    "item_type": "command_execution",
                    "command_status": "declined",
                    "arguments": "PRIVATE_DECLINED_ARGUMENTS",
                },
                "智能体的命令执行已被拒绝。",
                {},
            ),
        )

        for event, expected_summary, expected_metrics in cases:
            AiCliProfileRunner._emit_machine_event(progress.append, event)
            public_event = progress[-1]["public_event"]
            self.assertEqual(expected_summary, public_event["summary_zh"])
            self.assertEqual(
                event["command_status"],
                public_event["payload"]["command_status"],
            )
            for key, expected in expected_metrics.items():
                self.assertEqual(expected, public_event["payload"][key])

        serialized = json.dumps(progress, ensure_ascii=False)
        self.assertNotIn("PRIVATE_STARTED_COMMAND", serialized)
        self.assertNotIn("PRIVATE_SUCCEEDED_CWD", serialized)
        self.assertNotIn("PRIVATE_FAILED_OUTPUT", serialized)
        self.assertNotIn("PRIVATE_DECLINED_ARGUMENTS", serialized)

    def test_aicli_invalid_command_projection_is_not_emitted(self):
        invalid_events = (
            {
                "kind": "tool.activity",
                "status": "completed",
                "item_type": "command_execution",
                "command_status": "future_status",
            },
            {
                "kind": "tool.activity",
                "status": "completed",
                "item_type": "command_execution",
                "command_status": {"private": "PRIVATE_STATUS_OBJECT"},
            },
        )

        for event in invalid_events:
            with self.subTest(event=event):
                progress = []
                AiCliProfileRunner._emit_machine_event(progress.append, event)
                self.assertEqual([], progress)

    def test_aicli_protocol_failure_exposes_only_a_safe_static_code(self):
        progress = []

        AiCliProfileRunner._emit_machine_event(
            progress.append,
            {
                "kind": "run.failed",
                "status": "failed",
                "error_category": "protocol_or_process_failure",
                "error_code": "codex_appserver.thread_start_failed",
                "raw_stderr": "PRIVATE_RAW_STDERR",
                "prompt": "PRIVATE_PROMPT",
            },
        )

        self.assertEqual(1, len(progress))
        public_event = progress[0]["public_event"]
        self.assertEqual("agent.run.failed", public_event["kind"])
        self.assertEqual(
            "codex_appserver.thread_start_failed",
            public_event["payload"]["error_code"],
        )
        serialized = json.dumps(progress, ensure_ascii=False)
        self.assertNotIn("PRIVATE_RAW_STDERR", serialized)
        self.assertNotIn("PRIVATE_PROMPT", serialized)

    def test_public_agent_output_delta_is_projected_as_generating_progress(self):
        progress = []

        AiCliProfileRunner._emit_machine_event(
            progress.append,
            {
                "kind": "output.delta",
                "status": "updated",
                "item_type": "agent_message",
                "public_text": "公开增量",
                "steps": 3,
                "tool_calls": 2,
                "events_seen": 9,
                "reasoning": "PRIVATE_HIDDEN_REASONING",
            },
        )

        self.assertEqual(1, len(progress))
        event = progress[0]
        self.assertEqual("generating", event["phase"])
        self.assertEqual(
            "agent.output.delta",
            event["public_event"]["kind"],
        )
        self.assertEqual(
            "公开增量",
            event["public_event"]["summary_zh"],
        )
        self.assertEqual("公开增量", event["content_delta"])
        self.assertNotIn("content_replace", event)
        self.assertEqual(
            {
                "status": "updated",
                "item_type": "agent_message",
                "steps": 3,
                "tool_calls": 2,
                "events_seen": 9,
            },
            event["public_event"]["payload"],
        )
        self.assertNotIn(
            "PRIVATE_HIDDEN_REASONING",
            json.dumps(event, ensure_ascii=False),
        )

    def test_public_reasoning_summary_delta_is_separate_from_answer_draft(self):
        progress = []

        AiCliProfileRunner._emit_machine_event(
            progress.append,
            {
                "kind": "reasoning.summary.delta",
                "status": "updated",
                "item_type": "reasoning_summary",
                "summary_group": 2,
                "summary_index": 0,
                "public_text": "正在核对公开配置。",
                "reasoning": "PRIVATE_RAW_REASONING_CANARY",
            },
        )

        self.assertEqual(1, len(progress))
        event = progress[0]
        self.assertEqual("thinking", event["phase"])
        self.assertEqual(
            "agent.reasoning.summary.delta",
            event["public_event"]["kind"],
        )
        self.assertEqual(
            "公开工作思路正在更新。",
            event["public_event"]["summary_zh"],
        )
        self.assertEqual(
            {
                "summary_group": 2,
                "summary_index": 0,
                "delta": "正在核对公开配置。",
                "truncated": False,
            },
            event["reasoning_summary_delta"],
        )
        self.assertNotIn("content_delta", event)
        self.assertNotIn("content_replace", event)
        self.assertNotIn(
            "PRIVATE_RAW_REASONING_CANARY",
            json.dumps(event, ensure_ascii=False),
        )

    def test_public_reasoning_summary_delta_is_independently_bounded(self):
        progress = []

        AiCliProfileRunner._emit_machine_event(
            progress.append,
            {
                "kind": "reasoning.summary.delta",
                "summary_group": 3,
                "summary_index": 1,
                "public_text": "公开" * 2_100,
                "public_text_truncated": True,
                "reasoning": "PRIVATE_RAW_REASONING_CANARY",
            },
        )

        self.assertEqual(1, len(progress))
        event = progress[0]
        self.assertEqual(3, event["reasoning_summary_delta"]["summary_group"])
        self.assertEqual(1, event["reasoning_summary_delta"]["summary_index"])
        self.assertEqual(4_000, len(event["reasoning_summary_delta"]["delta"]))
        self.assertTrue(event["reasoning_summary_delta"]["truncated"])
        self.assertTrue(event["public_reasoning_summaries_truncated"])
        self.assertNotIn("content_delta", event)
        self.assertNotIn(
            "PRIVATE_RAW_REASONING_CANARY",
            json.dumps(event, ensure_ascii=False),
        )

    def test_public_agent_output_delta_preserves_stream_boundary_whitespace(self):
        progress = []

        AiCliProfileRunner._emit_machine_event(
            progress.append,
            {
                "kind": "output.delta",
                "status": "updated",
                "item_type": "agent_message",
                "public_text": " world\n",
            },
        )

        self.assertEqual(1, len(progress))
        self.assertEqual("world", progress[0]["public_event"]["summary_zh"])
        self.assertEqual(" world\n", progress[0]["content_delta"])

    def test_empty_or_unsafe_public_agent_output_delta_is_not_emitted(self):
        for label, public_text in {
            "empty": " \x00\t\r\n\u202e ",
            "unsafe": r"正在读取 C:\Users\alice\private.txt",
        }.items():
            with self.subTest(label=label):
                progress = []

                AiCliProfileRunner._emit_machine_event(
                    progress.append,
                    {
                        "kind": "output.delta",
                        "status": "updated",
                        "item_type": "agent_message",
                        "public_text": public_text,
                        "steps": 3,
                        "tool_calls": 2,
                        "events_seen": 9,
                    },
                )

                self.assertEqual([], progress)

    def test_public_agent_message_is_bounded_safe_and_used_as_timeline_summary(self):
        progress = []
        AiCliProfileRunner._emit_machine_event(
            progress.append,
            {
                "kind": "output.completed",
                "status": "completed",
                "public_text": (
                    " \x00<script>alert(1)</script>\n"
                    + ("公开进度" * 200)
                    + "\u202e"
                ),
                "reasoning": "PRIVATE_HIDDEN_REASONING",
            },
        )

        self.assertEqual(1, len(progress))
        event = progress[0]
        summary = event["public_event"]["summary_zh"]
        replacement = event["content_replace"]
        self.assertNotIn("content_delta", event)
        self.assertLessEqual(len(summary), 500)
        self.assertGreater(len(replacement), len(summary))
        self.assertLessEqual(len(replacement), 20_000)
        self.assertFalse(event.get("public_preview_truncated", False))
        self.assertTrue(summary.startswith("＜script＞alert(1)＜/script＞ 公开进度"))
        self.assertNotIn("<script>", summary)
        self.assertNotIn("<script>", replacement)
        self.assertFalse(any(ord(char) < 32 for char in summary))
        self.assertNotIn(
            "PRIVATE_HIDDEN_REASONING",
            json.dumps(event, ensure_ascii=False),
        )

        truncated = []
        AiCliProfileRunner._emit_machine_event(
            truncated.append,
            {
                "kind": "output.completed",
                "status": "completed",
                "public_text": "公开" * 11_000,
            },
        )
        self.assertTrue(truncated[0]["public_preview_truncated"])

    def test_empty_public_agent_message_is_not_emitted(self):
        progress = []

        AiCliProfileRunner._emit_machine_event(
            progress.append,
            {
                "kind": "output.completed",
                "status": "completed",
                "public_text": " \x00\t\r\n\u202e ",
            },
        )

        self.assertEqual([], progress)

    def test_unsafe_public_agent_messages_are_not_emitted(self):
        unsafe_messages = {
            "windows_drive": r"正在读取 C:\Users\alice\private.txt",
            "posix_single_segment": "/opt",
            "posix": "正在读取 /opt/private/config.json",
            "posix_adjacent_chinese": "路径是/opt/private/config.json",
            "unc": "//server/share/private.txt 已处理",
            "unc_adjacent_chinese": "路径是//server/share/private.txt",
            "windows_nt": r"正在读取 \Device\HarddiskVolume3\private.txt",
            "windows_nt_adjacent_chinese": (
                r"路径是\Device\HarddiskVolume3\private.txt"
            ),
            "file_uri": "正在读取 file:///opt/private/config.json",
            "secret_crossing_limit": (
                ("安全" * 245) + " token=abcdefgh12345678"
            ),
        }

        for label, public_text in unsafe_messages.items():
            with self.subTest(label=label):
                progress = []
                AiCliProfileRunner._emit_machine_event(
                    progress.append,
                    {
                        "kind": "output.completed",
                        "status": "completed",
                        "public_text": public_text,
                    },
                )

                self.assertEqual([], progress)

    def test_normal_chinese_public_agent_message_is_emitted(self):
        safe_messages = {
            "plain_chinese": (
                "已完成公开进度复核，未发现异常。",
                "已完成公开进度复核，未发现异常。",
            ),
            "chinese_slash": (
                "输入/输出均已完成。",
                "输入/输出均已完成。",
            ),
            "sanitized_tag": (
                "<script>公开状态</script>",
                "＜script＞公开状态＜/script＞",
            ),
            "public_url": (
                "详情见 https://example.com/input/output",
                "详情见 https://example.com/input/output",
            ),
        }

        for label, (public_text, expected) in safe_messages.items():
            with self.subTest(label=label):
                progress = []
                AiCliProfileRunner._emit_machine_event(
                    progress.append,
                    {
                        "kind": "output.completed",
                        "status": "completed",
                        "public_text": public_text,
                    },
                )

                self.assertEqual(1, len(progress))
                event = progress[0]
                self.assertEqual(
                    expected,
                    event["public_event"]["summary_zh"],
                )
                self.assertEqual(expected, event["content_replace"])
                self.assertNotIn("content_delta", event)

    def test_qwen_json_array_is_parsed_without_returning_event_trace(self):
        values = _json_values('[{"type":"tool","content":"hidden"},{"result":"FINAL_ONLY","stats":{"tools":{"totalCalls":3}}}]')
        self.assertEqual(2, len(values))
        self.assertEqual("FINAL_ONLY", values[-1]["result"])

    def test_all_default_agents_use_the_sandboxed_aicli_machine_boundary(self):
        runners = default_runners()
        for name in ("qwen-code", "opencode", "codex-cli", "claude-code"):
            self.assertIsInstance(runners[name], AiCliProfileRunner)
        self.assertIs(runners["data_factory"], runners["codex-cli"])

    def test_cloud_status_exposes_direct_only_backend_without_a_provider_call(self):
        toolkit = Toolkit(
            providers={
                "qwen3.7-flash": OpenAIChatProvider(
                    model="qwen3.7-flash",
                    base_url="https://example.invalid/v1",
                    api_key="configured-for-status",
                    thinking_field="enable_thinking",
                )
            },
            runners={},
        )

        result = toolkit.status("qwen3.7-flash")

        self.assertEqual("ok", result["status"])
        status = result["provider_status"]
        self.assertFalse(status["live_call_performed"])
        self.assertNotIn("agent_default", status)
        self.assertNotIn("agent_supported_runners", status)

    def test_legacy_direct_agent_runners_are_fail_closed(self):
        for runner in (QwenCodeRunner(executable="qwen"), OpenCodeRunner(executable="opencode")):
            with self.assertRaises(AgentRunnerError) as raised:
                runner.invoke("task", {"workspace": ".", "budget": {}})
            self.assertEqual("unsafe_direct_runner_disabled", raised.exception.error.category)

    def test_codex_agent_attaches_approved_native_images(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            image = root / "input.png"
            image.write_bytes(b"png")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-ollama-main", entry=str(entry)
            )
            execution = {
                "workspace": str(root),
                "model": "qwen-main-v1",
                "policy": "workspace-write",
                "native_images": [str(image)],
                "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 4},
            }
            envelope = {
                "run": {
                    "exitCode": 0,
                    "durationMs": 12,
                    "model": "qwen-main-v1",
                    "stdout": "\n".join(
                        [
                            '{"type":"thread.started","thread_id":"thread-1"}',
                            '{"type":"turn.started","turn_id":"turn-1"}',
                            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
                        ]
                    ),
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "hard",
                        "maxToolCalls": "hard",
                    },
                    "limitUsage": {
                        "steps": 0,
                        "toolCalls": 0,
                        "eventsSeen": 3,
                        "protocol": "codex-jsonl",
                        "stepDefinition": "distinct-non-output-thread-item-v2",
                        "cleanupConfirmed": True,
                        "cleanupMethod": "none",
                    },
                    "usage": {
                        "input_tokens": 120,
                        "cached_input_tokens": 32,
                        "output_tokens": 8,
                        "reasoning_output_tokens": 4,
                        "total_tokens": 132,
                        "current_context_tokens": 202000,
                        "context_window_tokens": 258400,
                    },
                    "eventProjection": "codex-public-v1",
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=after_codex_machine_event_probe(
                    (0, json.dumps(envelope), "", 12)
                ),
            ) as bounded:
                response = runner.invoke("task", execution)

            command = bounded.call_args.args[0]
            self.assertEqual("done", response.content)
            self.assertIn("--image", command)
            self.assertIn(str(image), command)
            self.assertIn("--model", command)
            self.assertEqual("qwen-main-v1", command[command.index("--model") + 1])
            self.assertEqual("hard", response.limit_enforcement["timeout"])
            self.assertEqual("hard", response.limit_enforcement["maxSteps"])
            self.assertEqual("hard", response.limit_enforcement["maxToolCalls"])
            self.assertEqual(0, response.steps)
            self.assertEqual(0, response.tool_calls)
            self.assertEqual("codex-jsonl", response.limit_usage["protocol"])
            self.assertEqual(
                "distinct-non-output-thread-item-v2",
                response.limit_usage["step_definition"],
            )
            self.assertTrue(response.limit_usage["cleanup_confirmed"])
            self.assertEqual("", response.limit_hit)
            self.assertEqual("codex-public-v1", response.event_projection)
            self.assertEqual(
                {
                    "input_tokens": 120,
                    "cached_input_tokens": 32,
                    "output_tokens": 8,
                    "reasoning_output_tokens": 4,
                    "total_tokens": 132,
                    "current_context_tokens": 202000,
                    "context_window_tokens": 258400,
                },
                response.usage,
            )

    def test_codex_agent_fails_closed_when_aicli_reports_a_different_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli",
                engine="codex",
                default_profile="codex-qwen-paygo",
                entry=str(entry),
            )
            execution = {
                "workspace": str(root),
                "model": "qwen3.7-flash",
                "profile": "codex-qwen-paygo",
                "policy": "read-only",
                "native_images": [],
                "budget": {
                    "timeout_seconds": 30,
                    "max_steps": 4,
                    "max_tool_calls": 4,
                },
            }
            envelope = {
                "run": {
                    "exitCode": 0,
                    "durationMs": 12,
                    "model": "qwen3.7-max-2026-06-08",
                    "stdout": "\n".join(
                        [
                            '{"type":"thread.started","thread_id":"thread-1"}',
                            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
                        ]
                    ),
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "hard",
                        "maxToolCalls": "hard",
                    },
                    "limitUsage": {
                        "steps": 0,
                        "toolCalls": 0,
                        "eventsSeen": 2,
                        "protocol": "codex-app-server",
                        "stepDefinition": "distinct-non-output-thread-item-v2",
                        "cleanupConfirmed": True,
                        "cleanupMethod": "none",
                    },
                }
            }
            with patch(
                "llm_backend_toolkit.agent_runners.shutil.which",
                return_value="pwsh",
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=after_codex_machine_event_probe(
                    (0, json.dumps(envelope), "", 12)
                ),
            ):
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke("task", execution)

            self.assertEqual("agent_model_mismatch", raised.exception.error.category)
            self.assertEqual(
                "qwen3.7-flash",
                raised.exception.receipt["requested_model"],
            )
            self.assertEqual(
                "qwen3.7-max-2026-06-08",
                raised.exception.receipt["effective_model"],
            )

    def test_aicli_agent_usage_ignores_non_integer_or_negative_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli",
                engine="codex",
                default_profile="codex-ollama-main",
                entry=str(entry),
            )
            envelope = {
                "run": {
                    "exitCode": 0,
                    "durationMs": 1000,
                    "stdout": (
                        '{"type":"item.completed",'
                        '"item":{"type":"agent_message","text":"done"}}'
                    ),
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "hard",
                        "maxToolCalls": "hard",
                    },
                    "usage": {
                        "input_tokens": 0,
                        "cached_input_tokens": -1,
                        "output_tokens": True,
                        "untrusted_extra": 99,
                    },
                }
            }

            with patch(
                "llm_backend_toolkit.agent_runners.shutil.which",
                return_value="pwsh",
            ), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=after_codex_machine_event_probe(
                    (0, json.dumps(envelope), "", 1000)
                ),
            ):
                response = runner.invoke(
                    "task",
                    {
                        "workspace": str(root),
                        "model": "qwen-main-v1",
                        "policy": "workspace-write",
                        "native_images": [],
                        "budget": {
                            "timeout_seconds": 30,
                            "max_steps": 4,
                            "max_tool_calls": 4,
                        },
                    },
                )

            self.assertEqual({"input_tokens": 0}, response.usage)

    def test_codex_agent_rejects_a_success_envelope_without_hard_event_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-ollama-main", entry=str(entry)
            )
            envelope = {
                "run": {
                    "exitCode": 0,
                    "durationMs": 12,
                    "stdout": '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "not-enforced",
                        "maxToolCalls": "not-enforced",
                    },
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=after_codex_machine_event_probe(
                    (0, json.dumps(envelope), "", 12)
                ),
            ):
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke(
                        "task",
                        {
                            "workspace": str(root),
                            "model": "qwen-main-v1",
                            "policy": "workspace-write",
                            "native_images": [],
                            "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 4},
                        },
                    )

            self.assertEqual("agent_budget_unenforced", raised.exception.error.category)
            self.assertEqual("not-enforced", raised.exception.receipt["limit_enforcement"]["maxSteps"])

    def test_codex_agent_returns_a_bounded_limit_receipt_without_hidden_reasoning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-ollama-main", entry=str(entry)
            )
            envelope = {
                "run": {
                    "exitCode": 75,
                    "durationMs": 22,
                    "stdout": "\n".join(
                        [
                            '{"type":"thread.started","thread_id":"thread-1"}',
                            '{"type":"item.completed","item":{"type":"agent_message","text":"safe public progress"}}',
                        ]
                    ),
                    "stderr": "Agent exceeded max_tool_calls=1.",
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "hard",
                        "maxToolCalls": "hard",
                    },
                    "limitUsage": {
                        "steps": 1,
                        "toolCalls": 2,
                        "eventsSeen": 5,
                        "protocol": "codex-jsonl",
                    },
                    "limitHit": "maxToolCalls",
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=after_codex_machine_event_probe(
                    (1, json.dumps(envelope), "", 22)
                ),
            ):
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke(
                        "task",
                        {
                            "workspace": str(root),
                            "model": "qwen-main-v1",
                            "policy": "workspace-write",
                            "native_images": [],
                            "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 1},
                        },
                    )

            self.assertEqual("agent_budget_exceeded", raised.exception.error.category)
            self.assertEqual("maxToolCalls", raised.exception.receipt["limit_hit"])
            self.assertEqual(2, raised.exception.receipt["tool_calls"])
            self.assertNotIn("reasoning", __import__("json").dumps(raised.exception.receipt).lower())

    def test_codex_event_protocol_failure_is_reported_as_budget_unenforced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-ollama-main", entry=str(entry)
            )
            envelope = {
                "run": {
                    "exitCode": 74,
                    "durationMs": 8,
                    "stdout": "",
                    "stderr": "Codex emitted a non-JSON event line.",
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "failed-closed",
                        "maxToolCalls": "failed-closed",
                    },
                    "limitHit": "maxToolCalls",
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=after_codex_machine_event_probe(
                    (1, json.dumps(envelope), "", 8)
                ),
            ):
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke(
                        "task",
                        {
                            "workspace": str(root),
                            "model": "qwen-main-v1",
                            "policy": "workspace-write",
                            "native_images": [],
                            "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 4},
                        },
                    )

            self.assertEqual("agent_budget_unenforced", raised.exception.error.category)
            self.assertEqual("budget_unenforced", raised.exception.receipt["stop_reason"])

    def test_aicli_wall_timeout_is_normalized_as_a_hard_timeout_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-ollama-main", entry=str(entry)
            )
            envelope = {
                "run": {
                    "exitCode": 3,
                    "durationMs": 30000,
                    "stdout": "",
                    "stderr": "Child process exceeded the configured wall timeout.",
                    "timedOut": True,
                    "limitEnforcement": {
                        "timeout": "hard",
                        "maxSteps": "hard",
                        "maxToolCalls": "hard",
                    },
                    "limitHit": "timeout",
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=after_codex_machine_event_probe(
                    (1, json.dumps(envelope), "", 30000)
                ),
            ):
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke(
                        "task",
                        {
                            "workspace": str(root),
                            "model": "qwen-main-v1",
                            "policy": "workspace-write",
                            "native_images": [],
                            "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 4},
                        },
                    )

            self.assertEqual("agent_timeout", raised.exception.error.category)
            self.assertEqual("timeout", raised.exception.receipt["limit_hit"])
            self.assertEqual("hard", raised.exception.receipt["limit_enforcement"]["timeout"])

    def test_toolkit_maps_a_hard_agent_budget_hit_to_blocked(self):
        class BudgetRunner:
            def invoke(self, prompt, execution):
                raise AgentRunnerError(
                    ToolError(
                        category="agent_budget_exceeded",
                        summary="hard limit reached",
                        retryable=False,
                        options=("increase-budget", "handle-in-codex"),
                    ),
                    {
                        "runner": "codex-cli",
                        "exit_code": 75,
                        "duration_ms": 22,
                        "steps": 1,
                        "tool_calls": 2,
                        "stop_reason": "maxToolCalls",
                        "limit_hit": "maxToolCalls",
                        "limit_enforcement": {
                            "timeout": "hard",
                            "maxSteps": "hard",
                            "maxToolCalls": "hard",
                        },
                    },
                )

        with tempfile.TemporaryDirectory() as temp:
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": BudgetRunner()},
            )
            result = toolkit.invoke(agent_request(Path(temp)))

        self.assertEqual("blocked", result["status"])
        self.assertEqual("agent_budget_exceeded", result["error"]["category"])
        self.assertEqual("maxToolCalls", result["execution_receipt"]["limit_hit"])

    @patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True)
    @patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True)
    @patch("llm_backend_toolkit.agent_runners.os.name", "nt")
    @patch("llm_backend_toolkit.agent_runners.subprocess.run")
    @patch("llm_backend_toolkit.agent_runners.subprocess.Popen")
    def test_outer_timeout_kills_the_complete_windows_process_tree(self, popen, native_run):
        process = MagicMock()
        process.pid = 24680
        process.wait.return_value = 0
        process.poll.return_value = 1
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["agent"], timeout=1),
            ("", ""),
        ]
        native_run.return_value.returncode = 0
        popen.return_value = process

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AgentRunnerError) as raised:
                _bounded_process(
                    ["agent"],
                    cwd=Path(temp),
                    stdin_text="task",
                    timeout_seconds=1,
                )

        self.assertEqual("agent_timeout", raised.exception.error.category)
        native_run.assert_called_once()
        self.assertEqual(
            ["taskkill", "/PID", "24680", "/T", "/F"],
            native_run.call_args.args[0],
        )
        self.assertTrue(
            popen.call_args.kwargs["creationflags"]
            & getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        self.assertTrue(
            popen.call_args.kwargs["creationflags"]
            & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        self.assertEqual(
            getattr(subprocess, "CREATE_NO_WINDOW", 0),
            native_run.call_args.kwargs["creationflags"],
        )

    @patch("llm_backend_toolkit.agent_runners.os.name", "nt")
    @patch("llm_backend_toolkit.agent_runners.subprocess.run")
    @patch("llm_backend_toolkit.agent_runners.subprocess.Popen")
    def test_outer_timeout_fails_closed_when_windows_tree_cleanup_is_unconfirmed(self, popen, native_run):
        process = MagicMock()
        process.pid = 13579
        process.wait.side_effect = subprocess.TimeoutExpired(cmd=["agent"], timeout=5)
        process.poll.return_value = None
        process.communicate.side_effect = subprocess.TimeoutExpired(cmd=["agent"], timeout=1)
        native_run.return_value.returncode = 1
        popen.return_value = process

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AgentRunnerError) as raised:
                _bounded_process(
                    ["agent"],
                    cwd=Path(temp),
                    stdin_text="task",
                    timeout_seconds=1,
                )

        self.assertEqual("agent_budget_unenforced", raised.exception.error.category)
        self.assertFalse(raised.exception.receipt["cleanup_confirmed"])

    def test_cloud_agent_is_blocked_when_no_validated_route_is_registered(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner(
                AgentResponse(
                    content='{"answer": 56}',
                    runner="codex-cli",
                    model="qwen3.7-flash",
                    exit_code=0,
                    duration_ms=456,
                )
            )
            toolkit = Toolkit(
                providers={"qwen3.7-flash": FakeCloudProvider()},
                runners={"data_factory": runner},
            )
            request = agent_request(Path(temp))
            request["provider"] = "qwen3.7-flash"
            request["privacy"] = {"cloud_allowed": True}
            del request["execution"]["runner"]

            result = toolkit.invoke(request)

            self.assertEqual("blocked", result["status"])
            self.assertEqual("agent_runner_incompatible", result["error"]["category"])
            self.assertEqual([], runner.calls)
            self.assertEqual("top_model", result["decision"]["owner"])

    def test_fast_middle_spark_pins_its_exact_profile_model_and_xhigh_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner(
                AgentResponse(
                    content='{"answer": 56}',
                    runner="codex-cli",
                    model="gpt-5.3-codex-spark",
                    exit_code=0,
                    duration_ms=321,
                )
            )
            toolkit = Toolkit(runners={"codex-cli": runner, "data_factory": runner})
            request = agent_request(Path(temp))
            request["backend"] = "fast-middle-agent"
            request["privacy"] = {"cloud_allowed": True}

            result = toolkit.invoke(request)

            self.assertEqual("ok", result["status"])
            execution = runner.calls[0]["execution"]
            self.assertEqual("codex-spark-xhigh", execution["profile"])
            self.assertEqual("gpt-5.3-codex-spark", execution["model"])
            receipt = result["execution_receipt"]
            self.assertEqual("xhigh", receipt["reasoning_effort"])
            self.assertTrue(receipt["route_live_verified"])
            self.assertEqual(
                "aicli_source_codex_0.145_workspace_rootfix_live_2026-07-29",
                receipt["route_basis"],
            )
            self.assertFalse(receipt["fallback_used"])
            self.assertFalse(result["backend"]["default_applied"])

    def test_withdrawn_qwen38_agent_route_fails_before_runner_invocation(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner()
            toolkit = Toolkit(runners={"codex-cli": runner})
            request = agent_request(Path(temp))
            request["backend"] = "qwen3.8-max"
            request["privacy"] = {"cloud_allowed": True}
            request["execution"]["runner"] = "codex-cli"
            request["execution"].pop("policy")
            request["execution"].pop("budget")

            result = toolkit.invoke(request)
            self.assertEqual("blocked", result["status"])
            self.assertEqual("invalid_request", result["error"]["category"])
            self.assertIn("Unknown backend", result["error"]["summary"])
            self.assertEqual([], runner.calls)

    def test_cloud_agent_rejects_a_runner_without_an_exact_cloud_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen3.7-flash": FakeCloudProvider()},
                runners={"qwen-code": runner},
            )
            request = agent_request(Path(temp))
            request["provider"] = "qwen3.7-flash"
            request["privacy"] = {"cloud_allowed": True}
            request["execution"]["runner"] = "qwen-code"

            result = toolkit.invoke(request)

            self.assertEqual("blocked", result["status"])
            self.assertEqual("agent_runner_incompatible", result["error"]["category"])
            self.assertEqual([], runner.calls)
            self.assertEqual("top_model", result["decision"]["owner"])

    def test_agent_mode_uses_compacted_prompt_and_never_calls_direct_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = FakeProvider()
            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": provider},
                runners={"data_factory": runner},
            )

            result = toolkit.invoke(agent_request(Path(temp)))

            self.assertEqual("ok", result["status"])
            self.assertEqual({"answer": 56}, result["output"])
            self.assertEqual([], provider.calls)
            self.assertEqual(1, len(runner.calls))
            self.assertIn("record-a", runner.calls[0]["prompt"])
            self.assertEqual("compact", result["context_receipt"]["mode"])
            self.assertEqual("qwen-code", result["execution_receipt"]["runner"])
            self.assertEqual(4, result["execution_receipt"]["tool_calls"])
            self.assertNotIn("reasoning", result)

    def test_agent_mode_normalizes_usage_and_marks_wall_clock_tps_as_estimated(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner(
                AgentResponse(
                    content='{"answer": 56}',
                    runner="codex-cli",
                    model="qwen-main-v1",
                    exit_code=0,
                    duration_ms=2000,
                    usage={
                        "input_tokens": 120,
                        "cached_input_tokens": 32,
                        "output_tokens": 40,
                        "reasoning_output_tokens": 12,
                        "total_tokens": 172,
                        "current_context_tokens": 202000,
                        "context_window_tokens": 258400,
                    },
                )
            )
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": runner},
            )

            result = toolkit.invoke(agent_request(Path(temp)))

            self.assertEqual(
                {
                    "prompt_tokens": 120,
                    "cached_tokens": 32,
                    "completion_tokens": 40,
                    "reasoning_tokens": 12,
                    "current_context_tokens": 202000,
                    "context_window_tokens": 258400,
                    "total_tokens": 172,
                    "elapsed_seconds": 2.0,
                    "tps": 20.0,
                    "tps_source": "wall_clock_estimate",
                },
                result["usage"],
            )
            self.assertNotIn("eval_duration_ns", result["usage"])

    def test_agent_usage_does_not_invent_total_without_upstream_total(self):
        usage = Toolkit._agent_usage(
            AgentResponse(
                content='{"answer": 56}',
                runner="codex-cli",
                model="qwen-main-v1",
                exit_code=0,
                duration_ms=2000,
                usage={
                    "input_tokens": 120,
                    "output_tokens": 40,
                    "reasoning_output_tokens": 12,
                },
            )
        )

        self.assertNotIn("total_tokens", usage)
        self.assertEqual(12, usage["reasoning_tokens"])

    def test_zero_tool_call_budget_is_preserved_instead_of_replaced_by_the_default(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": runner},
            )
            request = agent_request(Path(temp))
            request["execution"]["budget"]["max_tool_calls"] = 0

            result = toolkit.invoke(request)

            self.assertEqual("ok", result["status"])
            self.assertEqual(0, runner.calls[0]["execution"]["budget"]["max_tool_calls"])
            self.assertEqual(0, result["execution_receipt"]["budget"]["max_tool_calls"])

    def test_agent_mode_never_falls_back_to_an_unrequested_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            other = FakeRunner()
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"opencode": other},
            )
            request = agent_request(Path(temp))
            request["execution"]["runner"] = "missing-runner"

            result = toolkit.invoke(request)

            self.assertEqual("blocked", result["status"])
            self.assertEqual("agent_runner_unavailable", result["error"]["category"])
            self.assertEqual([], other.calls)
            self.assertEqual("top_model", result["decision"]["owner"])

    def test_failed_agent_returns_result_side_timing_without_an_event_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            toolkit = Toolkit(
                providers={"qwen-main-v1": FakeProvider()},
                runners={"data_factory": FailingRunner()},
            )

            result = toolkit.invoke(agent_request(Path(temp)))

            self.assertEqual("failed", result["status"])
            self.assertEqual(101700, result["execution_receipt"]["duration_ms"])
            self.assertEqual(1, result["execution_receipt"]["exit_code"])
            self.assertNotIn("events", result["execution_receipt"])

    def test_agent_mode_requires_a_real_workspace(self):
        toolkit = Toolkit(providers={"qwen-main-v1": FakeProvider()}, runners={})
        request = agent_request(Path("Z:/definitely-missing-agent-workspace"))

        result = toolkit.invoke(request)

        self.assertEqual("blocked", result["status"])
        self.assertEqual("invalid_request", result["error"]["category"])

    def test_cloud_agent_billing_failure_returns_the_decision_to_the_top_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / "aicli.ps1"
            entry.write_text("# stub\n", encoding="utf-8")
            runner = AiCliProfileRunner(
                name="codex-cli", engine="codex", default_profile="codex-cloud-paygo", entry=str(entry)
            )
            execution = {
                "workspace": str(root),
                "model": "remote-model-v1",
                "profile": "codex-cloud-paygo",
                "policy": "workspace-write",
                "native_images": [],
                "budget": {"timeout_seconds": 30, "max_steps": 4, "max_tool_calls": 4},
            }
            envelope = {
                "run": {
                    "exitCode": 1,
                    "durationMs": 12,
                    "stdout": "",
                    "stderr": "HTTP 400: Arrearage",
                }
            }
            with patch("llm_backend_toolkit.agent_runners.shutil.which", return_value="pwsh"), patch(
                "llm_backend_toolkit.agent_runners._bounded_process",
                side_effect=after_codex_machine_event_probe(
                    (1, json.dumps(envelope), "", 12)
                ),
            ):
                with self.assertRaises(AgentRunnerError) as raised:
                    runner.invoke("task", execution)

            self.assertEqual("billing_unavailable", raised.exception.error.category)
            self.assertEqual("top_model", raised.exception.error.decision_owner)
            self.assertIn("invoke:local-default", raised.exception.error.options)


if __name__ == "__main__":
    unittest.main()
