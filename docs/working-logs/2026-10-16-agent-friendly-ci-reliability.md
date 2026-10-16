# Agent-friendly CI reliability

The user requested researched, agent-friendly guidance after oversized pytest
parameter IDs stalled CI output. The previous repair changed the two offending
parameter groups; this change makes the general failure mode actionable during
ordinary local/CI collection and documents the broader reliability contract.

## Decisions

- Keep `AGENTS.md`/`CLAUDE.md` as a short map and put the contract in
  `docs/CI_RELIABILITY.md`, reached from CONTRIBUTING as well.
- Adapt pytest, GitHub, Google testing and AGENTS primary sources, linked in the
  guide. Classify checks by the resources and behavior they need rather than
  imposing another organization's timing limits or test-size percentages.
- Use the existing pytest collection hook, shared through the root conftest,
  for a deterministic 512-character full-node-ID budget. It changes neither
  test data nor test identity. The baseline default collection at `47bdae7`
  contains 3569 cases, maximum 318 characters; collection took 9.904 seconds
  including the shared verification/tool-provenance entry.
- Return bounded JSON diagnostics with a stable code and file/line/length
  examples. Do not echo the offending ID. Exercise a real 8 MiB automatic ID,
  the same data with an explicit short ID, exact budget boundaries, multiple
  violations and ordinary assertion failure in isolated pytest subprocesses.
- Reuse pytest's existing slow-phase timing records for a ten-entry diagnostic;
  one second is only an output filter, never a performance gate. Runtime gates
  require representative paired measurements and a stated requirement.
- Keep existing verification selection and workflow unchanged. Clearly document
  the remaining backend artifact/inner-deadline gap; this change does not claim
  runner-loss recovery or alter another Task's workflow-startup repair.

## Validation

The six initial collection-policy regressions pass through the canonical focused
entry (1.08 seconds). Independent review then demonstrated that collection-only
reporters still printed rejected IDs after UsageError. Clear the pending items
on rejection and add real `--collect-only -q`/`-vv` regressions with 8 MiB data;
this preserves the failing exit rather than reporting an empty-suite success.
Final format/type, complete suite, document, staged and
independent-review results are recorded against the delivered SHA in the Task
and delivery receipt. Live providers and production service behavior are outside
this change's evidence.

Before final verification, sync main `4dff0c5` to retain the independently
published queue/Goal work. The only manual resolution preserves both CHANGELOG
entries; the [fixed sync record](2026-10-16-agent-friendly-ci-merge-decisions.json)
binds that decision to merge `a0c08bf3f893934c281a4355fc5c3124678cd533`.

A second independent cross-review of `08d01c4` found that `--collect-only -x`
can skip item modification after a collection error and still print previously
collected payload IDs. Reuse the guard at the start of collection-finish as well.
Two new 8 MiB regressions fail against the old plugin (`-q` and `-vv`), then pass
with bounded diagnostics after the repair. A short-ID control preserves the
original early collection failure and prevents test execution. Preserve the
first failing review/probe and final fixed-SHA evidence in the Task receipt.
