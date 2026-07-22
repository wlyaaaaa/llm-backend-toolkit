from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw" / "events.jsonl"
DERIVED = ROOT / "derived"
EXPECTED_RAW_SHA256 = "5372491f82d5c04be4db5ad955fbab24725348ef238822cb31ea68b21e385f56"
REQUIRED_ROW_KEYS = {
    "record_key",
    "event_id",
    "event_time",
    "time_status",
    "text_safe",
    "secret_redacted",
    "integrity_status",
    "conflict_group",
    "source_lines",
    "raw_sha256",
}
TOTAL_CHECKS = 21


def report(checks: dict[str, bool], **extra) -> None:
    payload = {
        "passed": len(checks) == TOTAL_CHECKS and all(checks.values()),
        "score": sum(checks.values()),
        "total": TOTAL_CHECKS,
        "checks": checks,
    }
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    checks: dict[str, bool] = {}
    cleaner = ROOT / "clean.py"
    checks["cleaner_exists"] = cleaner.is_file()
    if not cleaner.is_file():
        report(checks)
        return 1

    source = cleaner.read_text(encoding="utf-8")
    checks["streaming_source"] = ".readlines(" not in source and ".read_text(" not in source
    raw_before = RAW.read_bytes()
    checks["raw_fixture_identity"] = hashlib.sha256(raw_before).hexdigest() == EXPECTED_RAW_SHA256

    first = subprocess.run([sys.executable, str(cleaner)], cwd=ROOT, capture_output=True, text=True, timeout=30)
    checks["first_run_exit_zero"] = first.returncode == 0
    outputs = [DERIVED / "cleaned.jsonl", DERIVED / "receipt.json", DERIVED / "checkpoint.json"]
    checks["outputs_exist"] = all(path.is_file() for path in outputs)
    if not checks["outputs_exist"]:
        report(checks, stderr=first.stderr[-500:])
        return 1

    first_bytes = {path.name: path.read_bytes() for path in outputs}
    second = subprocess.run([sys.executable, str(cleaner)], cwd=ROOT, capture_output=True, text=True, timeout=30)
    checks["second_run_exit_zero"] = second.returncode == 0
    checks["idempotent_bytes"] = all(path.read_bytes() == first_bytes[path.name] for path in outputs)
    checks["raw_unchanged"] = RAW.read_bytes() == raw_before

    try:
        rows = load_jsonl(outputs[0])
        receipt = load_json(outputs[1])
        checkpoint = load_json(outputs[2])
    except (OSError, UnicodeError, json.JSONDecodeError):
        checks["valid_json"] = False
        report(checks)
        return 1
    checks["valid_json"] = True
    checks["derived_count"] = len(rows) == 8
    checks["row_contract"] = all(REQUIRED_ROW_KEYS <= set(row) for row in rows)

    by_event: dict[str, list[dict]] = {}
    for row in rows:
        by_event.setdefault(str(row.get("event_id")), []).append(row)
    alpha = by_event.get("evt-001", [])
    checks["exact_duplicate_lineage"] = len(alpha) == 1 and alpha[0].get("source_lines") == [1, 2]
    conflicts = by_event.get("evt-002", [])
    checks["conflict_preserved"] = (
        len(conflicts) == 2
        and {row.get("text_safe") for row in conflicts} == {"Use plan A", "Use plan B"}
        and all(row.get("conflict_group") == "evt-002" for row in conflicts)
    )
    secret = by_event.get("evt-003", [{}])[0]
    checks["secret_redacted"] = bool(secret.get("secret_redacted")) and "834921" not in str(secret.get("text_safe"))
    unknown = by_event.get("evt-004", [{}])[0]
    checks["unknown_time_preserved"] = unknown.get("event_time") is None and unknown.get("time_status") == "unknown"
    uncertain = by_event.get("evt-005", [{}])[0]
    checks["uncertain_time_preserved"] = uncertain.get("time_status") == "uncertain"
    damaged = by_event.get("evt-006", [{}])[0]
    checks["damage_not_invented"] = damaged.get("integrity_status") == "damaged" and "�" in str(damaged.get("text_safe"))

    raw_lines = raw_before.decode("utf-8").splitlines()
    line_hashes = {hashlib.sha256(line.encode("utf-8")).hexdigest() for line in raw_lines}
    checks["raw_hash_lineage"] = all(row.get("raw_sha256") in line_hashes for row in rows)
    derived_blob = b"\n".join(path.read_bytes() for path in outputs)
    checks["secret_absent_from_all_derived"] = b"834921" not in derived_blob

    checks["receipt_contract"] = receipt == {
        "input_sha256": EXPECTED_RAW_SHA256,
        "input_records": 9,
        "derived_records": 8,
        "exact_duplicates_collapsed": 1,
        "conflicts": 1,
        "unknown_time": 1,
        "secrets_redacted": 1,
        "checkpoint_line": 9,
        "completed": True,
    }
    checks["checkpoint_bound"] = (
        checkpoint.get("input_sha256") == EXPECTED_RAW_SHA256
        and checkpoint.get("line") == 9
        and checkpoint.get("completed") is True
    )

    passed = len(checks) == TOTAL_CHECKS and all(checks.values())
    report(checks)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
