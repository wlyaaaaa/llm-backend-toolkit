# Interval library repair

Repair `src/intervals.py` so it satisfies the complete contract below. Do not
modify `TASK.md`, `check.py`, or files under `tests/`. Use only the Python
standard library.

Intervals are inclusive integer pairs `(start, end)`.

## `normalize(intervals)`

- Return a new sorted list of tuple intervals.
- Merge overlapping intervals and directly adjacent intervals.
- Preserve negative coordinates.
- Reject a reversed interval (`start > end`) with `ValueError`; never silently
  swap its endpoints.
- Do not mutate the caller's list.

## `available(base, blocked)`

- `base` is one valid inclusive interval; reject a reversed base.
- Normalize `blocked`, clip it to `base`, and return every unblocked inclusive
  range in sorted order.
- Blocked ranges wholly outside `base` have no effect.
- Return an empty list when the complete base is blocked.
- Do not mutate caller inputs.

Run `python check.py` until its visible tests pass. Additional contract cases
will be checked outside the workspace. Return only a brief final result.
