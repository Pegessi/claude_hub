# Goal signal recovery and explicit completion

## Evidence and root cause

The incident was reconstructed from a complete, read-only stream page (3,813
events, no further page), the Goal-by-ID API, and the controller at the task's
baseline. Private evidence is retained locally, not copied into this repository.

- The first recorded Goal turn ended after approximately 29 minutes with a
  runtime-interrupted error and **cancelled**, with no assistant answer.
- The next turn's metadata explicitly identified restart recovery of that turn.
  It ended after 2 hours 19 minutes with provider **completed**, a substantive
  delivery/validation summary and questions for the next user decision.
- The controller counts both terminal callbacks in `turns_completed`. That
  counter does **not** mean two successful turns, nor two protocol repair tries.
- On successful provider completion, the old controller immediately marked the
  Goal `failed` when its anchored status regex did not match. Checkpoint parsing
  and Strict review were after that return; the recorded Strict round count was
  zero. There was no missing-signal recovery turn. Restart recovery is a different
  mechanism and cannot repair this terminal protocol failure.
- The record was already cleared before this investigation; current returned
  null, while lookup by ID retained the omission diagnostic. No incident tab,
  Goal, provider session or shared service was modified by this task.

Public stream events intentionally omit private protocol blocks. They prove
the visible delivery and provider completion, but cannot distinguish an entirely
missing tag from malformed private trailing bytes. Model forgetting after a long
context is plausible, not established. Annotations after a signal and fenced
wrappers were independently reproduced as parser risks, not attributed to the
incident without raw evidence. The implementation does not judge whether that
other project's delivery claims were correct.

## Contract

`goal_signals.py` owns pure signal recognition with bounded protocol output for the controller and
one-shot transport watchdog. A plain terminal control block or a balanced,
protocol-only Markdown fence is reserved control input on a dispatched Goal turn.
Inline/code-span references, blockquotes, code examples containing prose,
unclosed fences and afterthoughts are not accepted. Formatting tolerance never
classifies arbitrary text as success. Ordinary Chat display sanitization retains
its existing scope; provider-native Goals do not use the recovery state machine.

For a successful provider turn with nonempty text but no usable signal:

1. Persist the original bounded response tail and charge one repair attempt
   **before** dispatch. The repair is instructed to use read-only verification,
   not repeat implementation or launch new work. This is prompt discipline,
   not a new tool sandbox.
2. Require a fresh explicit status. `complete` still invokes Strict review when
   enabled, with both original delivery and repair evidence. `needs_input`
   parks in the existing `blocked` state; `continue` resumes ordinary work.
3. A second missing signal parks for human input; it does not loop or mark work
   complete. Empty results, errors and cancellations retain their distinct
   failure/pause paths. Uncertain repair dispatch and restart retain the budget.
4. Original evidence survives persistence but is excluded from public Goal JSON.
   A valid signal ends the episode. New user steering/objective edits clear it.

This intentionally does not accept arbitrary text after a signal: that text
could contradict the purported completion. Repair asks the model to reconcile
it. If recovery itself fails, the user must inspect and decide.

The existing manual **Complete** action is a human override, including for
Strict Goals; it has never meant independent review passed. Historical
Hub-managed `failed` records with the exact legacy protocol diagnostic can now
use it only when idle, without active/review/dispatch identities, and only when
still the current Goal. Real execution failures, provider ownership, cleared
Goals and superseded records are not reopened. Terminal classification,
Resume/Edit eligibility, authentication and request deduplication remain intact.
The resulting message explicitly records manual completion, not Strict PASS.

## Verification and boundaries

- Sanitized interrupted-turn → restarted delivery → repair fixtures exercise
  the controller and actual stream completion observer, persistence/replay,
  Strict PASS/BLOCK, needs-input/continue, empty/error/cancelled results and
  uncertain transport handling.
- A replay during Strict review is stopped before side effects, so duplicate
  repair/original completions cannot launch another reviewer with stale evidence.
- Parser/watchdog tests distinguish wrappers from quotations and afterthoughts.
- API tests cover explicit legacy completion, identity/replay and negative
  guards. The mobile Goal browser scenario checks desktop and phone rendering,
  keyboard activation, conflict responses, disabled in-flight controls, reload
  persistence and ineligible states against mocked APIs.
- The browser gate was observed failing with the failed-state eligibility
  disabled, then passing with the fix. Its dedicated Vite server was stopped.
- Final full verification, checkout/privacy checks and candidate identity are
  recorded in the managed Task report. Browser mock results do not prove a live
  provider repair run; none was sent to the incident session. The shared Hub is
  unchanged until a separately authorized deployment.

## Lexical-boundary follow-up

Independent review found that cropping before lexing and selecting the last
status opener could promote quoted or nested text into a completion signal.
The follow-up preserves the full source for Markdown container/fence recognition
using the existing CommonMark dependency, then scans root Goal blocks with
balanced tag context. Four-space/tab code, blockquotes (including lazy
continuations), unclosed fences and nested Goal tags are not control input.
Only whitespace-contiguous trailing validated blocks enter the bounded protocol text consumed by the
checkpoint/plan parsers; no output budget truncates an opener into validity.
Paragraphs mixing literal protocol code spans and controls are conservatively
ambiguous; a distinct paragraph or protocol-only fence remains unambiguous.

The real one-shot watchdog passes the full accumulated text and clears its
completion latch on every non-signal, including an afterthought that pushes the
old signal outside the tail window. Multi-stage provider guards are unchanged.
Legal trailing whitespace, closed protocol-only wrappers, adjacent root blocks,
longer fence closers and CRLF input retain compatibility.

`test_goal_signal_boundaries.py` exercises the four reported bypasses and window
edges through the parser, non-Strict/Strict controller, and SessionTailer. It
also checks repair exhaustion rather than completion, no premature Strict
review or synthetic watchdog completion, and quoted checkpoint/plan exclusion.
The inherited candidate failed 55 of the initial 79 boundary cases; focused
iterations and full candidate gates are recorded in the continuation Task and
local `tasks/goal-status-evidence/boundary-p1/` evidence. The original incident
reconstruction and manual completion/browser evidence above are unchanged.

The next independent review found three interactions missed by the isolated
cases: CommonMark Type-7 HTML blocks could swallow a following fence/quote after
a standalone Goal tag; dropping a non-root outer opener could promote an inner
status; and concatenating earlier blocks across prose could import stale
checkpoint/plan examples. Goal-tag starts now bypass only the HTML-block rule,
leaving actual HTML comments/raw blocks excluded and Markdown container rules
active. Even an invalid inline-prefixed opener retains nesting until closed.
Metadata extraction walks backward only through whitespace-contiguous blocks
ending at the terminal status. A previously permissive test for an unclosed
inline outer opener now requires repair/input rather than treating its inner
status as authoritative.

These review reproductions first produced 70 failures / 118 passes on the prior
candidate. The extended matrix also validates selective metadata exclusion,
new live metadata after decoys, quoted/indented protocol-only fences, and a real
root envelope after a closed long example. A read-only helper proposed these
interaction cases without implementation context; the worker independently
adapted the fixtures to the actual metadata schema and executed them. Final
validation and the retained red/green evidence are in the Task report and
`tasks/goal-status-evidence/boundary-p1/round3/`.

Privacy acceptance is limited to the full baseline-to-candidate incremental
changed blobs, patch and commit metadata. Inherited history and unchanged
baseline files are explicitly outside this task; neither is rewritten.
