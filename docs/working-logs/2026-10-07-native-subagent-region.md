# Compact native subagent regions

## Scope and design

TraeX/Codex native subthreads now share one expandable region within each parent
turn. Rows use the complete provider thread ID, not a display name or short ID.
Recognized launch/directive tools join their child row instead of producing a
second standalone card. Expanded rows retain prompts, tool results, and reports.

The parser emits child-scoped observations without completing the parent turn.
Dispatch completion does not establish child completion, and a text-only thread
is not automatically considered complete. Launch/tool failures and child errors
remain independently visible without inventing a lifecycle transition.

The timeline reducer ignores unsupported empty events before allocating a region.
Failed regions remain visible when a historical turn's process is folded. Native
child events arriving without an active parent turn cannot create a replacement
parent turn or leave the Chat busy.

This change does not redesign Workspace Tasks, ChatWork controls, the top-level
Agents list, or MASO sessions. Other workflow changes in the integration worktree
are separate work in progress.

## Validation

All execution below used the canonical feature worktree
`/home/tiger/claude_hub_worktree/agent-workflow-v2`, existing dependencies, and
owned test resources. No Provider, OAuth, or Bot request was issued.

- Parser regression: 43 tests passed. The subsequent combined run passed all
  59 tests (43 parser plus 16 Task tests); evidence:
  `/tmp/claude-hub-v2-task-typed.ljhJ7z/pytest.log`.
- Frontend: 656 unit tests, ESLint, TypeScript, and Vite build passed; evidence:
  `/tmp/claude-hub-v2-native-final.7Cbj3J/`.
- Mocked browser: desktop and 390-pixel mobile viewport passed against that
  build; all API traffic was mocked, WebSockets blocked, and unknown requests
  and page errors were empty. Evidence:
  `/tmp/claude-hub-v2-patch-inbox/browser-run-6/`.
- The browser verifies that a second parent turn makes the first historical,
  its process is collapsed, its failed subagent header is still visible, and
  expansion restores the original prompt/report details. It also checks full-ID
  isolation and horizontal row bounds after the mobile drawer has closed.
- Earlier mobile screenshots were rejected during inspection because responsive
  layout had not settled before the drawer check. The harness now waits for the
  mobile class, closes the drawer if open, and waits for its right edge to leave
  the viewport. Earlier screenshots are not the final mobile acceptance.
- The owned static review listener on port 40543 was stopped and connection
  rejection verified in `browser-run-6/server-cleanup.txt`; the preceding review
  listener on port 44825 was likewise stopped and verified.

The native implementation's final Git blobs received independent incremental
review from Worker 7fc. The late-child, empty-region, and hidden-error findings
were fixed and re-reviewed. Worker 5362 additionally reviewed the historical
folding assertions. Review reports are static evidence, separate from the test
runs above.

## Remaining boundary

No production service was restarted, no change was merged into main, and nothing
was pushed. The Task integration's first complete backend type check found two
separate integration issues (a helper-name collision and a reused local variable);
that failing type check is not claimed as a pass here. Native browser tests use
simulated provider events, not a new live TraeX/Codex execution or a performance
measurement.
