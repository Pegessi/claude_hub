# Test Completeness Assessment — Claude Hub

_Date: 2026-06-11 · Branch: `feat/test-coverage-completeness`_

This document evaluates how completely Claude Hub's automated tests cover its
functionality (backend APIs, services, and frontend), ranks the remaining gaps
by risk, records the tests added in this pass, and recommends how to start
measuring coverage objectively.

---

## 1. Method

- Enumerated every backend API route (`claude_hub/api/*`), service
  (`claude_hub/services/*`), and the auth layer (`claude_hub/auth/*`).
- Enumerated existing tests under `backend/tests/` and `frontend/tests/`.
- Mapped each feature to its tests and graded coverage **Covered / Partial /
  Missing**.
- Ranked gaps by risk = (blast radius if broken) × (likelihood of regression).

> **Coverage tooling status:** there is **no `pytest-cov`** configured
> (confirmed: no `cov` entry in `pyproject.toml`, no `.coveragerc`). All grades
> below are derived by **manual feature→test mapping**, not measured line/branch
> coverage. See §6 for the recommendation to add it.

### Test inventory (at time of writing)

| Suite | Tests |
|---|---|
| `test_workspaces.py` | 81 |
| `test_ttyd_manager.py` | 32 |
| `test_terminal_replay.py` (excluded from CI) | 22 |
| `test_workspace_orchestrator_contract.py` | 19 |
| `test_workspace_state_policy.py` | 18 |
| `test_feedback_lessons.py` | 15 |
| `test_auth.py` | 13 *(new)* |
| `test_session_manager.py` | 9 *(new)* |
| `test_auth_session.py` | 9 *(new)* |
| `test_filesystem.py` | 8 *(new)* |
| `test_remote.py` | 7 *(new)* |
| `test_terminal_proxy.py` | 4 |
| `test_tabs.py` | 4 |
| `test_health.py` | 2 |
| `test_clipboard.py` | 2 |
| `test_system.py` | 1 |

Full backend suite: **~270 tests** excluding `test_terminal_replay.py` (which CI
runs in a separate job). Exact counts change with each PR — add
`pytest --collect-only -q | tail -1` to see the current number. Roughly 46 of
these were added in this coverage pass.

---

## 2. Backend API coverage

| Router / endpoint | Coverage | Notes |
|---|---|---|
| `auth` — login, callback, me, check, logout | **Covered** *(new)* | `test_auth.py`: disabled redirects, whitelist grant/deny (open_id + case-insensitive email), no-whitelist allow-all, authenticated `/check`, `/me` 401, logout clears session. |
| `auth.session` store | **Covered** *(new)* | `test_auth_session.py`: create/get round-trip, expiry purge, cleanup (expired + malformed), corrupt-file tolerance. |
| `filesystem` — list, home | **Covered** *(new)* | `test_filesystem.py`: dirs-first sort, dir/symlink flags, 404/400 branches, default-to-home. |
| `remote` — profiles, filesystem/list | **Covered** *(new)* | `test_remote.py`: 404 missing profile, success, 504 timeout, 502 non-zero/invalid-JSON, payload-error status propagation, profile list. |
| `workspaces` — board, tasks, lessons, sessions, dispatch, lifecycle | **Covered** | `test_workspaces.py` (81) + orchestrator contract + state policy suites cover the largest surface. |
| `tabs` — CRUD, order, status, duplicate | **Partial** | `test_tabs.py` has 4 tests; CRUD happy paths plus `ttyd_manager` indirectly. Reorder/duplicate/status edge cases and error paths thin. |
| `terminal` — history, proxy (HTTP) | **Partial** | `test_terminal_proxy.py` (4) + `test_terminal_replay.py` (22, **excluded from CI**). The two WebSocket endpoints (`/ws/{tab_id}`, `/proxy/{tab_id}/ws`) are not exercised in CI. |
| `clipboard` — image | **Partial** | `test_clipboard.py` (2): happy path + one error. Platform-specific decode branches thin. |
| `system` — network-access | **Partial** | `test_system.py` (1): single assertion. |

### Services

