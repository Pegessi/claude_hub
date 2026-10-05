# Review placement and evidence-backed feedback

## System overview

This applies the verification and governance parts of the
[Agent OS source document, revision 8](https://bytedance.larkoffice.com/docx/GuXNdeQvEoyHRBx1aNUcpVMJngb)
to existing review dispatch and feedback mechanisms. It does not introduce a
second reviewer, scoring service, or approval policy.

The prior local reviewer selection compared only execution target. A worker in
a feature worktree could reuse an idle reviewer in the primary checkout or a
different worktree. Repository identity alone does not identify the code being
reviewed. Both reuse paths now require equivalent resolved local execution
directories; new reviewer creation already follows the worker directory.

The feedback reaper previously saw iteration counts and the final task summary,
but lost the reports explaining why review failed. A successful final summary
is insufficient evidence for a reusable lesson. Automatic multi-task lesson
validation also permitted one real iteration record plus an absent task ID to
obtain the multi-task confidence cap.

## Module design

- `_review.py` and `_tmux_queries.py`: match the existing assigned reviewer and
  the idle reviewer pool to the worker execution cwd. Symlinks and normalized
  paths are equivalent; distinct worktrees are not. A legacy empty reviewer cwd
  means only the workspace root. Remote profile/cwd matching remains unchanged.
- `FeedbackFailureEvidence` and `FeedbackTaskDigest`: additive digest excerpts
  contain only source report ID, state, message, validation, and risks. Keep the
  latest three failure/blocker reports with per-field character caps. A missing
  field in an old cache defaults to an empty list.
- `feedback_lessons.py`: task records produce the excerpts and prompt serialization
  re-applies bounds. Automatic lesson creation requires every cited record to
  exist, have a valid report list, and match the requested task and workspace.
  Task IDs are literal strings, not filesystem patterns. Legacy records without
  workspace IDs remain valid; explicit conflicting identities are rejected.
- `_feedback.py`: counts qualify a candidate for extraction but do not prove a
  cause. The reaper should ground actionable advice in source evidence, use one
  supported five-layer tag, and emit no lesson where the causal evidence is weak.
  Input text remains untrusted data. Existing global prompt trimming and delayed
  processed-record commits still protect the budget and preserve carry-over.

## Key issues and limits

- Cwd placement protects against reviewing the wrong checkout at dispatch. It
  does not pin a Git SHA or prevent a user changing an existing terminal's cwd.
  Reviewer context clearing and terminal seat guards remain the existing policy.
- Excerpts are bounded evidence, not a replay of every report. Environment maps
  and unrelated record fields are never copied. Free-text report content retains
  its existing trust boundary; this change is not a general secret-redaction tool.
- Prompt guidance does not mechanically establish causality or enforce tags.
  Archived record identity verifies citation provenance, not the truth of an
  agent's reported observation. Manual confirmed reaper promotion can still
  bypass iteration checks and cite an unarchived task, with confidence capped.
- Existing active lessons are not invalidated or rewritten. Incremental summaries
  do not replay already-processed tasks just because prompt version became 6.
  An explicit full summary rereads archived records to populate the new evidence.
- No live Hub, production tmux server, or existing workspace state was used.
  Tests use the repository's isolated pytest runtime and socket.

## Validation

Run from this branch's `backend` directory with the available interpreter and
explicit source path, so the shared installed editable checkout cannot mask changes:

```sh
PATH="/opt/homebrew/bin:$PATH" PYTHONPATH="$PWD" \
  /Users/bytedance/claude_hub/backend/.venv/bin/python -m pytest -q \
  tests/test_reviewer_worktree_placement.py tests/test_feedback_evidence.py \
  tests/test_feedback_lessons.py tests/test_remote_agent_pipeline.py \
  tests/test_workspace_state_policy.py
```

New tests exercise dispatch through both assigned/pool reuse paths, creation in
the worker directory, equivalent symlink paths and legacy root behavior;
missing/corrupt/foreign evidence, literal IDs, and manual confirmation;
record-to-cache-to-prompt evidence preservation, field bounds, and prompt-budget
carry-over. They complement existing remote reviewer, feedback, and state-policy
coverage. No real model-driven review or browser E2E is claimed.

The initial broad run including all `test_workspaces.py` was intentionally stopped
after 73 passing tests in 111.81 seconds because it entered unrelated slow session
bootstrap tests. The focused suite is the acceptance run. Initial new-test fixture
errors were fixed before that run; the initial shell also lacked tmux in PATH,
which was corrected for the isolated pytest cleanup hook.

A second extended run including cold-restart tests was stopped at 109.76 seconds
after 166 passing tests while the final unrelated session bootstrap test remained
in flight. This is partial cold-restart evidence, not a complete suite pass.
The final acceptance command above excludes those slow lifecycle scenarios.
Result: **164 passed, 2 existing Pydantic deprecation warnings, in 1.05 seconds**.
Black and isort checks passed for all eight changed Python files; mypy passed
for `feedback_lessons.py` and `models/schemas.py`.
