# Backend log growth containment

## Incident

The long-running local backend had a single
`~/.claude_hub/logs/backend.log` file of about 29.35 GB. A five-second sample
showed 144,982 new bytes, or roughly 29 KB/s. The backend process serving port
8173 was the only process holding the file for writing.

In the most recent 10,000 lines, the two INFO messages emitted by
`TTYDManager.list_tabs()` occupied 61,767,989 of 62,507,009 bytes (98.8%).
Every poll serialized the complete persisted tab ID list, process ID list, and
active tab name list. The file handler was a plain `logging.FileHandler`, so
the documented rolling log had no actual retention boundary.

## Change

- `claude_hub.main` now uses `RotatingFileHandler`. Defaults are 10 MiB per
  file and five backups, bounding steady-state backend log storage to about
  60 MiB. `BACKEND_LOG_MAX_BYTES` and `BACKEND_LOG_BACKUP_COUNT` override the
  defaults.
- The file handler is attached only after acquiring the backend instance lock
  and is closed before releasing it, so a rejected duplicate cannot rotate the
  live owner's log out from under its open file descriptor.
- `TTYDManager.list_tabs()` emits only an active-tab count at DEBUG level. The
  method's ordering, archive filtering, and returned schema are unchanged.

The first write after upgrading rotates an already oversized active file using
the standard numbered-backup behavior. Existing oversized history therefore
still requires an explicit operator cleanup if disk space must be reclaimed
immediately; startup does not silently discard it.

## Regression coverage

- Regression tests exercise an actual first-write rollover, reject log rotation
  before instance-lock ownership, and cover configuration defaults, environment
  overrides, and invalid limits.
- `test_list_tabs_does_not_emit_full_state_at_info` uses a real tab to check that
  the polling hot path is quiet at the normal production log level. Existing
  list/archive tests continue to cover return behavior.

## Verification

- Focused log, lock, configuration, and tab-list regression scope: 7 passed.
- `verify.sh all`: 7 of 8 targets passed, including docs, backend format/types,
  and all frontend checks (701 tests). The backend target stopped after 1,798
  passes at the unrelated frontend-build process-group timeout test because
  macOS returned `Operation not permitted` while starting its helper process.
  The failing test passed immediately when rerun alone.
- Independent staged-diff review passed after the instance-lock ordering fix.
