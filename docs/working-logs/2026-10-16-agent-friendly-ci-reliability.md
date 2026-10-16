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
