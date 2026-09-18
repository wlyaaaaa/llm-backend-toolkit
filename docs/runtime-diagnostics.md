# Runtime diagnostics and task control

The tool remains an explicit backend job utility, not an independent agent or native child thread.

## No-generation diagnosis

`llm-backend-toolkit diagnose --backend <id>` reports the effective selected
endpoint, model, vision support, caller credential presence, normalized registry
snapshot hash, and actual package/interpreter source. It does not contact a
provider, read task materials, create a job or change state. A broken optional
provider cannot disable another route or the backend catalog.

When AICLI is selected by the caller's `LLM_TOOLKIT_AICLI_ENTRY`, its advertised
`aicli.runtime-diagnostics.v1` interface is used. An explicit
`--installed-aicli-entry <path>` compares another known installation, never a
fallback. `--bridge-registry <path>` selects an existing owner registry for
seven-file desktop-release verification. An old interface reports unsupported.
The observer profile is explicit: a service account's empty Desktop state does
not prove the logged-in user's Desktop is unconfigured.

Configuration, installed bytes, running-process loading, and end-to-end evidence
are independent. The latter two remain unknown until actually observed.

## Read-only task metadata

```
llm-backend-toolkit jobs --limit 50
llm-backend-toolkit jobs --limit 50 --cursor <returned-cursor>
llm-backend-toolkit inspect --id <job-id>
llm-backend-toolkit inspect --id <job-id> --result
```

These paths do not create a store, alter poll counters, recover dead workers,
clean inputs, or launch an observer. Results are opt-in; lists contain bounded
metadata, not prompts or raw results. A corrupt entry is reported locally.

The compatibility `job` command can perform maintenance, recovery and cleanup.
Its poll counter updates share the same per-job lock as completion and cancellation;
it re-reads the state under that lock, so polling cannot overwrite a newer terminal.

## Cancellation and recovery evidence

```
llm-backend-toolkit cancel --id <job-id>
```

Queued work can be cancelled before execution. For an active Codex job, the
worker consumes an immutable `aicli.run-control.v1` handle written before model
execution. The handle contains only the exact run/profile/model/workspace binding.
The same already validated AICLI entry sends one `run abort`; no stored command or
arbitrary executable is accepted. An uncertain delivery is not blindly retried.

`accepted` means requested, not stopped. Native process-tree cleanup and the local
GPU session's verified release are required before cancellation is terminal.
A cancellation before model attestation can provide valid cleanup evidence while
model identity remains unavailable; it cannot pass model acceptance or resume as
an attested session. Missing cleanup retains `cleanup_unconfirmed`, the exact run
handle, and needed evidence. Inspect the owning AICLI run before retrying work.
Older runners and direct providers remain cooperative until their calls settle.
No new daemon or task-scheduler service is installed.

## Provider and model boundaries

Local Ollama uses its registered public broker endpoint. An environment override
cannot turn local work into remote transfer or bypass the broker. Deliberate
local-port migration updates the existing owning configuration. Cloud requests
require explicit Boolean `privacy.cloud_allowed=true` before materials are read.
Provider redirects are refused, credentials are unredirected headers, and local
transport ignores inherited HTTP proxies.

Requested model, provider-reported model and independent identity verification
are separate fields. Missing reported identity is null. A different ID can be a
provider alias or reroute; it is not silently rewritten. Direct results lacking
an exact matching reported model are not reused as successful cached answers.
Vision capability is propagated from the selected registry route to the provider,
preflight and media processing; explicit unsupported native media fails visibly.

DeepSeek's existing legacy backend ID remains compatible, while its selected
model is now `deepseek-flash` and supports the documented image input format.
Current API facts: https://api-docs.deepseek.com/updates/ and
https://api-docs.deepseek.com/guides/vision/ . V4 Pro is not removed by this update.
