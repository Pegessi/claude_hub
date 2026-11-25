# Cursor ACP recovery

A persisted turn showed an inline retriable transport error followed by thinking and tool events, then a failed completion. Persisted normalized events do not retain the raw RPC response, so its precise ending cannot be reconstructed. Defer the retriable error until the prompt finishes: substantive parent output followed by success permits completion; an RPC error, process exit, or success without recovery fails with the original detail. Other inline errors keep their existing behavior.

Cold initialization already retries once with a fresh process and repeats network selection. Preserve redacted stderr from each timed-out attempt in diagnostics; keep the existing timeout budget and fail closed after the retry. Regression tests simulate recovery and both failure paths without contacting a provider.
