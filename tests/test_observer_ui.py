from __future__ import annotations

import re
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
        element_id = values.get("id")
        if element_id:
            self.ids.add(element_id)
        if tag in {"header", "main", "nav", "aside"}:
            self.landmarks.add(tag)
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.stylesheets.append(str(values["href"]))
        if tag == "script" and values.get("src"):
            self.scripts.append(str(values["src"]))


class ObserverUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
        cls.css = (UI_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.js = (UI_ROOT / "app.js").read_text(encoding="utf-8")

    def test_static_bundle_has_no_external_dependency(self) -> None:
        parser = _DocumentParser()
        parser.feed(self.html)

        self.assertEqual(
            ["/assets/styles.css?v=20260813-observer-remote-3"],
            parser.stylesheets,
        )
        self.assertEqual(
            ["/assets/app.js?v=20260813-observer-remote-3"],
            parser.scripts,
        )
        self.assertIn('href="/assets/favicon.svg"', self.html)
        self.assertTrue((UI_ROOT / "favicon.svg").is_file())
        self.assertFalse(re.search(r"https?://|//cdn", self.html, re.IGNORECASE))
        self.assertNotIn("@import", self.css)

    def test_document_exposes_accessible_three_region_workspace(self) -> None:
        parser = _DocumentParser()
        parser.feed(self.html)

        self.assertIn("<title>模型调用观察台</title>", self.html)
        self.assertTrue({"header", "main", "nav", "aside"}.issubset(parser.landmarks))
        self.assertTrue(
            {
                "run-list",
                "conversation-stream",
                "conversation-output",
                "conversation-new-events",
                "run-inspector",
                "timeline",
                "load-earlier-events",
                "return-latest-events",
                "draft-panel",
                "receipt-panel",
                "connection-state",
                "conversation-empty-state",
            }.issubset(parser.ids)
        )
        self.assertNotRegex(self.html, r'id="run-list"[^>]*aria-live')
        self.assertNotRegex(self.html, r'id="timeline"[^>]*aria-live')
        self.assertRegex(self.html, r'id="toast"[^>]*aria-live="polite"')
        self.assertIn('setAttribute("aria-current"', self.js)
        self.assertIn(":focus-visible", self.css)

    def test_remote_inspired_read_only_conversation_workspace(self) -> None:
        self.assertIn('class="conversation-pane"', self.html)
        self.assertIn('id="conversation-stream"', self.html)
        self.assertIn('class="read-only-chip"', self.html)
        self.assertIn('id="mobile-back-button"', self.html)
        self.assertIn('id="run-inspector"', self.html)
        self.assertIn("只读观察", self.html)
        self.assertIn("function renderConversation(detail)", self.js)
        self.assertIn("function groupWorkRecords(events)", self.js)
        self.assertIn('createElement("h3", "", "工作记录")', self.js)
        self.assertIn("summarizeWorkRecord(group.events)", self.js)
        self.assertIn("运行 ${formatNumber(commandCount)} 个命令", self.js)
        self.assertIn("编辑 ${formatNumber(fileEditCount)} 个文件", self.js)
        self.assertIn("检测到 ${formatNumber(observedFileCount)} 个文件变化", self.js)
        self.assertIn("查询公开资料 ${formatNumber(webSearchCount)} 次", self.js)
        self.assertIn('web_search: "查询公开资料"', self.js)
        self.assertIn("context-compaction-divider", self.js)
        self.assertIn("conversation-new-events", self.js)
        self.assertIn("新增 ${formatNumber(laterCount)} 条", self.js)
        self.assertIn("function conversationScrollContainer", self.js)
        self.assertIn("function returnConversationToLatest", self.js)
        self.assertIn("elements.conversationPane.addEventListener(\"scroll\"", self.js)
        self.assertIn('document.body.dataset.mobileView = "conversation"', self.js)
        self.assertIn('document.body.dataset.mobileView = "list"', self.js)
        self.assertIn("revealConversation = false", self.js)
        self.assertIn("{ revealConversation: true }", self.js)
        self.assertIn('grid-template-areas: "runs conversation inspector"', self.css)
        self.assertIn('grid-template-areas: "runs conversation"', self.css)
        self.assertIn('body[data-mobile-view="conversation"] .run-sidebar', self.css)
        self.assertIn("position: sticky", self.css)

    def test_read_only_output_and_inspector_keep_semantic_controls(self) -> None:
        self.assertIn('id="conversation-output"', self.html)
        self.assertIn('id="conversation-output-state"', self.html)
        self.assertIn('id="inspector-toggle"', self.html)
        self.assertIn('aria-controls="run-inspector"', self.html)
        self.assertIn('aria-expanded="false"', self.html)
        self.assertIn('id="inspector-close-button"', self.html)
        self.assertNotIn('role="tablist"', self.html)
        self.assertNotIn("<textarea", self.html)
        self.assertNotIn('type="submit"', self.html)
        self.assertNotIn('method: "POST"', self.js)
        self.assertNotIn('method: "PUT"', self.js)
        self.assertNotIn('method: "DELETE"', self.js)
        self.assertIn("function setInspectorOpen", self.js)
        self.assertIn('document.body.dataset.inspectorOpen', self.js)
        self.assertIn("同一输出节点原位切换为最终结果", self.js)

    def test_ui_contains_required_chinese_observer_labels(self) -> None:
        for label in (
            "调用记录",
            "详细时间线",
            "实时草稿",
            "最终结果",
            "校验回执",
            "执行方式",
            "推理等级",
            "Token",
            "当前上下文",
            "TPS",
            "耗时",
            "GPU",
            "交付状态",
        ):
            self.assertIn(label, self.html + self.js)

    def test_client_uses_refresh_only_sse_with_polling_fallback(self) -> None:
        self.assertIn('`/api/runs?limit=${limit}&offset=${offset}`', self.js)
        self.assertIn('`/api/runs/${encodeURIComponent(jobId)}`', self.js)
        self.assertIn('new EventSource("/api/stream")', self.js)
        self.assertRegex(self.js, r"eventSource\.onmessage\s*=\s*\(\)\s*=>\s*scheduleRefresh")
        self.assertIn("startPolling", self.js)
        self.assertIn("stopPolling", self.js)
        self.assertIn("setInterval", self.js)
        self.assertIn("loadMoreButton", self.js)
        self.assertIn("conversationLabel", self.js)
        error_handler = self.js.split("eventSource.onerror", 1)[1].split(
            "async function copyText", 1
        )[0]
        self.assertIn("startPolling()", error_handler)
        self.assertNotIn("eventSource.close()", error_handler)
        self.assertNotIn("state.eventSource = null", error_handler)

    def test_history_and_conversations_are_visible_without_manual_refresh(self) -> None:
        self.assertIn("历史与连续对话，仅供查看", self.html)
        self.assertIn('id="load-more-button"', self.html)
        self.assertIn("next_offset", self.js)
        self.assertIn("root_job_id", self.js)
        self.assertIn("第", self.js)
        self.assertIn("轮", self.js)
        self.assertIn("实时同步", self.js)

    def test_history_refresh_merges_head_and_preserves_keyed_rows(self) -> None:
        self.assertIn("function mergeRunPage", self.js)
        self.assertIn("mergeRunPage(state.runs, incoming, { prepend: true })", self.js)
        self.assertIn("const offset = append ? state.nextOffset : 0", self.js)
        self.assertIn("const runItemCache = new Map()", self.js)
        self.assertIn("elements.runList.insertBefore(entry.button, cursor)", self.js)
        self.assertIn("captureRunListViewport", self.js)
        self.assertIn("focused.focus({ preventScroll: true })", self.js)
        self.assertNotIn("elements.runList.replaceChildren()", self.js)

    def test_timeline_refresh_updates_stable_keyed_rows(self) -> None:
        self.assertIn("const timelineItemCache = new Map()", self.js)
        self.assertIn("function timelineEventKey", self.js)
        self.assertIn("function createTimelineItem", self.js)
        self.assertIn("function updateTimelineItem", self.js)
        timeline_render = self.js.split("function renderTimeline(detail)", 1)[1].split(
            "function extractDraft", 1
        )[0]
        self.assertIn("timelineItemCache.get(event.key)", timeline_render)
        self.assertIn("elements.timeline.insertBefore(entry.item, cursor)", timeline_render)
        self.assertNotIn("replaceChildren", timeline_render)
        self.assertNotIn("JSON.stringify(events)", timeline_render)

    def test_result_status_overrides_completed_job_status(self) -> None:
        normalized = self.js.split("function normalizedStatus(run)", 1)[1].split(
            "function statusInfo", 1
        )[0]
        self.assertLess(
            normalized.index("RESULT_STATUS_PRIORITY.has(resultStatus)"),
            normalized.index("run.job_status"),
        )
        for status in ("failed", "blocked", "stale", "partial"):
            self.assertIn(f'"{status}"', self.js)
        self.assertIn('partial: { label: "部分完成", tone: "warning" }', self.js)
        delivery = self.js.split("function deliveryLabel(detail)", 1)[1].split(
            "function normalizedRuns", 1
        )[0]
        self.assertIn("statusInfo(detail).label", delivery)

    def test_task_labels_never_fall_back_to_stored_prompt_text(self) -> None:
        self.assertIn('"display.task_label"', self.js)
        self.assertIn('"历史模型任务"', self.js)
        self.assertNotIn("task_goal", self.js)
        self.assertNotIn('"request.task.goal"', self.js)

    def test_ui_only_presents_public_progress_and_truthful_metrics(self) -> None:
        self.assertIn('"progress.public_preview"', self.js)
        self.assertNotIn('"progress.draft"', self.js)
        self.assertNotIn('"result.draft"', self.js)
        self.assertIn("eval_duration_ns", self.js)
        self.assertIn("performance.tokens_per_second", self.js)
        self.assertIn("estimated_output_tokens", self.js)
        self.assertIn("≈", self.js)
        self.assertIn("（公开内容估算）", self.js)
        self.assertIn("token_events", self.js)
        self.assertIn("片段", self.js)
        self.assertNotIn('title: "调用已提交"', self.js)
        self.assertNotIn('title: "模型开始工作"', self.js)
        self.assertIn("summary_zh", self.js)
        self.assertIn("occurred_utc", self.js)
        self.assertIn("编辑文件", self.js)
        self.assertIn('"agent.output.delta": "智能体公开进度"', self.js)
        self.assertIn('"workspace.change.observed": "检测到工作区变化"', self.js)
        self.assertIn("workspace-change-detail", self.js)
        self.assertIn("完整路径：${absolutePath}", self.js)
        self.assertIn("复制完整路径：${path.relativePath}", self.js)
        self.assertIn('typeof change.absolute_path === "string"', self.js)
        self.assertIn('copyText(path.absolutePath, "完整路径已复制")', self.js)
        self.assertIn("未验证由单一进程造成", self.js)
        self.assertIn("未在本次公开名单内，或因安全、大小上限未展示", self.js)
        self.assertIn("总计 ${formatNumber(totalTokens)}", self.js)
        self.assertIn("输入 ${formatNumber(promptTokens)}", self.js)
        self.assertIn("输出 ${formatNumber(completionTokens)}", self.js)
        self.assertIn("推理 ${formatNumber(reasoningTokens)}", self.js)
        self.assertIn("numericPrompt + numericCompletion +", self.js)
        self.assertIn("缓存 ${formatNumber(cachedTokens)}", self.js)
        self.assertIn("Number(cachedTokens) > 0", self.js)
        self.assertIn('id="metric-token-detail"', self.html)
        self.assertIn("输出 token/秒（整段墙钟估算）", self.js)
        self.assertIn("输出 token/秒（模型评估时段精确）", self.js)
        self.assertIn("function contextSummary(detail)", self.js)
        self.assertIn('"agent.context.usage.updated"', self.js)
        self.assertIn("Codex 运行时实测", self.js)
        self.assertIn("等待 Codex 运行时实测", self.js)
        self.assertIn("已用", self.js)
        self.assertIn("共", self.js)
        context_body = self.js.split("function contextSummary(detail)", 1)[1].split(
            "function formatDateTime", 1
        )[0]
        self.assertNotIn("当前后端配置", context_body)
        self.assertNotIn("运行回执", context_body)
        self.assertNotIn("estimated_tokens_after", context_body)
        self.assertNotIn("prompt_tokens", context_body)
        self.assertNotIn("result.usage", context_body)
        self.assertNotIn("progress.metrics", context_body)
        self.assertNotIn("detail, \"events\"", context_body)
        self.assertIn(
            '"context.compaction.completed": "已压缩调用输入"',
            self.js,
        )
        self.assertIn(
            '"agent.context.compaction.completed": "Codex 已自动压缩上下文"',
            self.js,
        )
        timeline_body = self.js.split("function timelineEvents(detail)", 1)[1].split(
            "function renderTimeline", 1
        )[0]
        self.assertLess(
            timeline_body.index('pick(detail, "events")'),
            timeline_body.index('pick(detail, "progress.events")'),
        )

    def test_live_elapsed_draft_and_event_history_are_incremental(self) -> None:
        duration_body = self.js.split("function calculateDuration(detail)", 1)[1].split(
            "function calculateTps", 1
        )[0]
        self.assertIn('"performance.elapsed_seconds"', duration_body)
        self.assertIn('"progress.metrics.elapsed_seconds"', duration_body)
        self.assertIn("ACTIVE_REFRESH_INTERVAL_MS", self.js)
        self.assertIn("setInterval(tickActiveDetail", self.js)
        self.assertIn("Math.round(milliseconds / 1000)", self.js)

        self.assertIn("function renderDraft(detail)", self.js)
        self.assertIn("nextText.startsWith(state.draftText)", self.js)
        self.assertIn("document.createTextNode(suffix)", self.js)
        render_conversation = self.js.split("function renderConversation(detail)", 1)[1].split(
            "function renderDetail", 1
        )[0]
        self.assertIn("renderDraft(detail)", render_conversation)
        self.assertIn("renderWorkRecords(events)", render_conversation)
        self.assertNotIn("elements.conversationOutput.textContent =", render_conversation)
        render_draft = self.js.split("function renderDraft(detail)", 1)[1].split(
            "function extractResult", 1
        )[0]
        self.assertIn("elements.conversationOutput.textContent = nextText", render_draft)
        self.assertIn("elements.conversationOutputNode.dataset.outputState", render_draft)
        self.assertIn("最终结果", render_draft)
        self.assertNotIn("elements.draftContent", self.js)
        self.assertNotIn("elements.resultContent", self.js)
        self.assertIn("逐段更新中", self.js)
        self.assertIn("public_preview_truncated", self.js)
        self.assertIn("草稿预览已达到安全上限", self.js)

        self.assertIn('id="load-earlier-events"', self.html)
        self.assertIn("function loadEarlierEvents()", self.js)
        self.assertIn("before_sequence", self.js)
        self.assertIn("agent.output.delta.batch", self.js)
        self.assertIn("完整内容已在“实时草稿”中逐段追加", self.js)
        self.assertIn("earlier_count", self.js)
        self.assertIn('id="return-latest-events"', self.html)
        self.assertIn("function returnToLatestEvents()", self.js)
        self.assertIn("更晚 ${formatNumber(laterCount)} 个", self.js)
        self.assertIn("新增 ${formatNumber(laterCount)} 条", self.js)
        self.assertIn(".slice(-MAX_TIMELINE_EVENTS)", self.js)
        self.assertIn(".slice(0, MAX_TIMELINE_EVENTS)", self.js)
        self.assertIn("timelineItemCache.get(anchorKey)", self.js)
        self.assertIn("latest_sequence: detail.event_page.latest_sequence", self.js)
        self.assertIn("任务已结束，但没有可展示结果", self.js)
        self.assertNotIn('matchMedia("(max-width: 1100px)")', self.js)

    def test_conversation_work_records_keep_safe_grouping_boundary(self) -> None:
        grouping = self.js.split("function groupWorkRecords(events)", 1)[1].split(
            "function appendWorkRecordItem", 1
        )[0]
        work_classifier = self.js.split("function isWorkRecordEvent(event)", 1)[1].split(
            "function isContextCompactionEvent", 1
        )[0]
        self.assertIn('event.kind === "agent.tool.activity"', work_classifier)
        self.assertIn('event.kind === "workspace.change.observed"', work_classifier)
        self.assertIn('event.kind === "agent.context.usage.updated"', grouping)
        self.assertIn('type: "compaction"', grouping)
        self.assertIn("不展示命令正文", self.js)
        self.assertIn("context-compaction-divider", self.js)
        self.assertIn("conversationWorkRecords.replaceChildren(fragment)", self.js)

    def test_layout_handles_edge_app_window_and_long_content(self) -> None:
        self.assertIn("#17e04b", self.css.lower())
        self.assertIn("100dvh", self.css)
        self.assertIn("minmax(0, 1fr)", self.css)
        self.assertIn("overflow-wrap: anywhere", self.css)
        self.assertRegex(self.css, r"@media\s*\(max-width:\s*1100px\)")
        self.assertRegex(self.css, r"@media\s*\(max-width:\s*680px\)")
        self.assertIn("prefers-reduced-motion", self.css)
        self.assertIn("--quiet: #657169", self.css)
        self.assertIn("outline: 2px solid var(--green-deep)", self.css)
        self.assertIn("repeat(auto-fit, minmax(96px, 1fr))", self.css)
        self.assertIn('grid-template-areas: "runs conversation inspector"', self.css)
        self.assertRegex(
            self.css,
            r"\.conversation-new-events\s*\{[^}]*position:\s*sticky",
        )
        narrow_workspace = self.css.split("@media (max-width: 1100px)", 1)[1].split(
            "@media (max-width: 680px)", 1
        )[0]
        self.assertIn('grid-template-areas: "runs conversation"', narrow_workspace)
        self.assertIn("position: fixed", narrow_workspace)
        self.assertIn("transform: translateX", narrow_workspace)
        self.assertIn('body[data-inspector-open="true"] .inspector-pane', narrow_workspace)
        mobile_workspace = self.css.split("@media (max-width: 680px)", 1)[1]
        self.assertIn('body[data-mobile-view="conversation"] .run-sidebar', mobile_workspace)
        self.assertIn('body[data-mobile-view="list"] .conversation-pane', mobile_workspace)
        self.assertNotIn("grid-auto-flow: column", mobile_workspace)
        self.assertNotIn("grid-auto-columns", mobile_workspace)

    def test_timeline_summarizes_native_compaction_and_safe_tool_progress(self) -> None:
        self.assertIn("第 ${compactionCount} 次自动压缩", self.js)
        self.assertIn("压缩后约", self.js)
        self.assertIn("第 ${toolNumber} 个命令", self.js)
        self.assertIn("执行成功", self.js)
        self.assertIn('"agent.run.failed": "智能体运行失败"', self.js)
        self.assertIn('"handoff.collected": "结果已取回"', self.js)
        self.assertIn('cancellation_requested: { label: "取消中", tone: "active" }', self.js)

    def test_agent_route_effort_and_max_label_take_priority(self) -> None:
        reasoning = self.js.split("function reasoningLevel(detail)", 1)[1].split(
            "function gpuLabel", 1
        )[0]
        self.assertLess(
            reasoning.index('"display.reasoning_effort"'),
            reasoning.index('"display.reasoning_mode"'),
        )
        self.assertLess(
            reasoning.index('"display.reasoning_effort"'),
            reasoning.index('"request.reasoning.mode"'),
        )
        self.assertLess(
            reasoning.index('"result.execution_receipt.reasoning_effort"'),
            reasoning.index('"display.reasoning_mode"'),
        )
        self.assertLess(
            reasoning.index('"result.execution_receipt.reasoning_effort"'),
            reasoning.index('"request.reasoning.mode"'),
        )
        self.assertIn('max: "最高"', reasoning)

    def test_client_formats_structured_values_without_html_injection(self) -> None:
        self.assertIn("JSON.stringify(value, null, 2)", self.js)
        self.assertIn('firstDefined(check.summary, check.message, check.detail) ?? ""', self.js)
        self.assertIn("textContent", self.js)
        self.assertNotIn("innerHTML", self.js)



if __name__ == "__main__":
    unittest.main()
