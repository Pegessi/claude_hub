# MASO integration into main

Task `1b8d9414-62bf-4e16-8cd4-ccae2f49759e` is initiator-controlled, with the
configured reviewed policy retained. The user explicitly authorized merging the
accepted MASO HTTP Chat, lifecycle corrections and availability preflight. The
root writer owns `~/claude_hub_worktree/maso-main-integration`, branch
`codex/maso-main-integration`; the independent native reviewer is read-only and
has no Task credentials or reporting authority.

## Fixed integration objects

The primary checkout had an unrelated dirty `start.sh`, local main nine commits
ahead and four behind origin. Fetching preserved that state. All integration,
conflict resolution and validation took place in the isolated worktree; no
primary checkout edits, stash, reset or rebase were used.

- `607dda54c6462a3fbf5cda1418e55fd94e30b09f` reconciles local main
  `86f75f72bd1c0a5d01855bb3214cd76cc8455e97` with fetched origin
  `30d2e33d042480b7202018156d9d0f9f0c2816b7`. The only native conflict was
  `CHANGELOG.md`; both sets of Unreleased entries are retained.
- `152934bfd5b0fe15a99ca195f17e867f798e1167` merges accepted MASO candidate
  `30869b12d5e782741a06c3e952da4e978a9553c5` into that reconciliation. Its only
  native conflict was `frontend/tests/browser_chat_composer.py`. The combined
  fixture retains MASO condensation/model/preflight/retry/mobile checks and
  origin directory selection, workspace context, lazy Codex picker and
  unowned-warning checks. The warning keeps explicit Codex identity while the
  active Chat fixture is MASO; both parents' route and state expectations run.

The two adjacent merge-decision JSON records were generated from these full
immutable SHAs. Only their reason/disposition/destinations fields were filled.
Both records independently passed the native reconstruction check. They bind
the merge trees; this later documentation commit is outside those trees.

The independent reviewer approved both fixed merges, examined auto-merged
TabBar and backend/provider call paths, and independently passed 26 related
Node tests. AST comparison retained every parent browser assertion: 73 and 78
parent assertions contribute 108 combined assertions, with none missing.
AGENTS.md and CLAUDE.md remain byte-identical.

## Verification

All standard targets passed on the fixed MASO merge using the declared tool
versions: Python 3.11.16, Node 24.21.0, pnpm 9.15.9, uv 0.12.15 and Playwright
1.58.0. `./scripts/verify.sh all` passed format, complete backend types, lint,
frontend types/build, documents and checkout safety, with 3285 backend tests,
seven existing skips and 781 frontend tests. Backend evidence is retained at
`/tmp/claude-hub-verify.ZkOaHs`; the full log is
`.agent_office/maso-main-integration/verify-all.log`.

All nine browser-mocks runners passed on dedicated Vite port 5301, including
the combined composer fixture. Evidence is
`/tmp/claude-hub-browser-mocks.vL2HJ8/run/summary.json`; that review server was
stopped. The focused frontend type/Node checks also passed.

The explicit terminal-hmr target initially failed its third case's transient
content-pending observation while the standard suite ran concurrently: two
passed, one failed, and the remaining cases were not run. Failed evidence is
retained at `/tmp/claude-hub-verify.xkNW0i`. The HMR test, harness and
TerminalView are unchanged from the original local main. Without any code or
assertion changes, a separate serial run passed all twelve cases in 93.80s;
evidence is `/tmp/claude-hub-verify.jblAps`. The first failure's precise timing
cause is not established; the passing retry does not erase that evidence.
Private backend/Vite/tmux resources are fixture-owned and cleaned on exit.

The merge records, this log and explicit staged paths receive document,
check-staged and diff checks before committing. The standard suite and browser
results cover the unchanged production/test source tree of the fixed merge;
the following commit adds only review records and this log.

## Release boundaries

Fetch is repeated before release, and main is advanced only by fast-forward to
the reviewed descendant. A normal push must confirm the published main SHA;
the Task is completed only after that confirmation and final state checks.

The primary `start.sh` must retain SHA-256
`c48d836cb583388ef10c18f6e9b10fce80b31145420365ccb4b45e24cba7f35e`.
Neither merge changes its tracked content. Primary Hub 8173/5173 and shared
MASO 9796 are not restarted; primary health instance remains
`37b9c814-dc3d-4be8-b2ed-f16a59f0e241`. A Git release alone does not load the
new provider into the running primary service.

The separately requested user experience service on 5297 remains available,
with instance `maso-preflight-preview-3e6a22e9fc2d4163b497c4d993ed8feb`. It uses
the accepted preflight candidate, its own Hub home/state and tmux socket, and
is retained as the earlier user-requested service. It is not the integration
review server. Its checkout and private state are preserved. No MASO inference
or session creation was needed for integration validation; preflight establishes
service response and advertised configuration, not upstream quota or inference.
