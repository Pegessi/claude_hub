# Recovering an undelivered reviewer verdict

The existing supported `claude-hub api raw` command can resubmit a reviewer
report through the authenticated, session-scoped reporting API. No new command
or permissive route alias is needed. Use `claude-hub api raw --help` for its
`METHOD PATH --payload-json JSON` interface and `claude-hub --help` for server
and authentication options.

Use this only when the original reviewer verdict is visible in its log and
has not reached the Hub. Read the current task and reports first; confirm the
same task, assigned reviewer session, review cycle and pending plan gate.
Preserve the original verdict, evidence and stable `call_id` exactly. If the
failed attempt omitted `call_id`, assign one containing the task and current
review cycle, record that recovery decision, and reuse it on every retry.
Do not turn a pending, failed or uncertain review into `review_passed`.

1. Select the actual Hub server. CLI precedence is `--base-url`,
   `CLAUDE_HUB_URL`, configured `base_url`, then the default. Verify task identity
   with `claude-hub --base-url "$HUB_URL" task get "$TASK_ID"` before writing.
   During the 2026-10-21 incident investigation, the configured Hub was
   `http://[::1]:8173`; the dispatch URL at port 11004 returned 404. A 404 can
   therefore mean the wrong service as well as the wrong path. Do not blindly
   reuse a dispatch port. Keep normal configured CLI authentication (or use
   the documented token/cookie options); do not bypass authentication.
2. Save the original report JSON as `review-verdict.json` in the task worktree.
   It must include `task_id`, `state: "review_passed"`, `call_id`, `message`,
   `message_en`, `message_zh`, and the original structured review evidence.
   Preserve review/profile fields and any acknowledgment IDs from the original.
3. Submit to the **reviewer session**, never the worker session:

   ```bash
   claude-hub --base-url "$HUB_URL" --json api raw POST \
     "/api/workspaces/sessions/$REVIEWER_SESSION_ID/reports" \
     --payload-json "$(cat review-verdict.json)"
   ```

   `/api/sessions/.../preview-report` is not a reporting endpoint and remains
   404. Do not change routes on retries. `api raw` exits nonzero for API errors;
   inspect its response rather than treating terminal output as delivery.
4. Verify the returned persisted report's task, session, state and `call_id`,
   then read the current task again to confirm the plan gate advanced. Reuse
   the same payload and `call_id` if delivery is uncertain; a conflicting
   payload with the same ID is rejected. Do not create a second review round.

This recovery uses the normal report validation and current review-round
handling; it does not skip implementation review or human acceptance. An old
verdict from another cycle is not authority to approve the current packet.
If the task/cycle/session no longer matches, stop recovery and request a fresh
review through normal task controls.

`backend/tests/test_review_resubmit_cli.py` exercises this exact public CLI
surface with a mock transport: explicit IPv6 server selection over a stale
port, configured authentication, canonical route, stable payload and call ID,
and visible errors for rejected/missing/conflicting submissions. It performs
no live report mutation.
