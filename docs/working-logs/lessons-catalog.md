# Lessons Catalog

Human-readable view of all active and archived lessons extracted from workspace task records. Regenerate by running the feedback reaper pipeline or the `render_lessons_catalog_md()` helper on each workspace.
**Totals**: 10 workspaces, 20 active lessons, 7 archived lessons.


Agent prompt injection does **not** read this file — the orchestrator pulls lessons at dispatch time via `FeedbackLessonStore.lesson_context_payload()` (keyword overlap scoring against the task title+prompt). This file exists so humans can browse and curate lessons outside the API.

## Workspace: H20 (`12564adf-8b7b-4298-b197-99484cdb1f58`)

11 active, 7 archived.

Auto-generated from `feedback/lesson-index.json`. 11 active, 7 archived.

### Active Lessons

### Performance-tuning tasks must baseline-measure the original symptom before validating the fix

- **id**: `a-gpu-utilization-or-throughput-optimization-task-that-only-measures-the-post-fi`  **scope**: `workspace`  **confidence**: 0.60  **hits**: 0  **successes**: 0
- **tags**: `performance`, `validation`, `gpu`, `baseline`

**Summary**: A GPU-utilization or throughput-optimization task that only measures the post-fix state (e.g. CUDA graph ON throughput) without ever measuring the pre-fix baseline cannot prove that the original symptom was actually addressed. The reviewer has to either trust the agent's characterization or re-measure, triggering rework.

**Applies when**:
- performance or GPU-utilization debug tasks
- any task whose acceptance criteria include 'improve X by Y%' or 'fix low Z'
- CUDA / Triton / kernel tuning tasks

**Do**: Before implementing any fix, capture the baseline: (1) run nvidia-smi / dcgmi / the workload's own perf counters and record numbers, (2) save the baseline timing/throughput output in the workspace, (3) after the fix, run the exact same measurement command and report both runs side-by-side with % delta, (4) if evals-cli or the official harness is available, prefer it over ad-hoc scripts; if blocked, document the substitution and why it's equivalent.

**Avoid**: Reporting 'CUDA graph is now captured and GPU util improved' without showing the before-and-after numbers, or using a different measurement harness pre- vs post-fix so the deltas are not comparable.

**Evidence tasks**: `ba1ba3ce-a1ea-412e-9b08-4422cea28c02`

### Revert commits must ship together with their rationale and a verifier that shows reverted behavior no longer fires

- **id**: `a-revert-task-that-only-pushes-the-revert-commit-without-documenting-which-behav`  **scope**: `workspace`  **confidence**: 0.60  **hits**: 0  **successes**: 0
- **tags**: `revert`, `git`, `delivery`, `validation`

**Summary**: A revert task that only pushes the revert commit without documenting which behavior it undoes, why the original change was wrong, and how to verify the revert is correct forces reviewers to reconstruct context across the original MR, the revert diff, and any intermediate changes. A revert deliverable is the revert commit plus the rationale plus a reproducer showing the old bug no longer triggers.

**Applies when**:
- any task whose primary deliverable is reverting one or more prior commits
- review_failed or needs_input iterations on a revert-only task

**Do**: For a revert task: (1) reference the original commit and its MR, (2) explain why the original behavior was wrong (not just 'this breaks things'), (3) show the minimal diff after the revert, (4) run the tests or e2e reproducer that originally motivated the revert and show its output, (5) confirm with the reviewer before pushing if the revert touches a shared hot path.

**Avoid**: Shipping a bare revert commit with no rationale and asking the reviewer to 'just diff it against master'; using 'revert looks fine' as validation without running the reproducer that justified the revert in the first place.

**Evidence tasks**: `f6c0200b-d389-4d96-8687-3212134696cc`

### MR review tasks must deliver substantive content on the first report, not content-free needs_input

- **id**: `a-review-task-that-reports-ready-for-review-with-no-substantive-findings-empty-n`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `review`, `delivery`, `mr`, `handoff`

