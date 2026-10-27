# Provider process group teardown: group-drain escalation and cancellation kill

Follow-up to b490fbb ("fix(maso): terminate provider child processes as a
process group"). An independent adversarial reviewer reproduced two teardown
bugs empirically against `ProcessSession._terminate_subprocess` in
`backend/claude_hub/services/agent_stream/providers/process.py`.

## Problem

Provider CLIs (Claude/Cursor/Codex) are spawned with `start_new_session=True`,
so the direct child leads its own process group and teardown signals the tree
with `killpg`. The escalation logic still had two holes.

### M1 — escalation keyed off the direct child, not the group

The old code did `await asyncio.wait_for(proc.wait(), timeout=5.0)` and only
SIGKILLed when that timed out. Provider CLIs normally die immediately on group
SIGTERM, so `wait()` resolves fast and teardown returned while
TERM-ignoring/trapping descendants (MCP servers shutting down, trapped bash,
nohup-style children) stayed alive, reparented to the subreaper/init and never
SIGKILLed.

Reproduction (reviewer's scenario):

```
sh -c 'sh -c "trap \"\" TERM; exec sleep 30" & wait'
```

The outer `sh` (group leader) waits on a child that ignores SIGTERM. Group
SIGTERM reaps the leader while the grandchild survives.

A related asyncio subtlety made this strictly worse than "wait resolves
early": `Process.wait()` only resolves after the subprocess transport's
stdin/stdout/stderr pipe protocols all disconnect, and surviving descendants
inherit those pipe fds. Confirmed empirically on this machine: with the
TERM-ignoring grandchild alive, `proc.returncode == -15` is set within ~20 ms
(the ThreadedChildWatcher reaps the leader) but `proc.wait()` stays pending
until the 5 s timeout. Keying escalation off `wait()` therefore both (a)
missed the group when the pipes happened to close promptly and (b) wasted the
full grace window even after the leader was dead without asking whether the
group was gone.

### M2 — cancellation during grace skipped SIGKILL and reap

If the teardown task was cancelled while awaiting grace, `CancelledError`
propagated immediately: no SIGKILL was issued and the direct child was never
reaped. The whole tree (plus a possible zombie) outlived the transport.

## Fix semantics

`_terminate_subprocess` is now a single deadline-driven state machine:

1. Record one deadline `now + 5s` and SIGTERM the group
   (`_signal_process_group`). If group signalling is unavailable (`pgid == 0`,
   non-POSIX, or `PermissionError`), remember fallback mode and signal the
   direct child only.
2. Start one `proc.wait()` task and poll until the deadline every ~0.1 s:
   return early only when the wait task is done AND (`fallback mode` OR
   `killpg(pgid, 0)` raises `ProcessLookupError`, i.e. the group is empty).
   The fast-leader/lazy-group case and the slow-leader case share the same
   budget — a prompt leader exit never restarts the clock.
3. At the deadline with the group still present: `killpg(pgid, SIGKILL)`
   (or `proc.kill()` in fallback), then await the leader reap bounded at 2 s
   and give the group up to 1 s to disappear (`killpg(0)` final check); an
   unrelated joiner cannot hang teardown forever.
4. On `CancelledError` anywhere in that flow: SIGKILL the group / direct child
   immediately, then re-raise.
5. A `finally` block performs the final reap of the direct child with
   `asyncio.wait_for(asyncio.shield(wait_task), timeout=2s)`, swallowing
   `CancelledError`/`TimeoutError`/`OSError`. The shield means a further
   cancellation while reaping cannot abort the best-effort reap of the just
   SIGKILLed child; the original cancellation still propagates.

Minor findings also addressed:

- `_spawn_subprocess` resets `self._process_group = 0` BEFORE awaiting
  `create_subprocess_exec` on every platform (N6), so a teardown racing the
  spawn cannot see the previous pgid and non-POSIX does not depend on a prior
  teardown.
- Docstring caveat (L4): descendants that themselves call `setsid`/`setpgid`
  leave the group and cannot be reached by these `killpg` calls.
- Comment at `_NEW_SESSION_KWARGS` (L3): owning their own session means
  providers no longer die implicitly with the backend's group; clean shutdown
  relies on lifespan teardown awaiting each transport's `stop()`.
- `PermissionError` from `killpg` is treated as "group signalling
  unavailable" (fallback to direct-child terminate/kill);
  `ProcessLookupError` is "group already gone".

## Tests

`backend/tests/test_provider_process_group.py` now exercises real
subprocesses (no mocks of process semantics), 10 tests:

- Existing vacuous assertion fixed: grandchildren are snapshotted by pid
  BEFORE teardown — after the leader is reaped, children are reparented and
  `_child_pids(leader)` returns an empty set, so the old post-teardown
  assertion over that set was vacuously true. TERM-ignoring fixtures wait for
  the inner `exec sleep` to land (`/proc/<pid>/comm == sleep`) so teardown
  cannot race trap installation.
- M1 regression `test_fast_leader_lazy_group_escalates_within_shared_deadline`:
  the reviewer's exact scenario; asserts the grandchild is dead AND fully
  reaped after stop and elapsed is within 4.5–8 s (one shared grace budget).
- M2 regression `test_cancelled_teardown_sigkills_and_reaps_group`: cancel
  teardown after ~0.5 s; asserts `CancelledError` propagates, leader
  `returncode == -9`, and leader and child both reach ESRCH (no zombies).
- Double-teardown idempotency: `_terminate_process` twice and `stop()` twice
  never raise.
- Failure injection: `PermissionError` from `killpg` falls back to direct
  child SIGTERM and, when the child ignores TERM, to direct-child SIGKILL
  (grace monkeypatched to 0.4 s); `ProcessLookupError` is group-gone;
  `PermissionError` on signal 0 counts as alive.
- A `_spawned` async context manager hard-kills the whole group in `finally`,
  including on assertion failure, so no test-spawn process leaks.

Evidence (repeated runs):

- `uv run pytest backend/tests/test_provider_process_group.py -q` —
  10 passed, stable across 3 total runs (~11.1 s each; the two grace tests
  take ~5.04 s each as designed).
- `uv run pytest tests/test_agent_stream.py tests/test_agent_stream_native.py
  tests/test_codex_sessions.py -q` — 384 passed.
- `black --check`, `isort --check`, full-backend `mypy` (582 files): clean.

Pre-fix confirmation on this machine: the old code left the grandchild alive
(`elapsed=5.01s ... grandchild_alive=True`); post-fix
(`elapsed=5.00s ... gc=False`, leader reaped).

## Residual limits

- Descendants that call `setsid(2)`/`setpgid(2)` themselves escape the
  provider's group; neither SIGTERM escalation nor cancellation SIGKILL can
  reach them. Nothing short of walking the process tree can, and this code
  does not attempt that.
- Providers own their sessions, so a hard `SIGKILL` (or crash) of the backend
  skips lifespan teardown and leaves provider groups running; only a graceful
  shutdown (SIGTERM/SIGINT handled by the lifespan) reaps them.
- The post-SIGKILL group drain is only observed for 1 s; a member that somehow
  survives SIGKILL (uninterruptible D-state) is not awaited forever, matching
  the requirement that teardown remain bounded.
