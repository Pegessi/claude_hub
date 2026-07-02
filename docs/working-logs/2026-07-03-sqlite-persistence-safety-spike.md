# SQLite Persistence Safety Spike (ADR + design)

Date: 2026-07-03
Status: **Spike / design-approved; prototype is additive and OFF by default.**
Task: workspace `369116d5-...` "SQLite persistence safety spike".
Branch: `feat/sqlite-persistence-spike`, cut from `develop` (integration target =
`develop`, per `docs/working-logs/2026-07-03-resident-develop-integration.md`).

> This is a **design-first spike**. It ships an additive `StorageBackend`
> abstraction plus a stdlib-`sqlite3` prototype and round-trip tests, all gated
> behind an opt-in flag that defaults to the existing JSON behavior. **No live
> data is migrated, no default behavior changes, and no existing state file is
> deleted or overwritten by this task.**

---

## 1. Current persistence path (as-is)

State lives in `backend/claude_hub/services/workspace_manager/`:

- `_state.py::_StateMixin.__init__` builds four in-memory dicts —
  `workspaces`, `tasks`, `sessions`, `reports` — and calls `_load_state()`.
- `_load_state()` reads `~/.claude_hub/workspaces/index.json` (nested layout)
  or the legacy `~/.claude_hub/workspaces.json`, reconstructing pydantic
  models via `Workspace(**...)`, `WorkspaceTask(**...)`, etc.
- `_persistence.py::_PersistenceMixin._save_state()` is the **only** writer. On
  every call it:
  1. Rewrites `index.json` with **all** workspaces.
  2. For **every** workspace, rewrites `<id>/state.json` containing **all**
     that workspace's tasks + sessions + reports (via `model_dump(mode="json")`).
  3. Rewrites `<id>/snapshot.md`.

### 1.1 Measured scale (this machine, 2026-07-03)

| Metric | Value |
| --- | --- |
| Workspaces on disk | 157 |
| Largest `state.json` (this workspace) | **7.3 MB** |
| Tasks in it | 112 |
| Reports in it | **2153** |
| `_save_state()` call sites | 34 (several in the 5s monitor loop) |

So every task update, every report POST, every monitor tick re-serializes and
rewrites a 7.3 MB file in full. This is the scaling pain the user directive
targets ("storage should scale and may use SQLite").

### 1.2 Data-loss hazards (independent of SQLite)

1. **Non-atomic writes.** `_save_state()` uses `Path.write_text(...)` directly
   on the live file (`_persistence.py:14,36`). A crash, `kill -9`, OOM, or
   disk-full **mid-write truncates the live `state.json`** — losing all 112
   tasks + 2153 reports for that workspace. There is no temp-file + `os.replace`.
2. **No backup.** There is no rolling `.bak`; the last-good state is not
   preserved before a rewrite.
3. **Read-side swallow.** `_load_nested_state()` catches `Exception` and logs,
   then continues with **partial** in-memory state (`_state.py:72`). A single
   corrupt `state.json` silently drops that workspace's tasks/reports on the
   next `_save_state()` (they are no longer in memory, so they are written out
   of existence).

**Conclusion:** the biggest near-term data-loss win is *atomic writes +
backup* on the **existing JSON path** — this is independent of SQLite and is
included as an additive, opt-in-by-default-safe helper below. SQLite then
addresses the *scaling* half of the directive.

---

## 2. Safest SQLite introduction point

The serialization boundary is tiny and well-defined: `_save_state()` writes
`model_dump(mode="json")` dicts; `_load_*state()` reconstructs models from those
dicts. **That boundary — not the 34 call sites — is where a backend plugs in.**

Introduce a `StorageBackend` protocol with two implementations:

- `JsonStorageBackend` — a faithful extraction of *today's* behavior (nested
  `index.json` + per-workspace `state.json`), optionally upgraded to atomic
  writes. **This is the default.**
- `SqliteStorageBackend` — the prototype. One SQLite DB, one table per entity,
  each row = `(id, workspace_id, json)` where `json` is exactly the same
  `model_dump(mode="json")` payload. A `schema_meta` table records
  `schema_version`.

Selection is by an opt-in setting `workspace_storage_backend` (default
`"json"`). With the flag unset, `workspace_manager` behaves byte-for-byte as
today.

```
_save_state()  ──serialize──▶  StorageBackend.save(snapshot)
_load_state()  ◀─deserialize──  StorageBackend.load() -> snapshot
                                   ├── JsonStorageBackend   (default)
                                   └── SqliteStorageBackend (opt-in prototype)
```

Because both sides speak the identical `model_dump(mode="json")` dict, a backend
swap is invisible to every one of the 34 call sites and to the API layer.

### 2.1 Why store JSON blobs per row (not a normalized column-per-field schema)

The pydantic models evolve constantly (resident fields, autonomous runs, goal
packets, review profiles). A normalized schema would need a migration for every
model field added — high churn, high risk. Storing the validated
`model_dump(mode="json")` as a single JSON column means **model evolution needs
no DDL change**; SQLite indexes only `id` and `workspace_id` (the two keys the
code queries by). This keeps the prototype safe and low-maintenance while still
enabling per-workspace/per-entity row-level writes (the real scaling win: no
more 7.3 MB full rewrite per mutation).

---

## 3. Migration-safe design

### 3.1 Schema versioning

- A `schema_meta(key TEXT PRIMARY KEY, value TEXT)` table holds
  `schema_version` (integer, starts at `1`).
- On open, the backend reads `schema_version`. If newer than the code supports,
  it **refuses to open** (fail-closed — never silently downgrade). If older, it
  runs forward migrations in order inside a transaction.
