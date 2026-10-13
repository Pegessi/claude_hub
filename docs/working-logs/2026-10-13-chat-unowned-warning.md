# Unowned TraeX warning left Chat stuck on Queue

On 2026-10-13, a Chat displayed only a horizontal warning line after its last
completed response. Web messages then showed Queue, while Stop had no real turn
to cancel. A later Feishu message opened a new turn and made the visible state
normal again.

The authenticated, paginated event stream shows the prior `turn_completed` at
sequence 58447. Four silent `thread/goal/cleared` notices followed. Sequence
58450 was a text-bearing `status` with `provider_status=warning`, a line of
dashes, and `turn_id=null`; it arrived more than two hours after completion.
The next `turn_started` was the later Feishu message at sequence 58454. There
was no active Hub turn between those two events.

The timeline reducer already ignored silent control notices. It still passed
text-bearing notices to `resolveTurn`, which created an incomplete legacy turn
for an event with no turn ID. The composer used that phantom turn as its latest
turn, so Send became Queue. Stop could send a cancel request, but the backend
had no matching active turn, and the UI had no turn ID for optimistic recovery.
The later Feishu turn became the latest real turn, hiding the phantom lock.

Persistent TraeX/Codex app-server statuses are now transcript rows only when
the tailer attributes them to a turn. An idle status remains in the durable
stream for control-plane consumers but cannot create a Chat turn. Statuses
with real turn IDs still render, and one-shot provider legacy rows are not
changed. The reducer regression uses the observed completed-turn/warning
sequence; the full Chat composer mock loads the same idle warning and checks
that Send is available, Stop is absent and only the completed turn is visible.

Validation on the feature worktree: the new reducer test failed before the
change (`2 !== 1` turns), then the focused reducer/queue suites passed 38/38.
The full frontend unit suite passed 777/777, along with lint, types and build.
The isolated browser-mocks gate passed all nine scenarios, including the Chat
composer scenario, using the declared Node/Python/Playwright versions; its
owned Vite port was closed after the run. With the old reducer temporarily
restored, that same browser gate failed only in the Chat composer scenario:
the new assertion saw two `.structured-turn` elements instead of one. The
fixed reducer was restored afterward. An independent read-only review found
no actionable correctness issue.

The full `verify.sh all` run passed docs, backend types, frontend lint, types,
777 frontend tests and build. Backend format initially found one newly edited
browser fixture and passed after formatting. The repository-wide backend suite
was stopped by an unrelated service-restart test: its build subprocess returned
macOS `Operation not permitted` where the test expected a timeout. The same
failure reproduced in a focused run with the declared uv version; 2,138 backend
cases had passed before the stop. The separate terminal-HMR gate passed eight
tests, then its remote-bootstrap scenario observed two history fetches where it
expected one; the remaining three scenarios did not run. Neither failure
exercises the Chat timeline reducer.
