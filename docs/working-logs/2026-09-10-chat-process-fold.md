# Chat process folding

Date: 2026-09-10

## System overview

A Chat turn renders as an ordered list of `TimelinePart`s: thinking segments,
tool groups, assistant text, approval cards, errors, and status notices. Before
this change every part rendered inline for the life of the conversation, so a
finished turn stayed as long as the work it took — scrolling back through a
session meant re-reading every step.

Two problems were reported together:

1. A long thinking or tool card could only be collapsed from the `<summary>` at
   its top. Once expanded past the viewport, closing it meant scrolling back up.
2. Finished turns kept their intermediate history on screen. The reference
   behaviour is Codex's: when a task completes, the working process collapses to
   one line showing how long it took, and clicking reveals the detail.

## Module design

### Split, don't tag

`splitTurnProcess(turn)` divides a completed turn's `parts` at its **last**
`text` part: everything before is the working process, the last text part and
anything after it is the delivery. The last text part — not the first — is the
answer, because everything the model says before it ("我先看一下…") is working
narration.

Tagging the *final* answer at reduce time was the obvious alternative and was
rejected: the reducer streams, so "the last text so far" is not knowable until
the turn ends, and a tag would have to be rewritten on every delta.

The split returns `null` — meaning "render untouched" — for a running turn, a
turn with no assistant text, and work continuing past the last text.

An error inside the region does **not** stop the fold either; it is reported as
`pinned` and rendered beside the header.

An approval card folds with everything else. Two live reports shaped this. The
first version refused to fold any turn holding a card, to avoid re-ordering
parts around a fold — and a session where three turns held an AskUserQuestion
card rendered 114, 81 and 19 tool cards with no way to collapse them. The second
version pinned *unanswered* cards, which sounds safer and is worse: these cards
are answered by replying in the next message, not by clicking, so
`approval_resolved` is never emitted and `resolved` stays false forever. Pinning
the unanswered card therefore meant never folding the turn at all — the original
bug, wearing a rule. Nothing live is hidden by folding them: a card that could
still be pending belongs to the newest turn, and the newest completed turn is
never folded.

The "work continuing past the last text" guard came out of a live report and is
worth spelling out, because the shape it rejects looks like an answer at a
glance. A turn cancelled mid-tool ends like this:

```
thinking_delta  '...a reasonable timeout.'
tool_call_started  Bash            <- still running when the user hit Stop
turn_completed  {"status": "cancelled"}
```

"The last text and everything after it" is only a delivery when the turn stopped
there. Here the last text is narration and four tool cards plus three thinking
segments follow it, so the folded view rendered the header *and* the process —
a header claiming to have hidden the very thing sitting under it. The delivery
is now required to be terminal: nothing that counts as work may follow it.
(Status and error parts are not work, so a turn whose answer is followed by a
status notice still folds.)

### State and the memo trap

`Process` fold state is per-viewer UI state, so it lives in `StructuredPane`
(`processExpandedOverrides`, a `Map<turnKey, boolean>`) rather than on the turn
the shared reducer produces. The default is positional: **the newest completed
turn stays open**, older completed turns fold.

This is the third time this file has hit the same trap, so it is worth restating:
`.structured-turn` carries `v-memo="[turn.renderRevision, …]"`, and any
render-relevant state that is *not* on the turn object does not change the
revision — so a click updates reactive state and `v-memo` skips the re-render.
The fix is the same pattern as `turnApprovalSignature`: `turnFoldSignature(turn)`
folds the fold decision and label into the deps array, and returns `''` for
turns with nothing foldable so the active turn stays on the cheap path.

### Rendering

`foldTurnParts(turn, expanded)` returns the parts list the template iterates:

- folded — `[{kind:'process', expanded:false}, ...delivery]`
- expanded — `[{kind:'process', expanded:true}, ...process, ...delivery]`
- not foldable — `turn.parts` by identity, so nothing changes for the streaming
  turn or the newest completed one

`process` is a synthetic part kind; the reducer never emits it. The header leads
in both states and keeps the key `process-{turn.key}` in both, so Vue reuses the
same DOM node: expanding inserts siblings *below* it, the control does not move,
and the pointer is still on it for the click that closes it again.

