# Tool-card presentation and rendering cost

## Scope

Frontend-only follow-up to the task-local Thinking candidate `b3c60b0`.
No provider protocol, retained event data, backend, shared service, or dependency
was changed. The source remains a candidate in the current workspace; there is
no merge or deployment in this task.

## Changes

- Tool summaries have consistent spacing, a visible expansion chevron, and
  readable static Running/Completed/Failed/Cancelled marks. Tool status never
  starts an animation, even for long-running calls.
- Only short CSS hover and chevron transitions are added. Reduced motion disables
  both. There are no new timers, animation frames, observers, icon packages,
  syntax highlighters, JSON parsers, or third-party dependencies.
- Closed main-thread and child-thread tool groups have no input/output body DOM.
  The template mounts plain-text details after the native `toggle` event and
  removes them again when closed.
- A shallow-reactive Map is keyed by the stable turn key, matching the existing
  process-fold state. Each immutable Set records that turn's open group keys and
  is a direct `v-memo` dependency. No tool payload or turn object is retained.
  Mirroring `open` restores state after historical process folding remounts a
  group. The component owns the small metadata map for its Chat lifetime.
- Browser testing caught an initial WeakMap keyed by the displayed turn object:
  `visibleTurns` makes a fresh display copy on each stream update, so completion
  lost the open state. Stable string keys fix that path, and the streamed-update
  browser assertion remains as a regression test.
- Group summaries inspect only the first three tool names, truncate each summary
  name to 48 characters, and state how many remain. Expanded content keeps full
  names and complete payloads. A 10,000-tool unit case verifies only three names
  are read, so summary construction does not scale with the full group size.
- Input and result panes stay plain text. They have visible keyboard focus,
  horizontal/vertical scrolling, and bounded visible height; nested cards fit
  their parent width. No payload is truncated or reformatted.

## Resource boundaries

The event reducer still retains tool strings as before. Lazy mounting reduces
DOM allocation and hidden-body rendering work; it does not claim to remove
payloads from JavaScript memory. Opening a group explicitly mounts all of its
contents, and the existing group-status aggregation still inspects statuses.
This is not a complete transcript-virtualization change or an end-to-end latency
claim.

## Validation

Focused summary tests, full frontend unit tests, TypeScript, ESLint, Vite build,
and mocked browser checks cover the change. The browser scenario uses a
120-tool group with inputs/results above 16 KiB, verifies zero closed-body DOM,
checks complete payloads after opening, exercises keyboard scrolling, historical
memoization, streamed completion, child groups, and narrow-pane visibility.

Final results: 904/904 frontend unit tests, TypeScript, ESLint, Vite build,
repository documentation/staged checks, and all eleven mocked browser scenarios
passed. Independent review rechecked the stable-key correction and reported no
unresolved finding. Browser evidence is under
`chat-thinking-ui/browser-tool4-5308` in the task workspace; the owned Vite exited
normally, the port was closed, and its temporary runtime was removed.

The browser also verifies open tool state through whole-process unmount/remount
and a native double-click. A fixture initially sent generic tool completion with
`status=cancelled`; the real adapters only emit completed/failed here, so the
fixture was corrected rather than broadening the reducer protocol for a style
change. The existing internal Cancelled label mapping remains unit-tested.

Vite's existing large-bundle warning remains.
The repository-wide verification requires a backend virtual environment and
pnpm that this source snapshot does not have; direct frontend commands and the
isolated browser owner provide the frontend evidence. Backend, terminal replay,
live-provider, and deployed-service performance are outside this validation.
