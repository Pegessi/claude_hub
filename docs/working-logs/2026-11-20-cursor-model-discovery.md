# Cursor model discovery recovery

The reported nine entries match the static emergency catalog exactly. On this
host, `agent --list-models` succeeded with 36 raw IDs, while the live Hub
capability endpoint returned 34 grouped options from a successful cached probe.
The command's catalog can change between probes; the live response and the
subsequent CLI command are not a simultaneous comparison. The original failure
was silent, so its specific cause cannot be reconstructed from these observations.
The picker displays the supplied options without an empty-search filter, and
keeps an unlisted current model visible; grouping retains provider IDs as effort
options. The cache uses ten minutes for successes and one minute for failures.

Retain the last catalog across transient failures, mark it non-cacheable so
clients can retry, retry when a Cursor picker with non-cacheable metadata opens,
and log probe failure categories without subprocess output or credentials. A
cold failure still uses the emergency catalog. A successful later probe replaces
it. Existing custom model input remains available.