| Service | Coverage | Notes |
|---|---|---|
| `session_manager` (WS ConnectionManager) | **Covered** *(new)* | `test_session_manager.py`: connect/disconnect/broadcast/personal, prune-on-failure, prune-empty-tab. |
| `ttyd_manager` | **Covered** | `test_ttyd_manager.py` (32). |
| `workspace_manager` | **Covered** | Largest service; exercised via workspaces + orchestrator + state-policy suites. |
| `feedback_lessons` | **Covered** | `test_feedback_lessons.py` (15). |
| `workspace_state_policy` | **Covered** | `test_workspace_state_policy.py` (18). |
| `remote_profiles` | **Partial** | Reached indirectly via `test_remote.py` monkeypatching; no direct persistence/CRUD tests. |

---

## 3. Frontend coverage

| Area | Coverage | Notes |
|---|---|---|
| Task abort logic | **Partial** | `frontend/tests/taskAbort.test.mjs` is the **only** test (`node --test`). |
| 15 `.vue` views/components, Pinia stores, API client | **Missing** | No component/store tests. No Vitest/Testing-Library setup. `build` runs `vue-tsc` (type check only). |

Frontend is the **largest coverage gap** by surface area: 15 components and the
entire state/display layer have essentially no behavioral tests.

---

## 4. Risk-ranked gaps (highest first)

1. **Terminal WebSocket proxy (`/ws/{tab_id}`, `/proxy/{tab_id}/ws`)** — core
   product function (live terminal I/O), complex async proxying, **not covered
   in CI**. `test_terminal_replay.py` exists but is excluded. _Highest blast
   radius._
2. **Frontend behavior (views/stores/API client)** — the entire user-facing
   display layer is untested; regressions ship silently to the UI.
3. **`tabs` reorder / duplicate / status edge cases** — multi-tab state is
   central; only happy paths are covered.
4. **`remote_profiles` persistence/CRUD** — only tested indirectly; profile
   save/load/delete and malformed-store handling unverified.
5. **`clipboard` platform decode branches & `system` network-access edge
   cases** — thin single-assertion suites.

### Gaps closed in this pass (were previously Missing/Partial)

- **auth API + whitelist + local-bypass** — was Missing → **Covered**.
- **auth session store** — was Missing → **Covered**.
- **session_manager WS ConnectionManager** — was Missing → **Covered**.
- **filesystem list/home edge cases** — was Missing → **Covered**.
- **remote filesystem/profiles error paths** — was Missing → **Covered**.

---

## 5. Bug found and fixed during this pass

**`claude_hub/api/filesystem.py` — `safe_list_dir` masked 404/400 as 500.**
The function raised `HTTPException(404/400)` for missing path / non-directory,
but a trailing broad `except Exception` re-wrapped those into `500`, so clients
always saw a server error for ordinary not-found / wrong-type inputs. Surfaced
directly by the new `test_filesystem.py`. Fix preserves the intentional
responses:

```python
except PermissionError:
    raise HTTPException(status_code=403, detail="Permission denied")
except HTTPException:
    # Preserve intentional 404/400 responses instead of masking them as 500.
    raise
except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))
```

This is a genuine production behavior fix, not just a test artifact.

---

## 6. Coverage measurement recommendation

Adopt `pytest-cov` so future grades are measured, not estimated:

- Add `pytest-cov` to the dev dependencies.
- Run locally / in CI:
  `uv run pytest --ignore=tests/test_terminal_replay.py --cov=claude_hub --cov-report=term-missing`
- Start with **reporting only** (no gate) to establish a baseline number, then
  introduce a `--cov-fail-under` threshold set just below the baseline and ratchet
  it up as gaps close. Avoid setting a hard gate before the WebSocket and tabs
  gaps are addressed, or it will fail on known holes.
- Frontend: introduce **Vitest + @vue/test-utils** and add a `test:unit:vue`
  script; begin with the Pinia stores and the highest-traffic views.

---

## 7. Pre-existing issues observed (out of scope, not introduced here)

These exist on `main` independently of this work and were **not** modified:

- `claude_hub/services/workspace_manager.py` — **black** would reformat it, and
  **mypy** reports 3 `union-attr` errors (`GoalPacket | None` at lines ~4031,
  ~4035, ~5150). Confirmed present on the `main` baseline. Recommend a separate
  cleanup change.

---

## 8. Validation status for this pass

- `black --check --line-length 100` — **clean** for all new/changed files.
- `isort --check --profile black` — **clean** for all new/changed files.
- `mypy claude_hub/api/filesystem.py` — **clean** (the only mypy errors come
  from the pre-existing `workspace_manager.py`).
- `pytest --ignore=tests/test_terminal_replay.py` — **268 passed**.
