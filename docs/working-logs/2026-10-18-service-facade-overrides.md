# Restore call-time legacy service overrides

## Scope

Follow up the three compatibility findings from the adversarial review of
`801df51`, whose runtime sources match the structural refactor in `976ab86`.
The affected legacy names are `feedback_lessons._now`,
`dot.service.WAKE_INTERVAL_SECONDS`, and `feishu_bot_pool.MAX_BOTS`.

Module-level aliases preserve the initial value but do not forward later
assignments to the extracted consumer's global namespace. The repair uses the
existing facade-subclass pattern to resolve each value at call time, including
for instances constructed before a patch. It does not introduce reverse imports,
mutable dependency registries, module proxies, or constructor snapshots.

## Implementation boundaries

- Feedback uses `_feedback_now()` through persistence, catalog and summary
  behavior. The public facade resolves its own `_now`; explicit `now=` still
  determines the corresponding operation's timestamp. Index write timestamps
  keep their separate clock lookup, as before the decomposition.
- Dot uses `_wake_interval_seconds()` when constructing default schedule timing.
  Existing explicit schedule timing is still retained. The watcher interval,
  schedule ownership, update conditions and singleton are unchanged.
- Bot pool uses `_max_bots()` inside the existing create lock. The public facade
  is a thin subclass of the feature implementation, matching Feedback and Dot.
  It inherits the constructor and all state/lock ownership unchanged. All
  production consumers continue importing the same public class.

The newly introduced private `bot_pool.store` implementation class is now a base
class rather than the identical object exported by the legacy facade. The public
class's module/name return to the pre-decomposition location. Structural tests
check the subclass relationship and preserve exact public consumer identity in
both the API and websocket modules. The internal feature-module clock/limit
bindings are not additional supported override APIs.

The API's separate import-time `MAX_BOTS` binding existed before decomposition;
this repair does not change that behavior. The previously identified cycle-gate
statement-coverage limitation is also outside this fix.

## Regression evidence

`tests/test_service_facade_overrides.py` adds six parameterized cases, covering
patches before and after construction, restoration on the same instance, and
actual public operations rather than assertions against the new helper methods.
All six fail on `801df51` for the reported timestamp, interval, and capacity
mismatches. Together with `test_service_decomposition.py`, all 11 cases pass
after the repair.

Additional assertions check Feedback catalog/index/summary timestamps and an
explicit timestamp, Dot's retained existing interval and recreated default,
and Bot limits of zero and one without losing existing entries. Public class
consumer identity and the existing acyclic dependency checks remain covered.

Validation uses the repository's `./scripts/verify.sh all` entrypoint, plus the
focused Feedback, Dot and Bot suites and the same-instance patch/restore probe.
No real provider, OAuth, Feishu network, shared Hub service, or frontend behavior
is changed by this repair; these external paths require their own integration
validation rather than being inferred from local test success.
