from __future__ import annotations


def normalize(intervals):
    ordered = []
    for start, end in intervals:
        start, end = int(start), int(end)
        if start > end:
            start, end = end, start
        ordered.append((start, end))
    ordered.sort()

    merged = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def available(base, blocked):
    base_start, base_end = base
    gaps = []
    cursor = base_start
    for start, end in normalize(blocked):
        if end < base_start or start > base_end:
            continue
        start = max(start, base_start)
        end = min(end, base_end)
        if cursor < start:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor <= base_end:
        gaps.append((cursor, base_end))
    return gaps
