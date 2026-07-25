from __future__ import annotations

import difflib
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


_DEFAULT_MAX_ENTRIES = 20_000
_DEFAULT_MAX_DEPTH = 32
_DEFAULT_TIMEOUT_SECONDS = 0.35
_MAX_CAPTURE_FILE_BYTES = 32 * 1024
_MAX_CAPTURE_TOTAL_BYTES = 256 * 1024
_MAX_PUBLIC_CHANGE_FILES = 6
_MAX_DIFF_CHARS_PER_FILE = 8_000
_MAX_DIFF_CHARS_TOTAL = 24_000
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SKIPPED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)
_SAFE_TEXT_SUFFIXES = frozenset(
    {
        ".bat",
        ".c",
        ".cfg",
        ".cmd",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".csv",
        ".go",
        ".h",
        ".hpp",
        ".htm",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsonl",
        ".jsx",
        ".kt",
        ".md",
        ".markdown",
        ".php",
        ".ps1",
        ".psd1",
        ".psm1",
        ".py",
        ".pyi",
        ".rb",
        ".rs",
        ".scss",
        ".sh",
        ".sql",
        ".svelte",
        ".toml",
        ".ts",
        ".tsv",
        ".tsx",
        ".txt",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_SECRET_NAME_PATTERN = re.compile(
    r"(?i)(?:^|[._-])(?:credential|credentials|passwd|password|secret|secrets|"
    r"token|tokens)(?:$|[._-])|^\.env(?:\.|$)|^id_(?:rsa|ed25519)(?:\.|$)|"
    r"(?:^|[._-])oauth(?:[._-]|$)"
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|authorization|passwd|password|secret|token)"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9+/_.=-]{8,}"
    ),
)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"""(?ix)
    (?:
        \bfile:(?:/{1,3}|\\\\)
        |
        (?<![A-Z0-9_/\\])[A-Z]:[\\/][^\s\x00]*
        |
        (?<![A-Z0-9_:/\\])(?:\\\\|//)[^\s\\/]+[\\/][^\s\\/]+
        |
        (?<![A-Z0-9_/\\])\\(?!\\)[^\s\\/]+\\[^\s\\/]+
        |
        (?<![A-Z0-9_/\\])/(?!/)[^\s\\/]+(?:/[^\s\\/]+)*
    )
    """,
)


class WorkspaceRootError(ValueError):
    """The workspace root cannot be bound to one safe directory identity."""


@dataclass(frozen=True)
class ValidatedWorkspaceRoot:
    canonical_path: Path
    _device: int = field(repr=False)
    _inode: int = field(repr=False)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    status: str
    _files: dict[str, tuple[int, int, int, int, int]]
    _texts: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class WorkspaceFileChange:
    relative_path: str
    change_kind: str
    lines_added: int
    lines_deleted: int
    diff_status: str
    unified_diff: str | None = None


@dataclass(frozen=True)
class WorkspaceChange:
    changed_files: int
    scan_status: str
    changes: tuple[WorkspaceFileChange, ...] = ()
    details_omitted: int = 0


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    return bool(
        int(getattr(file_stat, "st_file_attributes", 0) or 0)
        & _REPARSE_POINT
    )


def _directory_identity(file_stat: os.stat_result) -> tuple[int, int]:
    device = int(getattr(file_stat, "st_dev", 0) or 0)
    inode = int(getattr(file_stat, "st_ino", 0) or 0)
    if device <= 0 or inode <= 0:
        raise WorkspaceRootError("Workspace directory identity is unavailable.")
    return device, inode


