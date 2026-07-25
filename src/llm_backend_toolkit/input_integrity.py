from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from pathlib import Path
from typing import Any, Iterator


INPUT_INTEGRITY_SCHEMA = "llm-backend-toolkit.input-integrity.v1"
INPUT_SPOOL_CLEANUP_SCHEMA = "llm-backend-toolkit.input-spool-cleanup.v1"
EXPECTED_SHA256_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
_COPY_CHUNK_BYTES = 1024 * 1024
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


class InputIntegrityError(ValueError):
    def __init__(self, summary: str, receipt: dict[str, Any]) -> None:
        super().__init__(summary)
        self.summary = summary
        self.receipt = receipt


def _deep_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _normalize_expected(reference: dict[str, Any]) -> tuple[str | None, int | None]:
    has_sha256 = "expected_sha256" in reference
    has_bytes = "expected_bytes" in reference
    if has_sha256 != has_bytes:
        raise ValueError(
            "Path references must declare expected_sha256 and expected_bytes together"
        )
    if not has_sha256:
        return None, None
    expected_sha256 = reference.get("expected_sha256")
    if not isinstance(expected_sha256, str):
        raise ValueError("expected_sha256 must be a sha256:<64 lowercase hex> string")
    match = EXPECTED_SHA256_PATTERN.fullmatch(expected_sha256)
    if match is None:
        raise ValueError("expected_sha256 must be a sha256:<64 lowercase hex> string")
    expected_bytes = reference.get("expected_bytes")
    if (
        not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 0
    ):
        raise ValueError("expected_bytes must be a non-negative integer")
    return f"sha256:{match.group(1)}", expected_bytes


def _iter_references(
    request: dict[str, Any],
) -> Iterator[tuple[str, int, dict[str, Any]]]:
    task = request.get("task")
    if isinstance(task, dict):
        for index, source in enumerate(task.get("sources") or []):
            if isinstance(source, dict):
                yield "source", index, source
    media = request.get("media")
    if isinstance(media, dict):
        for index, attachment in enumerate(media.get("attachments") or []):
            if isinstance(attachment, dict):
                kind = str(attachment.get("kind") or "unknown").lower()
                yield f"media:{kind}", index, attachment


def declaration_scope(request: dict[str, Any]) -> list[dict[str, Any]]:
    scope: list[dict[str, Any]] = []
    for reference_kind, ordinal, reference in _iter_references(request):
        expected_sha256, expected_bytes = _normalize_expected(reference)
        scope.append(
            {
                "kind": reference_kind,
                "ordinal": ordinal,
                "id": str(reference.get("id") or ""),
                "expected_sha256": expected_sha256,
                "expected_bytes": expected_bytes,
            }
        )
    return scope


def pending_receipt(request: dict[str, Any]) -> dict[str, Any]:
    references: list[dict[str, Any]] = []
    declared_count = 0
    for item in declaration_scope(request):
        declared = item["expected_sha256"] is not None
        declared_count += int(declared)
        references.append(
            {
                **item,
                "expectation_declared": declared,
                "actual_sha256": None,
                "actual_bytes": None,
                "status": "pending",
            }
        )
    return {
        "schema": INPUT_INTEGRITY_SCHEMA,
        "status": "pending" if references else "not_applicable",
        "reference_count": len(references),
        "declared_reference_count": declared_count,
        "references": references,
    }


def _receipt_with_status(
    references: list[dict[str, Any]],
    *,
    status: str,
) -> dict[str, Any]:
    return {
        "schema": INPUT_INTEGRITY_SCHEMA,
        "status": status,
        "reference_count": len(references),
        "declared_reference_count": sum(
            int(bool(item.get("expectation_declared"))) for item in references
        ),
        "references": references,
    }


def _fail_reference(
    references: list[dict[str, Any]],
    *,
    index: int,
    reason: str,
    actual_sha256: str | None = None,
    actual_bytes: int | None = None,
) -> InputIntegrityError:
    failed = dict(references[index])
    failed.update(
        {
            "actual_sha256": actual_sha256,
            "actual_bytes": actual_bytes,
            "status": "failed",
            "failure": reason,
        }
    )
    references[index] = failed
    reference_id = str(failed.get("id") or f"ordinal-{failed.get('ordinal')}")
    summary = (
        f"Input integrity verification failed for {failed['kind']} "
        f"{reference_id}: {reason}."
    )
    return InputIntegrityError(
        summary,
        _receipt_with_status(references, status="failed"),
    )


def _safe_suffix(path: Path, reference_kind: str) -> str:
    suffix = path.suffix
    if _SAFE_SUFFIX.fullmatch(suffix):
        return suffix.lower()
    if reference_kind == "source":
        return ".txt"
    return ".bin"


