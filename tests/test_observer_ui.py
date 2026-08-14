from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = REPO_ROOT / "src" / "llm_backend_toolkit" / "observer_ui"


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.landmarks: set[str] = set()
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag in {"main", "nav", "section", "aside"}:
            self.landmarks.add(tag)
        if tag == "link" and values.get("rel") == "stylesheet":
            self.stylesheets.append(str(values.get("href") or ""))
        if tag == "script" and values.get("src"):
            self.scripts.append(str(values["src"]))


class ObserverUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
        cls.css = (UI_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.js = (UI_ROOT / "app.js").read_text(encoding="utf-8")

    def test_static_bundle_is_local_and_self_contained(self) -> None:
        parser = _DocumentParser()
        parser.feed(self.html)
        self.assertEqual(
            ["/assets/styles.css?v=20260814-conversations-15"],
            parser.stylesheets,
        )
        self.assertEqual(
            ["/assets/app.js?v=20260814-conversations-15"],
            parser.scripts,
        )
        self.assertIn('href="/assets/favicon.svg"', self.html)
        self.assertTrue((UI_ROOT / "favicon.svg").is_file())
        self.assertFalse(re.search(r"https?://|//cdn", self.html, re.IGNORECASE))
        self.assertNotIn("@import", self.css)

    def test_document_is_a_desktop_three_column_conversation_view(self) -> None:
        parser = _DocumentParser()
        parser.feed(self.html)
        self.assertIn("<title>模型调用观察台</title>", self.html)
        self.assertTrue({"main", "nav", "section", "aside"}.issubset(parser.landmarks))
        self.assertTrue(
            {
                "conversation-list",
                "conversation-feed",
                "turns",
                "facts-content",
                "fact-reasoning",
                "fact-tps",
                "connection-state",
            }.issubset(parser.ids)
        )
        self.assertIn("grid-template-columns: 276px minmax(480px, 1fr) 300px", self.css)
        self.assertIn("min-width: 1120px", self.css)
        self.assertNotIn("@media (max-width", self.css)

    def test_ui_contains_only_read_observation_controls(self) -> None:
        forbidden_ids = {
            "new-thread",
            "composer",
            "stop-button",
            "approval-button",
            "model-select",
            "settings-button",
            "files-button",
            "project-button",
            "inspector-toggle",
            "timeline",
        }
        parser = _DocumentParser()
        parser.feed(self.html)
        self.assertTrue(forbidden_ids.isdisjoint(parser.ids))
        self.assertNotIn("<textarea", self.html)
        self.assertNotIn("<input", self.html)
        self.assertIn("不会在这里提交消息、停止任务、审批操作或改变模型设置", self.html)
        self.assertNotRegex(self.js, r"fetch\([^\n]+method\s*:\s*[\"'](?:POST|PUT|PATCH|DELETE)")

    def test_frontend_uses_conversation_api_and_safe_event_paging(self) -> None:
        self.assertIn("/api/conversations?limit=", self.js)
        self.assertIn("/api/conversations/${encodeURIComponent(rootId)}", self.js)
        self.assertIn("/api/runs/${encodeURIComponent(jobId)}/events", self.js)
        self.assertIn('new EventSource("/api/stream")', self.js)
        self.assertIn("conversation.root_job_id", self.js)
        self.assertIn("const MAX_EVENTS_PER_TURN = 240", self.js)
        self.assertIn("const MAX_PINNED_EVENTS_PER_TURN = 8", self.js)
        self.assertIn("MAX_EVENTS_PER_TURN - MAX_PINNED_EVENTS_PER_TURN", self.js)
        self.assertIn("返回最新原始技术事件", self.js)
        self.assertIn(
            "!process.fullHistory && (hasEarlier || page?.browsingEarlier)",
            self.js,
        )
        self.assertNotIn("/api/runs?limit=", self.js)

    def test_conversation_header_uses_the_latest_turn_label(self) -> None:
        self.assertIn(
            "elements.title.textContent = taskLabel(first(latest, listItem));",
            self.js,
        )
        self.assertNotIn(
            "elements.title.textContent = taskLabel(first(turns[0], listItem));",
            self.js,
        )

    def test_new_managed_conversation_auto_follows_without_stealing_history(self) -> None:
        self.assertIn("followLatestConversation: true", self.js)
        self.assertIn(
            "followLatest: rootId === newestConversationRoot(state.conversations)",
            self.js,
        )
        self.assertIn("followLatest: state.followLatestConversation", self.js)
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")

        def function_source(name: str) -> str:
            start = self.js.index(f"function {name}(")
            end = self.js.find("\nfunction ", start + 1)
            return self.js[start:] if end < 0 else self.js[start:end]

        harness = "\n".join(
            function_source(name)
            for name in ("newestConversationRoot", "shouldSelectNewestConversation")
        ) + r'''
const newest = newestConversationRoot([{ root_job_id: "new" }, { root_job_id: "old" }]);
if (newest !== "new") throw new Error(`unexpected newest root: ${newest}`);
if (!shouldSelectNewestConversation({
  selectedRootId: "old", newestRootId: newest, followLatest: true, append: false,
})) throw new Error("latest-follow mode must select a newly arrived managed conversation");
if (shouldSelectNewestConversation({
  selectedRootId: "old", newestRootId: newest, followLatest: false, append: false,
})) throw new Error("manual history selection must not be stolen");
if (shouldSelectNewestConversation({
  selectedRootId: "old", newestRootId: newest, followLatest: true, append: true,
})) throw new Error("loading an older page must not change the selected conversation");
'''
        completed = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_feed_preserves_observation_semantics(self) -> None:
        render_turn = self.js.split("function renderTurn", 1)[1].split(
            "function latestTurn", 1
        )[0]
        self.assertNotIn("display.task_label", render_turn)
        self.assertNotIn('"task-message"', render_turn)
        self.assertNotIn('"你"', render_turn)
        self.assertNotIn(".task-message", self.css)
        self.assertNotIn(".task-withheld", self.css)
        self.assertIn("实时草稿", self.js)
        self.assertIn("最终答复", self.js)
        self.assertIn('createElement("strong", "", "工作思路")', self.js)
        self.assertNotIn('createElement("strong", "", "公开工作思路")', self.js)
        self.assertIn("public_commentary_segments", self.js)
        self.assertIn("public_commentary_truncated", self.js)
        self.assertIn('kind === "agent.commentary.delta"', self.js)
        self.assertIn('kind === "agent.commentary.completed"', self.js)
        self.assertIn("public_reasoning_summaries", self.js)
        self.assertIn('kind === "agent.reasoning.summary.delta"', self.js)
        self.assertIn("payload.delta", self.js)
        thought_data = self.js.split("function workThoughtData", 1)[1].split("function renderWorkThought", 1)[0]
        self.assertNotIn("summary_zh", thought_data)
        self.assertNotIn("turn.result", thought_data)
        self.assertNotIn("public_preview", thought_data)
        self.assertIn('createElement("details", "work-thought")', self.js)
        self.assertIn('details.open = true', self.js)
        self.assertIn('"work-thought-text markdown-body"', self.js)
        self.assertIn("priorThoughtTexts", self.js)
        self.assertNotIn("const justFinished", self.js)
        self.assertIn("updateMarkdownElement", self.js)
        self.assertIn(".work-thought", self.css)
        self.assertLess(
            render_turn.index("process.blocks.forEach"),
            render_turn.index("article.append(assistant)"),
        )
        output_data = self.js.split("function outputData", 1)[1].split(
            "function renderTurn", 1
        )[0]
        self.assertLess(
            output_data.index("if (!terminal)"),
            output_data.index("if (finalOutput.text)"),
        )
        self.assertIn("safeOutputText", output_data)
        self.assertIn('raw.type === "preview"', self.js)
        self.assertNotIn('if (typeof raw.output === "string")', self.js)
        self.assertNotIn('String(first(raw.preview, raw.output, ""))', output_data)
        self.assertIn("Codex 已自动压缩上下文", self.js)
        self.assertIn("已压缩调用输入", self.js)
        self.assertIn("变化归因未验证", self.js)
        self.assertIn("workspace-change-counts", self.js)
        self.assertIn("change.change_kind", self.js)
        self.assertIn("change.lines_added", self.js)
        self.assertIn("change.lines_deleted", self.js)
        self.assertIn("change.added_lines", self.js)
        self.assertIn('if (type === "command") return `命令${suffix}`', self.js)
        self.assertIn('if (type === "web") return `网页操作${suffix}`', self.js)
        self.assertIn('if (type === "mcp") return `MCP 调用${suffix}`', self.js)
        self.assertIn('if (type === "computer") return `电脑操作${suffix}`', self.js)
        self.assertIn('event.kind !== "agent.tool.activity"', self.js)
        self.assertIn("缓存 ${formatNumber(cached)}（已含于输入）", self.js)
        self.assertIn("Number(cached) > 0", self.js)
        self.assertNotIn("prompt || 0", self.js)
        self.assertNotIn("completion || 0", self.js)
        self.assertIn("const total = usage.total_tokens", self.js)
        self.assertNotIn("calculatedTotal", self.js)
        self.assertIn("optionalNumber(context.current_tokens)", self.js)
        self.assertIn('pick(latest, "display.execution_mode")', self.js)
        self.assertIn('executionMode === "direct"', self.js)
        self.assertIn("直接调用，无 Codex 上下文", self.js)
        self.assertIn("等待 Codex 运行时实测", self.js)
        self.assertIn("Codex 运行时未上报上下文", self.js)
        self.assertIn('createElement("details", "work-log")', self.js)
        self.assertIn("workSummary(work)", self.js)
        self.assertIn("page?.browsingEarlier", self.js)
        self.assertIn("visibleWorkRows(work.rows)", self.js)
        self.assertNotIn('`${work.rows.length} 项活动`', self.js)
        self.assertIn("workStatusDetail(item)", self.js)
        self.assertIn("isCompaction(event) || isOutcomeEvent(event)", self.js)
        self.assertIn('"outcome-note"', self.js)
        self.assertIn("node.lastElementChild.append(document.createTextNode(suffix))", self.js)
        self.assertIn("state.durationTimer = window.setInterval(tickDuration, 1000)", self.js)
        self.assertIn("factContextBar.style.width", self.js)
        self.assertIn("模型评估时段", self.js)
        self.assertIn("整段墙钟估算", self.js)
        self.assertIn('createElement("details", "receipt-card")', self.js)
        self.assertIn("运行与验收回执", self.js)
        self.assertIn("receipt.checks", self.js)
        self.assertIn('"context_receipt"', self.js)
        self.assertIn('"delegation_receipt"', self.js)
        self.assertIn('"source_receipt"', self.js)
        self.assertIn('"delivery_receipt"', self.js)
        self.assertIn('"cache_identity"', self.js)
        self.assertIn('"media_routes"', self.js)
        self.assertIn("完整安全回执", self.js)
        self.assertNotIn("JSON.stringify(receipt", self.js)
        self.assertNotIn("命令正文", self.js.split("function workLabel", 1)[1])
        self.assertIn('kind === "run.completed"', self.js)

    def test_javascript_parses_when_node_is_available(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")
        completed = subprocess.run(
            [node, "--check", str(UI_ROOT / "app.js")],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_web_search_receipt_uses_only_explicit_runtime_evidence(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")

        def function_source(name: str) -> str:
            start = self.js.index(f"function {name}(")
            end = self.js.find("\nfunction ", start + 1)
            return self.js[start:] if end < 0 else self.js[start:end]

        harness = "\n".join(
            function_source(name) for name in ("formatDuration", "receiptData")
        ) + r'''
const explicit = receiptData({ result: { execution_receipt: {
  web_search: {
    enabled: true,
    provider: "bing-rss-v1",
    searches: 2,
    event_evidence: "runtime-lifecycle",
  },
} } });
const searchFact = explicit.fields.find((field) => field.label === "网络搜索");
if (!searchFact || searchFact.value !== "bing-rss-v1 · 2 次 · 运行期事件") {
  throw new Error(`unexpected explicit search fact: ${JSON.stringify(searchFact)}`);
}
const absent = receiptData({ result: { execution_receipt: { tool_calls: 2 } } });
if (absent.fields.some((field) => field.label === "网络搜索")) {
  throw new Error("tool calls alone must not fabricate web search receipt");
}
'''
        completed = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_failed_turn_keeps_public_draft_without_calling_it_final(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")

        def function_source(name: str) -> str:
            start = self.js.index(f"function {name}(")
            end = self.js.find("\nfunction ", start + 1)
            return self.js[start:] if end < 0 else self.js[start:end]

        harness = "\n".join(
            function_source(name)
            for name in ("first", "statusName", "safeOutputText", "outputData")
        ) + r'''
const failed = outputData({
  job_status: "completed",
  result_status: "failed",
  progress: { public_preview: "失败前仍可核验的公开草稿" },
  result: { status: "failed" },
});
if (failed.final || failed.live || failed.state !== "partial") {
  throw new Error(`failed draft must be archived partial output: ${JSON.stringify(failed)}`);
}
const completed = outputData({
  job_status: "completed",
  progress: {},
  result: { output: "最终答复" },
});
if (!completed.final || completed.live || completed.state !== "final") {
  throw new Error(`completed output must remain final: ${JSON.stringify(completed)}`);
}
const active = outputData({
  job_status: "running",
  progress: { public_preview: "实时草稿" },
  result: {},
});
if (active.final || !active.live || active.state !== "draft") {
  throw new Error(`active output must remain live draft: ${JSON.stringify(active)}`);
}
'''
        completed = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_work_thought_data_keeps_public_sources_distinct(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")

        def function_source(name: str) -> str:
            start = self.js.index(f"function {name}(")
            end = self.js.find("\nfunction ", start + 1)
            return self.js[start:] if end < 0 else self.js[start:end]

        harness = "\n".join(
            function_source(name)
            for name in (
                "first",
                "eventSequence",
                "mergeEvents",
                "workThoughtEventSource",
                "workThoughtEventKey",
                "workThoughtData",
            )
        ) + r'''
const commentaryOnly = workThoughtData({
  progress: { public_commentary_segments: [
    { commentary_group: 1, text: "累积说明" },
  ] },
  events: [],
}, [{
  sequence: 1,
  kind: "agent.commentary.delta",
  payload: { commentary_group: 1, delta: "不应重复" },
}]);
if (JSON.stringify(commentaryOnly.items.map((item) => [item.source, item.text])) !== JSON.stringify([["commentary", "累积说明"]])) {
  throw new Error("accumulated commentary must be authoritative");
}

const reasoningOnly = workThoughtData({ progress: { public_reasoning_summaries: [
  { summary_group: 3, summary_index: 0, text: "本地公开摘要" },
] }, events: [] }, []);
if (JSON.stringify(reasoningOnly.items.map((item) => [item.source, item.text])) !== JSON.stringify([["reasoning", "本地公开摘要"]])) {
  throw new Error("reasoning-only progress must remain visible");
}

const mixed = workThoughtData({ progress: {}, events: [] }, [
  { sequence: 1, kind: "agent.commentary.delta", payload: { commentary_group: 2, delta: "正在核对" } },
  { sequence: 2, kind: "agent.commentary.completed", payload: { commentary_group: 2, content_replace: "核对完成。" } },
  { sequence: 3, kind: "agent.reasoning.summary.delta", payload: { summary_group: 1, summary_index: 0, delta: "公开摘要" } },
]);
if (JSON.stringify(mixed.items.map((item) => [item.source, item.text])) !== JSON.stringify([
  ["commentary", "核对完成。"], ["reasoning", "公开摘要"],
])) {
  throw new Error("mixed public sources must be ordered and completed commentary must replace deltas");
}

const absent = workThoughtData({
  progress: { public_preview: "实时草稿" },
  result: { output: "最终答复" },
  events: [],
}, [
  { sequence: 1, kind: "reasoning.activity", summary_zh: "只有状态文案", payload: {} },
]);
if (absent.items.length !== 0) throw new Error("non-commentary fields must not fabricate a work-thought node");
'''
        completed = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_chronological_process_interleaves_adjacent_activity_blocks(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")

        def function_source(name: str) -> str:
            start = self.js.index(f"function {name}(")
            end = self.js.find("\nfunction ", start + 1)
            return self.js[start:] if end < 0 else self.js[start:end]

        harness = "\n".join(
            function_source(name)
            for name in (
                "first",
                "statusName",
                "optionalNumber",
                "eventSequence",
                "mergeEvents",
                "isOutputEvent",
                "isCompaction",
                "isOutcomeEvent",
                "toolType",
                "eventStatus",
                "eventOrdinal",
                "specificWorkSummary",
                "workLabel",
                "normalizedWork",
                "normalizedActivitySegment",
                "workSummary",
                "workThoughtEventSource",
                "workThoughtEventKey",
                "workThoughtData",
                "chronologicalProcessData",
            )
        ) + r'''
const events = [
  { sequence: 1, kind: "agent.reasoning.summary.delta", payload: { summary_group: 1, summary_index: 0, delta: "思路 A" } },
  { sequence: 2, kind: "agent.tool.activity", payload: { item_type: "command", tool_ordinal: 1, command_status: "started" } },
  { sequence: 3, kind: "agent.tool.activity", payload: { item_type: "command", tool_ordinal: 1, command_status: "completed" } },
  { sequence: 4, kind: "agent.commentary.delta", payload: { commentary_group: 2, delta: "思路 B" } },
];
const view = chronologicalProcessData({ progress: {}, events }, events);
if (JSON.stringify(view.blocks.map((block) => block.type)) !== JSON.stringify(["thought", "activity", "thought"])) {
  throw new Error("thought and adjacent activity order must remain chronological");
}
if (workSummary(view.blocks[1].work) !== "命令 1 次 · 已完成") {
  throw new Error(`unexpected activity summary: ${workSummary(view.blocks[1].work)}`);
}

const capabilityWork = normalizedWork([
  { sequence: 5, kind: "agent.tool.activity", payload: { item_type: "file_change", tool_ordinal: 1, status: "completed" } },
  { sequence: 6, kind: "agent.tool.activity", payload: { item_type: "web_search", tool_ordinal: 1, status: "completed" } },
  { sequence: 7, kind: "agent.tool.activity", payload: { item_type: "mcp_tool_call", tool_ordinal: 1, status: "completed" } },
]);
if (workSummary(capabilityWork) !== "编辑文件 1 次 · 网络搜索 1 次 · MCP 调用 1 次 · 已完成") {
  throw new Error(`unexpected capability summary: ${workSummary(capabilityWork)}`);
}
for (const summary of ["智能体已完成编辑文件。", "智能体已完成查询公开资料。", "智能体执行命令成功。"] ) {
  if (specificWorkSummary({ summary_zh: summary, payload: {} })) {
    throw new Error(`generic activity must remain count-only: ${summary}`);
  }
}

const boundedWorkspace = normalizedWork([{
  sequence: 8,
  kind: "workspace.change.observed",
  payload: {
    changed_files: 2,
    details_included: 1,
    details_omitted: 1,
    changes: [{ relative_path: "result.json", change_kind: "added" }],
  },
}]);
if (workSummary(boundedWorkspace) !== "工作区变化 2 项") {
  throw new Error(`workspace total must not use only visible details: ${workSummary(boundedWorkspace)}`);
}

const canonical = chronologicalProcessData({ progress: {
  public_reasoning_summaries: [
    { summary_group: 1, summary_index: 0, text: "思路 A", first_sequence: 1, last_sequence: 1 },
  ],
  public_commentary_segments: [
    { commentary_group: 2, text: "思路 B", first_sequence: 4, last_sequence: 4 },
  ],
}, events: events.slice(1, 3) }, events.slice(1, 3));
if (JSON.stringify(canonical.blocks.map((block) => block.type)) !== JSON.stringify(["thought", "activity", "thought"])) {
  throw new Error("canonical first_sequence must anchor thought nodes around current-window activity");
}

const multiStageEvents = [
  { sequence: 11, kind: "agent.reasoning.summary.delta", payload: { summary_group: 11, summary_index: 0, delta: "先确认输入。" } },
  { sequence: 12, kind: "agent.tool.activity", payload: { item_type: "command_execution", tool_calls: 1, command_status: "in_progress" } },
  { sequence: 13, kind: "agent.tool.activity", payload: { item_type: "command_execution", tool_calls: 1, command_status: "succeeded" } },
  { sequence: 14, kind: "agent.tool.activity", payload: { item_type: "file_change", tool_calls: 2, status: "completed" } },
  { sequence: 15, kind: "agent.tool.activity", payload: { item_type: "web_search", tool_calls: 3, status: "completed" } },
  { sequence: 16, kind: "agent.commentary.delta", payload: { commentary_group: 12, delta: "再核对证据。" } },
  { sequence: 17, kind: "agent.tool.activity", payload: { item_type: "command_execution", tool_calls: 4, command_status: "in_progress" } },
  { sequence: 18, kind: "agent.tool.activity", payload: { item_type: "command_execution", tool_calls: 4, command_status: "succeeded" } },
  { sequence: 19, kind: "agent.tool.activity", payload: { item_type: "file_change", tool_calls: 5, status: "completed" } },
  { sequence: 20, kind: "agent.tool.activity", payload: { item_type: "web_search", tool_calls: 6, status: "completed" } },
  { sequence: 21, kind: "agent.reasoning.summary.delta", payload: { summary_group: 13, summary_index: 0, delta: "最后整理结果。" } },
];
const multiStage = chronologicalProcessData({ progress: {}, events: multiStageEvents }, multiStageEvents);
if (JSON.stringify(multiStage.blocks.map((block) => block.type)) !== JSON.stringify([
  "thought", "activity", "thought", "activity", "thought",
])) {
  throw new Error("multiple activity stages must stay between their adjacent public thoughts");
}
for (const block of multiStage.blocks.filter((item) => item.type === "activity")) {
  if (workSummary(block.work) !== "命令 1 次 · 编辑文件 1 次 · 网络搜索 1 次 · 已完成") {
    throw new Error(`unexpected multi-stage activity summary: ${workSummary(block.work)}`);
  }
}

const fullHistory = chronologicalProcessData({
  progress: {
    public_reasoning_summaries: [
      { summary_group: 1, summary_index: 0, text: "先检查环境。", first_sequence: 10, last_sequence: 19 },
      { summary_group: 2, summary_index: 0, text: "再核对资料。", first_sequence: 200, last_sequence: 220 },
      { summary_group: 3, summary_index: 0, text: "最后整理结果。", first_sequence: 400, last_sequence: 430 },
    ],
  },
  events: [{ sequence: 650, kind: "run.completed", payload: { result_status: "ok" } }],
  conversation_process: {
    schema: "llm-backend-toolkit.observer-conversation-process.v1",
    activity_segments: [
      { first_sequence: 20, last_sequence: 21, activities: [
        { type: "command", count: 2, completed: 2, failed: 0, active: 0, recorded: 0 },
      ] },
      { first_sequence: 250, last_sequence: 251, activities: [
        { type: "web_search", count: 3, completed: 3, failed: 0, active: 0, recorded: 0 },
      ] },
      { first_sequence: 450, last_sequence: 600, activities: [
        { type: "file_change", count: 3, completed: 3, failed: 0, active: 0, recorded: 0 },
      ], workspace_changed_files: 3, changes: [
        { relative_path: "notes/a.md", change_kind: "added" },
      ] },
    ],
    events: [{ sequence: 650, kind: "run.completed", summary_zh: "本轮运行已完成。", payload: {} }],
    truncated: false,
  },
}, [{ sequence: 650, kind: "run.completed", payload: { result_status: "ok" } }]);
if (JSON.stringify(fullHistory.blocks.map((block) => block.type)) !== JSON.stringify([
  "thought", "activity", "thought", "activity", "thought", "activity", "event",
])) {
  throw new Error(`full semantic history must be visible by default: ${JSON.stringify(fullHistory.blocks)}`);
}
const fullSummaries = fullHistory.blocks
  .filter((block) => block.type === "activity")
  .map((block) => workSummary(block.work));
if (JSON.stringify(fullSummaries) !== JSON.stringify([
  "命令 2 次 · 已完成",
  "网络搜索 3 次 · 已完成",
  "编辑文件 3 次 · 工作区变化 3 项 · 已完成",
])) {
  throw new Error(`unexpected full-history summaries: ${JSON.stringify(fullSummaries)}`);
}

const allThoughtNodes = Array.from({ length: 13 }, (_, index) => ({
  source: "reasoning",
  key: `reasoning:${index + 1}:0:part:1`,
  text: `完整思路 ${index + 1}`,
  first_sequence: index * 2 + 1,
  last_sequence: index * 2 + 1,
}));
const allThoughts = chronologicalProcessData({
  progress: { public_reasoning_summaries_truncated: true },
  events: [],
  conversation_process: {
    schema: "llm-backend-toolkit.observer-conversation-process.v1",
    thought_nodes: allThoughtNodes,
    activity_segments: [],
    events: [],
    truncated: false,
  },
}, []);
if (allThoughts.blocks.length !== 13 || allThoughts.blocks.some((block) => block.type !== "thought")) {
  throw new Error("canonical full conversation must not inherit the 12-node live-cache cap");
}
if (!fullHistory.fullHistory || fullHistory.blocks.some((block) => block.type === "activity" && !block.fullHistory)) {
  throw new Error("canonical activity segments must be marked as complete semantic history");
}
'''
        completed = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_safe_markdown_dom_builder_supports_common_ai_subset(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")

        def function_source(name: str) -> str:
            start = self.js.index(f"function {name}(")
            end = self.js.find("\nfunction ", start + 1)
            return self.js[start:] if end < 0 else self.js[start:end]

        harness = r'''
class Node {
  constructor(tag, text = "") { this.tag = tag; this.textContent = text; this.children = []; this.attrs = {}; this.dataset = {}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
}
const document = {
  createElement: (tag) => new Node(tag),
  createTextNode: (text) => new Node("#text", String(text)),
};
''' + "\n".join(
            function_source(name)
            for name in (
                "first",
                "createElement",
                "safeLinkHref",
                "appendInlineMarkdown",
                "isTableDivider",
                "splitTableRow",
                "appendMarkdown",
            )
        ) + r'''
const root = new Node("root");
appendMarkdown(root, `# 标题

- 列表 **粗体** 和 *斜体* 以及 \`代码\`
1. 有序项
> 引用

| A | B |
| --- | :---: |
| 1 | 2 |

\`\`\`js
alert(1)
\`\`\`

[安全](https://example.com) [危险](javascript:alert(1)) [数据](data:text/html,x) [文件](file:///tmp/x) <img src=x onerror=alert(1)>`);
const tags = [];
const links = [];
function walk(node) {
  tags.push(node.tag);
  if (node.tag === "a") links.push(node.attrs.href);
  for (const child of node.children) walk(child);
}
walk(root);
for (const tag of ["h1", "ul", "ol", "strong", "em", "code", "pre", "blockquote", "table", "a"]) {
  if (!tags.includes(tag)) throw new Error(`missing markdown tag ${tag}`);
}
if (links.length !== 1 || links[0] !== "https://example.com/") throw new Error("unsafe links must not be clickable");
if (JSON.stringify(root).includes("img" ) === false) throw new Error("raw HTML must remain literal text");
if (JSON.stringify(root).includes("innerHTML")) throw new Error("innerHTML is forbidden");

const underscoreRoot = new Node("root");
appendMarkdown(underscoreRoot, "LOCAL_SEARCH_EVENT_OK\n\n_独立斜体_");
const underscoreEmphasis = [];
function collectUnderscoreEmphasis(node) {
  if (node.tag === "em") underscoreEmphasis.push(node.textContent);
  for (const child of node.children) collectUnderscoreEmphasis(child);
}
collectUnderscoreEmphasis(underscoreRoot);
if (underscoreEmphasis.length !== 1 || underscoreEmphasis[0] !== "独立斜体") {
  throw new Error(`intraword underscores must stay literal: ${JSON.stringify(underscoreEmphasis)}`);
}
function visibleText(node) {
  return String(node.textContent || "") + node.children.map(visibleText).join("");
}
if (!visibleText(underscoreRoot).includes("LOCAL_SEARCH_EVENT_OK")) {
  throw new Error("identifier underscores must remain visible");
}
'''
        completed = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn(".innerHTML", self.js)


if __name__ == "__main__":
    unittest.main()