def _safe_directory_stat(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> os.stat_result:
    try:
        file_stat = os.lstat(path)
    except OSError as exc:
        raise WorkspaceRootError("Workspace directory cannot be inspected.") from exc
    if (
        stat.S_ISLNK(file_stat.st_mode)
        or _is_reparse_point(file_stat)
        or not stat.S_ISDIR(file_stat.st_mode)
    ):
        raise WorkspaceRootError(
            "Workspace directory must not be a symlink or reparse point."
        )
    identity = _directory_identity(file_stat)
    if expected_identity is not None and identity != expected_identity:
        raise WorkspaceRootError("Workspace directory identity changed.")
    return file_stat


def validate_workspace_root(root: Path | str) -> ValidatedWorkspaceRoot:
    """Bind an absolute, non-reparse workspace root to its canonical identity."""
    candidate = Path(root)
    if not candidate.is_absolute():
        raise WorkspaceRootError("Workspace root must be absolute.")

    # Inspect the caller-supplied terminal entry before resolving it. Resolving
    # first would turn a root symlink/junction into an apparently ordinary
    # target and let the runner and observer bind different directory names.
    original_stat = _safe_directory_stat(candidate)
    original_identity = _directory_identity(original_stat)
    try:
        canonical_path = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceRootError("Workspace root cannot be resolved.") from exc

    # Close replacement windows on both the original spelling and the
    # canonical spelling before issuing a reusable identity token.
    _safe_directory_stat(candidate, expected_identity=original_identity)
    canonical_stat = _safe_directory_stat(
        canonical_path,
        expected_identity=original_identity,
    )
    device, inode = _directory_identity(canonical_stat)
    return ValidatedWorkspaceRoot(
        canonical_path=canonical_path,
        _device=device,
        _inode=inode,
    )


def revalidate_workspace_root(root: ValidatedWorkspaceRoot) -> Path:
    """Fail closed unless the canonical root still has the bound identity."""
    if not isinstance(root, ValidatedWorkspaceRoot):
        raise WorkspaceRootError("A validated workspace root is required.")
    _safe_directory_stat(
        root.canonical_path,
        expected_identity=(root._device, root._inode),
    )
    return root.canonical_path


def is_safe_workspace_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 240:
        return False
    if value.startswith(("/", "\\")) or "\\" in value or ":" in value:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    parts = value.split("/")
    return (
        len(parts) <= 32
        and all(part not in {"", ".", ".."} and len(part) <= 96 for part in parts)
    )


def is_safe_public_text(value: object, *, max_chars: int) -> bool:
    if not isinstance(value, str) or len(value) > max_chars or "\0" in value:
        return False
    if _ABSOLUTE_PATH_PATTERN.search(value):
        return False
    if any(pattern.search(value) for pattern in _SECRET_TEXT_PATTERNS):
        return False
    return all(
        character in "\n\r\t" or ord(character) >= 32
        for character in value
    )


def _is_suspected_secret_name(relative_path: str) -> bool:
    for component in relative_path.split("/"):
        lowered = component.casefold()
        if (
            _SECRET_NAME_PATTERN.search(lowered) is not None
            or Path(lowered).suffix in {".key", ".p12", ".pem", ".pfx"}
        ):
            return True
    return False


def _same_open_file(
    observed: os.stat_result,
    opened: os.stat_result,
) -> bool:
    observed_inode = int(getattr(observed, "st_ino", 0) or 0)
    opened_inode = int(getattr(opened, "st_ino", 0) or 0)
    observed_device = int(getattr(observed, "st_dev", 0) or 0)
    opened_device = int(getattr(opened, "st_dev", 0) or 0)
    return (
        observed_inode > 0
        and opened_inode > 0
        and observed_inode == opened_inode
        and observed_device > 0
        and opened_device > 0
        and observed_device == opened_device
    )


def _file_metadata(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(file_stat.st_mode),
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(file_stat.st_ctime_ns),
        int(file_stat.st_ino),
    )


def _read_public_text_descriptor(
    descriptor: int,
    opened: os.stat_result,
    expected_metadata: tuple[int, int, int, int, int],
    *,
    remaining_bytes: int,
) -> tuple[str | None, int]:
    if (
        not stat.S_ISREG(opened.st_mode)
        or _is_reparse_point(opened)
        or _file_metadata(opened) != expected_metadata
        or opened.st_size > _MAX_CAPTURE_FILE_BYTES
        or opened.st_size > remaining_bytes
    ):
        return None, 0
    chunks: list[bytes] = []
    captured = 0
    while captured <= _MAX_CAPTURE_FILE_BYTES:
        chunk = os.read(
            descriptor,
            min(8192, _MAX_CAPTURE_FILE_BYTES + 1 - captured),
        )
        if not chunk:
            break
        chunks.append(chunk)
        captured += len(chunk)
    final_stat = os.fstat(descriptor)
    if (
        captured > _MAX_CAPTURE_FILE_BYTES
        or not _same_open_file(opened, final_stat)
        or _file_metadata(opened) != _file_metadata(final_stat)
    ):
        return None, 0
    raw = b"".join(chunks)
    if b"\0" in raw:
        return None, 0
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, 0
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not is_safe_public_text(text, max_chars=_MAX_CAPTURE_FILE_BYTES):
        return None, 0
    return text, len(raw)


def _capture_small_public_text(
    path: str,
    relative_path: str,
    observed: os.stat_result,
    *,
    remaining_bytes: int,
    validate_ancestors: Callable[[], None] | None = None,
) -> tuple[str | None, int]:
    if (
        not is_safe_workspace_relative_path(relative_path)
        or _is_suspected_secret_name(relative_path)
        or Path(relative_path).suffix.casefold() not in _SAFE_TEXT_SUFFIXES
        or observed.st_size > _MAX_CAPTURE_FILE_BYTES
        or observed.st_size > remaining_bytes
    ):
        return None, 0
    descriptor = None
    try:
        if validate_ancestors is not None:
            validate_ancestors()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | int(getattr(os, "O_BINARY", 0) or 0)
            | int(getattr(os, "O_NOFOLLOW", 0) or 0),
        )
        opened = os.fstat(descriptor)
        if validate_ancestors is not None:
            validate_ancestors()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
            or not _same_open_file(observed, opened)
            or _file_metadata(observed) != _file_metadata(opened)
        ):
            return None, 0
        return _read_public_text_descriptor(
            descriptor,
            opened,
            _file_metadata(observed),
            remaining_bytes=remaining_bytes,
        )
    except OSError:
        return None, 0
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return None, 0


