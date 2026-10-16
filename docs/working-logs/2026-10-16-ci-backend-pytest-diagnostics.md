# Backend CI cancellation and oversized pytest IDs

## Evidence

Several main-branch backend pytest jobs ended cancelled after about 35 minutes.
The GitHub check annotation explicitly reported the configured 30-minute job
limit; this was not evidence of a manual cancellation or an assertion failure.
The cancelled jobs' individual logs were unavailable, and their run archives
contained only the other 13 jobs.

An isolated diagnostic candidate on PR #17 retained the pytest log as an
artifact and sent full step output to a file. A temporary deadline at the Python
process boundary kept the log collector alive if pytest stalled. That run
completed normally: 3559 passed, 10 skipped in 229.15 seconds, without reaching
the deadline. All 14 CI checks passed on that diagnostic candidate.

Its log contained a single 8,389,016-character line for
`test_all_images_are_validated_before_any_provider_request`. Pytest had generated
the parameter ID from an oversized PNG fixture, copying approximately 8 MiB of
data into the verbose test name. A malformed registry case similarly generated
a 20,199-character line. This explains why raw console output was a necessary
comparison point even though the same tests completed with file output.

## Final candidate

Assign explicit, short IDs to these parameterized cases. The input payloads,
test bodies and assertions remain identical. Restore the original workflow and
verification script, including normal verbose console output and the original
job budget; the diagnostic deadline is not a proposed permanent suite limit.

The two affected modules pass 57 focused tests locally. The follow-up full CI
uses the original execution and logging path, allowing the test-name change to
be checked independently of the temporary diagnostic settings. Candidate SHA,
full validation results and independent review are recorded with PR #17 and its
Task evidence.

The [sync merge decision record](2026-10-16-ci-backend-pytest-merge-decisions.json)
binds the reviewed CHANGELOG resolution to immutable merge `2c355f540617f966915a7c6d2e1265886f11960d`.
