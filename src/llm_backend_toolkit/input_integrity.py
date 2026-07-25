from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes


INPUT_INTEGRITY_SCHEMA = "llm-backend-toolkit.input-integrity.v1"
INPUT_SPOOL_CLEANUP_SCHEMA = "llm-backend-toolkit.input-spool-cleanup.v1"
EXPECTED_SHA256_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
_COPY_CHUNK_BYTES = 1024 * 1024
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
_WINDOWS_REPARSE_ATTRIBUTE = 0x400
_ACTIVE_LEASES_LOCK = threading.Lock()
_ACTIVE_LEASES: dict[str, "ProtectedInputLease"] = {}


class InputIntegrityError(ValueError):
    def __init__(self, summary: str, receipt: dict[str, Any]) -> None:
        super().__init__(summary)
        self.summary = summary
        self.receipt = receipt


def _lease_key(job_dir: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(job_dir)))


def _path_is_reparse(path: Path) -> bool:
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(value.st_mode) or bool(
        int(getattr(value, "st_file_attributes", 0) or 0)
        & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _lexically_contained(job_dir: Path, candidate: Path) -> bool:
    job_value = os.path.normcase(os.path.abspath(os.fspath(job_dir)))
    candidate_value = os.path.normcase(os.path.abspath(os.fspath(candidate)))
    try:
        return os.path.commonpath((job_value, candidate_value)) == job_value
    except ValueError:
        return False


def assert_safe_job_path(
    job_dir: Path,
    candidate: Path,
    *,
    require_exists: bool,
) -> Path:
    job_absolute = Path(os.path.abspath(os.fspath(job_dir)))
    candidate_absolute = Path(os.path.abspath(os.fspath(candidate)))
    if not job_absolute.is_dir() or _path_is_reparse(job_absolute):
        raise ValueError("job directory is missing, a symlink, or a reparse point")
    if not _lexically_contained(job_absolute, candidate_absolute):
        raise ValueError("job path failed canonical containment")
    canonical_job = job_absolute.resolve(strict=True)
    relative = candidate_absolute.relative_to(job_absolute)
    current = job_absolute
    for part in relative.parts:
        current = current / part
        if not os.path.lexists(current):
            if require_exists:
                raise ValueError("required job path is missing")
            break
        if _path_is_reparse(current):
            raise ValueError("job path contains a symlink or reparse point")
        canonical_current = current.resolve(strict=True)
        if not canonical_current.is_relative_to(canonical_job):
            raise ValueError("job path failed canonical containment")
    if require_exists and not os.path.lexists(candidate_absolute):
        raise ValueError("required job path is missing")
    return candidate_absolute


def _open_spool_file(
    path: Path,
    *,
    expectation_declared: bool,
) -> BinaryIO:
    if os.name != "nt":
        if expectation_declared:
            raise OSError(
                "immutable path consumption binding is unavailable on this "
                "platform"
            )
        return path.open("x+b", buffering=0)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    generic_read = 0x80000000
    generic_write = 0x40000000
    file_share_read = 0x00000001
    create_new = 1
    file_attribute_normal = 0x00000080
    handle = create_file(
        str(path),
        generic_read | generic_write,
        file_share_read,
        None,
        create_new,
        file_attribute_normal,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDWR | os.O_BINARY,
        )
    except BaseException:
        close_handle(handle)
        raise
    return os.fdopen(descriptor, "w+b", buffering=0)


@dataclass
class _ProtectedSpoolFile:
    path: Path
    stream: BinaryIO
    st_dev: int
    st_ino: int
    st_size: int

    def validate(self, job_dir: Path) -> None:
        if self.stream.closed:
            raise ValueError("protected input handle is closed")
        safe_path = assert_safe_job_path(
            job_dir,
            self.path,
            require_exists=True,
        )
        by_handle = os.fstat(self.stream.fileno())
        by_path = os.stat(safe_path, follow_symlinks=False)
        if (
            by_handle.st_dev != self.st_dev
            or by_handle.st_ino != self.st_ino
            or by_handle.st_size != self.st_size
            or by_path.st_dev != self.st_dev
            or by_path.st_ino != self.st_ino
            or by_path.st_size != self.st_size
        ):
            raise ValueError("protected input handle no longer names the captured bytes")

    def close(self) -> None:
        if not self.stream.closed:
            self.stream.close()


@dataclass
class ProtectedInputLease:
    job_dir: Path
    files: list[_ProtectedSpoolFile] = field(default_factory=list)
    closed: bool = False

    def add(self, value: _ProtectedSpoolFile) -> None:
        if self.closed:
            value.close()
            raise ValueError("input lease is already closed")
        self.files.append(value)

    def validate(self, expected_reference_count: int) -> None:
        if self.closed or len(self.files) != expected_reference_count:
            raise ValueError("protected input lease is missing or incomplete")
        for value in self.files:
            value.validate(self.job_dir)

    def close(self) -> None:
        if self.closed:
            return
        for value in reversed(self.files):
            try:
                value.close()
            except OSError:
                pass
        self.closed = True


def register_job_input_lease(
    job_dir: Path,
    lease: ProtectedInputLease,
) -> None:
    key = _lease_key(job_dir)
    with _ACTIVE_LEASES_LOCK:
        previous = _ACTIVE_LEASES.pop(key, None)
        if previous is not None:
            previous.close()
        _ACTIVE_LEASES[key] = lease


def validate_job_input_lease(
    job_dir: Path,
    *,
    expected_reference_count: int,
) -> None:
    if expected_reference_count == 0:
        return
    with _ACTIVE_LEASES_LOCK:
        lease = _ACTIVE_LEASES.get(_lease_key(job_dir))
    if lease is None:
        raise ValueError("protected input lease is unavailable")
    lease.validate(expected_reference_count)


def has_active_job_input_lease(job_dir: Path) -> bool:
    with _ACTIVE_LEASES_LOCK:
        lease = _ACTIVE_LEASES.get(_lease_key(job_dir))
    return lease is not None and not lease.closed


def release_job_input_lease(job_dir: Path) -> None:
    with _ACTIVE_LEASES_LOCK:
        lease = _ACTIVE_LEASES.pop(_lease_key(job_dir), None)
    if lease is not None:
        lease.close()


def _safe_remove_tree(job_dir: Path, root: Path) -> None:
    safe_root = assert_safe_job_path(
        job_dir,
        root,
        require_exists=True,
    )
    entries = list(os.scandir(safe_root))
    for entry in entries:
        path = Path(entry.path)
        assert_safe_job_path(job_dir, path, require_exists=True)
        if entry.is_dir(follow_symlinks=False):
            _safe_remove_tree(job_dir, path)
        else:
            path.unlink()
    assert_safe_job_path(job_dir, safe_root, require_exists=True)
    safe_root.rmdir()


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
    target_stream: BinaryIO,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    copied_bytes = 0
    with source.open("rb") as source_stream:
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


def _hash_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    stream.seek(0)
    while True:
        chunk = stream.read(_COPY_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    stream.seek(0)
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
    try:
        assert_safe_job_path(job_dir, job_dir, require_exists=True)
        assert_safe_job_path(job_dir, spool_root, require_exists=False)
        spool_root.mkdir(exist_ok=True)
        assert_safe_job_path(job_dir, spool_root, require_exists=True)
    except (OSError, ValueError) as error:
        raise InputIntegrityError(
            f"Input spool path failed safety validation: {error}.",
            _receipt_with_status(references, status="failed"),
        ) from error
    lease = ProtectedInputLease(job_dir=job_dir)
    mutable_references = list(_iter_references(prepared))
    try:
        for reference_index, (
            reference_kind,
            ordinal,
            reference,
        ) in enumerate(mutable_references):
            reference_id = str(reference.get("id") or "")
            if not reference_id:
                raise _fail_reference(
                    references,
                    index=reference_index,
                    reason="reference id is empty",
                )
            try:
                source = Path(
                    str(reference.get("path") or "")
                ).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                raise _fail_reference(
                    references,
                    index=reference_index,
                    reason="reference path is invalid",
                )
            suffix = _safe_suffix(source, reference_kind)
            destination = (
                spool_root
                / (
                    f"{reference_index + 1:04d}-"
                    f"{reference_kind.replace(':', '-')}{suffix}"
                )
            )
            actual_sha256: str | None = None
            actual_bytes: int | None = None
            protected_stream: BinaryIO | None = None
            try:
                assert_safe_job_path(
                    job_dir,
                    destination,
                    require_exists=False,
                )
                expectation_declared = bool(
                    references[reference_index]["expectation_declared"]
                )
                protected_stream = _open_spool_file(
                    destination,
                    expectation_declared=expectation_declared,
                )
                actual_sha256, actual_bytes = _copy_and_hash(
                    source,
                    protected_stream,
                )
                expected_sha256 = references[reference_index][
                    "expected_sha256"
                ]
                expected_bytes = references[reference_index]["expected_bytes"]
                if expected_bytes is not None and actual_bytes != expected_bytes:
                    raise _fail_reference(
                        references,
                        index=reference_index,
                        reason="actual byte size does not match expected_bytes",
                        actual_sha256=actual_sha256,
                        actual_bytes=actual_bytes,
                    )
                if (
                    expected_sha256 is not None
                    and actual_sha256 != expected_sha256
                ):
                    raise _fail_reference(
                        references,
                        index=reference_index,
                        reason="actual SHA-256 does not match expected_sha256",
                        actual_sha256=actual_sha256,
                        actual_bytes=actual_bytes,
                    )
                readback_sha256, readback_bytes = _hash_stream(
                    protected_stream
                )
                if (
                    readback_sha256 != actual_sha256
                    or readback_bytes != actual_bytes
                ):
                    raise _fail_reference(
                        references,
                        index=reference_index,
                        reason=(
                            "private spool readback does not match copied bytes"
                        ),
                        actual_sha256=readback_sha256,
                        actual_bytes=readback_bytes,
                    )
                by_handle = os.fstat(protected_stream.fileno())
                lease.add(
                    _ProtectedSpoolFile(
                        path=destination,
                        stream=protected_stream,
                        st_dev=by_handle.st_dev,
                        st_ino=by_handle.st_ino,
                        st_size=by_handle.st_size,
                    )
                )
                protected_stream = None
            except InputIntegrityError:
                raise
            except (FileNotFoundError, OSError, PermissionError, ValueError):
                raise _fail_reference(
                    references,
                    index=reference_index,
                    reason=(
                        "reference is missing, unreadable, changed during read, "
                        "or cannot be bound to protected consumption"
                    ),
                    actual_sha256=actual_sha256,
                    actual_bytes=actual_bytes,
                )
            finally:
                if protected_stream is not None:
                    protected_stream.close()

            reference["path"] = str(destination)
            reference.pop("expected_sha256", None)
            reference.pop("expected_bytes", None)
            references[reference_index] = {
                **references[reference_index],
                "actual_sha256": actual_sha256,
                "actual_bytes": actual_bytes,
                "status": (
                    "verified"
                    if expectation_declared
                    else "captured_unverified"
                ),
            }

        all_declared = all(
            bool(item.get("expectation_declared")) for item in references
        )
        overall_status = "verified" if all_declared else "spooled_unverified"
        receipt = _receipt_with_status(references, status=overall_status)
        receipt["consumption_binding"] = (
            "windows-share-read-protected-handle-v1"
            if os.name == "nt"
            else "open-handle-unverified-v1"
        )
        register_job_input_lease(job_dir, lease)
        manifest_path = spool_root / "manifest.json"
        assert_safe_job_path(
            job_dir,
            manifest_path,
            require_exists=False,
        )
        manifest_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        assert_safe_job_path(
            job_dir,
            manifest_path,
            require_exists=True,
        )
        return prepared, receipt
    except BaseException:
        release_job_input_lease(job_dir)
        lease.close()
        raise


def cleanup_job_inputs(job_dir: Path) -> dict[str, Any]:
    spool_root = job_dir / "input-spool"
    cleanup = {
        "schema": INPUT_SPOOL_CLEANUP_SCHEMA,
        "status": "removed",
        "verified_absent": False,
    }
    release_job_input_lease(job_dir)
    try:
        assert_safe_job_path(job_dir, job_dir, require_exists=True)
        if os.path.lexists(spool_root):
            assert_safe_job_path(job_dir, spool_root, require_exists=True)
            _safe_remove_tree(job_dir, spool_root)
        for name in (
            "request.json",
            "prepared-request.json",
            "prepared-request.json.tmp",
        ):
            path = job_dir / name
            if os.path.lexists(path):
                assert_safe_job_path(job_dir, path, require_exists=True)
                path.unlink()
        cleanup["verified_absent"] = (
            not os.path.lexists(spool_root)
        )
        if not cleanup["verified_absent"]:
            cleanup["status"] = "failed"
    except (OSError, ValueError):
        cleanup["status"] = "blocked_unsafe_path"
        cleanup["verified_absent"] = False
        cleanup["error"] = (
            "Input spool cleanup could not be verified without crossing "
            "a protected path boundary."
        )
    return cleanup