def _normalize_windows_final_path(value: str) -> str:
    normalized = value
    if normalized.casefold().startswith("\\\\?\\unc\\"):
        normalized = "\\\\" + normalized[8:]
    elif normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    return os.path.normcase(os.path.normpath(normalized))


def _windows_final_path_from_handle(handle_value: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    handle = wintypes.HANDLE(handle_value)
    capacity = 512
    while capacity <= 32_768:
        buffer = ctypes.create_unicode_buffer(capacity)
        length = int(get_final_path(handle, buffer, capacity, 0))
        if length == 0:
            error = ctypes.get_last_error()
            raise OSError(error, "Cannot resolve an opened file handle.")
        if length < capacity:
            return _normalize_windows_final_path(buffer.value)
        capacity = length + 1
    raise OSError("Opened file path exceeds the safety limit.")


def _windows_final_path_from_descriptor(descriptor: int) -> str:
    import msvcrt

    return _windows_final_path_from_handle(msvcrt.get_osfhandle(descriptor))


def _close_windows_handles(handles: list[int]) -> None:
    if not handles:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    for handle in reversed(handles):
        close_handle(wintypes.HANDLE(handle))


def _open_windows_directory_guards(
    chain: tuple[tuple[Path, tuple[int, int]], ...],
) -> list[int]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

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
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL

    file_read_attributes = 0x0080
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    file_attribute_directory = 0x00000010
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    invalid_handle = ctypes.c_void_p(-1).value

    handles: list[int] = []
    try:
        for directory, expected_identity in chain:
            handle = create_file(
                str(directory),
                file_read_attributes,
                file_share_read | file_share_write,
                None,
                open_existing,
                file_flag_backup_semantics | file_flag_open_reparse_point,
                None,
            )
            handle_value = int(handle)
            if handle_value == invalid_handle:
                error = ctypes.get_last_error()
                raise OSError(error, "Cannot lock a workspace directory.")
            handles.append(handle_value)

            information = ByHandleFileInformation()
            if not get_information(handle, ctypes.byref(information)):
                error = ctypes.get_last_error()
                raise OSError(error, "Cannot inspect a workspace directory handle.")
            if (
                not information.dwFileAttributes & file_attribute_directory
                or information.dwFileAttributes & _REPARSE_POINT
                or _windows_final_path_from_handle(handle_value)
                != _normalize_windows_final_path(str(directory))
            ):
                raise OSError("Workspace directory handle failed containment checks.")
            _safe_directory_stat(
                directory,
                expected_identity=expected_identity,
            )
        return handles
    except Exception:
        _close_windows_handles(handles)
        raise


def _windows_guard_chain(
    root: ValidatedWorkspaceRoot,
    directory_chain: tuple[tuple[Path, tuple[int, int]], ...],
) -> tuple[tuple[Path, tuple[int, int]], ...]:
    parent = root.canonical_path.parent
    if parent == root.canonical_path:
        return directory_chain
    parent_stat = _safe_directory_stat(parent)
    return ((parent, _directory_identity(parent_stat)),) + directory_chain


def _capture_windows_exact_public_text(
    root: ValidatedWorkspaceRoot,
    relative_path: str,
    expected_metadata: tuple[int, int, int, int, int],
    *,
    remaining_bytes: int,
    directory_chain: tuple[tuple[Path, tuple[int, int]], ...],
) -> tuple[str | None, int]:
    expected_path = root.canonical_path.joinpath(*relative_path.split("/"))
    expected_final_path = _normalize_windows_final_path(str(expected_path))
    descriptor = None
    guards: list[int] = []
    try:
        revalidate_workspace_root(root)
        guards = _open_windows_directory_guards(
            _windows_guard_chain(root, directory_chain)
        )
        revalidate_workspace_root(root)
        descriptor = os.open(
            expected_path,
            os.O_RDONLY
            | int(getattr(os, "O_BINARY", 0) or 0)
            | int(getattr(os, "O_NOINHERIT", 0) or 0)
            | int(getattr(os, "O_NOFOLLOW", 0) or 0),
        )
        opened = os.fstat(descriptor)
        actual_final_path = _windows_final_path_from_descriptor(descriptor)
        if actual_final_path != expected_final_path:
            return None, 0
        revalidate_workspace_root(root)
        return _read_public_text_descriptor(
            descriptor,
            opened,
            expected_metadata,
            remaining_bytes=remaining_bytes,
        )
    except OSError:
        return None, 0
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _close_windows_handles(guards)


def _windows_exact_file_metadata(
    root: ValidatedWorkspaceRoot,
    relative_path: str,
    directory_chain: tuple[tuple[Path, tuple[int, int]], ...],
) -> tuple[int, int, int, int, int] | None:
    expected_path = root.canonical_path.joinpath(*relative_path.split("/"))
    expected_final_path = _normalize_windows_final_path(str(expected_path))
    descriptor = None
    guards: list[int] = []
    try:
        revalidate_workspace_root(root)
        guards = _open_windows_directory_guards(
            _windows_guard_chain(root, directory_chain)
        )
        revalidate_workspace_root(root)
        descriptor = os.open(
            expected_path,
            os.O_RDONLY
            | int(getattr(os, "O_BINARY", 0) or 0)
            | int(getattr(os, "O_NOINHERIT", 0) or 0)
            | int(getattr(os, "O_NOFOLLOW", 0) or 0),
        )
        opened = os.fstat(descriptor)
        if (
            _windows_final_path_from_descriptor(descriptor)
            != expected_final_path
            or not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
        ):
            return None
        revalidate_workspace_root(root)
        return _file_metadata(opened)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _close_windows_handles(guards)


def _capture_posix_exact_public_text(
    root: ValidatedWorkspaceRoot,
    relative_path: str,
    expected_metadata: tuple[int, int, int, int, int],
    *,
    remaining_bytes: int,
) -> tuple[str | None, int]:
    no_follow = int(getattr(os, "O_NOFOLLOW", 0) or 0)
    directory_flag = int(getattr(os, "O_DIRECTORY", 0) or 0)
    if (
        no_follow == 0
        or directory_flag == 0
        or os.open not in getattr(os, "supports_dir_fd", set())
    ):
        return None, 0

    descriptors: list[int] = []
    try:
        directory_flags = (
            os.O_RDONLY
            | no_follow
            | directory_flag
            | int(getattr(os, "O_CLOEXEC", 0) or 0)
        )
        current = os.open(root.canonical_path, directory_flags)
        descriptors.append(current)
        root_stat = os.fstat(current)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or _is_reparse_point(root_stat)
            or _directory_identity(root_stat) != (root._device, root._inode)
        ):
            raise WorkspaceRootError("Workspace root identity changed.")

        parts = relative_path.split("/")
        for component in parts[:-1]:
            current = os.open(
                component,
                directory_flags,
                dir_fd=current,
            )
            descriptors.append(current)
            directory_stat = os.fstat(current)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or _is_reparse_point(directory_stat)
            ):
                return None, 0

        descriptor = os.open(
            parts[-1],
            os.O_RDONLY
            | no_follow
            | int(getattr(os, "O_CLOEXEC", 0) or 0),
            dir_fd=current,
        )
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        return _read_public_text_descriptor(
            descriptor,
            opened,
            expected_metadata,
            remaining_bytes=remaining_bytes,
        )
    except OSError:
        return None, 0
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _posix_exact_file_metadata(
    root: ValidatedWorkspaceRoot,
    relative_path: str,
) -> tuple[int, int, int, int, int] | None:
    no_follow = int(getattr(os, "O_NOFOLLOW", 0) or 0)
    directory_flag = int(getattr(os, "O_DIRECTORY", 0) or 0)
    if (
        no_follow == 0
        or directory_flag == 0
        or os.open not in getattr(os, "supports_dir_fd", set())
    ):
        return None

    descriptors: list[int] = []
    try:
        directory_flags = (
            os.O_RDONLY
            | no_follow
            | directory_flag
            | int(getattr(os, "O_CLOEXEC", 0) or 0)
        )
        current = os.open(root.canonical_path, directory_flags)
        descriptors.append(current)
        root_stat = os.fstat(current)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or _is_reparse_point(root_stat)
            or _directory_identity(root_stat) != (root._device, root._inode)
        ):
            raise WorkspaceRootError("Workspace root identity changed.")

        parts = relative_path.split("/")
        for component in parts[:-1]:
            current = os.open(
                component,
                directory_flags,
                dir_fd=current,
            )
            descriptors.append(current)
            directory_stat = os.fstat(current)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or _is_reparse_point(directory_stat)
            ):
                return None

        descriptor = os.open(
            parts[-1],
            os.O_RDONLY
            | no_follow
            | int(getattr(os, "O_CLOEXEC", 0) or 0),
            dir_fd=current,
        )
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _is_reparse_point(opened):
            return None
        return _file_metadata(opened)
    except OSError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _exact_file_metadata(
    root: ValidatedWorkspaceRoot,
    relative_path: str,
    directory_chain: tuple[tuple[Path, tuple[int, int]], ...],
) -> tuple[int, int, int, int, int] | None:
    if not is_safe_workspace_relative_path(relative_path):
        return None
    if os.name == "nt":
        return _windows_exact_file_metadata(
            root,
            relative_path,
            directory_chain,
        )
    return _posix_exact_file_metadata(root, relative_path)


