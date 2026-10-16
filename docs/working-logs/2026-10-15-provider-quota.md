# Remaining quota readings for Codex and MASO

## Scope

Hub runs work against providers that meter it, but nothing in Hub said how much
allowance was left. This adds a read-only reading per provider — the share of
each limit still available, when it resets, and how old the reading is —
surfaced through `GET /api/quota`, `claude-hub quota`, and a menu panel.

Worktree `~/claude_hub_worktree/provider-quota`, branch `feat/provider-quota`,
base `45c552c`. Nothing here starts a session, spends quota, mutates provider
state, or restarts a service. Claude is out of scope: only Codex and MASO were
requested.

## What each provider actually publishes

The two providers are not symmetric, and the contract keeps them apart rather
than inventing a common figure neither reports.

**MASO** meters itself and answers `GET /api/llm-quota`, returning a per-model
`effectiveState` / `effectiveUsedPercent` / `resetAt`, an `enforcing` flag, and
its own overall `state`. It always answers 200 and caches for 60 seconds. Hub
passes these through unchanged; re-deriving a percentage or a threshold here
would contradict the upstream that owns the accounting.

**Codex** publishes no API. Its figures survive only as a `rate_limits` object
inside `token_count` records in the rollout files it writes per turn under
`~/.codex/sessions/`. Hub salvages the newest such record. This is a reading
Codex happened to leave behind, not a query, and the contract says so through
`as_of` and `stale`.

Measured on this host while building: 30 rollout files, 60.7 MB total; the
newest reading was 6 seconds old; locating it cost 0.83 ms. MASO cost ~130 ms
including endpoint discovery.

## Contract

`GET /api/quota` always answers 200. A passive panel must not be able to turn
one provider's outage into a failed request, so each provider carries its own
`available` / `state` / `message`, and the combining layer degrades a provider
that raises rather than failing the response.

`QuotaLimit` is the shared unit: `scope`, `label`, `state`, `used_percent`,
`remaining_percent`, `resets_at`. A share is `None` rather than `0` when it was
not measured — `no_limit`, `no_data` and `unknown` are three different answers
and none of them means "nothing left". `ProviderQuota` adds the provider-level
verdict plus the fields only one side has: `plan` and `credit_balance` for
Codex, `enforcing` for MASO.

## Pitfalls this cost us

**Ordering must come from mtime.** Rollout files live under `YYYY/MM/DD`
directories named for when the session *started*. Resuming an old session
appends current records to a file filed under its original date; a file under
`2026/10/05/` was observed carrying `2026-10-10` records. Sorting by directory
or filename silently reads a stale window. Verified directly: the newest file by
mtime and the newest by name are different files on this host.

**An elapsed window must not be shown as current.** `resets_at` closes a
rolling window. Once it passes, the stored `used_percent` describes a window
that no longer exists — observed dropping from 50% to 38% across a rollover.
Carrying it forward would overstate consumption and could read as "nearly out"
when the window has in fact reset. The share is withheld, the limit becomes
`unknown`, and the provider is marked `stale`. `resets_at` itself is kept: it is
the evidence for why the figure was withheld.

**One file is not enough.** 5 of 30 rollout files here contain no `rate_limits`
at all — sessions aborted before completing a turn. Reading only the newest file
returns nothing on those days, so the search falls back across recent files.

**Match the quoted key.** The bare word `rate_limits` appears in ordinary
conversation text inside these transcripts and over-matched in 3 files. The
scan requires `"rate_limits"` and a `token_count` payload.

**The MASO endpoint must be discovered, not assumed.** The documented default is
port 9796; the live instance here is on 11004, registered in
`~/.maso/instances.json`. The existing `discover_maso_service` is reused so the
quota reading comes from the same service Hub would use for a Chat.

**`credits.balance` is a string whose format drifts** — both `"62500"` and
`"62500.0000000000"` have been written. It is passed through verbatim; its unit
is undocumented, and the UI says so rather than implying a currency.

**Bounding the scan must not cost correctness.** The first implementation
stopped collecting paths mid-glob and sorted afterwards, so above the cap the
retained set was an artifact of filesystem order and could exclude the newest
file — defeating the one thing the function exists to do. Stat is the cheap
part and tail reads are separately bounded, so the scan budget is now a runaway
guard only, both directories are always traversed, and tripping the guard
degrades visibly through an older `as_of`.

**A reset time does not prove a reading is still valid.** Staleness was first
derived from `now > resets_at` alone. Trial use disproved it within hours: a
Codex allowance was reset out of band while the stored reading still said five
days remained, so the panel kept presenting 40% left when 100% was free. The
reset time in an older reading cannot predict a reset that was not on its
schedule. Age is the only signal that covers this, which is why an ageing
reading is marked a last known value independently of `stale`.

**A refresh that cannot refresh must say so.** Codex publishes usage only when a
turn completes, so pressing Refresh re-reads the same record and looks broken.
MASO really does re-answer. The panel now compares `as_of` across a refresh and
reports a provider that returned nothing newer, which keeps the control honest
without implying Hub can force an update it has no way to request.

**Absent is not broken.** A Hub with no MASO at all first reported the same
`unavailable` state as a MASO that was configured and down, telling the user to
start a service that was never part of the deployment. A permanent alert nobody
can act on devalues the real one. Explicit configuration, or a registry entry
proving MASO once ran here, now means a fault; neither means `not_configured`,
stated quietly and excluded from the unavailable count.

## Module responsibilities

- `services/provider_quota.py` — the only place that knows either source
  format. Reads, normalises, bounds and caches; raises nothing at the seam.
- `api/quota.py` — transport only.
- `models/schemas.py` — the shared shape, because the service layer produces it
  and the CLI consumes it; inlining it in the API module would have pointed
  services at api.
- `utils/quotaDisplay.ts` — the null/stale/state wording decisions, extracted so
  the boundaries are unit-testable without a DOM.
- `components/QuotaMenu.vue` — presentation and lazy loading, following
  `NetworkAccessMenu.vue`.

## Files

- Added: `backend/claude_hub/services/provider_quota.py`,
  `backend/claude_hub/api/quota.py`, `backend/tests/test_provider_quota.py`,
  `backend/tests/test_quota_api.py`, `frontend/src/components/QuotaMenu.vue`,
  `frontend/src/utils/quotaDisplay.ts`, `frontend/tests/quotaMenu.test.mjs`
- Changed: `backend/claude_hub/models/schemas.py`,
  `backend/claude_hub/services/maso_discovery.py`,
  `backend/claude_hub/models/__init__.py`, `backend/claude_hub/api/__init__.py`,
  `backend/claude_hub/cli/client.py`,
  `backend/claude_hub/cli/commands/rest.py`,
  `backend/claude_hub/cli/main.py`, `backend/tests/test_cli.py`,
  `frontend/src/types/index.ts`,
  `frontend/src/components/AppExtensionActions.vue`, `README.md`,
  `CHANGELOG.md`
