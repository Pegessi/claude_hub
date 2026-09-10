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
turn with no assistant text, and a process region containing an approval card or
an error. Folding an approval away would hide a control the user still has to
click; folding an error away would hide why the turn failed. Refusing to fold
the whole turn is easier to reason about than re-ordering parts around a fold.

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

- `node --test tests/*.test.mjs`: 318 passed (22 of them new).
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
- The dev server was stopped and its port confirmed closed before this log was
  written.

## Follow-up

- Per-tool durations are not shown; the collapsed line reports the turn's total.
  Pairing `tool_call_started`/`tool_call_completed` timestamps would give each
  tool its own elapsed time inside the expanded process.
- The fold decision is positional (newest completed turn stays open) rather than
  sticky-per-turn: a turn that was read and scrolled past re-opens if a newer
  completion is later removed from the list (e.g. history replaced on refresh).
