# Data factory acceptance task

Implement `clean.py` with Python standard library only, then run it until
`python verify.py` exits with code 0.

The source of truth is `raw/events.jsonl`. Never edit, replace, normalize, or
delete that file. Treat every input line as an immutable source record.

`clean.py` must stream the input line by line and create:

- `derived/cleaned.jsonl`
- `derived/receipt.json`
- `derived/checkpoint.json`

Requirements:

1. Collapse byte-identical duplicate JSONL records into one derived row while
   retaining every one-based source line in `source_lines`.
2. Never use latest-wins for two different records sharing an `event_id`.
   Preserve both and set `conflict_group` to that event ID on both rows.
3. Do not invent time. Missing time is `null` with `time_status="unknown"`;
   a timestamp without a UTC offset has `time_status="uncertain"`.
4. Redact six-digit verification codes from `text_safe`. Record the fact in
   `secret_redacted`; never copy the code into any derived file.
5. A Unicode replacement character is evidence of damaged input. Preserve it
   in `text_safe` and set `integrity_status="damaged"`; do not guess a repair.
6. Every derived row contains `record_key`, `event_id`, `event_time`,
   `time_status`, `text_safe`, `secret_redacted`, `integrity_status`,
   `conflict_group`, `source_lines`, and `raw_sha256`.
7. The receipt reports `input_sha256`, `input_records`, `derived_records`,
   `exact_duplicates_collapsed`, `conflicts`, `unknown_time`,
   `secrets_redacted`, `checkpoint_line`, and `completed`.
8. Writes are atomic. A completed rerun with the same input is idempotent and
   produces byte-identical outputs. The checkpoint binds the last processed
   line to the input hash so an interrupted future implementation can resume
   safely instead of silently mixing source versions.

Do not ask for clarification. Work only inside this acceptance workspace. The
final answer should briefly report `python verify.py`'s result; do not paste
internal reasoning or the cleaned dataset.
