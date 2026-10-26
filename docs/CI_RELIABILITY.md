# CI reliability: an execution contract for agents

Read this when adding tests, changing fixtures or verification, or investigating
a slow, flaky or cancelled check. The goal is a short, reproducible feedback loop
with enough evidence to choose the next action. Use the existing
[verification entry point](../CONTRIBUTING.md#5-run-validation); install its
declared tools and locked dependencies separately.

## Choose the smallest meaningful loop

| Change or claim | Run / evidence boundary |
| --- | --- |
| One backend behavior | `./scripts/verify.sh backend-tests -- tests/test_OWNER.py -q`; replace `OWNER` with the owning module. Focused results prove that selection only. |
| Backend fixture, collection policy or shared test helper | Run its focused regression, then `./scripts/verify.sh all`; inspect the default suite for selection and resource leaks. |
| UI or real terminal behavior | Use the explicit gates in [UI acceptance](UI_ACCEPTANCE.md), including their owned runtime and browser requirements. `all` does not run them. |
| CI workflow, timeout or output handling | Exercise the same command, output path and toolchain on the remote candidate; a local pass is supporting evidence. |
| Provider/OAuth or other live integration | Use an explicitly authorized integration check. Default tests use doubles and owned loopback services, without developer credentials or live providers. |

Prefer extending the existing behavior-owning test to adding overlapping tests.
Use the least expensive resources that can prove the contract: in-process
doubles first, owned local processes when required, real browser/end-to-end
checks for claims that depend on them. Do not replace a necessary integration
check with a mock to reduce its runtime. Keep collection/import work cheap;
create costly resources in fixtures with explicit cleanup.

Run targeted checks while editing. After a validated candidate passes, repeat or
broaden checks only for changed code, a failure, an unresolved concern or an
explicit required gate. Final handoff still follows the repository's `all` and
independent-review requirements.

## Keep output bounded and actionable

The collection plugin [tests/ci_health.py](../backend/tests/ci_health.py) rejects
full pytest node IDs longer than 512 characters before verbose case reporting.
This applies to the ordinary local/CI pytest paths through `tests/conftest.py`,
including collection-only runs. Failure exits with pytest usage-error status 4
and a JSON diagnostic containing `code: CI_NODE_ID_TOO_LONG`, the limit, count,
and at most five file/line/length examples. The diagnostic omits the actual IDs.
On rejection it clears the pending items before aborting, so collection-finish
reporters cannot echo the rejected names. An early collection-finish hook also
checks partial collections, including when `-x` stops on an import error before
the normal item-modification hook. This remains a failed collection, never an
empty-suite success. Ordinary collection errors keep their original failure
behavior when the collected IDs are within budget.

512 is this repository's readability budget, not a GitHub platform limit or a
test-data size limit. At adoption, the default collection had 3569 cases and a
maximum ID length of 318 characters. The code constant is the authority; a
policy change needs review and a demonstrated failing regression, not an
environment bypass or automatic truncation of test identity.

Use `pytest.param(value, id="meaningful-case")` or explicit `ids=` for binary
data, nested JSON and other large values. Keep the original input and assertion
that prove the boundary. Never embed credentials or user content in case names.

The guard covers collected IDs, **not arbitrary stdout, log messages, assertion
representations or import-time output**. Summarize large payloads before logging:
report type, length and relevant safe metadata. Keep detailed, sanitized evidence
in a task-owned file when necessary. `tail -n` and `rg -m` limit lines, not their
length; do not stream an unknown log into the conversation. A safe first probe is:

```bash
python - "$LOG_PATH" <<'PY'
import json, sys
lines = maximum = total = 0
with open(sys.argv[1], "rb") as source:
    for line in source:
        lines += 1
        total += len(line)
        maximum = max(maximum, len(line))
print(json.dumps({"lines": lines, "bytes": total, "max_line_bytes": maximum}))
PY
```

Set `LOG_PATH` to the owned evidence file. This probe prints metadata only; it
does not redact a log or authorize uploading it.

## Measure cost before changing budgets

Pytest's checked-in options report the ten slowest setup/call/teardown phases
lasting at least one second, using timing already collected by pytest. The
display cutoff is diagnostic, not a pass/fail threshold. Inspect this output
from the required run before starting another full suite just to profile it.
For a narrower investigation, the existing focused entry also accepts
`--durations=20 --durations-min=0.1` after its explicit test path.

When a new test or fixture materially increases cost, report its resource use
and the comparable before/after timings. Compare the same selection, runner,
toolchain, cache conditions and timeout scale. Separate queue/setup time from
test execution, and setup/teardown from the test call. One shared-runner sample
or a local/remote ratio is insufficient to set a hard performance gate. If noise
could change the decision, use bounded paired repetitions and report the sample
count and spread rather than rerunning until green.

Use deterministic counts or work bounds for a hard gate where they prove the
requirement. Add wall-time gates only with a stated performance requirement,
representative repeated measurements and an owner for recalibration. Choose
cleanup/upload headroom from observed cost when changing a job budget; do not
invent a universal number or move every slow test out of the required suite.

## Diagnose failure before changing the gate

| Observation | Next action |
| --- | --- |
| `CI_NODE_ID_TOO_LONG` | Follow the file/line diagnostic; supply a short ID and preserve the payload. Re-run collection or the owning module, then the required suite. |
| Assertion, setup or collection failure | Preserve the first failing result and exact command; reproduce the owning path and fix the cause. A retry that passes does not erase the failure. |
| Cancellation / no progress | Read job annotations and step state first: distinguish timeout, manual/concurrency cancellation, runner loss and failure before startup. Missing test output does not prove a deadlock. |
| Slow run | Inspect slow-phase output, fixture/process waits and log sizes; compare like-for-like evidence before changing timeouts or scope. |
| Suspected flake | Preserve failure evidence and use a bounded reproduction plan. Track the root cause; any retry, quarantine, skip or timeout relaxation needs an owner, reason and removal/recheck condition. |

New waits, network calls and subprocesses need finite deadlines and task-owned
cleanup. Prefer waiting for an observable condition to fixed sleeps. Bound
process-group cleanup where children are possible, and prove unrelated services
survive the failure path. Never kill shared processes or use the main Hub as a
test target. A test of timeout handling must demonstrate the timeout and cleanup,
not merely check that the configuration contains a number.

The current `faulthandler_timeout=60` emits thread stacks; it does not terminate
a stuck test. The backend job has a job-level deadline, and `verify.sh` retains
logs locally through `tee`. The default backend job does **not** currently upload
that evidence as an artifact or enforce an inner pytest deadline. Runtime-producing
verify targets now separate retained evidence from disposable runtime directories.
INT/TERM allow up to 60 seconds for the target's own finalizers before bounded
residual process-group termination; this is a shutdown budget, not a test deadline.
CI browser/HMR commands use `--kill-after=90s`, covering that 60s plus up to 9s
for group termination/reaping and 21s reporting/scheduling margin. The outer hard
kill must not coincide with the start of residual cleanup. HMR backend/Vite logs
are written directly into retained `terminal-hmr/` evidence, not pytest tmp_path.
Pytest targets receive group SIGINT so finalizers can run, while the command
returns 130/143 for the original signal. Cleanup and log/report errors must not
overwrite an existing nonzero test status. See
[runtime cleanup and retained evidence](../CONTRIBUTING.md#runtime-cleanup-and-retained-evidence)
for explicit retention, Linux process inspection and hard-kill limitations.

When implementing stronger timeout handling, terminate the test child while
keeping the collector alive, preserve its nonzero exit and allow cleanup before
the outer job deadline. Test both normal success and a real stalled child; verify
the retained stack/log/exit evidence. `always()` uploads are best-effort and cannot
guarantee retention after runner loss or a hard job termination. Coordinate such
workflow changes with their owner; the ID guard does not implement them.

## Handoff evidence, not a green adjective

Put a compact receipt in the existing Task/PR evidence; link owned logs rather
than pasting them. Use `null` for unknown values, and separate required gates
from optional diagnostics. These fields are a reporting template, not a new
runtime store or an enforced JSON schema:

```json
{
  "head_sha": "<exact verified commit>",
  "base_sha": null,
  "cwd": "<task worktree>",
  "worktree_dirty": null,
  "check": "backend-tests",
  "scope": "default|focused|integration",
  "command": ["./scripts/verify.sh", "backend-tests"],
  "environment": {"runner": "<OS/type>", "toolchain": "<pins or drift>", "timeout_scale": 1},
  "result": {"exit_code": null, "passed": null, "failed": null, "skipped": null, "elapsed_seconds": null},
  "attempts": 1,
  "evidence": ["<owned artifact path or run URL>"],
  "unverified": []
}
```

Set `base_sha` to the compared commit for a cost comparison; leave it `null`
when no comparable baseline was measured. Record the working directory and
dirty state; if verification includes uncommitted changes, link the exact patch.
A HEAD SHA alone does not identify those changes.

The reviewer verifies the final SHA and required scope, preserved assertions and
inputs, added cost, actual failure-path evidence, and any changed gate or retry
policy. A skipped, timed-out, missing or not-yet-run check is not a pass. Do not
silently add `continue-on-error`, selection exclusions, retries or looser budgets
to obtain green status.

## Basis and boundaries

This policy adapts primary sources to the existing repository; it does not adopt
another organization's timing limits, test percentages or infrastructure:

- [Software Engineering at Google: testing overview](https://abseil.io/resources/swe-book/html/ch11.html): resource size and behavioral scope are different; prefer the least costly reliable proof.
- [Google: flaky tests](https://testing.googleblog.com/2016/05/flaky-tests-at-google-and-how-we.html): retries/quarantine can reduce noise while hiding defects and increasing feedback time.
- [pytest parameter IDs](https://docs.pytest.org/en/9.0.x/example/parametrize.html#different-options-for-test-ids), [output](https://docs.pytest.org/en/9.0.x/how-to/output.html), [durations](https://docs.pytest.org/en/9.0.x/how-to/usage.html#profiling-test-execution-duration), and [fault handling](https://docs.pytest.org/en/9.0.x/how-to/failures.html#fault-handler): use the locked tool's existing ID and diagnostic facilities.
- [GitHub job deadlines](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idtimeout-minutes) and [artifacts](https://docs.github.com/en/actions/concepts/workflows-and-actions/workflow-artifacts): execution limits and retained evidence are distinct mechanisms.
- [AGENTS.md format](https://agents.md/): keep agent entry instructions discoverable and give concrete test commands; this guide holds the task-specific details.
