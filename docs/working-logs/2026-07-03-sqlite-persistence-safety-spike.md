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

---

## 7. Phase 3 — Read-only `storage verify` CLI (2026-07-05)

Phase 2 hardened the opt-in backend with busy-timeout, integrity_check gates,
orphan parity, Literal config typing, and restore-on-failure. The default is
still `json` and the running server does not touch SQLite. Phase 3 adds a
**read-only pre-flight command** so an operator (or CI) can exercise the
SQLite round-trip against their real JSON state *before* any live rollout.

### What ships

- `backend/claude_hub/services/storage/verify.py` — pure-Python helper:
  - `VerificationError` / `EntityDiff` / `VerificationReport` dataclasses.
  - `verify_state_dir(state_root)` loads the JSON snapshot, writes it into a
    **tempdir-scoped** SQLite file, runs `PRAGMA integrity_check`, reloads,
    exports via the production `export_sqlite_to_json`, reloads that export,
    and compares fingerprints. Returns `VerificationReport(ok=True)` on
    success; raises `VerificationError` with a partial `report` attribute on
    any failure.
  - Orphan items (no `workspace_id`) are counted and surfaced as **warnings**,
    not failures — both backends silently drop them on `save()`, so we
    compare fingerprints after stripping orphans.
  - `state_root` is opened read-only; no files are created inside it. All
    SQLite/export artifacts live under a `TemporaryDirectory` and are cleaned
    up on return.
- `backend/claude_hub/cli/commands/storage.py` — new `storage` Click group
  with one command:
  - `claude-hub storage verify [--state-root PATH] [--copy/--no-copy] [--json]`
  - Default `--state-root` is `~/.claude_hub/workspaces`.
  - Default `--copy` shutil.copytrees the source into a temp dir (ignoring
    `*.sqlite3*`, `*.bak`, `*.staging`) before verifying, so concurrent
    server saves cannot cause a torn JSON read. Pass `--no-copy` only when
    the server is stopped or you are pointing at a fixture.
  - Exit codes: `0` on pass, `1` on verification failure, `2` (Click default)
    on usage / IO errors.
  - Human output prints source counts, integrity / SQLite round-trip /
    JSON-export round-trip flags, warnings, and `PASS` / `FAIL`. `--json`
    emits the full `VerificationReport.to_dict()` (with an `error` field on
    failure).
- `backend/claude_hub/cli/main.py` — `_register()` imports and attaches
  `storage` between `session` and `lessons`.
- `backend/tests/test_storage_verify.py` — 10 tests covering:
  - Clean snapshot passes; verifier does not mutate `state_root`.
  - Missing `index.json` and corrupt JSON both raise `VerificationError`.
  - Integrity-check failure is raised as `VerificationError` (monkeypatched).
  - Orphan items surface as warnings; export round-trip still passes and
    orphans are correctly dropped.
  - CLI subcommand is registered, returns exit 0 with `PASS` in human output
    and a parseable JSON payload with `--json`, returns exit 1 for a missing
    root, and leaves `state_root` byte-identical under default `--copy`.
  - Meta-check: `workspace_storage_backend` still defaults to `"json"` and
    `get_storage_backend()` still returns `JsonStorageBackend` by default.

### Operator usage (develop only — not yet on main)

```bash
# Pre-flight your live state root (server may be running — default --copy is safe):
claude-hub storage verify

# Point at a fixture / test snapshot:
claude-hub storage verify --state-root /tmp/my-fixture --no-copy

# Machine-readable output:
claude-hub --json storage verify | jq .
```

Sample human output on a healthy fixture:

```
storage verify: /path/to/state
  source           : 1 workspaces, 4 tasks, 7 sessions, 12 reports
  integrity_check  : ok
  sqlite roundtrip : ok
  json export r/t  : ok
  result: PASS
```

On failure, the `FAIL` line lists the first error, and per-kind diffs
(`missing=`, `extra=`, `changed=`) are emitted to stderr so an operator can
see exactly which entity ids drifted.

### Do-not-flip guardrails (still) in force

