# Codex busy terminal status

Current Codex renders `? for shortcuts` and `Ask Codex to do anything` while
`• Working (12m 13s • esc to interrupt)` is active. The status classifier
returned `agent prompt visible` before consulting its existing matching Codex
regex. This was a precedence bug, not a missing Working regex.

For CODEX only, check the shared explicit working marker after attention and
bare-shell detection but before idle footer hints. Preserve `working_or_stale`
(45s frozen-frame attention), TraeX and all other provider ordering. No queue,
task or native-provider runtime logic changes.

Regression evidence lives in `backend/tests/fixtures/codex-status-20261020/`:
real styled busy/idle panes and six rapid busy frames, capture commands/times
and hashes, plus native-provider status projections. Derived variants are
explicitly labeled in tests and fixture README. Unmodified main fails 44 of
the initial 51 replay cases, while the candidate passes them.

The full investigation, exact checks, remaining evidence limits and local
commit handoff are in `/home/tiger/evo_workspace/hub-status-idle-fix-20261020/FINDINGS.md`.
This branch is local; the primary Hub is never restarted or deployed by this task.

Review round 3: the initial queue-height transformation did not validate resize.
Ran installed Codex 0.163.0 on private tmux/CODEX_HOME with a loopback mock
Responses stream. Actual width/height captures at nine sizes show ellipsized
interrupt text below 40 columns. The prior candidate fails 3 of 9 busy/idle
resize replays. Added strict elapsed-time/bullet + interrupt-prefix recognition
and derived soft-wrap handling, opted in only by CODEX terminal status. Default
shared detector policy (including auto-continue and TraeX) is unchanged. Full
commands/provenance/byte hashes are in the resized fixture README and manifest.
