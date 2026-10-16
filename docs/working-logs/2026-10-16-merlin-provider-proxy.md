# Merlin provider proxy configuration and cross-validation

## Scope

The user authorized inspecting and configuring the Merlin deployment, preserving
the local deployment configuration, and making proxy testing/configuration a
mandatory deployment step. Task: `c5103105-fd42-48bf-9130-58f68829db70`.

## Cross-validation

Independent launch and privacy reviewers challenged implementation `6fc097c`.
Confirmed and fixed: login rc overriding the configured proxy/direct setting;
stale Claude settings after clearing a session override; RPC reconnect/child
status diagnostic text bypassing private proxy redaction; separate usernames
and case-variant bypass hosts escaping diagnostics. Private launch files now
use mode-600 temporary files and atomic replacement.

Login initialization retains the original pre-login HOME/SHELL/environment;
only proxy variables are reasserted after rc initialization when a deployment
provider setting is present. Unconfigured local behavior remains unchanged.
Independent bash/zsh/custom-SHELL x unset/URL/direct counterprobes all pass,
including private HOME/profile PATH provider discovery and compound commands.
Reviewers reported no remaining actionable findings within their launch/privacy
scope; all probes used fictional values and isolated runtimes.

Related native/Terminal/stream regression scope: 588 passed. Additional private
HOME/PATH regression scope: 45 passed. Latest all-target validation remains
subject to the unchanged suite-only restart timeout test permission failure
recorded in the original provider-proxy working log; this is not a green default
backend-suite claim. No external inference was part of these local tests.

## Remote preflight

Initial remote checkout: clean `main` at `b6c003da037b5bd24941ea758d8b8c718bc82b93`,
with preserved unpublished integration changes; no `backend/.env`, and no
provider-proxy support. The existing backend inherited a generic process proxy.
Private candidate configuration is stored outside the deployment `.env` until
compatible code is ready. Real addresses/credentials are never written here.

Using the deployed run user's CLI and temporary process proxy overrides, three
15-second-deadline Cursor catalog probes returned 108 IDs in 5.68, 2.90 and
3.92 seconds. Fresh local control also returned 108; exact set comparison found
zero missing or extra IDs. A temporary empty-directory `auto` request exited0,
completed normally and returned the requested marker in 25.88 seconds.
This establishes that request only, not inference acceptance of all 108 IDs.

One remote native Claude Chat was working during preflight. Deployment activation
must account for that active turn; no Stop or user-message injection was issued.
Final integration/configuration and post-activation evidence will be appended.