An earlier revision replaced the header with the detail and put the collapse
control in a footer under it. That was wrong twice over: the button vanished out
from under the click that opened it (so the page jumped), and the only way back
was to scroll to the bottom of whatever had just been revealed — worst exactly
when the process was long. The header-as-toggle has neither problem, which is
why the footer kind was removed rather than kept alongside it.

### Problem 1: reachable collapse

Thinking and tool cards keep their uncontrolled `<details>` — the browser owning
that state is precisely what lets `v-memo` skip historical turns without losing
an expanded card. So the footer button reaches for the element instead of
introducing per-card Vue state:

```ts
target.closest('details')?.removeAttribute('open')
```

All three collapsible regions — the thinking card, the tool card, and an open
process header — also pin their header with `position: sticky`, which required
one CSS change (below). Their controls therefore stay reachable at any scroll
depth, and the per-card footers are the escape hatch for someone who has already
scrolled to the bottom of that card.

## Key issues / pitfalls

- **`overflow: hidden` silently disables `position: sticky`.** An ancestor with
  `overflow: hidden` becomes the sticky element's scrollport, and a box that
  never scrolls never sticks. `.tool-card` used `hidden` to clip child
  backgrounds to its rounded corners; it now uses `overflow: clip`, which clips
  without establishing a scroll container.
- **Timestamps are not always elapsed time.** `created_at` is the normalize-time
  wall clock. A turn streamed live carries real spans; a turn replayed from a
  provider transcript has every event stamped with the import time, so the span
  collapses to ~0. `turnElapsedMs` floors at one second and returns `null`
  below it, rather than labelling a long turn "0s".
- **The frontend unit tests have no module resolver.** They transpile a source
  file and import it from a `data:` URL, so `@/…` imports have to be stripped
  and the dependency's transpiled source concatenated ahead of it. Adding a new
  sibling import to `agentStreamTimeline.ts` means updating both
  `agentStreamTimeline.test.mjs` and `agentStreamTimelineReducer.test.mjs`.
- **`formatElapsedDuration` floors to whole units** (`83s → "1m"`). That is
  deliberate and shared with the workspace timeline; the fold label inherits it
  rather than inventing a second, finer format.

## Validation

- `node --test tests/*.test.mjs`: 324 passed (28 of them new);
  `pytest tests/test_agent_stream*.py`: 232 passed (2 of them new).
- `pnpm run lint:check`, `pnpm run build` (vue-tsc + vite): clean.
- Browser check against a worktree dev server on `:5199` pointed at the live
  backend (the live frontend on `:5173` was not touched). On the `ch ds` tab:
  6 folded lines; the first read `过程 · 19 个工具调用 · 50s`, which matches the
  turn's own event log (19 `tool_call_started`, 50.2 s) — the label is derived,
  not decorative. Expanding moved the header by **0.0 px** and left the scroll
  position unchanged, the detail appeared as the header's next sibling, and a
  second click on that same header collapsed it again from the same spot. 53
  `details-collapse` footers were present across the thinking/tool cards. No
  console errors.
- Consistency sweep across three live tabs (Claude × 2, Cursor × 1), 30 turns
  total: every folded turn's tail is the delivery alone — never a thinking or
  tool part — and each unfolded turn is unfolded for a stated reason (cancelled
  mid-tool, holds an approval card, is the newest completed turn, or is still
  running).
- The dev server was stopped and its port confirmed closed before this log was
  written.

## Independent review

An adversarial review (separate agent, the repository's `surgical-code-review`
method) returned "merge once the test is real", and it was right about the test.

The regression written alongside the cut-off-turn fix **did not reach the rule
it was written for**. Its fixture put the last text at index 0, so the older
"nothing to fold" guard (`index <= 0`) returned `null` first. Commenting out the
new rule left the file green — 24/24 — which the reviewer demonstrated by
mutation and this author reproduced. The fixture now places the text after a
tool so the new rule is the only thing that can reject it, and it carries a
sanity assertion on the part order it depends on. Re-verified by mutation: with
the rule commented out that one test fails, and the file is green again when it
is restored.

Worth stating plainly, because it is the kind of mistake that survives review:
**a regression test that passes both with and without the fix is not a test.**
The fix commit was green, the suite was green, and the guard was unprotected
anyway.

