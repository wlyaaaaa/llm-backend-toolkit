from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LATIN_WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]+")
CHINESE_RUN = re.compile(r"[\u3400-\u9fff]+")


@dataclass(frozen=True)
class SourceResult:
    inputs: list[dict[str, Any]]
    receipt: list[dict[str, Any]]


@dataclass(frozen=True)
class _Chunk:
    text: str
    line_start: int
    line_end: int


def _terms(text: str) -> set[str]:
    output = {match.group(0).lower() for match in LATIN_WORD.finditer(text)}
    for run in CHINESE_RUN.findall(text):
        if len(run) <= 4:
            output.add(run)
        for width in (2, 3, 4):
            output.update(run[index : index + width] for index in range(max(0, len(run) - width + 1)))
    return {term for term in output if len(term) >= 2}


def _chunks(text: str, target_chars: int) -> list[_Chunk]:
    lines = text.splitlines()
    if not lines:
        return [_Chunk("", 1, 1)]
    output: list[_Chunk] = []
    current: list[str] = []
    current_start = 1
    current_chars = 0
    for line_number, line in enumerate(lines, start=1):
        projected = current_chars + len(line) + 1
        if current and projected > target_chars:
            output.append(_Chunk("\n".join(current), current_start, line_number - 1))
            current = []
            current_start = line_number
            current_chars = 0
        current.append(line)
        current_chars += len(line) + 1
    if current:
        output.append(_Chunk("\n".join(current), current_start, len(lines)))
    return output


class SourceLoader:
    def __init__(self, *, chunk_chars: int = 2000, default_top_k: int = 6, default_max_chars: int = 12_000) -> None:
        self.chunk_chars = max(128, chunk_chars)
        self.default_top_k = max(1, default_top_k)
        self.default_max_chars = max(256, default_max_chars)

    def load(self, sources: list[dict[str, Any]], *, query: str) -> SourceResult:
        inputs: list[dict[str, Any]] = []
        receipt: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        query_terms = _terms(query)

        for source in sources:
            source_id = str(source.get("id") or "").strip()
            if not source_id or source_id in seen_ids:
                raise ValueError("Source IDs must be non-empty and unique")
            seen_ids.add(source_id)
            path = Path(str(source.get("path") or "")).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"Approved source does not exist: {source_id}")
            raw = path.read_bytes()
            if b"\x00" in raw:
                raise ValueError(f"Source is not a supported text artifact: {source_id}")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Source is not UTF-8 text: {source_id}") from exc
            digest = hashlib.sha256(raw).hexdigest()
            source_chunks = _chunks(text, self.chunk_chars)
            scored: list[tuple[int, int, _Chunk]] = []
            for index, chunk in enumerate(source_chunks):
                lowered = chunk.text.lower()
                score = sum(lowered.count(term) * (1 + min(4, len(term))) for term in query_terms)
                if chunk.text.lstrip().startswith("#"):
                    score += 1
                scored.append((score, index, chunk))
            top_k = max(1, int(source.get("top_k") or self.default_top_k))
            max_chars = max(256, int(source.get("max_chars") or self.default_max_chars))
            ranked = sorted(scored, key=lambda item: (-item[0], item[1]))
            if ranked and ranked[0][0] == 0 and len(source_chunks) > 1:
                ranked = [scored[0], scored[-1]]
            # Keep relevance order so the backend sees the strongest evidence first.
            selected = ranked[:top_k]
            used = 0
            ranges: list[dict[str, int]] = []
            for _score, _index, chunk in selected:
                remaining = max_chars - used
                if remaining <= 0:
                    break
                excerpt = chunk.text[:remaining]
                if not excerpt:
                    continue
                inputs.append(
                    {
                        "source_id": source_id,
                        "line_start": chunk.line_start,
                        "line_end": chunk.line_end,
                        "sha256": digest,
                        "excerpt": excerpt,
                    }
                )
                used += len(excerpt)
                ranges.append({"line_start": chunk.line_start, "line_end": chunk.line_end})
            receipt.append(
                {
                    "id": source_id,
                    "sha256": digest,
                    "source_chars": len(text),
                    "selected_chars": used,
                    "selected_ranges": ranges,
                }
            )
        return SourceResult(inputs, receipt)
