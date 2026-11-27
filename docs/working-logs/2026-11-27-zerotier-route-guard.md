# ZeroTier route guard IFA and probe repair

Task: `5401621f-b1f2-4381-ab57-8aed47b48696` (reviewed Workspace execution).

## Scope and mechanism

The deployed macOS guard accepted physical split routes solely by gateway and
interface. A DHCP address change could leave a stale route IFA unnoticed. An
unbounded `route get` had also held a previous cycle for more than two days.
This change versions the existing guard source under `scripts/ops/`; Hub does
not install or start it automatically.

- Inspect the verbose exact `/1` route replies, including IFA, and compare with
  the current DHCP address. Refresh only existing owned routes using `-ifa`.
- Keep the physical sentinel veto, single physical exit, remote ZeroTier entry,
  VPN conflict, cooldown and route ownership checks. A clean Dynamic Store
  permits only owned stale-IFA repair, never generic missing-route recovery.
- Bound external network/IPC commands with a macOS system Perl watchdog using
  a private process group. Kill descendants at the deadline; a lock-local marker
  carries failures out of subshells and pipelines and closes mutation gates.
- In observe mode, DNS key stripping only records intent. Keep existing IPv6
  fallback configuration and behavior. Remove disruptive DNS cache/daemon
  recovery after HTTP failures. Record mihomo acceptance on port 7890.
- Inventory existing interfaces before querying registered services: an unplugged
  adapter may remain in network service order. Failure to read the inventory or
  an existing interface still closes the gate.

## Validation and operational evidence

The full-script fixtures replace every network command, configuration, state and
lock path. They require no sudo and cannot touch production network state. They
cover stale/dynamic IFA, idempotence, unknown ownership/IFA, conflicts, remote
entry, ambiguous exits, observe DNS/IPv6 boundaries, deadlines, next-cycle
recovery, duplicate routes and registered absent adapters. The two original
stale-IFA/observe-DNS regression tests fail against the pre-deployment source.

An independent read-only reviewer reproduced two initial safety-gate findings;
both were fixed and regression-tested before deployment. The reviewer also
verified descendant cleanup after a stuck probe and successful next-cycle work.

The first live candidate conservatively exited on a registered but absent USB
adapter. This interrupted DNS maintenance and a system DNS failure was observed.
The original root backup was atomically restored without a service restart;
normal DNS maintenance resumed. The interface-inventory fix adds explicit
regressions and a real-host observe check with isolated state before redeploy.
The failed candidate, failed cycles and successful rollback remain evidence.

Machine-local evidence, full hashes, exact rollback and final acceptance live in
`~/.claude_hub/dot/artifacts/network-repair-20261127T080853Z/guard-hardening/`.
No credentials or complete operations document are retained. `verify.sh all`
was executed: docs and checkout-safety passed; seven application targets could
not run because the assigned checkout lacks backend/frontend dependencies.
Those results are not an application-suite pass.

Apple/Google account login and subscription acceptance are outside this task.
Operational pre-deployment review, Workspace review and human acceptance are
separate gates. No main-branch merge or service restart is part of this change.