def _capture_exact_public_text(
    root: ValidatedWorkspaceRoot,
    relative_path: str,
    expected_metadata: tuple[int, int, int, int, int],
    *,
    remaining_bytes: int,
    directory_chain: tuple[tuple[Path, tuple[int, int]], ...],
) -> tuple[str | None, int]:
    if (
        not is_safe_workspace_relative_path(relative_path)
        or _is_suspected_secret_name(relative_path)
        or Path(relative_path).suffix.casefold() not in _SAFE_TEXT_SUFFIXES
        or expected_metadata[1] > _MAX_CAPTURE_FILE_BYTES
        or expected_metadata[1] > remaining_bytes
    ):
        return None, 0
    if os.name == "nt":
        return _capture_windows_exact_public_text(
            root,
            relative_path,
            expected_metadata,
            remaining_bytes=remaining_bytes,
            directory_chain=directory_chain,
        )
    return _capture_posix_exact_public_text(
        root,
        relative_path,
        expected_metadata,
        remaining_bytes=remaining_bytes,
    )


def capture_workspace_snapshot(
    root: Path | ValidatedWorkspaceRoot,
    *,
    max_entries: int = _DEFAULT_MAX_ENTRIES,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    public_text_allowlist: frozenset[str] = frozenset(),
) -> WorkspaceSnapshot:
    """Capture bounded metadata and small public-safe text without following links."""
    if max_entries < 1 or max_depth < 0 or timeout_seconds <= 0:
        raise ValueError("Workspace snapshot limits must be positive.")
    validated_root = (
        root
        if isinstance(root, ValidatedWorkspaceRoot)
        else validate_workspace_root(root)
    )
    root_path = revalidate_workspace_root(validated_root)
    root_identity = (validated_root._device, validated_root._inode)

    deadline = time.monotonic() + timeout_seconds
    files: dict[str, tuple[int, int, int, int, int]] = {}
    file_chains: dict[
        str,
        tuple[tuple[Path, tuple[int, int]], ...],
    ] = {}
    texts: dict[str, str] = {}
    captured_bytes = 0
    root_chain = ((root_path, root_identity),)
    stack: list[
        tuple[Path, str, int, tuple[tuple[Path, tuple[int, int]], ...]]
    ] = [
        (root_path, "", 0, root_chain)
    ]
    entries_seen = 0
    partial_error = False
    depth_limited = False

    def revalidate_chain(
        chain: tuple[tuple[Path, tuple[int, int]], ...],
    ) -> None:
        revalidate_workspace_root(validated_root)
        for directory, expected_identity in chain[1:]:
            _safe_directory_stat(
                directory,
                expected_identity=expected_identity,
            )

    def snapshot(
        status: str,
        chain: tuple[tuple[Path, tuple[int, int]], ...] = root_chain,
    ) -> WorkspaceSnapshot:
        revalidate_chain(chain)
        return WorkspaceSnapshot(status, files, texts)

    while stack:
        if time.monotonic() >= deadline:
            return snapshot("partial_time_limit")
        directory, relative_directory, depth, directory_chain = stack.pop()
        try:
            revalidate_chain(directory_chain)
            with os.scandir(directory) as iterator:
                # os.scandir(path) may have opened a replacement between the
                # pre-check and handle creation. Recheck the complete chain
                # before accepting any entries from that handle.
                revalidate_chain(directory_chain)
                for entry in iterator:
                    if time.monotonic() >= deadline:
                        return snapshot("partial_time_limit", directory_chain)
                    entries_seen += 1
                    if entries_seen > max_entries:
                        return snapshot("partial_item_limit", directory_chain)
                    if entry.name.casefold() in _SKIPPED_DIRECTORY_NAMES:
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        file_stat = os.lstat(entry.path)
                    except OSError:
                        partial_error = True
                        continue
                    if _is_reparse_point(file_stat):
                        continue

                    relative = (
                        f"{relative_directory}/{entry.name}"
                        if relative_directory
                        else entry.name
                    )
                    if stat.S_ISDIR(file_stat.st_mode):
                        if depth >= max_depth:
                            depth_limited = True
                            continue
                        try:
                            directory_identity = _directory_identity(file_stat)
                        except WorkspaceRootError:
                            partial_error = True
                            continue
                        stack.append(
                            (
                                Path(entry.path),
                                relative,
                                depth + 1,
                                directory_chain
                                + ((Path(entry.path), directory_identity),),
                            )
                        )
                        continue
                    if not stat.S_ISREG(file_stat.st_mode):
                        continue
                    verified_metadata = _exact_file_metadata(
                        validated_root,
                        relative,
                        directory_chain,
                    )
                    if verified_metadata is not None:
                        files[relative] = verified_metadata
                        file_chains[relative] = directory_chain
                    else:
                        partial_error = True
                revalidate_chain(directory_chain)
        except OSError:
            partial_error = True

    if depth_limited:
        status = "partial_depth_limit"
    elif partial_error:
        status = "partial_error"
    else:
        status = "scoped_complete"

    # Recursive discovery is permanently metadata-only. Public text is opened
    # afterwards through an exact-path reader whose same file descriptor is
    # checked against the validated root before any body bytes are read.
    if status == "scoped_complete":
        for relative in sorted(public_text_allowlist):
            if time.monotonic() >= deadline:
                return snapshot("partial_time_limit")
            expected_metadata = files.get(relative)
            directory_chain = file_chains.get(relative)
            if expected_metadata is None or directory_chain is None:
                continue
            text, used_bytes = _capture_exact_public_text(
                validated_root,
                relative,
                expected_metadata,
                remaining_bytes=_MAX_CAPTURE_TOTAL_BYTES - captured_bytes,
                directory_chain=directory_chain,
            )
            if text is not None:
                texts[relative] = text
                captured_bytes += used_bytes
    return snapshot(status)