- The JSON payloads themselves are already forward-tolerant: pydantic ignores
  unknown fields on load only if configured, but the existing `_normalize_*`
  helpers already massage older shapes; SQLite reuses those same helpers on
  load, so JSON-era normalization is preserved.

### 3.2 Atomic write + backup/restore

- **JSON path (immediate safety upgrade, applies with flag off too):**
  `atomic_write_text(path, data)` writes to `path.tmp` then `os.replace()`
  (atomic on POSIX) — a crash can never truncate the live file. Before replace,
  the previous file is copied to `path.bak` (one-deep rolling backup).
- **SQLite path:** writes are transactional (`BEGIN … COMMIT`); a crash rolls
  back to the last commit. WAL mode gives crash-safe durability. Backup uses the
  online `sqlite3` backup API / `VACUUM INTO` to a timestamped file.

### 3.3 JSON → SQLite import (never destructive)

- `import_json_to_sqlite(state_root, db_path)`:
  1. Reads the current JSON state **read-only** (never opens for write).
  2. Creates the SQLite DB at a **new** path (default
     `~/.claude_hub/workspaces/state.sqlite3`), never overwriting JSON.
  3. Inserts every workspace/task/session/report row.
  4. Verifies a **round-trip**: reload from SQLite and assert the reconstructed
     model set equals the JSON-sourced model set (same ids, same
     `model_dump(mode="json")`). Import **fails loudly** if they differ; the DB
     is discarded and JSON remains untouched.
- Import is idempotent (upsert by `id`) and can be re-run.

### 3.4 Rollback path

- Rollback is trivial *because the source is never mutated before verification*:
  set `workspace_storage_backend=json` (or unset it) and restart. The JSON files
  are still the last-good source of truth.
- If SQLite was promoted to primary in a later task, rollback =
  `export_sqlite_to_json(db_path, state_root)` (the inverse round-trip). This is
  **non-destructive and staged**: it builds the JSON tree in a sibling
  `<state_root>.staging` dir, verifies the round-trip there, and only then backs
  up any existing live tree to `<state_root>.bak` and atomically swaps the
  verified tree into place. If verification fails it raises `RoundTripError` and
  leaves the live `state_root` untouched. Then flip the flag.

### 3.5 Opt-in rollout flag / config

- New setting `workspace_storage_backend: Literal["json","sqlite"] = "json"`
  in `config.py` (env `WORKSPACE_STORAGE_BACKEND`).
- **Default `"json"` ⇒ zero behavior change.** `sqlite` is opt-in for
  experimentation only; JSON remains the shipped default until a *separate*,
  explicitly-approved task promotes it.
- Rollout ladder (future tasks, not this one):
  1. (this spike) backend abstraction + prototype + tests, flag defaults json.
  2. shadow-write: write both JSON and SQLite, JSON authoritative (verify drift).
  3. flip default to sqlite with JSON shadow-backup + one-command rollback.
  4. retire JSON writer.

### 3.6 Validation strategy

- Unit tests (this task) prove: (a) flag-off path is unchanged, (b)
  JSON↔SQLite round-trip preserves representative Workspace/WorkspaceTask/
  ManagedSession/AgentReport records field-for-field, (c) atomic write leaves no
  truncated file on simulated failure.
- CI (`black`, `isort`, `mypy`, `pytest`) must stay green.
- Future integration validation (shadow-write phase) compares JSON vs SQLite
  snapshots on every save and logs any drift before any default flip.

---

## 4. What this task actually ships (additive, OFF by default)

- `backend/claude_hub/services/storage/__init__.py` — `StorageSnapshot`,
  `StorageBackend` protocol, `atomic_write_text`, `get_storage_backend()`.
- `backend/claude_hub/services/storage/json_backend.py` — extraction of the
  current JSON behavior with an atomic-write option (behaviorally identical to
  today when used the same way).
- `backend/claude_hub/services/storage/sqlite_backend.py` — stdlib `sqlite3`
  prototype (JSON-blob-per-row, `schema_meta`, transactional, WAL).
- `backend/claude_hub/services/storage/migrate.py` — non-destructive
  `import_json_to_sqlite` / `export_sqlite_to_json` with round-trip verification.
- `backend/claude_hub/config.py` — `workspace_storage_backend` flag (default
  `"json"`).
- `backend/tests/test_storage_backend.py` — round-trip + no-default-change +
  atomic-write tests.

**Not wired into `workspace_manager` in this task.** `_save_state`/`_load_state`
are left exactly as-is; the abstraction exists but the running system still uses
the current code path. Wiring is the next task, behind the same flag.

---

## 5. Integration notes for `develop` → main

- This branch is cut from `develop` and should merge into **`develop`**, not
  `main` (see `2026-07-03-resident-develop-integration.md`).
- To try the prototype locally (no effect on running system):
  `WORKSPACE_STORAGE_BACKEND=sqlite` is defined but intentionally **not** yet
  consulted by `workspace_manager`; it only affects `get_storage_backend()`,
  which nothing in the hot path calls yet. Enabling it changes nothing until a
  follow-up task wires the manager to it.
- Forbidden-ops safety (per the integration doc): no `reset --hard` / force-push
  on `develop`/`main`, no bulk untracked-file deletion, no live-state mutation.
- **Recommended next task:** "Wire workspace_manager to StorageBackend behind
  the flag + shadow-write validation" — add atomic JSON writes to the live path
  (immediate data-loss fix, flag-independent), then shadow-write to SQLite and
  log drift, before ever flipping the default.

## 6. Lessons consulted

Scanned the workspace lessons catalog; none of the 5 lessons
(review-decision policy, mobile keyboard, dispatch race, new-agent-type parsing,
multi-tab refresh) apply to persistence/storage. No lessons force-fit.
