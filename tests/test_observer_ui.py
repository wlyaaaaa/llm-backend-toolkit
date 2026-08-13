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
            ["/assets/styles.css?v=20260813-conversations-5"],
            parser.stylesheets,
        )
        self.assertEqual(
            ["/assets/app.js?v=20260813-conversations-5"],
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
        self.assertIn("返回本轮最新记录", self.js)
        self.assertNotIn("/api/runs?limit=", self.js)

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
        self.assertIn('createElement("details", "work-log")', self.js)
        self.assertIn("workSummary(work)", self.js)
        self.assertIn("当前窗口：${workSummary(work)}", self.js)
        self.assertIn("page?.browsingEarlier", self.js)
        self.assertIn("visibleWorkRows(work.rows)", self.js)
        self.assertNotIn('`${work.rows.length} 项活动`', self.js)
        self.assertIn("workStatusDetail(item)", self.js)
        self.assertIn("isCompaction(event) || isOutcomeEvent(event)", self.js)
        self.assertIn('"outcome-note"', self.js)
        self.assertIn("document.createTextNode(nextText.slice(priorText.length))", self.js)
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


if __name__ == "__main__":
    unittest.main()