def _copy_and_hash(
    source: Path,
    temporary: Path,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    copied_bytes = 0
    with source.open("rb") as source_stream, temporary.open("xb") as target_stream:
        before = os.fstat(source_stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError("reference is not a regular file")
        while True:
            chunk = source_stream.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            copied_bytes += len(chunk)
            target_stream.write(chunk)
        target_stream.flush()
        os.fsync(target_stream.fileno())
        after = os.fstat(source_stream.fileno())
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise OSError("reference changed while it was being read")
    return f"sha256:{digest.hexdigest()}", copied_bytes


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return f"sha256:{digest.hexdigest()}", total


def prepare_job_inputs(
    request: dict[str, Any],
    *,
    job_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prepared = _deep_copy(request)
    references = pending_receipt(prepared)["references"]
    if not references:
        return prepared, _receipt_with_status([], status="not_applicable")

    spool_root = job_dir / "input-spool"
    spool_root.mkdir(parents=True, exist_ok=True)
    mutable_references = list(_iter_references(prepared))
    for reference_index, (reference_kind, ordinal, reference) in enumerate(
        mutable_references
    ):
        reference_id = str(reference.get("id") or "")
        if not reference_id:
            raise _fail_reference(
                references,
                index=reference_index,
                reason="reference id is empty",
            )
        try:
            source = Path(str(reference.get("path") or "")).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            raise _fail_reference(
                references,
                index=reference_index,
                reason="reference path is invalid",
            )
        suffix = _safe_suffix(source, reference_kind)
        destination = (
            spool_root
            / f"{reference_index + 1:04d}-{reference_kind.replace(':', '-')}{suffix}"
        )
        temporary = spool_root / (
            destination.name + f".{secrets.token_hex(8)}.partial"
        )
        actual_sha256: str | None = None
        actual_bytes: int | None = None
        try:
            actual_sha256, actual_bytes = _copy_and_hash(source, temporary)
            expected_sha256 = references[reference_index]["expected_sha256"]
            expected_bytes = references[reference_index]["expected_bytes"]
            if expected_bytes is not None and actual_bytes != expected_bytes:
                raise _fail_reference(
                    references,
                    index=reference_index,
                    reason="actual byte size does not match expected_bytes",
                    actual_sha256=actual_sha256,
                    actual_bytes=actual_bytes,
                )
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                raise _fail_reference(
                    references,
                    index=reference_index,
                    reason="actual SHA-256 does not match expected_sha256",
                    actual_sha256=actual_sha256,
                    actual_bytes=actual_bytes,
                )
            readback_sha256, readback_bytes = _hash_file(temporary)
            if (
                readback_sha256 != actual_sha256
                or readback_bytes != actual_bytes
            ):
                raise _fail_reference(
                    references,
                    index=reference_index,
                    reason="private spool readback does not match copied bytes",
                    actual_sha256=readback_sha256,
                    actual_bytes=readback_bytes,
                )
            os.replace(temporary, destination)
        except InputIntegrityError:
            raise
        except (FileNotFoundError, OSError, PermissionError):
            raise _fail_reference(
                references,
                index=reference_index,
                reason="reference is missing, unreadable, or changed during read",
                actual_sha256=actual_sha256,
                actual_bytes=actual_bytes,
            )
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

        reference["path"] = str(destination.resolve())
        reference.pop("expected_sha256", None)
        reference.pop("expected_bytes", None)
        references[reference_index] = {
            **references[reference_index],
            "actual_sha256": actual_sha256,
            "actual_bytes": actual_bytes,
            "status": "verified",
        }

    receipt = _receipt_with_status(references, status="verified")
    manifest_path = spool_root / "manifest.json"
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_manifest, manifest_path)
    return prepared, receipt


def cleanup_job_inputs(job_dir: Path) -> dict[str, Any]:
    spool_root = job_dir / "input-spool"
    cleanup = {
        "schema": INPUT_SPOOL_CLEANUP_SCHEMA,
        "status": "removed",
        "verified_absent": False,
    }
    try:
        if spool_root.is_symlink():
            spool_root.unlink()
        elif spool_root.exists():
            shutil.rmtree(spool_root)
        for name in ("request.json", "prepared-request.json"):
            (job_dir / name).unlink(missing_ok=True)
        cleanup["verified_absent"] = (
            not spool_root.exists() and not spool_root.is_symlink()
        )
        if not cleanup["verified_absent"]:
            cleanup["status"] = "failed"
    except OSError:
        cleanup["status"] = "failed"
        cleanup["verified_absent"] = False
    return cleanup
