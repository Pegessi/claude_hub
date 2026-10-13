# Create Session directory and Codex picker

The native working-directory `datalist` applied the browser's prefix filter
to a prefilled path, hiding other recently used directories. The Create
Session dialog now has an explicit path list. It combines target-scoped recent
history with directories from open sessions on the same local host or remote
profile, deduplicates them, and shows them in a scrollable list. Opening the field
shows the common paths regardless of its current value; typing filters by
substring. Pointer and arrow/Enter selection keep manual path entry and the
existing Browse action available.

The Codex Session picker previously mounted expanded and immediately requested
`/api/codex/sessions` whenever Codex was selected. It now starts collapsed
with a fresh session selected. The first explicit expansion loads the list;
later expansions reuse it, and Refresh remains available when expanded.

The full Chat browser mock now checks the prefilled-path case, an eight-entry
history plus open Workspace paths, non-prefix filtering, direct and keyboard
path selection, no Codex request before opening, one request on opening, reuse
after collapsing, and mobile dialog reachability.
The API is mocked in this test; it does not prove live Codex discovery.

Validation: the isolated browser-mocks gate passed 9/9 with its Vite port
closed afterward. Frontend lint, types, 777 unit tests and production build
passed; docs and backend format checks passed. `verify.sh all` reached 2,138
passing backend tests before the unchanged service-restart timeout test failed:
macOS returned `Operation not permitted` when it tried to start its simulated
frontend build child. A focused run reproduced the same environment failure.
The changed UI was also inspected from desktop, dropdown-open and mobile
browser captures. The main Hub service and real Codex API were not exercised.
