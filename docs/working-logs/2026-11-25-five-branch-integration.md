# Five-branch integration

The integration starts at `d5d8910a`. Each accepted source tree contributes one
linear commit, in this order.

| Source | Scope |
| --- | --- |
| `1e7610ed` | Feishu WebSocket controls, automation filtering and replay |
| `2641a5ce` | MASO archive and session rotation |
| `e71d2316` | Goal status recovery and signal boundaries |
| `97f9b524` | Cursor empty-session recovery |
| `fdd28f74` | Mocked browser synchronization |

The changelog was the only shared path. All five source sections were retained.
The separate Feishu completion-reaction feature was excluded from this release.

Publication requires the full standard suite, mocked browser acceptance,
isolated terminal HMR, staged and committed checkout checks, AI metric checks,
incremental public disclosure checks and independent review of the fixed head.
The managed task report records the gate results and final source mapping.