def _bounded_unified_diff(
    relative_path: str,
    before: str,
    after: str,
    *,
    remaining_chars: int,
) -> tuple[str, int, int, str] | None:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
            n=3,
            lineterm="\n",
        )
    )
    if not lines:
        return "", 0, 0, "metadata_only"
    added = sum(
        1
        for line in lines
        if line.startswith("+") and not line.startswith("+++")
    )
    deleted = sum(
        1
        for line in lines
        if line.startswith("-") and not line.startswith("---")
    )
    limit = min(_MAX_DIFF_CHARS_PER_FILE, remaining_chars)
    if limit < 256:
        return None
    complete = "".join(lines)
    if len(complete) <= limit:
        diff = complete
        diff_status = "available"
    else:
        suffix = "\n… diff 已按安全上限截断 …\n"
        diff = complete[: max(0, limit - len(suffix))] + suffix
        diff_status = "truncated"
    if not is_safe_public_text(diff, max_chars=limit):
        return None
    return diff, added, deleted, diff_status


def compare_workspace_snapshots(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> WorkspaceChange:
    statuses = {before.status, after.status}
    if "unavailable" in statuses:
        return WorkspaceChange(changed_files=0, scan_status="unavailable")

    common = before._files.keys() & after._files.keys()
    changed_common = {
        name for name in common if before._files[name] != after._files[name]
    }
    if statuses == {"scoped_complete"}:
        changed_names = changed_common | (before._files.keys() ^ after._files.keys())
        scan_status = "scoped_complete"
    else:
        changed_names = changed_common
        priority = (
            "partial_time_limit",
            "partial_item_limit",
            "partial_depth_limit",
            "partial_error",
        )
        scan_status = next(
            (candidate for candidate in priority if candidate in statuses),
            "partial_error",
        )

    changes: list[WorkspaceFileChange] = []
    diff_chars = 0
    if statuses == {"scoped_complete"}:
        for relative_path in sorted(changed_names):
            if len(changes) >= _MAX_PUBLIC_CHANGE_FILES:
                break
            existed_before = relative_path in before._files
            exists_after = relative_path in after._files
            before_text = before._texts.get(relative_path)
            after_text = after._texts.get(relative_path)
            if (
                not is_safe_workspace_relative_path(relative_path)
                or (existed_before and before_text is None)
                or (exists_after and after_text is None)
            ):
                continue
            change_kind = (
                "added"
                if not existed_before
                else "deleted"
                if not exists_after
                else "modified"
            )
            generated = _bounded_unified_diff(
                relative_path,
                before_text or "",
                after_text or "",
                remaining_chars=_MAX_DIFF_CHARS_TOTAL - diff_chars,
            )
            if generated is None:
                continue
            unified_diff, added, deleted, diff_status = generated
            if diff_status == "metadata_only":
                change_kind = "metadata"
            diff_chars += len(unified_diff)
            changes.append(
                WorkspaceFileChange(
                    relative_path=relative_path,
                    change_kind=change_kind,
                    lines_added=added,
                    lines_deleted=deleted,
                    diff_status=diff_status,
                    unified_diff=unified_diff or None,
                )
            )

    changed_files = len(changed_names)
    return WorkspaceChange(
        changed_files=changed_files,
        scan_status=scan_status,
        changes=tuple(changes),
        details_omitted=max(0, changed_files - len(changes)),
    )
