from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class BenchmarkTask:
    name: str
    root: Path
    public_root: Path
    verifier: Path


def discover_tasks(suite_root: Path) -> list[BenchmarkTask]:
    task_root = suite_root / "tasks"
    if not task_root.is_dir():
        raise FileNotFoundError(f"Benchmark task directory is missing: {task_root}")
    tasks: list[BenchmarkTask] = []
    for root in sorted(path for path in task_root.iterdir() if path.is_dir()):
        public_root = root / "public"
        verifier = root / "verify.py"
        if not (public_root / "TASK.md").is_file() or not verifier.is_file():
            raise ValueError(f"Incomplete benchmark task contract: {root.name}")
        tasks.append(BenchmarkTask(root.name, root, public_root, verifier))
    if not tasks:
        raise ValueError("Benchmark suite contains no tasks")
    return tasks


def suite_fingerprint(suite_root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in suite_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    if not files:
        raise ValueError("Cannot fingerprint an empty benchmark suite")
    for path in files:
        relative = path.relative_to(suite_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def aggregate_results(receipts: Iterable[dict[str, Any]], *, expected_tasks: list[str]) -> list[dict[str, Any]]:
    expected = set(expected_tasks)
    if not expected:
        raise ValueError("expected_tasks cannot be empty")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for receipt in receipts:
        grouped.setdefault(str(receipt["runner"]), []).append(receipt)

    rows: list[dict[str, Any]] = []
    for runner, items in grouped.items():
        by_task = {str(item["task"]): item for item in items}
        if len(by_task) != len(items):
            raise ValueError(f"Duplicate benchmark receipt for runner {runner}")
        score = sum(int((item.get("deterministic_score") or {}).get("score") or 0) for item in items)
        total = sum(int((item.get("deterministic_score") or {}).get("total") or 0) for item in items)
        passed_tasks = {
            task
            for task, item in by_task.items()
            if bool((item.get("deterministic_score") or {}).get("passed"))
        }
        protocol_tasks = {
            task
            for task, item in by_task.items()
            if (item.get("toolkit_result") or {}).get("status") == "ok"
            and ((item.get("toolkit_result") or {}).get("execution_receipt") or {}).get("exit_code") == 0
        }
        qualified = expected <= passed_tasks and expected <= protocol_tasks
        rows.append(
            {
                "runner": runner,
                "qualified": qualified,
                "tasks_present": sorted(by_task),
                "tasks_passed": len(expected & passed_tasks),
                "protocol_successes": len(expected & protocol_tasks),
                "expected_tasks": len(expected),
                "score": score,
                "total": total,
                "correctness_rate": score / total if total else 0.0,
                "wall_ms": sum(int(item.get("harness_wall_ms") or 0) for item in items),
            }
        )

    rows.sort(
        key=lambda row: (
            not row["qualified"],
            -row["tasks_passed"],
            -row["correctness_rate"],
            -row["protocol_successes"],
            row["wall_ms"],
            row["runner"],
        )
    )
    return rows
