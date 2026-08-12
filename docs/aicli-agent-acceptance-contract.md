# AICLI Agent / acceptance receipt contract

The Toolkit treats agent capability as receipt evidence, not as a model claim.
Only the managed AICLI receipt `aicli.agent.acceptance-receipt.v1` is accepted;
the model's text, a PONG, static profile metadata, or a Toolkit
`execution_receipt` is not an Agent acceptance receipt.

The receipt must bind three distinct observations:

- `requested`: the AICLI request (`model`, `provider_id`, `endpoint`, `wire`,
  reasoning, approval policy, and sandbox policy);
- `effective`: the app-server thread/result model, provider, and permission
  values after the request is resolved;
- `attested`: the independent provider `/responses` result, including exact
  response model, request identity, wire, and reasoning evidence.

Missing, contradictory, or model-authored identity fails closed. A live claim
also needs one fresh nontrivial Agent task in an isolated workspace, an
independent deterministic verifier, and confirmed process cleanup. This is the
minimum initial local capability proof. Stability, pressure, stress, and
long-horizon suites are optional; when not run they are `not_required` or
`not_run`, never an implied pass.

Evidence states remain separate:

| state | meaning |
| --- | --- |
| `static` | configuration/protocol inspection only |
| `historical` | a dated prior receipt, not a current live pass |
| `live` | fresh exact-identity AICLI receipt plus verifier |
| `unverified` | missing, stale, mismatched, or withdrawn evidence |

The reserved `codex-qwen3-7-plus-paygo` route is exact `qwen3.7-plus` and
Responses/Workspace only; it remains unverified and non-selectable until its
own receipt exists. Receipts for Max or Flash cannot be borrowed. Local and
Qwen3.8 evidence stay bound to their own model, provider, endpoint, runner,
Profile, and digest/fingerprint.