Everything else the review attacked held up, and it verified by construction
what the source-text tests only asserted:

- `v-memo`: clicking one turn's header mutated only that turn's subtree (zero
  mutations observed on the other nine); injecting a new completed turn moved
  `latestCompletedTurnKey` and the previously-newest turn **did** grow a folded
  header, so `turnFoldSignature` genuinely carries memo invalidation.
- `position: sticky`: measured across a 6,400 px scroll, the open header pins at
  the timeline's 34 px padding and holds (`stickDelta == step`). Re-running the
  same trace with an `overflow: hidden` ancestor gives `stickDelta = 0`, which
  confirms the `hidden → clip` change was load-bearing rather than cosmetic.
- `overflow: clip`: pixel-counted against `hidden` on the rounded corners and
  straight edges (both 0 leaked pixels, `visible` leaked 418/608), and the inner
  `pre` still scrolls.
- Split rules: no new hole found across interleaved text/tool, `status` at the
  end, orphan completions, and approvals landing in the delivery region.

Two things were left deliberately unchanged: `turnFoldSignature` costs 0.0155 ms
per parent render on a 397-part session (about 0.1% of a frame, not worth a
cache that would have to be invalidated), and the cut-off rule is scoped to
"work continues past the last text" rather than "the turn was cancelled" — a
turn cancelled *after* its answer still folds, which is safe because its text
and any error stay visible.

It also flagged the provider coverage gap handled in the next section.

## Provider coverage

The review could only exercise folding against live Claude sessions and pointed
at two unverified shapes. Both are now closed.

**Cursor needed nothing.** Two live Cursor chats fold correctly with zero
leaked process parts, and their durations are real elapsed time (17m, 5m, 1h 2m)
rather than the collapsed spans a replayed transcript would give — the Cursor
tailer streams live, so `created_at` is authored when the event arrives. Its
`turn_ended` statuses (`success` / `error` / `aborted` → `completed` / `failed`
/ `cancelled`) feed the same cut-off rule Claude uses. No adapter-specific code
was required, which is the point of keeping the fold rules structural: they read
`TimelinePart`s, not provider identities.

**Codex had a real hole.** `item/plan/delta` is mapped to `TEXT_DELTA`
(`codex_jsonl.py:231-242`), i.e. the agent's plan renders as an assistant
message — reasonable in itself, and unchanged here. But the fold takes the last
*prose* part as the delivery, so a plan update arriving after the answer would
be taken as the answer and the real answer would be folded away with the
process. That is a silent failure: the reader would see a plan where the answer
should be, with the answer one click away and nothing indicating it was there.

The fix separates classification from rendering. Plan deltas now carry
`payload.plan = true`; the reducer tags the part `fromPlan` and the fold treats
it as work. Two consequences worth naming:

- A plan part and an answer part never merge, even when adjacent. Merging them
  would produce one part that is half work and half answer, which the fold could
  then only classify wrongly. They render as two adjacent bubbles, as they
  effectively did before.
- `assistantText` still accumulates plan text, so the compatibility aggregate
  and everything reading it are unchanged.

Historical Codex events predate the flag, so old sessions keep classifying plan
text as prose and can still mis-fold in the rare plan-last case. Re-deriving the
flag for stored events would mean a migration; the exposure is a few sessions
and the failure is cosmetic, so it was left alone.

Verification is by construction rather than by live session — no Codex chat has
ever run on this machine, which is exactly why the hole survived review:

- `test_codex_adapter_marks_plan_deltas_as_plan` and
  `test_codex_adapter_leaves_answer_deltas_unmarked` pin the adapter's side.
- Four reducer tests cover the shapes: plan-then-answer, answer-then-plan,
  work-then-plan-only, and plan/answer adjacency.
- Mutation-checked, per the lesson above: reverting `deliveryIndex` to
  `part.kind === 'text'` fails exactly the two tests that depend on the flag.

## Follow-up

- Per-tool durations are not shown; the collapsed line reports the turn's total.
  Pairing `tool_call_started`/`tool_call_completed` timestamps would give each
  tool its own elapsed time inside the expanded process.
- The fold decision is positional (newest completed turn stays open) rather than
  sticky-per-turn: a turn that was read and scrolled past re-opens if a newer
  completion is later removed from the list (e.g. history replaced on refresh).
