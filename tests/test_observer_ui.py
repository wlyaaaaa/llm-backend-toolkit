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

        self.assertEqual(["/assets/styles.css"], parser.stylesheets)
        self.assertEqual(["/assets/app.js"], parser.scripts)
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
                "timeline",
                "draft-panel",
                "result-panel",
                "receipt-panel",
                "connection-state",
                "empty-state",
            }.issubset(parser.ids)
        )
        self.assertNotRegex(self.html, r'id="run-list"[^>]*aria-live')
        self.assertNotRegex(self.html, r'id="timeline"[^>]*aria-live')
        self.assertRegex(self.html, r'id="toast"[^>]*aria-live="polite"')
        self.assertIn('setAttribute("aria-current"', self.js)
        self.assertIn(":focus-visible", self.css)

    def test_output_and_verification_use_keyboard_accessible_tabs(self) -> None:
        self.assertIn('role="tablist"', self.html)
        self.assertIn('role="tab"', self.html)
        self.assertIn('role="tabpanel"', self.html)
        self.assertIn('aria-controls="receipt-panel"', self.html)
        self.assertIn("function selectDetailTab", self.js)
        self.assertIn('event.key === "ArrowRight"', self.js)
        self.assertIn('event.key === "ArrowLeft"', self.js)
        self.assertIn(".detail-tab[aria-selected=\"true\"]", self.css)

    def test_ui_contains_required_chinese_observer_labels(self) -> None:
        for label in (
            "调用记录",
            "工作时间线",
            "实时草稿",
            "最终结果",
            "校验回执",
            "执行方式",
            "推理等级",
            "Token",
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
        self.assertIn("调用记录与历史", self.html)
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
        self.assertIn("timelineSignature", self.js)

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
        self.assertIn("（估算）", self.js)
        self.assertIn("token_events", self.js)
        self.assertIn("片段", self.js)
        self.assertNotIn('title: "调用已提交"', self.js)
        self.assertNotIn('title: "模型开始工作"', self.js)
        self.assertIn("summary_zh", self.js)
        self.assertIn("occurred_utc", self.js)
        self.assertIn("编辑文件", self.js)
        timeline_body = self.js.split("function timelineEvents(detail)", 1)[1].split(
            "function renderTimeline", 1
        )[0]
        self.assertLess(
            timeline_body.index('pick(detail, "events")'),
            timeline_body.index('pick(detail, "progress.events")'),
        )

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

    def test_layout_handles_edge_app_window_and_long_content(self) -> None:
        self.assertIn("#17e04b", self.css.lower())
        self.assertIn("100dvh", self.css)
        self.assertIn("minmax(0, 1fr)", self.css)
        self.assertIn("overflow-wrap: anywhere", self.css)
        self.assertRegex(self.css, r"@media\s*\(max-width:\s*980px\)")
        self.assertRegex(self.css, r"@media\s*\(max-width:\s*680px\)")
        self.assertIn("prefers-reduced-motion", self.css)
        self.assertIn("--quiet: #657169", self.css)
        self.assertIn("outline: 2px solid var(--green-deep)", self.css)
        self.assertIn("repeat(auto-fit, minmax(96px, 1fr))", self.css)


if __name__ == "__main__":
    unittest.main()
