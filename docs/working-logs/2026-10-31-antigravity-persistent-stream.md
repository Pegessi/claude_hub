# Persistent Antigravity stream-json sessions — 2026-10-31

## Goal and baseline

Optimize the existing `codex/antigravity-cli-provider` branch without changing
its model or permission defaults. Baseline commit: `eb28cbf`. The deployed
review backend on `8273` and frontend on `5273` remain untouched; all live
verification uses task-owned processes and isolated Hub state.

The reported Chat had two turns. Hub events and their matching CLI logs showed:

| Prompt | Time to first text | Before CLI submitted message | Submit to first text |
| --- | ---: | ---: | ---: |
| `hi` | 76.05 s | 54.93 s | 21.11 s |
| `who are u` | 37.85 s | 24.77 s | 13.09 s |

Hub spawned the CLI within about 0.12 s in both cases. Most of the delay was
inside repeated CLI initialization: silent sign-in, model discovery and
workspace/feature refresh. The Hub event endpoint took about 2–3 ms locally.
The first OAuth token refresh itself took only 0.8 s; the second token was
still valid. Re-authenticating the account does not remove the repeated setup.

## Protocol evidence and design

The [official headless documentation](https://antigravity.google/docs/cli/headless#stream-prompts-from-stdin)
explicitly supports keeping one process alive:

- Start `agy --input-format stream-json --output-format stream-json` once.
- Write one newline-terminated `user` NDJSON record for each turn.
- Consume `step_update` records immediately and wait for `result` before
  submitting another turn. `init` occurs once per process.
- Leave stdin open between turns. Closing it means graceful session shutdown,
  not cancellation of the current turn.
- `result.response` belongs to the current turn; `result.usage`, `num_turns`
  and `duration_seconds` are cumulative. Per-step usage is available on
  `step_update` and must not be added repeatedly for repeated step updates.
- Stream control messages and runtime slash-command model changes are not
  supported. Model/mode changes require a replacement process.

The closest public implementation is
[`pi-antigravity`'s persistent driver](https://github.com/TianZuo555/pi-extensions/blob/main/packages/pi-antigravity/lib/agy-driver.ts).
Its serial input, process reuse, result boundary and configuration recycling
agree with the official contract. Its private-database fallback, permission
bypass default, automatic resends and very long timeout are not copied.
The ACP projects that poll Antigravity's private SQLite/protobuf data are not
used; Hub already has the public stdout protocol.

A real three-turn CLI probe confirmed a single PID and conversation: first
turn 47.06 s, subsequent first-text times 6.49 s and 6.34 s. The third turn
correctly recalled the second prompt. This proves the protocol, not the final
Hub performance result.

## Implementation boundaries

`AntigravitySession` continues to reuse `ProcessSession` and `SessionCore`:

- One process and stdout/stderr reader pair, with serialized startup/shutdown.
- Existing NDJSON buffering, process-generation filtering, turn guard and
  bounded process-group teardown; no second queue or provider-specific Hub
  consumer loop.
- Persistent/fatal-EOF provider spec: a completed turn must not invalidate the
  still-live reader. Unexpected process EOF remains a runtime failure.
- A successful spawn is ready to accept input; there is no separate agy RPC
  handshake that may gate the first prompt.
- Hash the actual launch env, mode, Solo setting and cwd. A changed setting
  replaces the process between turns, preserving a verified conversation id.
  Learning an id from `init` is not a configuration change.
- Stop replaces the process through the existing recovery/teardown barrier.
  Broken-pipe delivery is reported, never implicitly retried.
- Usage is computed per turn from step snapshots; result-only fallback must
  not misreport unknown historical cumulative counters as current usage.

No changes to the shared proxy, credentials, public admission surfaces, model
catalog, permission defaults, primary Hub or current review service.

## Verification record

A reproducible native-transport benchmark uses the same model
`gemini-4-argon-high`, the same working directory, Hub guidance and three
prompts for both variants. The third prompt verifies conversation memory.

Baseline measured immediately before editing:

| Turn | First text | Completion | Process launches so far |
| --- | ---: | ---: | ---: |
| `hi` | 25.26 s | 25.41 s | 1 |
| `who are u` | 52.18 s | 52.51 s | 2 |
| recall previous question | 30.82 s | 31.38 s | 3 |

Final controlled comparison used one fixed copy of the deployment proxy on a
task-owned port for both variants. The baseline transport was loaded from
`eb28cbf`; both used the current shared event normalization, identical prompts,
model and working directory. The proxy's separate connection-pool work is not
attributed to this branch.

| Turn | Baseline first text | Persistent first text | Process launches, baseline / persistent |
| --- | ---: | ---: | ---: |
| `hi` | 37.40 s | 10.84 s | 1 / 1 |
| `who are u` | 14.94 s | 6.45 s | 2 / 1 |
| recall previous question | 11.00 s | 4.23 s | 3 / 1 |

For the two subsequent turns in this sample, mean time to first text fell from
12.97 s to 5.34 s (58.8% lower, 2.43x faster). Input submission fell from
1.12–1.37 s to about 1.2 ms. The first-turn figures are retained but excluded
from the improvement claim: initialization and upstream cache state vary.
Both variants answered the memory check correctly. This is a small live sample,
not a throughput or long-context benchmark.

Validation completed:

- `scripts/verify.sh all`: all targets passed; backend **5942 passed,
  10 skipped**, frontend **1139 passed**, format/types/docs/checkout/build passed.
  Existing optional/backend-environment skips remain; browser-mock, terminal
  replay/HMR and manual UI targets are not part of `all` and were not run.
- Focused Antigravity suite: **58 passed**. It exercises the real tailer with
  fake subprocess I/O, including concurrent startup, serial turns, interrupted
  generations, configuration changes, readiness, usage, cancellation and EOF.
- Independent review found and closed three issues: replacement EOF must not
  inherit the old process's expected-stop exemption; fatal EOF must retain the
  already-redacted CLI exit diagnostic; idle shutdown must try stdin EOF with
  a bounded grace period before process-group cleanup. Regression tests cover
  each boundary. No remaining code-review blockers were found.
- Real isolated HTTP backend: two consecutive turns completed, the second
  recalled the first turn's marker, and Stop produced one cancelled terminal
  event after 0.21 s. The cancellation request itself returned in about 6 ms.

Live follow-up after the external network correction:

The earlier HTTP recovery attempt reached the same verified CLI conversation
but failed during automatic keyring sign-in. Its log loaded an unexpired token,
then reported `keyringAuth: timed out after 10s` / `silent auth failed`, before
any model request. Fresh Chats also reproduced the failure. Those failed runs
remain in the evidence; no credentials or authentication settings were changed
by this branch.

After the separate network task expanded the proxy's per-host warm connection
pools, a new isolated HTTP Chat completed the remaining checks at **15:06 on
2026-10-31**, using unchanged production code from `14a80cd`:

| Check | Result | Completion time |
| --- | --- | ---: |
| first turn, Argon High | completed | 12.22 s |
| next turn, same process | completed; recalled marker | 7.37 s |
| Stop during generation | exactly one cancelled terminal event | 0.21 s |
| next turn after Stop | completed; same verified conversation and marker | 11.85 s |
| change High to Medium | completed; same verified conversation and marker | 16.36 s |

The Stop request itself returned in about 5 ms. The two initial turns shared
one CLI PID; Stop and the explicit model change each created one replacement.
All four completed turns had no error events. The test Chat, isolated backend
and its three CLI processes were removed and verified absent afterward.
This closes the live post-Stop/model-change validation gap. The model change
was a functional check only; the performance comparison above still uses High
for both variants and does not include the network task's separate gains.

Earlier live attempts interrupted by external restarts of the shared proxy
are retained as failures, not included in the controlled improvement claim.
The shared proxy owner confirmed those restarts and separately restored its
connection-pooled service. All task-owned proxy/backend/CLI processes were
terminated and checked absent. Neither the main Hub nor the existing review
backend was restarted. The branch still needs an explicitly authorized service
reload before the existing review URL can use this transport.

Local evidence is retained under `runtime/antigravity-performance/` in the
operator workspace: `performance-summary.json`, the benchmark summaries and
CLI logs, isolated HTTP smoke summaries, and `verification/final-verify.log`.