**Summary**: A review task that reports ready_for_review with no substantive findings (empty needs_input placeholders, no diff read, no MR comments) wastes reviewer cycles and triggers review_failed rework. A review deliverable is concrete MR comments / a structured findings document with file:line citations — a progress note is not an acceptable final deliverable.

**Applies when**:
- any task whose primary deliverable is code review (MR review, implementation review, static analysis review)
- multi-iteration review flows where the human re-runs the same review prompt after a no-content first pass

**Do**: Before reporting ready_for_review on a review task, confirm that: (1) the MR diff / every changed file has been read end-to-end, (2) findings cite concrete file:line locations, (3) any posted MR comment IDs are listed in the report or, if read-only, a structured notes/*.md artifact is saved in the workspace.

**Avoid**: Posting content-free needs_input reports ('need to review') or completed reports that describe intent to review rather than actual findings. review_failed_count>=2 on a pure-review task is the signal that this pattern occurred.

**Evidence tasks**: `4ff25349-e489-4180-ab28-da6279dc0124`

### Push remote commits and open the MR before reporting task complete

- **id**: `across-multiple-tasks-code-changes-implemented-on-a-remote-worktree-were-left-as`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `git`, `mr`, `delivery`, `remote-host`, `handoff`

**Summary**: Across multiple tasks, code changes implemented on a remote worktree were left as uncommitted edits or as local-only commits with no MR, forcing a follow-up review cycle to detect and push them. The deliverable is not 'code changed on disk' — it is an upstream-visible MR branch.

**Applies when**:
- any task whose final deliverable should be a Codebase MR or pushed branch
- implementation work on merlin_dev / merlin_dev_evo working in a remote-only worktree
- multi-repo refactors (e.g. model_interface → llmserver migration) where each repo needs its own MR

**Do**: Before reporting completion, run 'git status --short', 'git log origin/<branch>..HEAD', and 'git ls-remote origin refs/heads/<branch>' on every touched repo, and confirm bytedcli/bitscli returns an MR entry. Record the MR number + URL in the completed report.

**Avoid**: Reporting 'changes done' based only on remote working-tree edits or local commits that have not been pushed and do not have a Codebase MR. A reviewer-side grep for 'git push' or 'MR URL' should always find concrete evidence.

**Evidence tasks**: `3a0fbb0a-3ef7-4148-82ef-8d9f032506e4`, `f8517b7e-7859-4a91-a406-2a8621a077e6`, `ddedde4e-9d3a-4157-be10-273d092daa0c`

### Serial Gloo pairwise send/recv times out on heavy multi-image DP workloads

- **id**: `cross-dp-embedder-pairwise-exchange-that-calls-dist-send-dist-recv-serially-one`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `distributed`, `gloo`, `cross-dp`, `embedder`, `timeout`, `vlm`

**Summary**: cross-DP embedder pairwise_exchange that calls dist.send / dist.recv serially (one rank-pair at a time) fails with Gloo 'Connection closed by peer' and 'Application timeout caused pair closure' under heavy multi-image (200 images/request × 20 requests) same-node DP workloads even on a 60 s default timeout, and hangs indefinitely when the timeout is raised to 600 s. Posting all receives before sends via concurrent daemon threads with deadline-based join fixes the 20/20 benchmark.

**Applies when**:
- xgpt_server cross-DP embedder collective_step with XGPT_SERVER_EMBEDDER_CROSS_DP_COLLECTIVE=1 scope=same_node
- any Gloo ProcessGroup used for pairwise rank exchange under heavy multi-image / long-sequence VLM batches
- debugging Gloo 'Connection closed by peer' or 'Application timeout caused pair closure' in pairwise transport

**Do**: Structure pairwise exchange so every rank posts all its inbound recvs (with a deadline) before issuing any outbound sends, driven by per-peer daemon threads or non-blocking ops; aggregate errors and join on the deadline. Bump XGPT_SERVER_EMBEDDER_CROSS_DP_TIMEOUT_S well above the per-step budget when the workload is expected to be heavy.

**Avoid**: Implementing pairwise_exchange as nested loops of blocking dist.send then dist.recv over rank pairs — this serializes the rendezvous path and breaks under load even on the same machine.

**Evidence tasks**: `68105ab9-6bbb-4207-86cf-f801f6206b16`

### Clear __pycache__ on remote hosts before re-validating after source edits

- **id**: `on-remote-dev-hosts-under-opt-tiger-stale-bytecode-in-pycache-can-continue-execu`  **scope**: `workspace`  **confidence**: 0.60  **hits**: 0  **successes**: 0
- **tags**: `remote-host`, `pycache`, `stale-bytecode`, `validation`, `merlin_dev_evo`

**Summary**: On remote dev hosts under /opt/tiger/*, stale bytecode in __pycache__ can continue executing old logic even after the source file is edited, masking real behavior changes and producing review_failed iterations. Always invalidate bytecode before running validation on remotes where code is edited in-place.

**Applies when**:
- editing Python source directly on a remote host via SSH
- running validation on merlin_dev / merlin_dev_evo after source edits
- CUDA graph / Triton / torch.compile warmup paths where behavior is sensitive to which code version ran first

**Do**: Before re-running tests or benchmarks after a remote source edit, clear stale bytecache (find project_root -type d -name __pycache__ -prune -exec rm -rf '{}' ';') and re-launch any long-running Python processes.

**Avoid**: Trusting that timestamp-based pycache invalidation will pick up an edit on a remote host, especially when the remote and local clocks diverge or when code is hot-reloaded in a long-running server process.

**Evidence tasks**: `ba1ba3ce-a1ea-412e-9b08-4422cea28c02`

### nvidia-smi [Not Found] PIDs may be alive — verify with lsof /dev/nvidia*

- **id**: `processes-whose-cmdline-is-unreadable-across-cgroup-container-boundaries-appear`  **scope**: `workspace`  **confidence**: 0.95  **hits**: 0  **successes**: 0
- **tags**: `gpu`, `nvidia-smi`, `shared-host`, `merlin_dev_evo`, `cleanup-safety`

**Summary**: Processes whose cmdline is unreadable across cgroup/container boundaries appear in nvidia-smi as [Not Found] but are NOT zombies. Always cross-check with `lsof /dev/nvidia*` and `pgrep -af <service>` before invoking gpu-zombie-cleanup; killing live unrelated services (e.g. /opt/tiger/llmserver tree on shared hosts) is destructive. On shared hosts, request owner approval rather than auto-killing.

**Evidence tasks**: `68105ab9-6bbb-4207-86cf-f801f6206b16`

### Snapshot remote git status at task start — pre-existing dirty files cause attribution bugs

- **id**: `shared-remote-checkouts-on-merlin-dev-merlin-dev-evo-often-carry-pre-existing-mo`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `remote-host`, `git`, `attribution`, `merlin_dev_evo`, `shared-host`

**Summary**: Shared remote checkouts on merlin_dev / merlin_dev_evo often carry pre-existing modified files (uv.lock, local .so binaries, debug commits, YAML tweaks) from prior tasks or manual sessions. Failing to snapshot git status at task start mixes pre-existing dirt with task-authored changes, causing reviewer-side attribution confusion (wrong changed_files list, uv.lock accidentally included in diffs, false regression claims).

**Applies when**:
- working on any shared remote checkout under /opt/tiger/* on merlin_dev / merlin_dev_evo
- tasks that produce MR diffs or changed_files lists from a remote worktree
- tasks where a second pass (reviewer) independently inspects the same remote checkout

**Do**: Run 'git status --short' and 'git diff --stat' on the remote checkout as the very first step and record the snapshot in the task report. Before committing, diff only the files the task intentionally edited; stash or revert unrelated pre-existing dirt.

**Avoid**: Running 'git add .' or assuming 'only my edits are dirty' on a shared remote checkout; never include pre-existing uv.lock drift or unrelated local commits in the task's changed_files list.

**Evidence tasks**: `68105ab9-6bbb-4207-86cf-f801f6206b16`, `ba1ba3ce-a1ea-412e-9b08-4422cea28c02`, `3a076f57-1698-43a6-9212-381f8c4288e0`, `13dc011e-da52-4d0b-9f58-daca5b46c6ec`

### Missing internal deps on the review host silently turn pytest into a no-op

- **id**: `unit-tests-for-llmserver-executor-adapter-model-interface-routinely-fail-to-coll`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `testing`, `dependencies`, `validation`, `internal-packages`, `merlin_dev_evo`

**Summary**: Unit tests for llmserver / executor_adapter / model_interface routinely fail to collect on review hosts (local macOS or restricted venvs) because of missing internal packages (bytedkvcache, model_interface, zmq, google.protobuf, numpy). When pytest fails at import/collection time with ModuleNotFoundError, the task report must not claim 'tests pass' based on py_compile alone — the gap needs to be called out explicitly and validation must happen on a GPU host that carries the internal dependency set.

**Applies when**:
- pytest on files under tests/inferencer/, tests/processor/, or tests/llmhub/ in the llmserver repo
- any test importing bytedkvcache, model_interface, zmq, or protobuf generated modules
- review performed on a local macOS host or a venv that was not bootstrapped with scripts/executor_adapter_backend/setup_*_venv.sh

**Do**: When pytest fails at collection with ModuleNotFoundError for an internal dep, (a) flag it as an explicit validation gap in the report, (b) re-run on the designated GPU host (merlin_dev_evo) using scripts/executor_adapter_backend/setup_torch25_venv.sh or the matching setup script, and (c) cite concrete 'N/M tests passed' output, not just py_compile.

**Avoid**: Treating ast.parse / py_compile success as a substitute for pytest when the task's acceptance criteria require real test execution, and never reporting 'tests pass' without pasting the actual pytest summary line.

**Evidence tasks**: `13dc011e-da52-4d0b-9f58-daca5b46c6ec`, `3f38726e-d002-4f50-8774-e693a7b5e114`, `f8517b7e-7859-4a91-a406-2a8621a077e6`, `ddedde4e-9d3a-4157-be10-273d092daa0c`, `cb1d1b6f-e172-4359-9498-e55503826da0`

### Reproducer handoffs to reviewers must use absolute paths and non-ephemeral fixture locations

- **id**: `when-a-correctness-or-performance-task-hands-off-a-reproducer-to-a-reviewer-rela`  **scope**: `workspace`  **confidence**: 0.60  **hits**: 0  **successes**: 0
- **tags**: `review`, `reproducer`, `handoff`, `fixtures`

**Summary**: When a correctness or performance task hands off a reproducer to a reviewer, relative paths (./fixtures, ../../data) and worktree-relative symlinks break the moment the reviewer clones into a different directory or the originating worktree is torn down. The reviewer then wastes a full iteration reconstructing the fixture layout instead of validating the change.

**Applies when**:
- any task whose deliverable includes a reproducer script, test fixture, or dataset that the reviewer must run independently
- review_failed on a correctness repro task where the reviewer reports missing files or broken symlinks

**Do**: Before reporting ready_for_review on a repro task: (1) hard-code fixture paths as absolute (shared data hosts like /opt/tiger/, HDFS paths, or committed test-data artifacts), (2) if using worktree-local symlinks, also copy or describe how to materialize the fixture from a non-ephemeral source, (3) run the reproducer from a fresh CWD to confirm relative paths do not leak, (4) print the sha256 of any large binary fixture so the reviewer can verify integrity.

**Avoid**: Baking `../../models/p6d_audio_test`-style relative paths into the test harness, relying on the originating worktree staying alive, or omitting the fixture location from the final report (forcing the reviewer to grep the diff for data paths).

**Evidence tasks**: `c1f08e9f-1965-456f-99e3-c77ae0c5f819`

### Docker image deliverables must be built from the task's final HEAD and labeled with the commit SHA

- **id**: `when-a-task-delivers-a-docker-image-building-from-an-intermediate-commit-or-fail`  **scope**: `workspace`  **confidence**: 0.60  **hits**: 0  **successes**: 0
- **tags**: `docker`, `delivery`, `icm`, `build`

**Summary**: When a task delivers a Docker image, building from an intermediate commit or failing to stamp the image with the git SHA that produced it means the reviewer cannot verify the image matches the code under review. Image label mismatches against HEAD cause review_failed iterations and force rebuilds.

**Applies when**:
- any task whose deliverable is a Docker image or ICM/Harbor image push
- multi-iteration tasks involving Dockerfile changes plus image rebuilds

**Do**: For any Docker/ICM image deliverable: (1) build from the exact HEAD commit being sent for review (never an earlier savepoint), (2) label the image with org.opencontainers.image.revision=<full sha>, (3) report the image digest, size, and HEAD SHA side-by-side so the reviewer can confirm alignment, (4) if the image was built in a prior iteration, re-verify HEAD still matches before reporting complete.

**Avoid**: Recommending an image built from a non-HEAD commit, omitting the commit SHA from the report, or pushing an image and separately committing more changes without rebuilding.

**Evidence tasks**: `03c15d5d-38c4-4ccf-8738-fdb423feca9a`

### Archived Lessons

- `np-ravel-list-of-lists-raises-valueerror-on-modern-numpy-when-inner-row-lengths` — np.ravel/Tensor truthiness break on ragged inputs — use isinstance helper
- `on-merlin-dev-evo-opt-tiger-xgpt-server-default-uv-selects-python-3-13-and-the-g` — uv sync on /opt/tiger/xgpt_server needs --python /usr/bin/python3.11
- `scripts-gen-multi-image-bench-data-py-queries-get-image-len-on-the-running-xgpt` — xgpt bench data generators must run AFTER server health gate
- `test-signal-a-passes` — test single-evidence Signal A passes
- `triton-3-x-compiler-does-not-support-the-continue-keyword-inside-triton-jit-kern` — Triton 3.x @triton.jit rejects `continue` — refactor to if/else
- `when-a-single-triton-kernels-file-is-split-into-thematic-submodules-common-py-dr` — Splitting Triton kernels across submodules needs a _KernelsModule patch-mirror
- `when-moving-a-package-e-g-model-interface-mock-backend-llmserver-backends-mock-k` — PEP 562 lazy __getattr__ + sys.modules shim for relocated Python packages
## Workspace: 25fa7e59-e5b5-492b-a7cb-fa75f97099bb (`25fa7e59-e5b5-492b-a7cb-fa75f97099bb`)

1 active, 0 archived.

Auto-generated from `feedback/lesson-index.json`. 1 active, 0 archived.

### Active Lessons

### Explicit review requests need handoff evidence

- **id**: `explicit-review-handoff`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `review`, `handoff`

**Summary**: Explicit review requests need handoff evidence.

**Applies when**:
- explicit review
- review request

**Do**: Check changed files, validation, risks, and acceptance evidence.

**Avoid**: Do not pass review based only on the completion message.
## Workspace: 2d87ce06-ade7-4d66-bd32-2f603d434f80 (`2d87ce06-ade7-4d66-bd32-2f603d434f80`)

1 active, 0 archived.

Auto-generated from `feedback/lesson-index.json`. 1 active, 0 archived.

### Active Lessons

### Explicit review requests need handoff evidence

- **id**: `explicit-review-handoff`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `review`, `handoff`

**Summary**: Explicit review requests need handoff evidence.

**Applies when**:
- explicit review
- review request

**Do**: Check changed files, validation, risks, and acceptance evidence.

**Avoid**: Do not pass review based only on the completion message.
## Workspace: 73525b3d-f54a-4a3d-b1b2-a28bd7fa81b0 (`73525b3d-f54a-4a3d-b1b2-a28bd7fa81b0`)

1 active, 0 archived.

Auto-generated from `feedback/lesson-index.json`. 1 active, 0 archived.

### Active Lessons

### Explicit review requests need handoff evidence

- **id**: `explicit-review-handoff`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `review`, `handoff`

**Summary**: Explicit review requests need handoff evidence.

**Applies when**:
- explicit review
- review request

**Do**: Check changed files, validation, risks, and acceptance evidence.

**Avoid**: Do not pass review based only on the completion message.
## Workspace: 978ef1fe-7f60-4c52-a372-b2d59890f392 (`978ef1fe-7f60-4c52-a372-b2d59890f392`)

1 active, 0 archived.

Auto-generated from `feedback/lesson-index.json`. 1 active, 0 archived.

### Active Lessons

### Explicit review requests need handoff evidence

- **id**: `explicit-review-handoff`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `review`, `handoff`

**Summary**: Explicit review requests need handoff evidence.

**Applies when**:
- explicit review
- review request

**Do**: Check changed files, validation, risks, and acceptance evidence.

**Avoid**: Do not pass review based only on the completion message.
## Workspace: a9ba4ebd-1e2a-4b21-a165-23e466bc22cf (`a9ba4ebd-1e2a-4b21-a165-23e466bc22cf`)

1 active, 0 archived.

Auto-generated from `feedback/lesson-index.json`. 1 active, 0 archived.

### Active Lessons

### Explicit review requests need handoff evidence

- **id**: `explicit-review-handoff`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `review`, `handoff`

**Summary**: Explicit review requests need handoff evidence.

**Applies when**:
- explicit review
- review request

**Do**: Check changed files, validation, risks, and acceptance evidence.

**Avoid**: Do not pass review based only on the completion message.
## Workspace: d61044c2-292b-4cc5-83b8-298c6b2463ee (`d61044c2-292b-4cc5-83b8-298c6b2463ee`)

1 active, 0 archived.

Auto-generated from `feedback/lesson-index.json`. 1 active, 0 archived.

### Active Lessons

### Explicit review requests need handoff evidence

- **id**: `explicit-review-handoff`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `review`, `handoff`

**Summary**: Explicit review requests need handoff evidence.

**Applies when**:
- explicit review
- review request

**Do**: Check changed files, validation, risks, and acceptance evidence.

**Avoid**: Do not pass review based only on the completion message.
## Workspace: d6127ec5-85c2-4594-afff-a696a0e70ab8 (`d6127ec5-85c2-4594-afff-a696a0e70ab8`)

1 active, 0 archived.

Auto-generated from `feedback/lesson-index.json`. 1 active, 0 archived.

### Active Lessons

### Explicit review requests need handoff evidence

- **id**: `explicit-review-handoff`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `review`, `handoff`

**Summary**: Explicit review requests need handoff evidence.

**Applies when**:
- explicit review
- review request

**Do**: Check changed files, validation, risks, and acceptance evidence.

**Avoid**: Do not pass review based only on the completion message.
## Workspace: d78984aa-9edd-4c7f-afde-5ce5aaaec8bd (`d78984aa-9edd-4c7f-afde-5ce5aaaec8bd`)

1 active, 0 archived.

Auto-generated from `feedback/lesson-index.json`. 1 active, 0 archived.

### Active Lessons

### Explicit review requests need handoff evidence

- **id**: `explicit-review-handoff`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `review`, `handoff`

**Summary**: Explicit review requests need handoff evidence.

**Applies when**:
- explicit review
- review request

**Do**: Check changed files, validation, risks, and acceptance evidence.

**Avoid**: Do not pass review based only on the completion message.
## Workspace: ff7c7002-16a7-4655-a5f7-bb073df98b9b (`ff7c7002-16a7-4655-a5f7-bb073df98b9b`)

1 active, 0 archived.

Auto-generated from `feedback/lesson-index.json`. 1 active, 0 archived.

### Active Lessons

### Explicit review requests need handoff evidence

- **id**: `explicit-review-handoff`  **scope**: `workspace`  **confidence**: 0.80  **hits**: 0  **successes**: 0
- **tags**: `review`, `handoff`

**Summary**: Explicit review requests need handoff evidence.

**Applies when**:
- explicit review
- review request

**Do**: Check changed files, validation, risks, and acceptance evidence.

**Avoid**: Do not pass review based only on the completion message.