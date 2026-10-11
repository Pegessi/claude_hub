# 2026-10-12 Task create recovery dialog and lost reporter keys

Tasks: `3e1a3af1` (Add Task dialog looks broken) and `a5ce0f0e` (an initiator
Task stayed `working` for a day after its work merged).

## Add Task recovery dialog

**Symptom.** Clicking **Create Task** immediately replaced the form with
"Saved request: … needs confirmation. Recover the exact saved request, or
[discard it and edit a new Task]". The discard action rendered as a grey
inline chip inside the sentence, and the primary button read "Recover saved
create request" with a spinner.

**Cause.** The modern create flow stages the request (request key and reporter
key) before the network call so an uncertain response can be retried
identically. The dialog treated "a staged attempt exists" as "recovery needed",
so every normal in-flight create showed the recovery copy.

**Now.**

- While the create request is in flight, the dialog shows a calm
  `Creating “<title>”…` line and a **Creating Task** loading button.
- Only an unconfirmed attempt (failed or reloaded) shows the recovery panel:
  a titled, bordered notice explaining that retry sends the identical request
  and cannot create a duplicate.
- **Discard and start over** is a normal secondary button on the left of the
  action row; the primary button reads **Retry saved request**.

## Lost initiator reporter keys

**Symptom.** Task `a4298e73` was implemented and merged (`a5388a8` is in
`main`), but its status stayed `working`.

**Cause.** The initiator Chat registered the Task with an ad-hoc
`--reporter-key-file` path. A later thread could not find that file, tried a
different key, got 403, and never recorded `completed`. Nothing tied the key
location to the Task.

**Now.**

- `task register` without `--reporter-key-file` stages a new key under
  `~/.claude_hub/reporter-keys/` and, once the Hub returns the Task ID, moves it
  to `~/.claude_hub/reporter-keys/<task-id>.key` (mode 0600). The directory is
  client-side and independent of the checkout; `CLAUDE_HUB_REPORTER_KEY_DIR`
  overrides it.
- `task handoff --execution-control initiator` without
  `--new-reporter-key-file` does the same and replaces the previous epoch's key
  only after the handoff succeeds.
- If the outcome is unknown (transport error or 5xx), the staged key is kept and
  the error prints the exact retry flags, including the call ID for handoff.
  A 4xx rejection deletes the staged key. A staged file passed back explicitly
  is still installed at the default path on success.
- `task progress` without `--reporter-key-file` reads the Task's default file;
  if it is missing, it names the path and sends nothing.
- `task context` reports `reporter_key_file` (the path only) for initiator
  Tasks whose default file exists.
- Runtime guidance tells agents to omit the key-file options.

Explicit key-file options keep their previous behavior. Keys are never printed.

This change does not retroactively close `a4298e73`: its key is still lost, and
another Chat's credential must not be tried. Closing it is a human
manual-progress decision based on the merge evidence.

## Validation

- CLI tests: default register installs the key and progress finds it; unknown
  outcome keeps a retryable staged key and the explicit retry installs it; a
  4xx rejection removes it; a response without a Task ID names the staged file;
  progress without a default key sends nothing; handoff replaces the key only
  after success; context reports only the path.
- Existing explicit-path, permission, and secret-redaction tests still pass.
- Browser smoke `tests/browser_agent_workspace_task_create.py` uses the new
  button labels and captures the recovery dialog.
- Full backend suite passes except the pre-existing
  `test_service_restart.py::test_frontend_build_timeout_keeps_current_backend_and_dist`,
  which fails only under full-suite load on this macOS host and passes alone.
