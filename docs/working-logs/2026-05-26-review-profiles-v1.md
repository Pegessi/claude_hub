# Review Profiles V1

## System Overview
Review Profiles V1 makes reviewer evidence more explicit without changing the
existing Direct, Reviewed, Autonomous, or final human acceptance workflow.
Reviewer assignments now infer enabled review lenses from task mode, task/report
metadata, changed files, attachments, strictness, artifact policy, and report
text.

The first supported profiles are `general`, `code`, `ui`, `artifact`,
`delivery`, and `boundary`. They are additive metadata: old tasks and reports
load without profile data, and reviewers can still post the existing
`review_passed`, `review_failed`, or `review_needs_input` verdicts.

## Module Design
- `models/schemas.py` defines review profile enums, profile-result evidence,
  task policy fields, and report/evaluation metadata.
- `workspace_state_policy.py` owns pure profile inference and prompt guidance
  text.
- `WorkspaceManager` normalizes legacy profile fields, injects enabled profiles
  into reviewer prompts, reads bounded `REVIEW.md` guidance from the workspace,
  and preserves reviewer profile results in reports and autonomous evaluation
  records.
- `AgentWorkspaceView.vue` renders configured review profiles, report profile
  results, artifact refs, confidence, and autonomous evaluation profile
  summaries.

## Key Issues / Pitfalls
- Profiles are not an automatic reviewer engine. They make expected evidence
  explicit and auditable, but V1 still depends on the reviewer agent to inspect
  the artifacts and report structured findings.
- `REVIEW.md` discovery is deliberately bounded to the workspace root and
  ancestors of changed files. This avoids expensive scans and prevents
  out-of-workspace guidance from being pulled into the prompt.
- `require_artifact_review` now has prompt-level enforcement through the
  `artifact` profile, but visual/model scoring remains a future extension.

## Validation
- `cd backend && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_workspace_state_policy.py tests/test_workspaces.py -q`
  - 109 passed, 2 existing Pydantic deprecation warnings.
