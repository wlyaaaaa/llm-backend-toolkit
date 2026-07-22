# General Agent v1

This suite compares agent harnesses, not foundation-model intelligence. Every
candidate uses the same local model identity and receives the same public task
workspace. Hidden deterministic verifiers remain outside the writable sandbox.

The suite covers three bounded capabilities:

- evidence reasoning and untrusted-instruction resistance;
- code repair against a written contract and hidden edge cases;
- constrained workflow planning with prohibited actions and a unique optimum.

Safety and correctness are gates. Time breaks ties only after correctness.
Results are valid only for the recorded suite fingerprint, model digest,
runtime, agent CLI versions, sandbox contract, and toolkit commit.

The PersonalOS data-factory benchmark remains a separate scenario-specific
acceptance and is not included in a universal score.

The version-bound 2026-07-22 acceptance is recorded in
[`docs/general-agent-benchmark-v1.md`](../../docs/general-agent-benchmark-v1.md).
