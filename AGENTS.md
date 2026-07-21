# Project rules

This is a public, secret-free tool project. Collaborate with the owner in Simplified Chinese.

## Product boundary

- Keep this a tool for a top-level model, not an autonomous agent.
- The caller explicitly selects `qwen3.7-plus` or `qwen-main-v1`; never add automatic fallback.
- Keep context compaction visible through receipts and keep reasoning output disabled by default.
- Prefer result-side checks and compact artifacts over continuous process monitoring.
- Use asynchronous `submit` plus `job` as the normal AI entry. Keep synchronous `invoke` as a low-level interface.
- Keep native multimodal input, LocalOCR, and ChineseASR as distinct selectable routes.
- Never bypass LocalGpuBroker or call an internal Ollama backend directly.
- Require explicit `privacy.cloud_allowed=true` for all cloud-bound text, source excerpts, and media.

## Public safety

- Never commit API keys, authorization headers, private configuration, raw prompts/results, media, transcripts, OCR output, job state, logs, or machine snapshots.
- Use environment-variable names and public-safe examples only.
- Do not make another application's private configuration a project dependency.
- Do not perform a live cloud-model call in automated tests. Use mocks for cloud protocol and error behavior.

## Engineering

- Keep the Python core dependency-free when practical.
- Preserve stable JSON request, result, job, and error contracts.
- Add focused tests for behavior changes and run `python -m unittest discover -s tests -v`.
- Run `git diff --check` and a public-exposure scan before every push.