- `settings.workspace_storage_backend` still defaults to `"json"`; the
  `Literal["json", "sqlite"]` typing still rejects any other value at
  Settings-construction time, and `get_storage_backend()` still falls back to
  JSON for any unknown runtime value.
- `workspace_manager` is still **not** wired to `get_storage_backend()`. The
  running server continues to read/write `index.json` + `<ws>/state.json`
  exactly as before. Phase 3 only adds the CLI verifier.
- All verify artifacts live in temp dirs; the command never opens the live
  state root for writing, never creates `*.sqlite3` next to live data, and
  never touches `*.bak` / `*.staging` files.

### Phase 4 preview (not in this task)

1. Wire `workspace_manager._save_state` / `_load_state` to
   `get_storage_backend()` behind the existing flag. Make JSON writes use
   `atomic_write_text` unconditionally (this is a data-loss fix independent
   of SQLite).
2. Add a shadow-write wrapper that saves to both backends, reloads SQLite,
   compares fingerprints against the JSON snapshot on every save, and logs
   (but does not block on) drift. Run this shadow mode for N days in
   `develop` while the default remains `json`.
3. Once drift logs are empty across real deployments, add an offline
   migration subcommand (built on `import_json_to_sqlite` + the verify
   helper), then flip the default to `sqlite` behind a major-version note.

## 9. Phase 4 — atomic hot-path + opt-in shadow (2026-07-07)

Phase 4 lands the narrowest safety gate that:

1. **Hardens the production JSON hot-path against truncation-on-crash**
   (flag-free, no behavior change beyond durability).
2. **Adds an opt-in shadow writer + one-shot CLI dry-run** for pre-flighting
   SQLite against any state root before any cutover.

### 9.1 What ships

* **AC1-2 — atomic hot-path writes.** `workspace_manager/_persistence._save_state`
  now routes both `index.json` and per-workspace `<ws>/state.json` through the
  existing `atomic_write_text()` helper (tempfile + `fsync` + `os.replace` +
  one-deep `.bak`). The previous implementation called `Path.write_text(...)`
  directly on the live file, which could truncate authoritative state on
  mid-write crash / disk-full. `snapshot.md` (human-readable, regenerable)
  continues to use plain `write_text` by design.
* **AC3-5 — `ShadowStorageBackend`.** New `services/storage/shadow.py` wraps a
  primary (authoritative, JSON) and a secondary (best-effort, SQLite) backend:
  * `load()` always delegates to primary (secondary is write-only).
  * `save()` writes primary first and re-raises primary failures; on primary
    success it calls secondary inside a try/except, routing any exception to
    an `on_error` callback (default: `logger.warning`) and swallowing it so
    primary durability is never compromised.
  * After a successful secondary save, the wrapper reloads both backends and
    diffs fingerprints via the existing `verify._fingerprint/_diff` helpers.
    Drift is surfaced as a `ShadowDriftWarning` routed through `on_error` but
    never raises.
* **AC6 — live-root guard.** `assert_path_outside_root(candidate, *roots)`
  refuses any shadow DB path (or its parent) that resolves under a forbidden
  state root, with graceful symlink/`..` resolution fallback. Called by the CLI
  and intended for any future server-side opt-in wiring.
* **AC7 — `claude-hub storage shadow` CLI.** One-shot dry-run: copies the
  state root to a tempdir (default `--copy`), dual-writes through
  `ShadowStorageBackend`, prints a `PASS` / `FAIL` summary with drift details,
  and exits 0 on match, 1 on drift/error. `--db-path` is optional (a temp DB
  is used by default and cleaned up). Human + `--json` output both supported.
* **AC8-11 — tests, formatting, docs.** 17 new focused tests in
  `tests/test_storage_shadow.py` covering atomic routing, primary/secondary
  failure semantics, drift detection, live-root guard, CLI exit codes,
  JSON-default meta-check, and Protocol structural compatibility.

### 9.2 What is *not* wired

* `workspace_manager` continues to load from JSON directly; `_load_state` is
  intentionally untouched this phase.
* `get_storage_backend()` is still not called by the running server;
  `workspace_storage_backend` still defaults to `"json"`.
