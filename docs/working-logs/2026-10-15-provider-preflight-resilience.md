# Provider preflight timeout resilience

## Scope and evidence

Task `3362a288-8b45-4493-bff4-3497cf191531`, initiator controlled. Base `45c552c`,
worktree `~/claude_hub_worktree/provider-preflight-resilience`, branch
`fix/provider-preflight-resilience`. The primary checkout has unrelated local
changes and was left untouched. No production restart, merge or deployment is
part of this change.

A real configured Claude gateway returned 200 for authenticated model discovery
and a one-output-token request, but its unauthenticated proxy HEAD took 3.37
seconds. The existing three-second preflight rejected the turn before launching
the provider, reporting both direct and proxy timeouts. A per-chat `inherit`
workaround restored that conversation before this source repair.

## Behavior

Automatic selection still prefers a reachable direct route and then a reachable
configured proxy. When the effective route times out, it preserves the caller's
environment instead of declaring the provider unavailable. This includes direct
timeouts when no proxy applies or `NO_PROXY` excludes it. Proxy configuration is
validated before fallback. Explicit `direct` and `proxy` modes, malformed or
conflicting configuration, and non-timeout failures remain strict.

Timeout fallback logs a redacted `timeout_inherit` warning and uses the existing
five-second failure-cache TTL rather than the thirty-second success TTL. It
neither increases the HEAD timeout nor sends/retries any model request itself.
An actual provider request can still fail and reports its own result normally.

Independent review found that Claude settings generation previously used
`lowercase or uppercase`, which could restore a proxy disabled by an explicitly
empty lowercase value. The generated settings now use lowercase key presence,
matching route selection. Existing base settings are not rewritten by selection.

The current contract is also recorded in the architecture's environment/proxy
section, replacing its obsolete claim that backend import clears global proxies.

## Validation

- Before the repair, the selector regressions failed with the original preflight
  error; the native launch regression separately reproduced that same failure.
  Evidence: `/tmp/claude-hub-verify.XYc6Ja` and
  `/tmp/claude-hub-verify.RnzEqG`.
- After repair, the selector and native transport suites passed 223 tests.
  Evidence: `/tmp/claude-hub-verify.sLmRih`.
- A local HTTP proxy integration test holds HEAD past its test deadline while
  accepting exactly one subsequent POST, proving an inconclusive probe cannot
  block a working request. It uses no real model, API key or external endpoint.
- Native tests verify one prompt launch, unchanged configured model/credentials,
  retained proxy settings and explicitly disabled proxy precedence. Selector
  tests cover redaction, forced modes, NO_PROXY, configuration validation, known
  proxy failures, fallback cache expiry and recovery to a proven direct route.

- `CLAUDE_HUB_VERIFY_STRICT_VERSIONS=1 ../scripts/verify.sh all`, run from the
  worktree's `frontend/`, passed every standard target: docs, committed-checkout
  safety, backend formatting, backend types (252 files), default backend tests,
  frontend lint/types/tests and frontend build. Locked Python 3.11.16, Node
  24.21.0, uv 0.12.15 and pnpm 9.15.9 were used. Backend: 3335 passed, 7 skipped;
  frontend: 781 passed. Skips were the opt-in browser cases and clean-delivery
  Git-provenance case. Existing Pydantic deprecation/build-size warnings remain.
  Log: `.agent_office/provider-preflight-resilience/verify-all.log`; isolated
  backend evidence: `/tmp/claude-hub-verify.OU7WmG`.
- Final independent read-only review found no remaining actionable issues and
  independently passed the two new local proxy/native precedence regressions.
  Evidence: `/tmp/claude-hub-verify.j9rAO7`. The reviewed tracked code/current
  architecture/changelog diff SHA-256 was
  `ec319b3ccff6248344f1ac385dbdaff8ae12f6714862e5b064ad598c2d29fa03`;
  it was rechecked unchanged before recording these final results.
- `./scripts/verify.sh check-staged` passed before commit. Browser rendering,
  terminal replay/HMR and fixed-merge validation were not run: this is a backend
  routing repair with no UI/terminal change or merge.

Unit/local proxy checks do not prove every external provider or live Chat
integration. No production source changes were activated.
