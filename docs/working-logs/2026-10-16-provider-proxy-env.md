# Private provider proxy defaults

## Requirement and boundary

Different Hub deployments need different provider exits. One remote deployment
needs a Cursor proxy while the local deployment should retain its current
networking. Real proxy hosts and credentials belong in untracked backend/.env,
never in the public repository. This change does not configure or restart a
shared service and does not merge main.

Base: `4dff0c52555524b07c2b3472833f44eae7665426`.
Branch: `codex/provider-proxy-env`.

## Ownership

- Settings owns startup parsing of four private proxy URLs and bypass lists.
  Secret values are excluded from repr/serialization; validation messages omit
  raw inputs. URLs accept HTTP(S) CONNECT or `direct`; empty inherits.
- `services/provider_proxy.py` composes process, deployment and explicit session
  layers without mutating process or session state. Scoped configuration keys
  are stripped from child environments, including other providers' credentials.
- Native Chat environment construction and Cursor catalog probing use that
  helper. Existing route selection and global catalog cache semantics remain.
- Local Terminal wrappers and Claude launch settings apply the same defaults
  through protected files, preserving remote SSH isolation and tab serialization.
- Cursor's model-probe deadline is a positive finite startup setting, default 5s.
- Native stderr is sanitized before exit-error logging/API publication; structured
  error diagnostic fields use the same private-proxy redaction. Ordinary message
  text and Terminal raw output retain their existing behavior.
- Checkout safety rejects private root/backend .env snapshots without reading
  or printing their contents. The tracked example has only empty proxy fields.

## Acceptance evidence

Focused tests cover real temporary .env loading, provider isolation, source
precedence, explicit empty/removal overrides, direct mode, invalid URL redaction,
native catalog/request environment agreement, Terminal child execution, private
file modes, remote isolation and forced Git additions. A real local Python child
prints a fictional configured proxy to stderr and exits with an error; the
runtime diagnostic is checked for host/credential removal.

Independent review identified two issues, both fixed and rechecked: `ls-tree`
does not expand the `.env.*` patterns accepted by `ls-files`, and provider stderr
could otherwise expose the private proxy in runtime error details. The final
focused configuration/security suite passes 61 tests, also independently rerun.
The reviewer reported no remaining confirmed actionable defects in this scope.

`scripts/verify.sh all` passed docs, checkout safety, backend formatting/types,
frontend lint/types, all 870 frontend tests and production build. The backend
default suite initially stopped at an existing merge-audit test because Apple
Git 2.39.3 lacks `merge-tree --merge-base` (2055 passed, 1 skipped before stop).
A task-private Git 2.51.0 was built under ignored `backend/.venv/task-tools` for
the backend rerun; system Git was left untouched. Two subsequent default-scope
runs each reached 2571 passed / 1 skipped before the unchanged
`test_frontend_build_timeout_keeps_current_backend_and_dist` failed: its operation
message reported `Operation not permitted` instead of the expected build timeout.
That test passes standalone (1 passed). The precise source of the suite-only
permission error is not established. The default suite is therefore not green.
Remaining modules from `test_service_restart.py` onward are checked separately,
with that separately passed timeout test deselected; this is focused continuation
coverage, not a default-suite pass.
The focused continuation finished with 1050 passed / 1 skipped / 1 deselected
in 185.00s. Together with the earlier default runs and standalone recheck, this
executes the remaining modules without hiding the default-scope failure.

Staged-snapshot safety and `git diff --cached --check` passed. A private check of
the staged blobs found none of the real proxy hostnames used in the investigation.
No actual deployment configuration, shared service or system tool was changed.

Local toolchain: Python 3.11.16 and pnpm 9.15.9; uv 0.11.3 and Node 24.14.0 differ
from the declared uv 0.12.15 / Node 24.21.0 pins, so these results are not
CI-equivalent. No dependency lockfiles changed.

Evidence: `/tmp/ch-provider-proxy-verify-final.log` (all targets),
`/tmp/ch-provider-proxy-backend-final.log` and
`/tmp/ch-provider-proxy-backend-confirm.log` (Git-compatible default reruns),
`/tmp/ch-provider-proxy-restart-recheck.log` (standalone restart case),
`/tmp/ch-provider-proxy-backend-tail.log` (remaining modules), and
the verification-owned runtime directories printed by each invocation.

No real proxy address/credentials are included in this log or test fixtures.
Live external model inference and deployed service acceptance remain outside
this implementation's validation scope.
