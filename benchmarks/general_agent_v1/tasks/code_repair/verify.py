from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


EXPECTED_CONTRACT_HASHES = {
    "TASK.md": "f28110e40deded3a32bd80bda7b6c461832f33d14872e53c38e7ad7e0a810bef",
    "check.py": "ba3553e3fe3794e204ec2552dcc885e5ed978a9daacf2421179de363b9d60a58",
    "tests/test_intervals.py": "447bca6374b7be801d333a61252b7728f84acfbc90fdda1c291f1446b330aafd",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("candidate_intervals", path)
    if spec is None or spec.loader is None:
        raise ImportError("candidate module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def raises_value_error(function, *args) -> bool:
    try:
        function(*args)
    except ValueError:
        return True
    except Exception:
        return False
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    root = args.workspace.resolve()
    checks = {}
    checks["contract_files_unchanged"] = all(
        sha256(root / name) == digest for name, digest in EXPECTED_CONTRACT_HASHES.items()
    )
    try:
        public_check = subprocess.run(
            [sys.executable, str(root / "check.py")],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        checks["public_tests"] = public_check.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        checks["public_tests"] = False
    try:
        module = load_module(root / "src" / "intervals.py")
        original = [(5, 6), (1, 2), (3, 4)]
        checks["adjacency_and_no_mutation"] = module.normalize(original) == [(1, 6)] and original == [
            (5, 6),
            (1, 2),
            (3, 4),
        ]
        checks["negative_coordinates"] = module.normalize([(-5, -3), (-2, 1)]) == [(-5, 1)]
        checks["reversed_interval_rejected"] = raises_value_error(module.normalize, [(4, 3)])
        checks["reversed_base_rejected"] = raises_value_error(module.available, (5, 1), [])
        checks["blocked_ranges_are_clipped"] = module.available((1, 10), [(-5, 2), (9, 20)]) == [(3, 8)]
        checks["complete_block"] = module.available((1, 3), [(0, 9)]) == []
        checks["outside_block_is_ignored"] = module.available((-2, 2), [(-9, -8), (8, 9)]) == [(-2, 2)]
    except Exception:
        for name in (
            "adjacency_and_no_mutation",
            "negative_coordinates",
            "reversed_interval_rejected",
            "reversed_base_rejected",
            "blocked_ranges_are_clipped",
            "complete_block",
            "outside_block_is_ignored",
        ):
            checks.setdefault(name, False)
    score = sum(bool(value) for value in checks.values())
    result = {"passed": score == len(checks), "score": score, "total": len(checks), "checks": checks}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
