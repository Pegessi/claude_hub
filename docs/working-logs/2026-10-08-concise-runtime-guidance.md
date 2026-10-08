# Concise runtime guidance and optional progress tracking

## Scope

This change continues `feat/agent-workflow-v2` after `782d295`. The user requested
shorter Agent guidance and an optional progress-tracking subagent for complex,
long-running work. It changes the shared `EXECUTION_POLICY` and native Chat
`HUB_RUNTIME_GUIDANCE`, not Task execution, credentials, dispatch, scheduling,
Bot behavior, or provider transport code. No feature branches are split or merged.

Native Chat still receives one sentinel-wrapped guidance prefix on the first
turn of each transport session. It is omitted from the displayed/persisted user
text, but the model sees it. Managed assignments reuse the shared policy and
retain their existing task-specific reporting and review instructions.

## Decisions

- Keep one Task per independent goal, separate source from execution control,
  preserve same-ID handoff, and require explicit Workspace dispatch.
- Keep direct execution, available native subagents, and Hub assistance as choices
  based on the work. Do not imply an independent quota for a new session.
- For complex, long-running work, a progress-tracking subagent is optional when
  the coordination benefit justifies its overhead. It receives the same Task
  reference, concise evidence updates, and explicit reporting instructions.
- The tracker summarizes milestones, blockers, and validation without duplicating
  execution or declaring completion/release on its own. Direct Task reporting
  needs explicit authorization; otherwise it returns a brief update to the owner.
  Avoid repeated wakeups without new evidence or a requested check, and skip
  unchanged reports. The owner still decides, integrates evidence, and delivers
  the result.
- Tracking is a role, not a new credential scope. Existing reporter keys permit
  progress, whole-Task completion, and release; they do not grant other Task
  management. Keys remain in protected files, and child context/permissions must
  not be assumed to be inherited. Managed Tasks retain their managed report path.
- Keep source-backed user corrections, relevant-lesson lookup, user-requested
  scheduling, correct Chat-versus-Terminal schedule kinds, and queued rather than
  interrupting Chat turns. Remove repeated schedule authorization and tmux rules
  from the native-specific section when the shared policy already states them.

There is no new always-running tracking service, automatic subagent creation,
parallel Task registry, or default credential handoff. Delegating bookkeeping
does not move the owner's existing tool history out of its context. Actual
provider behavior, permissions, context isolation, and cost still determine
whether this option is useful.

## Size and verification

Measured by evaluating only the string-constant ASTs, without importing the
runtime or reading any live state:

| Text | Before | After |
| --- | ---: | ---: |
| Shared execution policy | 1,668 characters | 1,715 characters |
| Complete native guidance, including shared policy | 4,371 characters | 3,642 characters |

The complete native prefix is 16.68% shorter. The shared policy is slightly
longer because it now includes the optional tracker's role and boundaries.
These are character counts, not measured model tokens or cost savings. ASTs of
both production modules are identical after replacing the two prompt constants
with placeholders: runtime logic and injection mechanics are unchanged.
Evidence: `/tmp/claude-hub-guidance-release.S6BBC9/size.json`.

Focused validation covers policy boundaries, simple/complex managed assignments,
native first-turn injection and transcript stripping, prompt measurement, Task
ownership, CLI, and documentation contracts. The final wording passed 237 tests;
all 122 backend source files passed mypy, and the three changed Python files
passed Black/isort checks. Evidence: `/tmp/claude-hub-guidance-release.S6BBC9`.
Earlier wording runs are retained separately and are not added to this count.

Independent read-only review covered the exact final policy/native/test objects
and confirmed the optional role, ownership, reporting access, and unchanged
mechanisms. The reviewer did not run tests. Prompt assertions and mocked
transport tests do not prove that a real model will choose or operate a tracking
subagent correctly.

No real Provider, OAuth, or Bot execution, main merge, push, deployment, or
production restart is part of this change.
