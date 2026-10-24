# Verification runtime cleanup

## Problem and scope

`verify.sh` created private backend tool homes, pytest homes/state/tmp directories
and HMR temporary roots without a common exit owner. Browser mocks only removed
their short temporary root after a fully passing run; profile/cache homes remained
with evidence. Repeated runs therefore accumulated disposable data.

The bounded local inspection recognized 1,202 historical directories using
224,882,688 allocated bytes (about 225 MB). That confirms accumulation, but does
not explain the machine's total disk usage. This repair does not delete those
historical directories or claim that all disk pressure came from them.

Worktree: `fix/verify-sandbox-cleanup`, based on `8991871`. No main-branch merge,
push, shared-service restart or historical cleanup is part of this change.

## Implementation

- `scripts/check_verify_runtime.py` is the stdlib process owner for runtime-producing
  targets. `verify.sh` re-enters under that owner without replacing target logic.
  Each invocation creates its own short `/tmp/ch-v-*` runtime and separate evidence
  directory. Private environment fields identify the immediate managed child;
  partial/stale inherited fields do not bypass allocation.
- Backend tools use `runtime/tools`; pytest uses `runtime/pytest`; HMR keeps its
  short TMPDIR; browser tool/profile/cache homes use `runtime/browser`. Evidence
  paths retain provenance, logs, emitted JUnit/`.coverage`, exit codes and browser
  reports/screenshots. The existing default coverage XML path is unchanged.
- After the target exits, the owner stops only its new process group, checks the
  runtime directory identity and visible Linux references, then removes the owned
  runtime. Changed paths and in-use runtimes are retained with a reason. Nested
  symlinks and report-file symlinks must not modify outside sentinels.
- `CLAUDE_HUB_VERIFY_KEEP_RUNTIME=1` explicitly keeps raw runtime state. There is no
  historical sweep, new daemon, dependency, retention registry or scheduled job.
- INT/TERM first allow up to 60 seconds for cooperative target cleanup. Browser
  owners receive the original signal; pytest shell pipelines receive group SIGINT
  so pytest finalizers run. `tee -i` keeps the log reader alive during that work.
  Remaining group members then receive bounded TERM/KILL cleanup. The outer exit
  preserves the original interrupt status (130/143).
- Cleanup and final report failures preserve an existing nonzero target result;
  logging failures are recorded separately from pytest status. A late signal
  during report publication cannot silently return success.

The 60-second grace was added after independent review found that a three-second
outer group kill could interrupt the browser owner's two sequential cleanup
budgets before its summary was written. Additional tests cover a 3.6-second
nested finalizer and the actual Bash → tee → fake pytest signal path.

## Verification

Final results on the reviewed owner blob
`9921ac9f34ae43dd2df5a5a53d563ee89d2d6a43`:

| Check | Result |
| --- | --- |
| `./scripts/verify.sh all` | All targets passed; backend 5,244 passed / 10 skipped in 325.74s; frontend 1,135 passed |
| Backend format/types | 587 format inputs unchanged; 572 source files passed mypy |
| Runtime-owner selection | 30 passed, including closed stdout/stderr readers preserving target status 7 |
| Actual Bash/tee/pytest interruption and reporting errors | 6 passed; finalizer output survives INT and TERM |
| Browser mocks | 15/15 passed; summary retained and runtime removed |
| `terminal-hmr` | 12 passed in 98.63s; JUnit/logs retained and runtime removed |
| Real browser interruption | TERM returned 143 after one runner started; summary says INTERRUPTED, 14 runners not started, runtime removed |
| Independent review | Closed with no remaining actionable finding |

All six changed code/test files matched their pre-run SHA-256 hashes after the
final full gate. Normal browser scenarios ran before the final diagnostic-output-only
hardening; real browser interruption, HMR and the final full gate ran after it.
The final owner also redirects a failed console descriptor to `/dev/null`, so
interpreter shutdown cannot replace the target status with a flush error.

Local evidence is retained under
`/home/tiger/claude_hub_workspace/verify-sandbox-cleanup`: `verify-all-delivery.log`,
`final-code.sha256`, `browser-mocks-final.log`, `terminal-hmr-final.log`,
`browser-interrupt/result.json`, and the separate `checks/` evidence directories.
Runtime tests used real bounded child processes and 2 MiB disposable samples;
explicitly kept samples and escaped test children were removed by their own test
teardown. No historical runtime was used as a cleanup test.

Earlier iteration evidence is preserved:

- Initial entrypoint regression: 76 passed; intermediate combined selection:
  134 passed.
- One full run reached 5,235 backend passes and 10 skips, then failed its shell
  target because `verify.sh` was edited while Bash was still reading it. That run
  was invalidated. Its runtime was reclaimed and failure log retained. The frozen
  candidate subsequently passed all gates, and the final post-hardening run above
  passed again with unchanged code hashes.

## Limits

- SIGKILL, host loss or inaccessible process state cannot guarantee cleanup or
  complete reporting. The owner does not sweep remnants on a later invocation.
- Linux inspects same-user visible environment/cwd/open-file references, including
  escaped sessions. Other POSIX systems confirm only the owned process group;
  escaped-session protection is not claimed there. This is not isolation from
  malicious same-user code.
- Tests still own their fixture lifecycle. The outer owner does not authorize
  deleting caller paths, killing shared tmux/services or stopping unrelated
  processes. It retains unsafe runtimes rather than guessing ownership.
- Retained logs/reports have no automatic age/size policy. Explicit keep mode also
  intentionally retains data. A future historical cleanup requires a separately
  bounded ownership check and authorization.
