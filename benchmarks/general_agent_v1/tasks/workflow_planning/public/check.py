from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    path = ROOT / "plan.json"
    if not path.is_file():
        raise SystemExit("plan.json is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if set(payload) != {"policy", "selected_order", "total_duration", "total_value", "skipped"}:
        raise SystemExit("invalid plan shape")
    if not isinstance(payload["selected_order"], list) or not isinstance(payload["skipped"], list):
        raise SystemExit("selected_order and skipped must be arrays")
    if any(set(item) != {"id", "reason"} for item in payload["skipped"] if isinstance(item, dict)):
        raise SystemExit("invalid skipped item")
    print("STRUCTURE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
