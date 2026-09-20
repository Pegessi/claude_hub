# Workspace Agent Bootstrap Submit Recovery

## System overview

Creating a managed workspace agent starts a terminal tab, persists its
`ManagedSession`, and then sends a multi-line bootstrap prompt through tmux.
The bootstrap must be accepted by the agent TUI before the create request can
be considered successful.

## Failure mode

The Hub previously submitted pasted prompts with tmux `send-keys C-m`. With
`extended-keys on` and `extended-keys-format csi-u`, tmux preserves that as a
Ctrl-M key chord. Claude Code distinguishes it from the semantic Enter key, so
the pasted-text placeholder could remain in the composer through every retry.
The send then raised after the terminal and managed session had already been
created, leaving an idle session behind while the API returned an unclassified
500 response.

## Fix

- All prompt-submit paths use tmux `send-keys Enter`: initial delivery, the
  receipt-gated atomic command, receipt recovery, and monitor retries.
- A failed bootstrap compensates the partial create by removing the managed
  session, clearing workspace pointers that reference it, persisting the
  corrected state, and best-effort deleting the terminal tab.
- The API records the chained exception and returns HTTP 502 with a stable
  initialization-failure detail. Cleanup errors are logged without hiding the
  original delivery failure.

## Verification

Regression coverage asserts semantic Enter for both delivery implementations,
the monitor retry path, and full session/tab rollback on bootstrap failure. The
real-tmux receipt suite continues to verify at-most-once paste behavior.