* `ShadowStorageBackend` is NOT constructed by default anywhere. It is only
  instantiated when an operator explicitly runs `claude-hub storage shadow`,
  and lazy-imported in `storage/__init__.py` so that merely importing the
  storage package does not pull in `sqlite_backend` / `verify` (keeping the
  production import graph unchanged).
* No long-running dual-write daemon. The CLI is a one-shot dry-run; a
  resident/dev canary shadow (phase-5) will require explicit opt-in wiring
  and a separate rollout plan.

### 9.3 Operator usage (develop only)

```bash
# Pre-flight a fixture / copy of live state against SQLite (default: temp DB,
# copies state root first so a running server cannot cause torn reads):
claude-hub storage shadow

# Persist the shadow DB for inspection and point at a specific root:
claude-hub storage shadow --state-root ~/.claude_hub/workspaces --db-path /tmp/shadow.db

# Machine-readable output (exit 0 = match, exit 1 = drift/error):
claude-hub --json storage shadow --state-root <fixture> --no-copy | jq .
```

Sample human output on a clean snapshot:

```
storage shadow: /path/to/state
  db            : /tmp/shadow.db
  primary→secondary : ok
  result: PASS
```

On drift (simulated by corrupting the secondary):

```
storage shadow: /path/to/state
  db            : /tmp/shadow.db
  primary→secondary : DRIFT
  drift detail  : shadow drift: tasks missing from secondary: 1 (t1)
  result: FAIL
```

### 9.4 Do-not-flip guardrails (still) in force

* `settings.workspace_storage_backend` still defaults to `"json"`.
* No code path writes `*.sqlite3` files under `~/.claude_hub/workspaces` by
  default. The CLI refuses any `--db-path` under the source state root.
* All tests use `tmp_path`; no test or CLI invocation touches the live state
  root when `--state-root` is not supplied against a running server
  (default `--copy` snapshots into a TemporaryDirectory first).
* Frontend is untouched.

## 10. Validation evidence (phase 4)

- `black --check` / `isort --check-only` green on touched files
  (`storage/shadow.py`, `storage/__init__.py`, `storage/json_backend.py`
  unchanged API, `cli/commands/storage.py`,
  `workspace_manager/_persistence.py`, `tests/test_storage_shadow.py`).
- `mypy` green on the 4 production files + the new test file
  (`Success: no issues found in 5 source files`).
- `uv run pytest tests/test_storage_backend.py tests/test_storage_verify.py
  tests/test_storage_shadow.py` → **59 passed** (42 prior + 17 new).
- Manual CLI smoke (see §9.3) confirmed exit 0 on a clean fixture, exit 2
  (Click usage) on a `--db-path` inside the state root, and exit 1 with drift
  detail when the secondary was forced to drop a task row.
- Meta-check: a grep for `Shadow` in `workspace_manager` sources finds no
  references (shadow is never constructed on the hot path); a grep for
  `INDEX_FILE.write_text` / `_workspace_state_file(..).write_text` in
  `_persistence.py` returns zero matches (hot path is atomic).
- Main worktree at `/Users/bytedance/claude_hub` remains at `59fa368` with
  the three protected untracked files untouched; develop base for this
  phase is `98650ba`. No frontend files touched.

## 11. Phase 5 preview

1. Add a long-running opt-in shadow backend wired behind an explicit env/flag
   (`WORKSPACE_STORAGE_SHADOW_DB=/out/of/tree/shadow.db`) for resident/dev
   canaries. Keep secondary failures non-fatal and log drift to a structured
   sink (lessons store or log file) rather than stderr.
2. Let shadow age in canary until drift logs are clean across real
   workloads (target: ≥7 days on develop, ≥3 days on a dev deployment).
3. Wire `_load_state` behind `get_storage_backend()` with the same opt-in
   flag, default still `"json"`, with a `migrate` subcommand that performs
   the export/import/verify round-trip offline before any cutover.
4. Final cutover PR: flip default to `sqlite` behind a CHANGELOG note, keep
   JSON read fallback for one release, then remove raw JSON writes after
   verification.
