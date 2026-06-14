# 2026-06-14 — Terminal input latency v4: measure, then remove per-frame regex/decode

Follow-up to `2026-06-14-terminal-input-latency-v3.md`. After v3 shipped, typing
in a terminal tab **still felt 不跟手** — but only *under load*. The v3 round was
never quantified (an open risk the v3 log itself flagged: *performance work must
baseline-measure the symptom*). This round fixes that: build a measurement
harness, capture a real before-number, fix the next bottleneck, re-measure.

## System overview

Focused desktop typing flows straight through the transport — it does **not**
use the parent SharedArrayBuffer keystroke ring (that only carries
synthetic/mobile/compose keys):

```
xterm.js (in ttyd iframe) ──WS──▶ FastAPI proxy ──▶ tmux ──▶ pty
        ▲                                                     │
        └──────────── output frames ◀──────────────────────────┘
```

The transport itself is already clean: WebGL renderer, `TCP_NODELAY` on every
socket, no batching/compression, no per-keystroke parent work. The remaining
cost is **render-side main-thread contention**: the injected `term.write`
override (a Python f-string template — literal JS braces are doubled `{{ }}`)
runs bookkeeping on every output frame, and under heavy output that bookkeeping
competes with keystroke echo for the main thread.

## The bottleneck v3 left behind

v3 removed the per-frame DOM **layout reflow**. But the same hot path
(`term.write` → `noteLiveWrite` → `noteResyncPressure`) still called
`terminalDataStats(data)` on **every frame**, and that function did, over the
*entire* frame:

- `new TextDecoder().decode(data)` — allocate + decode the whole buffer,
- **four** regex `.replace()` passes (stripping ANSI / control sequences),
- a `.match(/\n/g)` to count newlines.

Under a fast wide-line flood this dominates the main thread and starves
keystroke echo — the felt lag. (Agent/TUI tabs gate the resync path off via
`AUTO_HISTORY_RESYNC_ENABLED`, so this only bites `agent_type="terminal"` tabs —
which is what the harness drives.)

## Measurement harness

`backend/tests/test_terminal_input_latency_perf.py` — pytest-driven, opt-in,
run-on-demand (`-s` to print the table). **Not a CI timing gate**: absolute
latencies are machine-dependent and would flake on shared runners. The static
guard test protects the hot path structurally instead.

How it measures keystroke-to-glyph latency, all in-page (no clock skew):

- `t0`: a capture-phase `keydown` listener stamps `performance.now()` before
  xterm handles the key.
- `t1`: `term.onRender` stamps `performance.now()` the first frame the typed
  sentinel glyph count exceeds its pre-keystroke baseline.
- Cycles distinct sentinels (`a-y`, no `x`/`z` so they never collide with the
  load output, which is only `X`/digits/newlines); `Backspace` + settle between
  samples; 5 warmup discarded; 45 measured per condition; p50/p95 in Python.
- **Load condition**: an out-of-band tmux loop
  `while true; do printf 'X%.0s' $(seq 1 400); echo; done` floods wide wrapped
  lines to maximize bytes-per-frame; `C-c` to stop.

## Fix

`backend/claude_hub/api/terminal.py` — replace `terminalDataStats` with an
allocation-free O(n) scan; delete the now-dead `terminalDataText`:

- `lineBreaks` = count of bytes `=== 10` (`\n`) via one tight loop. Exact: `0x0a`
  is single-byte and never a UTF-8 continuation byte.
- `chars` = `data.length` (raw byte length) as a char proxy. Over-counts vs the
  old regex (includes ANSI/multibyte), but the **only** consumers are the coarse
  burst thresholds in `hasEnoughResyncPressure()` (`chars >= 4096` OR
  `lineBreaks >= 8`), so it merely arms the idle resync marginally earlier —
  harmless.
- Keeps a `charCodeAt` string branch for the replay path (strings flow through
  `writeThrough`).

The resync state machine downstream (`noteResyncPressure`, `scheduleResync`,
`runResync`, the at-bottom/idle/no-recent-input gates) is unchanged.

Rejected alternatives: (b) defer/debounce keeps total CPU and adds buffers/timers;
(c) sample-every-Nth risks under-counting newline bursts from fast TUIs.
Approach (a) eliminates the cost outright while keeping the newline trigger exact.

## Before / after (this machine, n=45 each)

| condition  | before p50 | before p95 | after p50 | after p95 |
| ---------- | ---------: | ---------: | --------: | --------: |
| idle       |      18.22 |      38.35 |     24.05 |     33.16 |
| under-load |     151.03 |     613.01 |     78.75 |    225.09 |

Under-load p50 dropped ~48%, p95 ~63%. Idle is unchanged within run-to-run
noise. The idle→under-load gap — the regression signal — shrank from ~595ms (p95)
to ~192ms.

## Verification

- Harness re-run against a worktree backend serving the patched JS (port 8174)
  produced the "after" column above.
- Correctness: `test_terminal_replay.py` (pushes 120 wrapped ~240-char lines,
  well over both resync thresholds — resync must still fire) passes, along with
  `test_terminal_input_latency_guard.py` and `test_terminal_proxy.py`.
  (When the Playwright sync suite and the asyncio proxy suite run in the *same*
  process, the two proxy tests hit a pre-existing
  `Runner.run() cannot be called from a running event loop` test-isolation
  error; both pass in isolation. Unrelated to this change.)
- Static: black / isort / mypy clean on touched files; injected-JS brace balance
  intact; `python -c "import claude_hub.api.terminal"` OK.
- Guard extended: `terminalDataStats` is now asserted to contain no
  `TextDecoder` / `.replace(` / `.match(` (comments stripped before scanning so
  the explanatory prose doesn't trip it).

## Key issues / pitfalls

- **Injected JS is an f-string** — literal braces must stay doubled `{{ }}`.
- **Only `terminal` tabs** exercise the resync/stats path; agent TUI tabs gate it
  off. The harness must create an `agent_type="terminal"` tab.
- **`uv sync --dev` does not install the `dev` extra** (it's an
  `optional-dependencies` extra, not a `dependency-groups` group); use
  `uv sync --extra dev` to get Playwright in a fresh worktree venv.
- **Don't measure against the live workspace backend** (port 8173) — it serves
  the *old* injected JS until main is updated. Run a dedicated worktree backend
  on a separate port for the "after" numbers.
