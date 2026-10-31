# Native clean-EOF fix: main integration

## Scope and immutable candidate

The user authorized integrating the local native clean-EOF fix together with
the latest remote main. Work was performed in the isolated
`codex/native-eof-main-20261031` worktree; the primary Hub was not restarted.

- Merge: `b7b9e23623daca2118725d9bad6449cc5680bbfa`.
- Local parent: `0c18ffa71c09e5156b88ec6c8a81c548d1802c99`.
- Remote-main parent: `d337781859bb05278857a35ad5e8223af72bc4ca`.
- Merge base: `15f31c0dde3fb516033bcc85e6c420536e9b030a`.
- Native conflict: `CHANGELOG.md`; both parents' release entries were retained.
- The adjacent merge-decision JSON binds this exact merge, including the two
  additional files changed in response to independent review. Its structural
  check is not itself semantic approval.

The original fix synthesizes completion for an ordinary one-shot native turn
that exits cleanly with a complete answer but without a terminal result. Empty
answers, nonzero exits, provider-owned completion, Goals, active tools, approval
and capacity waits must not become successful completions.

## Independent review and correction

A read-only native reviewer found a P2 issue: commentary before a tool call,
followed by a completed tool and clean EOF, could incorrectly count as the final
answer even when the provider never answered after the tool. The synthesis guard
now requires nonblank final assistant text after the last tool.

The regression feeds real Claude assistant/tool-result records and covers absent,
whitespace-only and valid post-tool answers. It checks terminal state,
`final_text`, completion subscriptions and turn cleanup. The absent-answer case
failed before the guard; all three cases pass afterwards. Independent re-review
of the resulting call paths found no further actionable findings.

The ship skill's repository-local checklist was absent. Review continued using
the installed review checklist and the repository's fixed-merge workflow; this
is not a claim that every step of the ship skill ran.

## Validation

Validation used task-owned dependencies, runtime homes and tmux socket overrides;
it did not target the primary Hub's runtime state. Local logs are under
`/tmp/claude-hub-native-eof-checks.Msvxvb` and are not durable CI artifacts.

| Check | Result |
| --- | --- |
| `./scripts/verify.sh all` | Exit 1: the three backend targets stop before running because macOS Python lacks the required `waitid/WNOWAIT` runtime-ownership support. |
| `all`: docs and checkout-safety | Passed. |
| `all`: frontend lint, types, unit tests, build | Passed; 1152 unit tests, zero failures. Vite reports large-chunk warnings. |
| Direct isolated Black and isort, `claude_hub tests` | Passed; Black reports 607 files unchanged. |
| Direct isolated mypy, `claude_hub tests` | Passed, 607 source files. |
| Final stream/native/retirement/Feishu bridge suite | 418 passed in 9.64 seconds. |
| Focused clean/nonzero-EOF cases | 11 passed. |
| Default backend suite, direct fallback with `-x` | 3547 passed, one skipped, then one failure; remaining tests were not run. |
| Pristine remote-parent reproduction | Both parameters of the same handoff test fail with the same one-second timeout on unmodified `d3377818`. |
| Fixed-merge decision validation | Passed: three decisions, complete coverage of immutable merge `b7b9e236...`. |
| Staged changes / whitespace | `check-staged` and `git diff --check` passed for the code merge. Documentation follow-up is checked separately. |

The default-suite failure is
`test_native_goal_transport.py::test_stop_claims_start_at_wait_for_consumer_handoff[confirmed]`,
waiting for `read_task_created` at line 836. The pristine remote parent also fails
the `[unconfirmed]` parameter. This demonstrates a pre-existing failure in this
environment, not a green full-suite result or proof of its underlying cause.

Reproduction commands (from the integration checkout, using the task's isolated
environment wrapper):

```bash
./scripts/verify.sh all
bash /tmp/claude-hub-native-eof-checks.Msvxvb/run-backend.sh -m pytest \
  tests/test_agent_stream.py tests/test_agent_stream_native.py \
  tests/test_agent_stream_retirement.py tests/test_feishu_protocol_client_bridge.py \
  -q --tb=short
bash /tmp/claude-hub-native-eof-checks.Msvxvb/run-backend.sh -m pytest \
  -x -q --tb=short --ignore=tests/test_terminal_replay.py \
  --ignore=tests/test_terminal_input_latency_perf.py \
  --ignore=tests/test_terminal_hmr_recovery_e2e.py
./scripts/verify.sh merge-decisions \
  --merge b7b9e23623daca2118725d9bad6449cc5680bbfa \
  --record docs/working-logs/2026-10-31-native-eof-main-merge-decisions.json
```

## Limits

The available toolchain is noncanonical: Python 3.12.12, Node 24.13.0 and pnpm
10.28.2 for the final verification frontend targets, rather than the declared
versions. Dependencies were installed with pnpm 9.15.9 and the frozen lockfile;
no dependency manifest or lockfile was changed. These local results are not
CI-equivalent. Live-provider, browser, performance and terminal E2E checks were
not run. No primary-service restart, live-state migration or remote CI success
is implied by this integration.
