# Verification runtime cross-validation

## Decision and scope

The user requested a fresh cross-validation of `303672d`, followed by local-main
integration only if no issues remained. Main was clean at `8991871`, the original
candidate's base. Two new reviewers independently exercised filesystem/process
safety and verify/pytest/browser/CI contracts. They did not inherit the original
approval as an acceptance premise.

The original candidate was rejected. Its passing tests and prior approval remain
historical evidence, not evidence that the following cases were safe.

## Confirmed findings and repairs

| Finding | Repair |
| --- | --- |
| Root pathname swapped after its inode check redirected recursive deletion into another directory | Pin the root descriptor from creation, derive identity with `fstat`, traverse children only relative to that descriptor, and recheck its pathname before the final empty-root removal; holding the original inode also prevents identity reuse |
| Evidence parent replaced by a symlink let final reports overwrite unrelated history | Keep the original evidence directory descriptor open; create temporary report files and atomically replace names using `dir_fd`; make changed-path success nonzero |
| Escaped descendant with `PR_SET_DUMPABLE=0` denied environment inspection and survived deletion of its runtime | Enable Linux child-subreaper adoption before spawning, inspect actual owned/adopted child membership, and retain runtime while any remains alive, even in keep mode |
| Numeric process-group identity could be reused after reaping the leader | Observe the leader with `waitid(WNOWAIT)`, reset inherited SIGCHLD-ignore behavior, and perform the single final group stop before bounded reaping; never signal that group afterward |
| HMR backend/Vite logs were still inside reclaimed pytest tmp_path | Write them into retained `terminal-hmr/<case>/` evidence with unique names per launch; direct pytest without an owner keeps its existing fallback |
| CI hard-killed the owner at 60 seconds, just when its cooperative grace ended | Set browser/HMR `--kill-after=90s`: 60s cooperative shutdown, up to 9s group termination/reaping, and 21s reporting/scheduling margin |
| Subreaper restoration ran after final publication, losing restore failures or late TERM status | Restore before the last status reconciliation; retain the evidence descriptor until corrected receipts are published |
| The first repaired full run exposed dead adopted children keeping a nested build process group present | Reap only dead adoptees during normal execution and cooperative waits, always excluding the main leader anchor; the existing service-restart timeout test and new nested-group tests pass |

Bash also pins pytest/JUnit log files before running tests, and publishes status
receipts through the inherited evidence directory descriptor. A test replacing
its evidence pathname cannot redirect those later shell writes. No new daemon,
external dependency, history sweep or shared-service operation was introduced.

## Independent counterexamples

All three original safety counterexamples passed after repair:

- A swap during `/proc` scanning retains both the original and replacement data.
  A swap after traversal begins can affect only the pinned disposable tree; the
  replacement's nested sentinel remains intact.
- A real child moves the evidence directory aside and substitutes a link to
  existing history. History remains byte-identical; final reports stay in the
  original directory. Target success becomes 1; an existing failure stays 7.
- A real detached, non-dumpable child remains observable as an adopted child.
  Runtime is retained while it lives, with both keep settings; no unrelated
  protected process is signalled.

Additional independent probes verified the zombie PID anchor, inherited
SIGCHLD-ignore behavior, status/error publication after restoring adoption, real
uv/pytest finalizers lasting over four seconds, and the exact 90-second CI kill
window. The timeout probe completed in 61.70s with GNU timeout status 124, owner
status 143, final receipts, no remaining child and removed runtime.

The `all` target's Ctrl-C aggregation behavior was also compared with the base;
its continuation through later targets is pre-existing and was not changed here.

## Validation record

Focused results before the final broad gate:

- Runtime ownership: 45 passed, including all non-dumpable-child, restoration,
  nested-group reaping and runtime-inode lifetime cases, with no skips.
  Final reviewed owner blob: `b3281312b96c89dfafbd07aae1b2173bdc3920d2`.
- Verify entrypoint and HMR evidence contracts: 103 passed.
- New HMR evidence tests: 10 passed independently under process/network mocks.
- Safety and contract reviewers closed all their reported actionable findings.

Final frozen-candidate gates all passed:

| Gate | Result |
| --- | --- |
| `verify.sh all` | All targets passed; backend 5,271 passed / 10 skipped in 348.62s, frontend 1,135 passed |
| Browser mocks | 15/15 passed on the final owner |
| Terminal HMR | 12 passed in 99.10s on the final owner |
| Post-run evidence | Both real runtimes removed, no owned children remained; 13 backend and 13 Vite logs retained after HMR cleanup |
| Source identity | All eight code/test/CI files matched their pre-run SHA-256 values |

The earlier repaired full run stopped at 3,971 passes because dead adopted
children held a nested build group present. That run is not a passing gate; the
periodic reaper repair and new normal/interrupted nested-group regressions closed
it before the complete rerun above. Runtime inode reuse was not forced or
reproduced; the new test instead proves that the original unlinked inode stays
open in the owner while a replacement pathname is preserved.

The user authorized local-main integration after these checks; the merge remains
a guarded fast-forward from `8991871` to this repaired branch, with no push or
shared-service restart. The exact resulting commit is recorded in the final
external delivery receipt.

Raw bounded counterexamples and repaired results are retained under
`/home/tiger/claude_hub_workspace/verify-sandbox-cleanup/cross-validation/review-evidence/`.
The final gate logs, source digest and live evidence receipt are in the parent
`cross-validation/` directory. No historical directory was used as a destructive
test fixture.

## Remaining limits

The recursive-delete guarantee is tied to the pinned directory descriptor. POSIX
has no inode-conditional `rmdir`: after the last identity check, a concurrent
replacement by another empty directory could still remove that empty name, but
cannot cause recursive traversal of its contents. This is not a security boundary
against arbitrary same-user code.

Non-Linux escaped-descendant detection was not validated and is not claimed.
SIGKILL, host loss and unbounded filesystem delays can still prevent complete
cleanup/reporting. Logs and deliberately retained runtime have no automatic
expiry policy. Historical directories and shared services remain untouched.
