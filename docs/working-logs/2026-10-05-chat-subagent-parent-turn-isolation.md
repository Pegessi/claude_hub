# Native subagent completion ending the parent Chat turn

## Incident and evidence

User report: Chat tab `6d945ff3-e233-4327-93d1-2bb14d18eb8b` appeared to
stop twice during long multi-agent work. Investigation used clean
`main@683e1f5` and the isolated `fix/chat-turn-interruption` worktree.

The authenticated stream API was fully paginated: 5,783 events, sequences
0–5782, across two pages. Times below are Asia/Shanghai on 2026-10-05.

| Time | Hub stream | Provider evidence |
| --- | --- | --- |
| 18:16:12 | Parent completed at sequence 4865; assistant summary includes the UI child's report | Parent rollout records `turn_aborted(reason=interrupted)` 75 ms later. This confirms a real provider interruption on the first run, but does not identify its initiator. |
| 19:05:07 | Parent completed at sequence 5235 with the feedback child's report | Child `01a10bb8-2294-7e81-8798-2f09e869f128` completes its own turn at the same time. Parent is still running. |
| 19:05–19:15 | Three synthetic top-level turns opened by child continuations | Hub `provider_turn_id` values match child turns, not the running parent turn. |
| 19:15:35 | Sequence 5760 completes a synthetic parent turn; its summary interleaves parent and child text | Review child finishes while parent is preparing integration delivery. |
| 19:17:25 | No final answer in the Hub stream | Parent rollout completes provider turn `01a10bb7-0e74-7860-8476-0fc683ee196d`, including the final delivery report. Backend log explicitly drops `turn_completed` as unattributed, after dropping text deltas. |

The second apparent interruption is therefore proven to be a Hub lifecycle
and output-loss bug, not a long-turn timeout. The first run also contains the
misattributed completion, but its subsequent provider interruption has no
established initiator in the retained evidence. No watchdog error occurs in
the affected stream. Elapsed runtime alone is not causal evidence.

Provider evidence came from the exact pinned parent rollout
`~/.codex/sessions/2026/10/05/rollout-2026-10-05T02-02-13-01a10814-a4a2-7531-88a5-cc0e34c3c2da.jsonl`
and the two corresponding child rollouts in the same date directory. No
home-wide search was used. Backend diagnostics were filtered from the exact
log file; recent output-loss evidence was read from a bounded tail.

The missing parent report says candidate `codex/chat-workflow-integration`
was delivered at `0b4156e`, with 367 backend and 602 frontend tests plus a
real workflow smoke run. Those are recovered report claims, not validation
rerun by this investigation. No conversation events were rewritten or
messages resent, and no shared service was restarted.

## System overview and root cause

One persistent Codex/TraeX app-server transports parent and child JSON-RPC
notifications. Item text already carried `payload.subagent_thread`, but
`turn/started`, `turn/completed`, token usage, and errors did not enforce the
same owner boundary.

Consequently a child `turn/completed` became a top-level Hub completion.
`SessionTailer` persisted it, released the parent guard, and notified Goal and
schedule observers. Subsequent parent items had no active Hub turn and were
discarded by the existing late-replay guard. A later child `turn/started`
could open a phantom top-level turn. Independently, the tailer's final-answer
accumulator concatenated child text into the parent answer, despite the
frontend correctly rendering the live deltas in separate groups.

With no viewer and an expired idle TTL, releasing the parent guard also makes
the running provider eligible for idle process cleanup. This is a reproducible
consequence of the bug, but is not claimed as the proven initiator of the
first interruption.

## Module changes

- `codex_jsonl.py`: use existing thread attribution for lifecycle ownership.
  Child starts cannot create Hub turns; child completions cannot finish them.
  Completion still closes the child's reasoning indicators. Child errors
  become child-scoped status records, and child usage/Goal notices cannot
  update parent control state. Parent completion retains its existing final
  cleanup of all reasoning indicators.
- `native.py`: only a parent completion cleans up parent in-flight images.
  Missing thread identity retains legacy compatibility.
- `tailer.py`: only parent text enters the parent final answer and Goal
  protocol accumulator; child deltas remain persisted in their own groups.
- Removed an existing duplicate local type annotation in the touched adapter
  function so the targeted mypy check can pass without changing behavior.

## Validation and limits

Eleven regression cases failed on the original code and passed after the
fix. They cover both adapters/transports, child completed/failed/interrupted
status, reasoning and usage isolation, child errors and Goal notifications,
image ownership, continued parent output and a late child start while idle.
The async tailer test asserts that completion observers run only once and
the parent final answer excludes the child's report.

A twelfth regression verifies that a viewer-less parent survives child
completion even after the idle TTL expires; it passes on the fix.

Targeted stream, provider, Goal, queue/cancel and scheduled-task regression:
548 passed, plus the additional headless regression. Targeted black, isort
and mypy checks pass for all touched production modules. No frontend source
change was required.

Validation uses the installed interpreter with `PYTHONPATH` pinned to this
worktree's backend, and clears inherited `CLAUDE_HUB_TEST_BACKEND_URL`,
`CLAUDE_HUB_STATE_ROOT` and `CLAUDE_HUB_ALLOW_LIVE_RUNTIME`. The full backend
command matches CI's exclusions:

```sh
python -m pytest -q --ignore=tests/test_terminal_replay.py \
  --ignore=tests/test_terminal_input_latency_perf.py
```

The remote-agent fixture failure in
`test_cli_reuse_lifecycle.py::test_ensure_workspace_agent_explicit_remote_target_creates_remote`
was independently reproduced on pristine `683e1f5`: the fixture returns a
plain `object` instead of a profile with the `interactive` field. It is
unrelated to this stream fix. A second failure,
`test_task_mailbox_report_intake.py::test_precommit_save_failure_rolls_back_mailbox_and_same_call_retry`,
was likewise reproduced on pristine `683e1f5`: its worker cwd fixture is
outside the temporary workspace and fails the existing cwd validation.
The temporary baseline worktrees were removed after verifying clean Git
state and no process using them.

The expanded run was explicitly interrupted with SIGINT after 14m25s in the
unrelated workspace lifecycle wait/retry tests. Its result was **1,762 passed,
2 failed (both baseline failures above), 2 skipped, 1 rerun**; it did not
finish all 1,913 collected tests. The additional headless regression was run
separately after that collection and passed. Full-suite/CI success is not
claimed. Logs are retained locally as `/tmp/hub-interruption-*.log`.

Tests use isolated runtime/state directories. These are deterministic
protocol and backend integration checks, not a new hours-long real-model
run. Existing corrupt/missing historical Hub events are intentionally not
rewritten by this fix. Deployment is a separate shared-service operation.
