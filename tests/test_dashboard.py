from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "scripts" / "Show-LlmBackendDashboard.ps1"


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is required")
class DashboardTests(unittest.TestCase):
    def test_once_view_is_human_readable_and_reports_tps(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_root = Path(temp)
            job_dir = state_root / ("a" * 24)
            job_dir.mkdir()
            (job_dir / "state.json").write_text(
                json.dumps(
                    {
                        "job_id": "a" * 24,
                        "job_status": "completed",
                        "backend": "local-default",
                        "created_utc": "2026-07-24T00:00:00Z",
                        "updated_utc": "2026-07-24T00:00:10Z",
                        "display": {
                            "task_goal": "判断一条证据",
                            "execution_mode": "direct",
                            "reasoning_mode": "on",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (job_dir / "result.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "output": {
                            "cases": [
                                {
                                    "id": "ambiguous_identity",
                                    "decision": "escalate",
                                    "needs_escalation": True,
                                    "brief_rationale": "同名对象不能自动合并。",
                                }
                            ],
                            "batch_summary": {
                                "total_cases": 1,
                                "decision_distribution": {"escalate": 1},
                            },
                        },
                        "backend": {"model": "qwen-main-v1"},
                        "provider": {"actual": "qwen-main-v1"},
                        "usage": {
                            "prompt_tokens": 20,
                            "completion_tokens": 100,
                            "total_duration_ns": 5_000_000_000,
                        },
                        "checks": [
                            {
                                "id": "valid_json",
                                "passed": True,
                                "summary": "Output is valid JSON.",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    "pwsh",
                    "-NoLogo",
                    "-NoProfile",
                    "-File",
                    str(DASHBOARD),
                    "-Once",
                    "-StateDir",
                    str(state_root),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

        output = completed.stdout
        self.assertIn("R 展开原始数据", output)
        self.assertIn("同名身份存在歧义 → 升级给顶级模型", output)
        self.assertIn("生成速度约 20 TPS", output)
        self.assertIn("结构化结果可解析", output)
        self.assertNotIn('"cases"', output)

    def test_live_dashboard_uses_incremental_truecolor_rendering(self) -> None:
        source = DASHBOARD.read_text(encoding="utf-8-sig")

        self.assertNotIn("Clear-Host", source)
        self.assertIn("[48;2;23;224;75m", source)
        self.assertIn("$script:ShowRawJson = -not $script:ShowRawJson", source)
        self.assertIn(".Split(\"`n\")", source)


if __name__ == "__main__":
    unittest.main()
