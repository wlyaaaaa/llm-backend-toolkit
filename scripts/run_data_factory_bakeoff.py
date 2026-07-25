from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from llm_backend_toolkit.toolkit import Toolkit


ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "benchmarks" / "data_factory" / "seed"
CANDIDATES = ("qwen-code", "opencode", "codex-cli", "claude-code")


def default_output_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".cache"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return base / "llm-backend-toolkit" / "bakeoffs" / stamp


def verify(workspace: Path) -> dict:
    try:
        completed = subprocess.run(
            [sys.executable, str(workspace / "verify.py")],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"passed": False, "score": 0, "total": 0, "error": type(exc).__name__}
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        result = {"passed": False, "score": 0, "total": 0, "stdout": completed.stdout[-500:]}
    result["exit_code"] = completed.returncode
    result["stderr"] = completed.stderr[-500:]
    return result


def run_candidate(toolkit: Toolkit, runner: str, output_root: Path) -> dict:
    workspace = output_root / runner
    if workspace.exists():
        raise FileExistsError(f"Refusing to overwrite an existing bake-off workspace: {workspace}")
    shutil.copytree(SEED, workspace)
    task_text = (workspace / "TASK.md").read_text(encoding="utf-8")
    request = {
        "provider": "qwen-main-v1",
        "task": {
            "goal": "Complete the data-factory acceptance task in TASK.md and run python verify.py.",
            "instructions": [
                "Work autonomously inside the supplied workspace.",
                "Do not modify raw/events.jsonl or verify.py.",
                "Return only a brief final result; do not expose chain-of-thought.",
            ],
            "inputs": [task_text],
            "expected_output": {"format": "text"},
        },
        "context": {"mode": "compact", "target_tokens": 4096},
        "reasoning": {"mode": "off"},
        "privacy": {"cloud_allowed": False},
        "execution": {
            "mode": "agent",
            "runner": runner,
            "workspace": str(workspace),
            "policy": "workspace-write",
            "model": "qwen-main-v1",
            # Public agent messages are excluded from steps by the v2 contract.
            # This scenario still needs headroom for Qwen's post-write verification
            # loop; keep the wider limit local to this benchmark instead of
            # weakening the toolkit's global hard-budget semantics.
            "budget": {"timeout_seconds": 900, "max_steps": 45, "max_tool_calls": 120},
        },
    }
    started = time.monotonic()
    result = toolkit.invoke(request)
    wall_ms = int((time.monotonic() - started) * 1000)
    score = verify(workspace)
    receipt = {
        "schema": "llm-backend-toolkit.data-factory-bakeoff.v1",
        "runner": runner,
        "workspace": str(workspace),
        "toolkit_result": result,
        "harness_wall_ms": wall_ms,
        "deterministic_score": score,
    }
    (output_root / f"{runner}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner", action="append", choices=CANDIDATES)
    parser.add_argument("--output-root", type=Path, default=default_output_root())
    args = parser.parse_args(argv)
    runners = args.runner or list(CANDIDATES)
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    toolkit = Toolkit()
    receipts = []
    for runner in runners:
        receipts.append(run_candidate(toolkit, runner, output_root))
    summary = {
        "output_root": str(output_root),
        "results": [
            {
                "runner": item["runner"],
                "toolkit_status": item["toolkit_result"].get("status"),
                "passed": item["deterministic_score"].get("passed"),
                "score": item["deterministic_score"].get("score"),
                "total": item["deterministic_score"].get("total"),
                "wall_ms": item.get("harness_wall_ms"),
            }
            for item in receipts
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(item["deterministic_score"].get("passed") for item in receipts) else 2


if __name__ == "__main__":
    raise SystemExit(main())
