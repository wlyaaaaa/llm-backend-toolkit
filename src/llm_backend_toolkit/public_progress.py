from __future__ import annotations

import re
import unicodedata
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|authorization|passwd|password|secret|token)"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9+/_.=-]{8,}"
    ),
)
_ALWAYS_UNSAFE_PATH_PATTERNS = (
    re.compile(r"\bfile:(?:/{1,3}|\\\\)", re.IGNORECASE),
    re.compile(r"(?<![A-Z0-9_/\\])[A-Z]:[\\/][^\s\x00]*", re.IGNORECASE),
    re.compile(r"\\\\[^\s\\/]+\\[^\s\\/]+"),
    re.compile(r"\\Device\\[^\s\\/]+\\[^\s\\/]+", re.IGNORECASE),
    re.compile(r"""(?:^|[\s(\[{'\"=:：（【「『])\\(?!\\)[^\s\\/]+\\[^\s\\/]+"""),
)
_NON_URL_PATH_PATTERNS = (
    re.compile(r"//[^\s\\/]+/[^\s\\/]+"),
    re.compile(r"(?<![:/])/(?!/)[^\s\\/]+/[^\s\\/]+"),
    re.compile(r"""(?:^|[\s(\[{'\"=:：（【「『])/(?!/)[^\s\\/]+(?:/[^\s\\/]+)*"""),
)
_URL_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>＜＞]+")
_POTENTIAL_SECRET_SUFFIX_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|authorization|passwd|password|secret|token)"
    r"\s*[:=]\s*[\"']?[A-Za-z0-9+/_.=-]*\Z"
)


def is_safe_public_progress_text(value: str) -> bool:
    if any(
        pattern.search(value)
        for pattern in (*_SECRET_PATTERNS, *_ALWAYS_UNSAFE_PATH_PATTERNS)
    ):
        return False
    non_url_probe = _URL_PATTERN.sub("", value)
    return not any(pattern.search(non_url_probe) for pattern in _NON_URL_PATH_PATTERNS)


def has_potential_secret_suffix(value: str) -> bool:
    return bool(_POTENTIAL_SECRET_SUFFIX_PATTERN.search(value))


def _sanitize(value: Any, *, preserve_layout: bool) -> str:
    safe_chars: list[str] = []
    for char in str(value or ""):
        if preserve_layout and char in {"\n", "\t"}:
            safe_chars.append(char)
        elif unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            safe_chars.append(" ")
        elif char == "<":
            safe_chars.append("＜")
        elif char == ">":
            safe_chars.append("＞")
        else:
            safe_chars.append(char)
    return "".join(safe_chars)


def bounded_public_text(value: Any, *, max_chars: int) -> str:
    normalized = " ".join(_sanitize(value, preserve_layout=False).split())
    if not is_safe_public_progress_text(normalized):
        return ""
    return normalized[:max_chars].rstrip()


def bounded_public_draft(value: Any, *, max_chars: int) -> tuple[str, bool]:
    preserved = _sanitize(value, preserve_layout=True)
    if not preserved.strip() or not is_safe_public_progress_text(preserved):
        return "", False
    return preserved[:max_chars], len(preserved) > max_chars
