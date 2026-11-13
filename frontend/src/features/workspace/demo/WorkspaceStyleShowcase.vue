<template>
  <div class="zen-shell">
    <!-- Single, quiet 44px top bar — zero double headers, zero emoji clutter -->
    <header class="zen-topbar">
      <div class="zen-topbar-left">
        <span class="zen-brand">Workspace</span>
        <span class="zen-sep">/</span>
        <select
          v-model="workspaceFilter"
          class="zen-ws-select"
          aria-label="Filter workspace"
        >
          <option value="all">
            All repositories
          </option>
          <option
            v-for="ws in workspaceNames"
            :key="ws"
            :value="ws"
          >
            {{ ws }}
          </option>
        </select>
        <span class="zen-topbar-summary zen-num">
          {{ needsYouTasks.length }} sign-off · {{ workingTasks.length }} active
        </span>
      </div>

      <nav
        class="zen-style-tabs"
        role="tablist"
        aria-label="Design direction"
      >
        <button
          v-for="opt in DEMO_STYLES"
          :key="opt.id"
          type="button"
          role="tab"
          :aria-selected="activeStyle === opt.id"
          :class="['zen-style-tab', { active: activeStyle === opt.id }]"
          @click="activeStyle = opt.id"
        >
          <span class="zen-tab-full">{{ opt.label }}</span>
          <span class="zen-tab-short">{{ opt.shortLabel }}</span>
        </button>
      </nav>

      <div class="zen-topbar-right">
        <span class="zen-ref-hint">{{ currentStyleMeta.reference }}</span>
        <button
          type="button"
          class="zen-ghost-btn"
          @click="$emit('exit')"
        >
          Classic view
        </button>
      </div>
    </header>

    <!-- =======================================================================
         CANDIDATE 1: 1 · LINEAR TRIAGE PRO (Linear Triage × GitButler Spine)
         ======================================================================= -->
    <div
      v-if="activeStyle === 'linear_zen'"
      class="zen-linear"
    >
      <aside
        class="zen-linear-list"
        aria-label="Tasks"
      >
        <div class="zen-linear-groups">
          <section
            v-for="group in groupedTasks"
            :key="group.key"
            class="zen-group"
          >
            <header class="zen-group-header">
              <span>{{ group.label }}</span>
              <span class="zen-num">{{ group.items.length }}</span>
            </header>

            <div
              v-for="item in group.items"
              :key="item.id"
              :class="['zen-pro-row', { active: selectedTask.id === item.id }]"
              tabindex="0"
              @click="selectTask(item.id)"
              @keydown.enter="selectTask(item.id)"
            >
              <div class="zen-pro-row-top">
                <span :class="['zen-dot', `zen-dot--${item.statusTone}`]" />
                <span class="zen-pro-code zen-mono">{{ item.code }}</span>
                <span class="zen-pro-ws zen-mono">{{ item.workspace }}</span>
                <span :class="['zen-pro-state', `zen-pro-state--${item.statusTone}`]">
                  {{ item.statusLabel }}
                </span>
                <span class="zen-pro-elapsed zen-num">{{ item.elapsed }}</span>
              </div>
              <div class="zen-pro-row-title">
                {{ item.title }}
              </div>
              <div
                v-if="typeof item.progressPct === 'number'"
                class="zen-pro-progress-track"
              >
                <div
                  class="zen-pro-progress-fill"
                  :style="{ width: `${item.progressPct}%` }"
                />
              </div>
            </div>
          </section>
        </div>

        <footer class="zen-rail-footer">
          <span class="zen-kbd-group">
            <kbd>J</kbd><kbd>K</kbd>
            <span>navigate</span>
          </span>
          <span class="zen-kbd-group">
            <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd>
            <span>view</span>
          </span>
          <span class="zen-kbd-group">
            <kbd>⌘↵</kbd>
            <span>accept</span>
          </span>
        </footer>
      </aside>

      <main class="zen-linear-detail">
        <div class="zen-editorial">
          <!-- Meta Breadcrumb -->
          <div class="zen-meta-line">
            <span :class="['zen-status-pill', `zen-status-pill--${selectedTask.statusTone}`]">
              <i :class="['zen-dot', `zen-dot--${selectedTask.statusTone}`]" />
              {{ selectedTask.statusLabel }}
            </span>
            <span class="zen-mono zen-meta-code">{{ selectedTask.code }}</span>
            <span class="zen-sep">·</span>
            <span class="zen-mono">{{ selectedTask.workspace }}</span>
            <span class="zen-sep">·</span>
            <span class="zen-branch-chip zen-mono">
              <svg
                class="zen-icon"
                viewBox="0 0 16 16"
                fill="none"
                aria-hidden="true"
              >
                <circle
                  cx="5"
                  cy="4"
                  r="1.75"
                  stroke="currentColor"
                  stroke-width="1.5"
                />
                <circle
                  cx="5"
                  cy="12"
                  r="1.75"
                  stroke="currentColor"
                  stroke-width="1.5"
                />
                <circle
                  cx="11"
                  cy="6.5"
                  r="1.75"
                  stroke="currentColor"
                  stroke-width="1.5"
                />
                <path
                  d="M5 5.75v4.5M11 8.25c0 2-2 2.25-6 2.25"
                  stroke="currentColor"
                  stroke-width="1.5"
                  stroke-linecap="round"
                />
              </svg>
              {{ selectedTask.branch }}
            </span>
            <template v-if="selectedTask.diffStat">
              <span class="zen-sep">·</span>
              <span class="zen-diff-add zen-mono zen-num">+{{ selectedTask.diffStat.added }}</span>
              <span class="zen-diff-del zen-mono zen-num">−{{ selectedTask.diffStat.removed }}</span>
              <span class="zen-mono zen-num">({{ selectedTask.diffStat.files }} files)</span>
            </template>
            <span class="zen-meta-updated zen-num">{{ selectedTask.updatedAgo }} · {{ selectedTask.elapsed }}</span>
          </div>

          <h1 class="zen-title">
            {{ selectedTask.title }}
          </h1>

          <p class="zen-summary">
            {{ selectedTask.outcomeSummary }}
          </p>

          <!-- Architectural Deliverable & Consensus Spec Frame -->
          <section
            class="zen-spec-frame"
            aria-label="Deliverable and consensus specification"
          >
            <!-- Row 1: Decision / Live Gate -->
            <div
              v-if="selectedTask.bucket === 'needs_you'"
              class="zen-spec-gate"
            >
              <div class="zen-spec-gate-left">
                <span class="zen-verdict-badge zen-verdict-badge--pass zen-mono">
                  <svg
                    class="zen-icon"
                    viewBox="0 0 16 16"
                    fill="none"
                    aria-hidden="true"
                  >
                    <path
                      d="M3.5 8.25L6.5 11.25L12.5 4.75"
                      stroke="currentColor"
                      stroke-width="1.5"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    />
                  </svg>
                  {{ selectedTask.consensus.verdictLabel }}
                </span>
                <span class="zen-decision-copy">{{ selectedTask.decisionLine }}</span>
              </div>
              <div class="zen-decision-btns">
                <button
                  type="button"
                  class="zen-ghost-btn"
                >
                  Steer…
                </button>
                <button
                  type="button"
                  class="zen-primary-btn"
                  @click="triggerAccept(selectedTask.id)"
                >
                  {{ acceptedIds.has(selectedTask.id) ? 'Accepted' : 'Accept' }}
                </button>
              </div>
            </div>

            <div
              v-else-if="selectedTask.bucket === 'working'"
              class="zen-spec-gate"
            >
              <div class="zen-spec-gate-left">
                <span class="zen-verdict-badge zen-verdict-badge--live zen-mono">
                  <span class="zen-pulse-dot" />
                  {{ selectedTask.consensus.verdictLabel }}
                </span>
                <span class="zen-mono zen-live-step-text">{{ selectedTask.liveStep }}</span>
              </div>
              <div class="zen-spec-progress-wrap">
                <div class="zen-spec-progress-bar">
                  <div
                    class="zen-spec-progress-fill"
                    :style="{ width: `${selectedTask.progressPct ?? 50}%` }"
                  />
                </div>
                <span class="zen-mono zen-num">{{ selectedTask.progressPct ?? 50 }}%</span>
              </div>
            </div>

            <div
              v-else
              class="zen-spec-gate"
            >
              <div class="zen-spec-gate-left">
                <span class="zen-verdict-badge zen-verdict-badge--pass zen-mono">
                  {{ selectedTask.consensus.verdictLabel }}
                </span>
                <span class="zen-decision-copy">{{ selectedTask.consensus.roundsLabel }}</span>
              </div>
              <span class="zen-mono zen-checks-pill">✓ {{ selectedTask.checksLabel }}</span>
            </div>

            <!-- Row 2: Consensus Matrix -->
            <div class="zen-spec-consensus">
              <div class="zen-consensus-cell">
                <span class="zen-cell-label">CONVERGENCE PAIR</span>
                <div class="zen-cell-val">
                  <span>{{ selectedTask.consensus.implementer }}</span>
                  <span class="zen-arrow">→</span>
                  <span>{{ selectedTask.consensus.reviewer }}</span>
                </div>
              </div>
              <div class="zen-consensus-cell">
                <span class="zen-cell-label">REVIEW TRAJECTORY</span>
                <div class="zen-cell-val">
                  {{ selectedTask.consensus.roundsLabel }}
                </div>
              </div>
              <div class="zen-consensus-cell">
                <span class="zen-cell-label">EVIDENCE &amp; BENCHMARK</span>
                <div class="zen-cell-val zen-mono zen-num">
                  {{ selectedTask.consensus.metricHighlight }}
                </div>
              </div>
            </div>

            <!-- Row 3: Deliverable Artifacts -->
            <div class="zen-spec-artifacts">
              <div class="zen-artifacts-list">
                <a
                  v-for="link in selectedTask.links"
                  :key="link.label"
                  :href="link.url"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="zen-artifact-row"
                >
                  <svg
                    v-if="link.kind === 'mr'"
                    class="zen-icon zen-artifact-icon"
                    viewBox="0 0 16 16"
                    fill="none"
                    aria-hidden="true"
                  >
                    <circle
                      cx="4.5"
                      cy="4"
                      r="1.75"
                      stroke="currentColor"
                      stroke-width="1.5"
                    />
                    <circle
                      cx="4.5"
                      cy="12"
                      r="1.75"
                      stroke="currentColor"
                      stroke-width="1.5"
                    />
                    <circle
                      cx="11.5"
                      cy="12"
                      r="1.75"
                      stroke="currentColor"
                      stroke-width="1.5"
                    />
                    <path
                      d="M4.5 5.75v4.5M11.5 10.25V6.5a2.5 2.5 0 0 0-2.5-2.5H7.5"
                      stroke="currentColor"
                      stroke-width="1.5"
                      stroke-linecap="round"
                    />
                  </svg>
                  <svg
                    v-else-if="link.kind === 'doc'"
                    class="zen-icon zen-artifact-icon"
                    viewBox="0 0 16 16"
                    fill="none"
                    aria-hidden="true"
                  >
                    <rect
                      x="3"
                      y="2.25"
                      width="10"
                      height="11.5"
                      rx="1.5"
                      stroke="currentColor"
                      stroke-width="1.5"
                    />
                    <path
                      d="M5.5 5.75h5M5.5 8.25h5M5.5 10.75h3"
                      stroke="currentColor"
                      stroke-width="1.5"
                      stroke-linecap="round"
                    />
                  </svg>
                  <svg
                    v-else-if="link.kind === 'arena'"
                    class="zen-icon zen-artifact-icon"
                    viewBox="0 0 16 16"
                    fill="none"
                    aria-hidden="true"
                  >
                    <path
                      d="M2 8.5h2.5L6.25 4l3.25 8 2-3.5H14"
                      stroke="currentColor"
                      stroke-width="1.5"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    />
                  </svg>
                  <svg
                    v-else
                    class="zen-icon zen-artifact-icon"
                    viewBox="0 0 16 16"
                    fill="none"
                    aria-hidden="true"
                  >
                    <circle
                      cx="8"
                      cy="8"
                      r="2.25"
                      stroke="currentColor"
                      stroke-width="1.5"
                    />
                    <path
                      d="M2 8h3.75M10.25 8H14"
                      stroke="currentColor"
                      stroke-width="1.5"
                      stroke-linecap="round"
                    />
                  </svg>
                  <span class="zen-artifact-label">{{ link.label }}</span>
                  <span class="zen-artifact-meta zen-mono">{{ link.meta }}</span>
                  <span class="zen-artifact-ext">↗</span>
                </a>
              </div>
              <span class="zen-checks-note zen-mono">✓ {{ selectedTask.checksLabel }}</span>
            </div>
          </section>

          <!-- Sculptural Epoch Run Spine -->
          <section class="zen-spine-section">
            <div class="zen-spine-header">
              <h2 class="zen-micro-heading">
                EXECUTION &amp; REVIEW SPINE
              </h2>
              <span class="zen-spine-overview zen-mono zen-num">
                {{ selectedTask.timeline.length }} stages · epoch {{ selectedTask.internalMeta.epoch }} · {{ selectedTask.elapsed }}
              </span>
            </div>

            <ol class="zen-spine">
              <template
                v-for="node in selectedTask.timeline"
                :key="node.id"
              >
                <li
                  v-if="node.epochLabel"
                  class="zen-epoch-row"
                >
                  <span class="zen-epoch-label zen-mono">{{ node.epochLabel }}</span>
                  <span class="zen-epoch-line" />
                </li>

                <li :class="['zen-spine-item', `zen-spine-item--${node.tone}`, `zen-spine-role--${node.actorRole}`]">
                  <div class="zen-spine-time-col zen-num">
                    <span class="zen-spine-time">{{ node.timeLabel }}</span>
                    <span class="zen-spine-delta zen-mono">{{ node.deltaLabel }}</span>
                  </div>

                  <span class="zen-spine-track">
                    <i class="zen-spine-node" />
                  </span>

                  <div class="zen-spine-body">
                    <div class="zen-spine-head">
                      <strong>{{ node.stage }}</strong>
                      <span class="zen-spine-actor">
                        {{ node.actor }}
                        <em class="zen-role-tag">{{ node.actorRole }}</em>
                      </span>
                      <span
                        v-if="node.badge"
                        :class="['zen-node-badge', 'zen-mono', `zen-node-badge--${node.tone}`]"
                      >
                        {{ node.badge }}
                      </span>
                      <button
                        v-if="node.sessionTabId"
                        type="button"
                        class="zen-inline-link zen-mono"
                        @click="openLogNodeId = openLogNodeId === node.id ? null : node.id"
                      >
                        {{ openLogNodeId === node.id ? 'Hide transcript' : 'Transcript ↗' }}
                      </button>
                    </div>

                    <p>{{ node.summary }}</p>

                    <div
                      v-if="node.filesTouched || node.receipt"
                      class="zen-spine-evidence zen-mono"
                    >
                      <span
                        v-if="node.filesTouched"
                        class="zen-evidence-files"
                      >
                        {{ node.filesTouched }}
                      </span>
                      <span
                        v-if="node.filesTouched && node.receipt"
                        class="zen-sep"
                      >·</span>
                      <span
                        v-if="node.receipt"
                        class="zen-evidence-receipt"
                      >
                        ✓ {{ node.receipt }}
                      </span>
                    </div>

                    <div
                      v-if="openLogNodeId === node.id"
                      class="zen-log-excerpt"
                    >
                      <div
                        v-for="(line, idx) in selectedTask.transcriptExcerpt"
                        :key="idx"
                        class="zen-log-row"
                      >
                        <span class="zen-mono zen-num">{{ line.time }}</span>
                        <span :class="['zen-mono', 'zen-log-speaker', `zen-log-speaker--${line.role}`]">
                          {{ line.speaker }}
                        </span>
                        <span>{{ line.text }}</span>
                      </div>
                    </div>
                  </div>
                </li>
              </template>
            </ol>
          </section>

          <details class="zen-meta-disclosure">
            <summary>System governance &amp; runtime metadata</summary>
            <p class="zen-mono">
              control={{ selectedTask.internalMeta.control }} · mode={{ selectedTask.internalMeta.mode }} · epoch={{ selectedTask.internalMeta.epoch }} · seat={{ selectedTask.internalMeta.seat }}
            </p>
          </details>
        </div>
      </main>
    </div>

    <!-- =======================================================================
         CANDIDATE 2: 2 · COMMAND STUDIO (Decision Spotlight × Autonomous River)
         ======================================================================= -->
    <div
      v-else-if="activeStyle === 'focused_inbox'"
      class="zen-studio"
    >
      <main class="zen-studio-main">
        <!-- Zone A: Decision Spotlight (Only needs_you tasks) -->
        <section class="zen-studio-zone">
          <header class="zen-zone-header">
            <div class="zen-zone-title">
              <span class="zen-dot zen-dot--attention" />
              <h2>NEEDS YOUR SIGN-OFF</h2>
              <span class="zen-zone-count zen-mono zen-num">{{ needsYouTasks.length }}</span>
            </div>
            <span class="zen-zone-sub">Heterogeneous review converged · awaiting human merge</span>
          </header>

          <div class="zen-spotlight-grid">
            <article
              v-for="item in needsYouTasks"
              :key="item.id"
              :class="['zen-spotlight-card', { selected: selectedTask.id === item.id }]"
              tabindex="0"
              @click="selectTask(item.id)"
              @keydown.enter="selectTask(item.id)"
            >
              <div class="zen-spotlight-top">
                <span class="zen-mono zen-pro-code">{{ item.code }}</span>
                <span class="zen-mono">{{ item.workspace }}</span>
                <span class="zen-mono zen-branch-label">{{ item.branch }}</span>
                <template v-if="item.diffStat">
                  <span class="zen-diff-add zen-mono zen-num">+{{ item.diffStat.added }}</span>
                  <span class="zen-diff-del zen-mono zen-num">−{{ item.diffStat.removed }}</span>
                </template>
                <span class="zen-spotlight-elapsed zen-num">{{ item.elapsed }}</span>
              </div>

              <h3 class="zen-spotlight-title">
                {{ item.title }}
              </h3>

              <p class="zen-spotlight-decision">
                {{ item.decisionLine }}
              </p>

              <div class="zen-spotlight-consensus">
                <div class="zen-spotlight-pair">
                  <span class="zen-cell-label">CONSENSUS</span>
                  <strong>{{ item.consensus.implementer }} → {{ item.consensus.reviewer }}</strong>
                </div>
                <span class="zen-verdict-badge zen-verdict-badge--pass zen-mono">
                  {{ item.consensus.verdictLabel }}
                </span>
                <span class="zen-spotlight-metric zen-mono zen-num">
                  {{ item.consensus.metricHighlight }}
                </span>
              </div>

              <footer class="zen-spotlight-footer">
                <div class="zen-spotlight-links">
                  <a
                    v-for="link in item.links"
                    :key="link.label"
                    :href="link.url"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="zen-text-link"
                    @click.stop
                  >
                    {{ link.label }} ↗
                  </a>
                </div>
                <div class="zen-decision-btns">
                  <button
                    type="button"
                    class="zen-ghost-btn zen-btn-sm"
                    @click.stop="selectTask(item.id)"
                  >
                    Steer…
                  </button>
                  <button
                    type="button"
                    class="zen-primary-btn zen-btn-sm"
                    @click.stop="triggerAccept(item.id)"
                  >
                    {{ acceptedIds.has(item.id) ? 'Accepted' : 'Accept' }}
                  </button>
                </div>
              </footer>
            </article>
          </div>
        </section>

        <!-- Zone B: Active Autonomous Runs (Sleek Horizontal Swimlanes) -->
        <section class="zen-studio-zone">
          <header class="zen-zone-header">
            <div class="zen-zone-title">
              <span class="zen-dot zen-dot--working" />
              <h2>ACTIVE AUTONOMOUS RUNS</h2>
              <span class="zen-zone-count zen-mono zen-num">{{ workingTasks.length }}</span>
            </div>
            <span class="zen-zone-sub">Streaming telemetry &amp; cross-model verification in progress</span>
          </header>

          <div class="zen-swimlanes">
            <div
              v-for="item in workingTasks"
              :key="item.id"
              :class="['zen-swimlane', { selected: selectedTask.id === item.id }]"
              tabindex="0"
              @click="selectTask(item.id)"
              @keydown.enter="selectTask(item.id)"
            >
              <div class="zen-swimlane-main">
                <div class="zen-swimlane-head">
                  <span class="zen-pulse-dot" />
                  <span class="zen-mono zen-pro-code">{{ item.code }}</span>
                  <strong class="zen-swimlane-title">{{ item.title }}</strong>
                  <span class="zen-mono zen-swimlane-ws">{{ item.workspace }} · {{ item.branch }}</span>
                </div>
                <div class="zen-swimlane-ticker zen-mono">
                  <span>{{ item.liveStep }}</span>
                </div>
              </div>

              <div class="zen-swimlane-right">
                <div class="zen-swimlane-metrics">
                  <span class="zen-mono zen-num">{{ item.statusLabel }}</span>
                  <span class="zen-sep">·</span>
                  <span class="zen-mono">{{ item.checksLabel }}</span>
                  <span class="zen-sep">·</span>
                  <span class="zen-mono zen-num">{{ item.elapsed }}</span>
                </div>
                <div class="zen-swimlane-bar">
                  <div
                    class="zen-swimlane-bar-fill"
                    :style="{ width: `${item.progressPct ?? 50}%` }"
                  />
                </div>
              </div>
            </div>
          </div>
        </section>

        <!-- Zone C: Autonomous Archive (Feishu Triage & Auto-Merged) -->
        <section class="zen-studio-zone">
          <header class="zen-zone-header">
            <div class="zen-zone-title">
              <span class="zen-dot zen-dot--done" />
              <h2>FEISHU TRIAGE &amp; AUTO-MERGED</h2>
              <span class="zen-zone-count zen-mono zen-num">{{ doneTasks.length }}</span>
            </div>
            <span class="zen-zone-sub">Zero-toil patrol · InfoWall synced · ephemeral sessions released</span>
          </header>

          <div class="zen-archive-ledger">
            <div
              v-for="item in doneTasks"
              :key="item.id"
              :class="['zen-archive-row', { selected: selectedTask.id === item.id }]"
              tabindex="0"
              @click="selectTask(item.id)"
              @keydown.enter="selectTask(item.id)"
            >
              <span class="zen-dot zen-dot--done" />
              <span class="zen-mono zen-pro-code">{{ item.code }}</span>
              <span class="zen-archive-title">{{ item.title }}</span>
              <span class="zen-archive-flow">{{ item.consensus.roundsLabel }}</span>
              <span class="zen-mono zen-archive-checks">✓ {{ item.checksLabel }}</span>
              <span class="zen-mono zen-num zen-archive-time">{{ item.elapsed }}</span>
            </div>
          </div>
        </section>
      </main>

      <!-- Right Docked Stage Spine & Transcript Inspector -->
      <aside
        class="zen-studio-inspector"
        aria-label="Stage Spine Inspector"
      >
        <div class="zen-inspector-head">
          <div class="zen-inspector-meta">
            <span class="zen-micro-heading">STAGE SPINE INSPECTOR</span>
            <span class="zen-mono zen-num">{{ selectedTask.code }} · {{ selectedTask.elapsed }}</span>
          </div>
          <h3 class="zen-inspector-title">
            {{ selectedTask.title }}
          </h3>
          <p class="zen-inspector-summary">
            {{ selectedTask.outcomeSummary }}
          </p>
          <div class="zen-inspector-links">
            <a
              v-for="link in selectedTask.links"
              :key="link.label"
              :href="link.url"
              target="_blank"
              rel="noopener noreferrer"
              class="zen-text-link"
            >
              {{ link.label }} ↗
            </a>
            <span class="zen-mono zen-checks-note">· ✓ {{ selectedTask.checksLabel }}</span>
          </div>
        </div>

        <div class="zen-inspector-body">
          <ol class="zen-spine zen-spine--compact">
            <template
              v-for="node in selectedTask.timeline"
              :key="node.id"
            >
              <li
                v-if="node.epochLabel"
                class="zen-epoch-row"
              >
                <span class="zen-epoch-label zen-mono">{{ node.epochLabel }}</span>
                <span class="zen-epoch-line" />
              </li>
              <li :class="['zen-spine-item', `zen-spine-item--${node.tone}`, `zen-spine-role--${node.actorRole}`]">
                <div class="zen-spine-time-col zen-num">
                  <span class="zen-spine-time">{{ node.timeLabel }}</span>
                  <span class="zen-spine-delta zen-mono">{{ node.deltaLabel }}</span>
                </div>
                <span class="zen-spine-track">
                  <i class="zen-spine-node" />
                </span>
                <div class="zen-spine-body">
                  <div class="zen-spine-head">
                    <strong>{{ node.stage }}</strong>
                    <span
                      v-if="node.badge"
                      :class="['zen-node-badge', 'zen-mono', `zen-node-badge--${node.tone}`]"
                    >
                      {{ node.badge }}
                    </span>
                  </div>
                  <p>{{ node.summary }}</p>
                  <div
                    v-if="node.receipt"
                    class="zen-spine-evidence zen-mono"
                  >
                    ✓ {{ node.receipt }}
                  </div>
                </div>
              </li>
            </template>
          </ol>

          <div class="zen-inspector-transcript">
            <div class="zen-micro-heading">
              VERIFICATION TRANSCRIPT
            </div>
            <div class="zen-log-excerpt zen-log-excerpt--flush">
              <div
                v-for="(line, idx) in selectedTask.transcriptExcerpt"
                :key="idx"
                class="zen-log-row"
              >
                <span class="zen-mono zen-num">{{ line.time }}</span>
                <span :class="['zen-mono', 'zen-log-speaker', `zen-log-speaker--${line.role}`]">
                  {{ line.speaker }}
                </span>
                <span>{{ line.text }}</span>
              </div>
            </div>
          </div>
        </div>
      </aside>
    </div>

    <!-- =======================================================================
         CANDIDATE 3: 3 · REVIEW BLUEPRINT (3-Pane Stripe Workbench × PR Checks)
         ======================================================================= -->
    <div
      v-else
      class="zen-blueprint"
    >
      <!-- Left Pane: 268px Triage Rail -->
      <aside
        class="zen-bp-rail"
        aria-label="Blueprint Triage Rail"
      >
        <section
          v-for="group in groupedTasks"
          :key="group.key"
          class="zen-group"
        >
          <header class="zen-group-header">
            <span>{{ group.label }}</span>
            <span class="zen-num">{{ group.items.length }}</span>
          </header>

          <div
            v-for="item in group.items"
            :key="item.id"
            :class="['zen-pro-row', { active: selectedTask.id === item.id }]"
            tabindex="0"
            @click="selectTask(item.id)"
            @keydown.enter="selectTask(item.id)"
          >
            <div class="zen-pro-row-top">
              <span :class="['zen-dot', `zen-dot--${item.statusTone}`]" />
              <span class="zen-pro-code zen-mono">{{ item.code }}</span>
              <span :class="['zen-pro-state', `zen-pro-state--${item.statusTone}`]">
                {{ item.statusLabel }}
              </span>
              <span class="zen-pro-elapsed zen-num">{{ item.elapsed }}</span>
            </div>
            <div class="zen-pro-row-title">
              {{ item.title }}
            </div>
          </div>
        </section>
      </aside>

      <!-- Center Pane: Iterative Run & Transcript Spine -->
      <main class="zen-bp-center">
        <header class="zen-bp-header">
          <div class="zen-meta-line">
            <span class="zen-mono zen-meta-code">{{ selectedTask.code }}</span>
            <span class="zen-sep">·</span>
            <span class="zen-mono">{{ selectedTask.workspace }}</span>
            <span class="zen-sep">·</span>
            <span class="zen-mono">{{ selectedTask.branch }}</span>
            <span class="zen-sep">·</span>
            <span class="zen-mono zen-num">{{ selectedTask.elapsed }} elapsed</span>
          </div>

          <h1 class="zen-bp-title">
            {{ selectedTask.title }}
          </h1>
          <p class="zen-bp-summary">
            {{ selectedTask.outcomeSummary }}
          </p>

          <!-- Horizontal Pipeline Stage Overview Strip -->
          <div class="zen-bp-pipeline">
            <div
              v-for="(node, idx) in selectedTask.timeline"
              :key="node.id"
              :class="['zen-bp-step', `zen-bp-step--${node.tone}`]"
            >
              <span class="zen-bp-step-num zen-mono zen-num">{{ idx + 1 }}</span>
              <span class="zen-bp-step-name">{{ node.shortStage }}</span>
              <span class="zen-bp-step-actor zen-mono">{{ node.deltaLabel }}</span>
            </div>
          </div>
        </header>

        <section class="zen-bp-spine-wrap">
          <div class="zen-spine-header">
            <h2 class="zen-micro-heading">
              CHRONOLOGICAL ENGINEERING SPINE
            </h2>
            <span class="zen-mono zen-spine-overview">
              {{ selectedTask.consensus.roundsLabel }}
            </span>
          </div>

          <ol class="zen-spine">
            <template
              v-for="node in selectedTask.timeline"
              :key="node.id"
            >
              <li
                v-if="node.epochLabel"
                class="zen-epoch-row"
              >
                <span class="zen-epoch-label zen-mono">{{ node.epochLabel }}</span>
                <span class="zen-epoch-line" />
              </li>

              <li :class="['zen-spine-item', `zen-spine-item--${node.tone}`, `zen-spine-role--${node.actorRole}`]">
                <div class="zen-spine-time-col zen-num">
                  <span class="zen-spine-time">{{ node.timeLabel }}</span>
                  <span class="zen-spine-delta zen-mono">{{ node.deltaLabel }}</span>
                </div>

                <span class="zen-spine-track">
                  <i class="zen-spine-node" />
                </span>

                <div class="zen-spine-body">
                  <div class="zen-spine-head">
                    <strong>{{ node.stage }}</strong>
                    <span class="zen-spine-actor">
                      {{ node.actor }}
                      <em class="zen-role-tag">{{ node.actorRole }}</em>
                    </span>
                    <span
                      v-if="node.badge"
                      :class="['zen-node-badge', 'zen-mono', `zen-node-badge--${node.tone}`]"
                    >
                      {{ node.badge }}
                    </span>
                  </div>

                  <p>{{ node.summary }}</p>

                  <div
                    v-if="node.filesTouched || node.receipt"
                    class="zen-spine-evidence zen-mono"
                  >
                    <span
                      v-if="node.filesTouched"
                      class="zen-evidence-files"
                    >
                      {{ node.filesTouched }}
                    </span>
                    <span
                      v-if="node.filesTouched && node.receipt"
                      class="zen-sep"
                    >·</span>
                    <span
                      v-if="node.receipt"
                      class="zen-evidence-receipt"
                    >
                      ✓ {{ node.receipt }}
                    </span>
                  </div>
                </div>
              </li>
            </template>
          </ol>
        </section>

        <section class="zen-bp-transcript-section">
          <h2 class="zen-micro-heading">
            SESSION VERIFICATION STREAM
          </h2>
          <div class="zen-log-excerpt zen-log-excerpt--flush">
            <div
              v-for="(line, idx) in selectedTask.transcriptExcerpt"
              :key="idx"
              class="zen-log-row"
            >
              <span class="zen-mono zen-num">{{ line.time }}</span>
              <span :class="['zen-mono', 'zen-log-speaker', `zen-log-speaker--${line.role}`]">
                {{ line.speaker }}
              </span>
              <span>{{ line.text }}</span>
            </div>
          </div>
        </section>
      </main>

      <!-- Right Pane: 332px Deliverable & Verification Spec Sheet -->
      <aside
        class="zen-bp-spec"
        aria-label="Deliverable and Verification Spec Sheet"
      >
        <!-- Block 1: Sign-off Gate -->
        <div class="zen-spec-block">
          <div class="zen-spec-block-head">
            <span class="zen-micro-heading">SIGN-OFF GATE</span>
            <span :class="['zen-verdict-badge', 'zen-mono', `zen-verdict-badge--${selectedTask.consensus.verdictTone}`]">
              {{ selectedTask.consensus.verdictLabel }}
            </span>
          </div>
          <p class="zen-bp-gate-copy">
            {{ selectedTask.decisionLine || selectedTask.liveStep || selectedTask.consensus.roundsLabel }}
          </p>
          <div
            v-if="selectedTask.bucket === 'needs_you'"
            class="zen-bp-cta-row"
          >
            <button
              type="button"
              class="zen-primary-btn zen-btn-full"
              @click="triggerAccept(selectedTask.id)"
            >
              {{ acceptedIds.has(selectedTask.id) ? 'Deliverable Accepted' : 'Accept Deliverable' }}
            </button>
            <button
              type="button"
              class="zen-ghost-btn"
            >
              Steer…
            </button>
          </div>
        </div>

        <!-- Block 2: Heterogeneous Consensus Matrix -->
        <div class="zen-spec-block">
          <div class="zen-micro-heading">
            HETEROGENEOUS CONSENSUS
          </div>
          <dl class="zen-kv-list">
            <div class="zen-kv-row">
              <dt>Implementer</dt>
              <dd class="zen-mono">
                {{ selectedTask.consensus.implementer }}
              </dd>
            </div>
            <div class="zen-kv-row">
              <dt>Reviewer</dt>
              <dd class="zen-mono">
                {{ selectedTask.consensus.reviewer }}
              </dd>
            </div>
            <div class="zen-kv-row">
              <dt>Convergence</dt>
              <dd>
                {{ selectedTask.consensus.roundsLabel }}
              </dd>
            </div>
            <div class="zen-kv-row">
              <dt>Key Metric</dt>
              <dd class="zen-mono zen-num">
                {{ selectedTask.consensus.metricHighlight }}
              </dd>
            </div>
          </dl>
        </div>

        <!-- Block 3: Diff Stat & Proportion Bar -->
        <div
          v-if="selectedTask.diffStat"
          class="zen-spec-block"
        >
          <div class="zen-spec-block-head">
            <span class="zen-micro-heading">DIFF SCOPE</span>
            <span class="zen-mono zen-num">{{ selectedTask.diffStat.files }} files</span>
          </div>
          <div class="zen-diff-summary-row zen-mono zen-num">
            <span class="zen-diff-add">+{{ selectedTask.diffStat.added }} added</span>
            <span class="zen-diff-del">−{{ selectedTask.diffStat.removed }} removed</span>
          </div>
          <div class="zen-diff-bar">
            <span
              class="zen-diff-bar-add"
              :style="{ width: `${diffAddPct(selectedTask)}%` }"
            />
            <span
              class="zen-diff-bar-del"
              :style="{ width: `${100 - diffAddPct(selectedTask)}%` }"
            />
          </div>
        </div>

        <!-- Block 4: Deliverable Artifacts -->
        <div class="zen-spec-block">
          <div class="zen-micro-heading">
            DELIVERABLE ARTIFACTS
          </div>
          <div class="zen-bp-artifacts">
            <a
              v-for="link in selectedTask.links"
              :key="link.label"
              :href="link.url"
              target="_blank"
              rel="noopener noreferrer"
              class="zen-bp-artifact-card"
            >
              <div class="zen-bp-artifact-top">
                <strong>{{ link.label }}</strong>
                <span>↗</span>
              </div>
              <span class="zen-mono zen-bp-artifact-meta">{{ link.meta }}</span>
            </a>
          </div>
        </div>

        <!-- Block 5: Verification & Governance -->
        <div class="zen-spec-block">
          <div class="zen-micro-heading">
            VERIFICATION &amp; GOVERNANCE
          </div>
          <dl class="zen-kv-list">
            <div class="zen-kv-row">
              <dt>Checks</dt>
              <dd class="zen-mono">
                ✓ {{ selectedTask.checksLabel }}
              </dd>
            </div>
            <div class="zen-kv-row">
              <dt>Control</dt>
              <dd class="zen-mono">
                {{ selectedTask.internalMeta.control }} · {{ selectedTask.internalMeta.mode }}
              </dd>
            </div>
            <div class="zen-kv-row">
              <dt>Session Seat</dt>
              <dd class="zen-mono">
                epoch {{ selectedTask.internalMeta.epoch }} · {{ selectedTask.internalMeta.seat }}
              </dd>
            </div>
          </dl>
        </div>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  DEMO_STYLES,
  DEMO_TASKS,
  type DemoStyleId,
  type DemoTaskRecord,
} from './workspaceDemoData'

const props = defineProps<{
  initialStyle?: string
}>()

defineEmits<{
  exit: []
}>()

function normalizeStyle(raw?: string): DemoStyleId {
  if (raw === 'focused_inbox' || raw === 'precision_grid') return raw
  return 'linear_zen'
}

const activeStyle = ref<DemoStyleId>(normalizeStyle(props.initialStyle))
const workspaceFilter = ref<string>('all')
const selectedTaskId = ref<string>(DEMO_TASKS[0].id)
const openLogNodeId = ref<string | null>('t3')
const acceptedIds = ref<Set<string>>(new Set())

const currentStyleMeta = computed(() =>
  DEMO_STYLES.find(s => s.id === activeStyle.value) ?? DEMO_STYLES[0],
)

const workspaceNames = computed(() => [...new Set(DEMO_TASKS.map(t => t.workspace))])

const filteredTasks = computed<DemoTaskRecord[]>(() =>
  workspaceFilter.value === 'all'
    ? [...DEMO_TASKS]
    : DEMO_TASKS.filter(t => t.workspace === workspaceFilter.value),
)

const needsYouTasks = computed(() => filteredTasks.value.filter(t => t.bucket === 'needs_you'))
const workingTasks = computed(() => filteredTasks.value.filter(t => t.bucket === 'working'))
const doneTasks = computed(() => filteredTasks.value.filter(t => t.bucket === 'done'))

const groupedTasks = computed(() => [
  { key: 'needs_you', label: 'NEEDS YOU', items: needsYouTasks.value },
  { key: 'working', label: 'ACTIVE', items: workingTasks.value },
  { key: 'done', label: 'RECENT', items: doneTasks.value },
])

const selectedTask = computed<DemoTaskRecord>(() =>
  filteredTasks.value.find(t => t.id === selectedTaskId.value)
  ?? filteredTasks.value[0]
  ?? DEMO_TASKS[0],
)

function selectTask(id: string) {
  selectedTaskId.value = id
  if (id === '47e984bf') {
    openLogNodeId.value = 't3'
  } else {
    const task = DEMO_TASKS.find(t => t.id === id)
    openLogNodeId.value = task?.timeline.find(n => n.sessionTabId)?.id ?? null
  }
}

function triggerAccept(id: string) {
  const next = new Set(acceptedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  acceptedIds.value = next
}

function diffAddPct(task: DemoTaskRecord): number {
  if (!task.diffStat) return 100
  const total = task.diffStat.added + task.diffStat.removed
  return total <= 0 ? 100 : Math.max(8, Math.min(92, Math.round((task.diffStat.added / total) * 100)))
}

function handleKeydown(e: KeyboardEvent) {
  const target = e.target as HTMLElement | null
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT')) return
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault()
    triggerAccept(selectedTask.value.id)
    return
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return
  if (e.key === '1') activeStyle.value = 'linear_zen'
  else if (e.key === '2') activeStyle.value = 'focused_inbox'
  else if (e.key === '3') activeStyle.value = 'precision_grid'
  else if (e.key === 'j' || e.key === 'ArrowDown') {
    const list = filteredTasks.value
    if (!list.length) return
    const idx = list.findIndex(t => t.id === selectedTask.value.id)
    selectTask(list[(idx + 1) % list.length].id)
  } else if (e.key === 'k' || e.key === 'ArrowUp') {
    const list = filteredTasks.value
    if (!list.length) return
    const idx = list.findIndex(t => t.id === selectedTask.value.id)
    selectTask(list[(idx - 1 + list.length) % list.length].id)
  }
}

onMounted(() => window.addEventListener('keydown', handleKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', handleKeydown))
</script>

<style scoped>
/* ============================================================================
   DESIGN CRAFT TOKENS — High-Contrast Dark & Light (WCAG >= 5.0:1 guaranteed)
   ============================================================================ */
.zen-shell {
  --z-bg-canvas: #111113;
  --z-bg-panel: #16171a;
  --z-bg-elevated: #1c1d22;
  --z-bg-hover: #1f2127;
  --z-bg-selected: #22242b;
  --z-line: #2a2d35;
  --z-line-subtle: #1f2229;
  --z-line-strong: #3a3f4b;
  --z-fg-1: #edeef0;
  --z-fg-2: #b8bdc7;
  --z-fg-3: #949aa4;
  --z-accent: #3d4fd6;
  --z-accent-text: #a5b4ff;
  --z-amber: #f0b45a;
  --z-amber-bg: #261e12;
  --z-green: #5cd696;
  --z-green-bg: #12261c;
  --z-live-bg: #171c30;
  --z-red: #ff8a80;

  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--z-bg-canvas);
  color: var(--z-fg-1);
  font-family: var(--ch-font-sans);
  font-size: 13px;
  line-height: 1.5;
  overflow: hidden;
}

:root[data-theme='light'] .zen-shell {
  --z-bg-canvas: #f8f9fa;
  --z-bg-panel: #f1f3f5;
  --z-bg-elevated: #ffffff;
  --z-bg-hover: #e9ecef;
  --z-bg-selected: #e5e8ef;
  --z-line: #d5d9e2;
  --z-line-subtle: #e4e7ee;
  --z-line-strong: #b8bfcc;
  --z-fg-1: #141619;
  --z-fg-2: #353942;
  --z-fg-3: #505664;
  --z-accent: #3645c4;
  --z-accent-text: #313fb2;
  --z-amber: #944f04;
  --z-amber-bg: #fef3e2;
  --z-green: #126940;
  --z-green-bg: #e6f6ee;
  --z-live-bg: #e8ecfc;
  --z-red: #b42824;
}

.zen-num { font-variant-numeric: tabular-nums; }
.zen-mono { font-family: var(--ch-font-mono); font-size: 12px; }
.zen-icon { width: 14px; height: 14px; flex: 0 0 14px; }

/* Top Bar — 42px single quiet navigation line */
.zen-topbar {
  min-height: 42px;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 0 18px;
  border-bottom: 1px solid var(--z-line);
  background: var(--z-bg-canvas);
}
.zen-topbar-left, .zen-topbar-right { display: flex; align-items: center; gap: 8px; }
.zen-brand { font-weight: 600; color: var(--z-fg-1); letter-spacing: -0.01em; }
.zen-sep { color: var(--z-fg-3); }
.zen-ws-select {
  height: 26px;
  padding: 0 6px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: var(--z-bg-canvas);
  color: var(--z-fg-2);
  font-size: 12.5px;
  cursor: pointer;
}
.zen-ws-select:hover { background: var(--z-bg-hover); color: var(--z-fg-1); }
.zen-topbar-summary {
  padding-left: 6px;
  border-left: 1px solid var(--z-line);
  font-size: 11.5px;
  color: var(--z-fg-3);
}
.zen-style-tabs {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border-radius: 7px;
  background: var(--z-bg-panel);
  border: 1px solid var(--z-line);
}
.zen-style-tab {
  height: 26px;
  padding: 0 12px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--z-fg-2);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}
.zen-style-tab:hover { color: var(--z-fg-1); }
.zen-style-tab.active {
  background: var(--z-bg-elevated);
  color: var(--z-fg-1);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.16);
}
.zen-tab-short { display: none; }
.zen-ref-hint { font-size: 12px; color: var(--z-fg-3); }

.zen-ghost-btn {
  height: 28px;
  padding: 0 10px;
  border: 1px solid var(--z-line-strong);
  border-radius: 6px;
  background: var(--z-bg-elevated);
  color: var(--z-fg-1);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}
.zen-ghost-btn:hover { background: var(--z-bg-hover); }
.zen-primary-btn {
  height: 28px;
  padding: 0 12px;
  border: 0;
  border-radius: 6px;
  background: var(--z-accent);
  color: #ffffff;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}
.zen-btn-sm { height: 26px; padding: 0 10px; }
.zen-btn-full { flex: 1; }

.zen-dot, .zen-pulse-dot {
  width: 6px;
  height: 6px;
  flex: 0 0 6px;
  border-radius: 50%;
  background: var(--z-fg-3);
}
.zen-dot--attention { background: var(--z-amber); }
.zen-dot--working, .zen-pulse-dot { background: var(--z-accent-text); }
.zen-dot--done { background: var(--z-green); }

/* ============================================================================
   CANDIDATE 1: 1 · LINEAR TRIAGE PRO
   ============================================================================ */
.zen-linear {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 360px minmax(0, 1fr);
  overflow: hidden;
}
.zen-linear-list {
  border-right: 1px solid var(--z-line);
  background: var(--z-bg-panel);
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
.zen-linear-groups { flex: 1; overflow-y: auto; padding: 10px 0; }
.zen-group + .zen-group { margin-top: 12px; }
.zen-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px 4px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--z-fg-3);
}
.zen-pro-row {
  position: relative;
  padding: 8px 16px 9px;
  border-bottom: 1px solid var(--z-line-subtle);
  display: flex;
  flex-direction: column;
  gap: 3px;
  cursor: pointer;
}
.zen-pro-row:hover { background: var(--z-bg-hover); }
.zen-pro-row.active { background: var(--z-bg-selected); }
.zen-pro-row.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--z-accent-text);
}
.zen-pro-row-top { display: flex; align-items: center; gap: 7px; min-width: 0; }
.zen-pro-code { font-size: 11.5px; font-weight: 600; color: var(--z-fg-1); }
.zen-pro-ws {
  font-size: 11.5px;
  color: var(--z-fg-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zen-pro-state {
  margin-left: auto;
  font-size: 11px;
  font-weight: 500;
  color: var(--z-fg-2);
  white-space: nowrap;
}
.zen-pro-state--attention { color: var(--z-amber); }
.zen-pro-state--working { color: var(--z-accent-text); }
.zen-pro-state--done { color: var(--z-green); }
.zen-pro-elapsed { font-size: 11.5px; color: var(--z-fg-3); flex: 0 0 auto; }
.zen-pro-row-title {
  font-size: 13px;
  color: var(--z-fg-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zen-pro-row.active .zen-pro-row-title { color: var(--z-fg-1); font-weight: 500; }
.zen-pro-progress-track {
  margin-top: 3px;
  height: 2px;
  border-radius: 2px;
  background: var(--z-line);
  overflow: hidden;
}
.zen-pro-progress-fill { height: 100%; background: var(--z-accent-text); }

.zen-rail-footer {
  flex: 0 0 auto;
  padding: 8px 16px;
  border-top: 1px solid var(--z-line);
  background: var(--z-bg-canvas);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
  color: var(--z-fg-3);
}
.zen-kbd-group { display: inline-flex; align-items: center; gap: 3px; }
.zen-kbd-group span { margin-left: 2px; }
.zen-kbd-group kbd {
  padding: 1px 4px;
  border: 1px solid var(--z-line-strong);
  border-radius: 3px;
  background: var(--z-bg-panel);
  color: var(--z-fg-2);
  font-family: var(--ch-font-mono);
  font-size: 10.5px;
  line-height: 1.2;
}

/* Right Editorial Canvas */
.zen-linear-detail { overflow-y: auto; padding: 16px 36px 28px; }
.zen-editorial { max-width: 740px; margin: 0 auto; }
.zen-meta-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--z-fg-3);
  margin-bottom: 6px;
}
.zen-status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  border-radius: 4px;
  border: 1px solid var(--z-line-strong);
  background: var(--z-bg-panel);
  font-size: 11.5px;
  font-weight: 600;
  color: var(--z-fg-1);
}
.zen-status-pill--attention { color: var(--z-amber); background: var(--z-amber-bg); }
.zen-status-pill--working { color: var(--z-accent-text); background: var(--z-live-bg); }
.zen-status-pill--done { color: var(--z-green); background: var(--z-green-bg); }
.zen-meta-code { font-weight: 600; color: var(--z-fg-1); }
.zen-branch-chip { display: inline-flex; align-items: center; gap: 4px; color: var(--z-fg-2); }
.zen-meta-updated { margin-left: auto; color: var(--z-fg-3); }
.zen-diff-add { color: var(--z-green); font-weight: 500; }
.zen-diff-del { color: var(--z-red); font-weight: 500; }
.zen-title {
  margin: 0 0 4px;
  font-size: 19px;
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: -0.018em;
  color: var(--z-fg-1);
}
.zen-summary {
  margin: 0 0 11px;
  font-size: 13px;
  line-height: 1.52;
  color: var(--z-fg-2);
}

/* Architectural Deliverable & Consensus Spec Frame */
.zen-spec-frame {
  border: 1px solid var(--z-line-strong);
  border-radius: 8px;
  background: var(--z-bg-panel);
  overflow: hidden;
}
.zen-spec-gate {
  padding: 8px 14px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: var(--z-bg-elevated);
}
.zen-spec-gate-left {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex: 1;
}
.zen-verdict-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
  white-space: nowrap;
  border: 1px solid var(--z-line-strong);
  color: var(--z-fg-1);
  background: var(--z-bg-panel);
}
.zen-verdict-badge--pass { color: var(--z-green); background: var(--z-green-bg); }
.zen-verdict-badge--live { color: var(--z-accent-text); background: var(--z-live-bg); }
.zen-verdict-badge--auto { color: var(--z-fg-2); }
.zen-decision-copy { font-size: 12.5px; color: var(--z-fg-1); line-height: 1.38; }
.zen-live-step-text { color: var(--z-fg-2); }
.zen-decision-btns { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; }
.zen-spec-progress-wrap { display: flex; align-items: center; gap: 8px; }
.zen-spec-progress-bar {
  width: 110px;
  height: 5px;
  border-radius: 3px;
  background: var(--z-line);
  overflow: hidden;
}
.zen-spec-progress-fill { height: 100%; background: var(--z-accent-text); }
.zen-checks-pill { color: var(--z-green); }

.zen-spec-consensus {
  border-top: 1px solid var(--z-line);
  display: grid;
  grid-template-columns: 0.9fr 1fr 1.35fr;
}
.zen-consensus-cell {
  padding: 6px 14px;
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}
.zen-consensus-cell + .zen-consensus-cell { border-left: 1px solid var(--z-line); }
.zen-cell-label {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--z-fg-3);
}
.zen-cell-val {
  font-size: 12px;
  font-weight: 500;
  color: var(--z-fg-1);
  display: flex;
  align-items: center;
  gap: 5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zen-arrow { color: var(--z-fg-3); }

.zen-spec-artifacts {
  border-top: 1px solid var(--z-line);
  padding: 6px 14px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.zen-artifacts-list { display: flex; flex-wrap: wrap; align-items: center; gap: 14px; }
.zen-artifact-row {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  text-decoration: none;
  color: var(--z-fg-1);
  font-size: 12px;
}
.zen-artifact-row:hover .zen-artifact-label {
  color: var(--z-accent-text);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.zen-artifact-icon, .zen-artifact-label { color: var(--z-accent-text); font-weight: 500; }
.zen-artifact-meta, .zen-artifact-ext { color: var(--z-fg-3); font-size: 11px; }
.zen-checks-note { font-size: 11.5px; color: var(--z-green); }
.zen-text-link {
  color: var(--z-accent-text);
  font-size: 12.5px;
  font-weight: 500;
  text-decoration: none;
}
.zen-text-link:hover { text-decoration: underline; text-underline-offset: 3px; }

/* Sculptural Epoch Run Spine */
.zen-spine-section { margin-top: 14px; }
.zen-spine-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
}
.zen-micro-heading {
  margin: 0;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.07em;
  color: var(--z-fg-3);
}
.zen-spine-overview { color: var(--z-fg-3); font-size: 11.5px; }
.zen-spine { list-style: none; margin: 0; padding: 0; }
.zen-epoch-row { display: flex; align-items: center; gap: 10px; margin: 6px 0; }
.zen-epoch-row:first-child { margin-top: 0; }
.zen-epoch-label {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--z-fg-3);
  white-space: nowrap;
}
.zen-epoch-line { flex: 1; height: 1px; background: var(--z-line); }

.zen-spine-item {
  display: grid;
  grid-template-columns: 44px 20px minmax(0, 1fr);
  gap: 0 8px;
}
.zen-spine-time-col { display: flex; flex-direction: column; align-items: flex-end; }
.zen-spine-time { font-size: 12px; color: var(--z-fg-2); line-height: 1.25; }
.zen-spine-delta { font-size: 10.5px; color: var(--z-fg-3); line-height: 1.15; }
.zen-spine-track { position: relative; display: flex; justify-content: center; }
.zen-spine-track::after {
  content: '';
  position: absolute;
  top: 13px;
  bottom: -4px;
  width: 1px;
  background: var(--z-line-strong);
}
.zen-spine-item:last-child .zen-spine-track::after { display: none; }
.zen-spine-node {
  margin-top: 4px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--z-bg-canvas);
  border: 1.5px solid var(--z-fg-3);
  z-index: 1;
}
.zen-spine-role--worker .zen-spine-node { background: var(--z-fg-2); border-color: var(--z-fg-2); }
.zen-spine-item--warn .zen-spine-node {
  border-radius: 2px;
  transform: rotate(45deg);
  border-color: var(--z-amber);
  background: var(--z-amber);
}
.zen-spine-item--pass .zen-spine-node { border-color: var(--z-green); background: var(--z-green); }
.zen-spine-item--live .zen-spine-node { border-color: var(--z-accent-text); background: var(--z-accent-text); }

.zen-spine-body { padding-bottom: 9px; min-width: 0; }
.zen-spine-head { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }
.zen-spine-head strong { font-size: 13px; font-weight: 600; color: var(--z-fg-1); }
.zen-spine-actor { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--z-fg-2); }
.zen-role-tag { font-style: normal; font-size: 10.5px; color: var(--z-fg-3); }
.zen-node-badge {
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--z-line-strong);
  background: var(--z-bg-panel);
  color: var(--z-fg-2);
  font-size: 10.5px;
  font-weight: 600;
}
.zen-node-badge--warn { color: var(--z-amber); background: var(--z-amber-bg); }
.zen-node-badge--pass { color: var(--z-green); background: var(--z-green-bg); }
.zen-node-badge--live { color: var(--z-accent-text); background: var(--z-live-bg); }
.zen-inline-link {
  margin-left: auto;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--z-accent-text);
  font-size: 11.5px;
  cursor: pointer;
}
.zen-inline-link:hover { text-decoration: underline; }
.zen-spine-body p { margin: 1px 0 0; font-size: 12.5px; color: var(--z-fg-2); line-height: 1.42; }
.zen-spine-evidence {
  margin-top: 2px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 11px;
}
.zen-evidence-files { color: var(--z-fg-2); }
.zen-evidence-receipt { color: var(--z-fg-3); }

.zen-log-excerpt {
  margin-top: 5px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--z-bg-panel);
  border: 1px solid var(--z-line-strong);
  display: grid;
  gap: 3px;
}
.zen-log-excerpt--flush { margin-top: 6px; }
.zen-log-row {
  display: grid;
  grid-template-columns: 38px 90px minmax(0, 1fr);
  gap: 8px;
  font-size: 11.5px;
  color: var(--z-fg-2);
}
.zen-log-row > span:first-child { color: var(--z-fg-3); }
.zen-log-speaker { font-weight: 600; color: var(--z-fg-1); }
.zen-log-speaker--gate { color: var(--z-amber); }
.zen-log-speaker--reviewer { color: var(--z-green); }
.zen-log-speaker--worker { color: var(--z-accent-text); }

.zen-meta-disclosure {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--z-line);
  color: var(--z-fg-3);
  font-size: 11.5px;
}
.zen-meta-disclosure summary { cursor: pointer; user-select: none; }
.zen-meta-disclosure p { margin: 4px 0 0; }

/* ============================================================================
   CANDIDATE 2: 2 · COMMAND STUDIO
   ============================================================================ */
.zen-studio {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 400px;
  overflow: hidden;
}
.zen-studio-main {
  overflow-y: auto;
  padding: 20px 26px 36px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.zen-studio-zone { display: flex; flex-direction: column; gap: 10px; }
.zen-zone-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--z-line);
}
.zen-zone-title { display: flex; align-items: center; gap: 8px; }
.zen-zone-title h2 {
  margin: 0;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--z-fg-1);
}
.zen-zone-count {
  padding: 0 6px;
  border-radius: 4px;
  background: var(--z-bg-panel);
  border: 1px solid var(--z-line);
  color: var(--z-fg-2);
  font-size: 11px;
}
.zen-zone-sub { font-size: 12px; color: var(--z-fg-3); }
.zen-spotlight-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.zen-spotlight-card {
  padding: 13px 15px;
  border-radius: 8px;
  border: 1px solid var(--z-line);
  background: var(--z-bg-panel);
  display: flex;
  flex-direction: column;
  gap: 8px;
  cursor: pointer;
}
.zen-spotlight-card:hover { border-color: var(--z-line-strong); }
.zen-spotlight-card.selected { border-color: var(--z-accent-text); background: var(--z-bg-elevated); }
.zen-spotlight-top {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  font-size: 11.5px;
  color: var(--z-fg-3);
}
.zen-branch-label { color: var(--z-fg-2); }
.zen-spotlight-elapsed { margin-left: auto; color: var(--z-fg-3); }
.zen-spotlight-title { margin: 0; font-size: 14.5px; font-weight: 600; line-height: 1.35; color: var(--z-fg-1); }
.zen-spotlight-decision { margin: 0; font-size: 12.5px; line-height: 1.48; color: var(--z-fg-2); }
.zen-spotlight-consensus {
  padding: 7px 10px;
  border-radius: 6px;
  background: var(--z-bg-canvas);
  border: 1px solid var(--z-line);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.zen-spotlight-pair { display: flex; flex-direction: column; gap: 1px; margin-right: auto; }
.zen-spotlight-pair strong { font-size: 12px; color: var(--z-fg-1); }
.zen-spotlight-metric { font-size: 11px; color: var(--z-fg-2); }
.zen-spotlight-footer {
  margin-top: auto;
  padding-top: 4px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.zen-spotlight-links { display: flex; flex-wrap: wrap; gap: 10px; }

.zen-swimlanes { display: flex; flex-direction: column; gap: 8px; }
.zen-swimlane {
  padding: 10px 14px;
  border-radius: 7px;
  border: 1px solid var(--z-line);
  background: var(--z-bg-panel);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  cursor: pointer;
}
.zen-swimlane:hover { border-color: var(--z-line-strong); }
.zen-swimlane.selected { border-color: var(--z-accent-text); background: var(--z-bg-elevated); }
.zen-swimlane-main { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 3px; }
.zen-swimlane-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
.zen-swimlane-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--z-fg-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zen-swimlane-ws { color: var(--z-fg-3); font-size: 11.5px; white-space: nowrap; }
.zen-swimlane-ticker {
  padding-left: 14px;
  color: var(--z-fg-2);
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zen-swimlane-right {
  flex: 0 0 320px;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 5px;
}
.zen-swimlane-metrics {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--z-fg-2);
  white-space: nowrap;
}
.zen-swimlane-bar { width: 100%; height: 4px; border-radius: 2px; background: var(--z-line); overflow: hidden; }
.zen-swimlane-bar-fill { height: 100%; background: var(--z-accent-text); }

.zen-archive-ledger {
  border: 1px solid var(--z-line);
  border-radius: 7px;
  background: var(--z-bg-panel);
  overflow: hidden;
}
.zen-archive-row {
  padding: 9px 14px;
  display: grid;
  grid-template-columns: 6px 64px minmax(0, 1.3fr) minmax(0, 1fr) auto 40px;
  align-items: center;
  gap: 10px;
  cursor: pointer;
}
.zen-archive-row + .zen-archive-row { border-top: 1px solid var(--z-line); }
.zen-archive-row:hover { background: var(--z-bg-hover); }
.zen-archive-row.selected { background: var(--z-bg-selected); }
.zen-archive-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--z-fg-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zen-archive-flow {
  font-size: 12px;
  color: var(--z-fg-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zen-archive-checks { color: var(--z-green); font-size: 11.5px; }
.zen-archive-time { text-align: right; color: var(--z-fg-3); }

/* Right Docked Stage Spine Inspector */
.zen-studio-inspector {
  border-left: 1px solid var(--z-line);
  background: var(--z-bg-panel);
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
.zen-inspector-head {
  padding: 12px 16px 10px;
  border-bottom: 1px solid var(--z-line);
  background: var(--z-bg-elevated);
}
.zen-inspector-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
  color: var(--z-fg-3);
}
.zen-inspector-title { margin: 0 0 4px; font-size: 14px; font-weight: 600; color: var(--z-fg-1); }
.zen-inspector-summary { margin: 0 0 6px; font-size: 12px; line-height: 1.42; color: var(--z-fg-2); }
.zen-inspector-links { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.zen-inspector-body {
  flex: 1;
  overflow-y: auto;
  padding: 10px 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.zen-spine--compact .zen-spine-body { padding-bottom: 7px; }
.zen-inspector-transcript { padding-top: 8px; border-top: 1px solid var(--z-line); }

/* ============================================================================
   CANDIDATE 3: 3 · REVIEW BLUEPRINT (3-Pane Workbench)
   ============================================================================ */
.zen-blueprint {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 268px minmax(0, 1fr) 332px;
  overflow: hidden;
}
.zen-bp-rail {
  border-right: 1px solid var(--z-line);
  background: var(--z-bg-panel);
  overflow-y: auto;
  padding: 12px 0;
}
.zen-bp-center {
  overflow-y: auto;
  padding: 18px 26px 32px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.zen-bp-header { padding-bottom: 14px; border-bottom: 1px solid var(--z-line); }
.zen-bp-title {
  margin: 0 0 5px;
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.015em;
  color: var(--z-fg-1);
}
.zen-bp-summary { margin: 0 0 12px; font-size: 13px; line-height: 1.55; color: var(--z-fg-2); }
.zen-bp-pipeline {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 6px;
}
.zen-bp-step {
  padding: 5px 9px;
  border-radius: 5px;
  border: 1px solid var(--z-line);
  background: var(--z-bg-panel);
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.zen-bp-step--warn { border-color: var(--z-amber); background: var(--z-amber-bg); }
.zen-bp-step--pass { border-color: var(--z-green); background: var(--z-green-bg); }
.zen-bp-step--live { border-color: var(--z-accent-text); background: var(--z-live-bg); }
.zen-bp-step-num { color: var(--z-fg-3); font-size: 11px; }
.zen-bp-step-name {
  font-weight: 600;
  color: var(--z-fg-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.zen-bp-step-actor { margin-left: auto; font-size: 11px; color: var(--z-fg-3); }
.zen-bp-transcript-section { padding-top: 12px; border-top: 1px solid var(--z-line); }

/* Right Pane: 332px Spec Sheet */
.zen-bp-spec {
  border-left: 1px solid var(--z-line);
  background: var(--z-bg-panel);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}
.zen-spec-block {
  padding: 13px 16px;
  border-bottom: 1px solid var(--z-line);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.zen-spec-block-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.zen-bp-gate-copy { margin: 0; font-size: 12.5px; line-height: 1.48; color: var(--z-fg-2); }
.zen-bp-cta-row { display: flex; gap: 8px; }
.zen-kv-list { margin: 0; display: grid; gap: 6px; }
.zen-kv-row { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; font-size: 12px; }
.zen-kv-row dt { color: var(--z-fg-3); flex: 0 0 auto; }
.zen-kv-row dd { margin: 0; color: var(--z-fg-1); font-weight: 500; text-align: right; }
.zen-diff-summary-row { display: flex; justify-content: space-between; font-size: 12px; }
.zen-diff-bar { height: 6px; border-radius: 3px; overflow: hidden; display: flex; background: var(--z-line); }
.zen-diff-bar-add { background: var(--z-green); }
.zen-diff-bar-del { background: var(--z-red); }
.zen-bp-artifacts { display: flex; flex-direction: column; gap: 6px; }
.zen-bp-artifact-card {
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px solid var(--z-line-strong);
  background: var(--z-bg-elevated);
  text-decoration: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.zen-bp-artifact-card:hover { border-color: var(--z-accent-text); }
.zen-bp-artifact-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--z-accent-text);
  font-size: 12.5px;
}
.zen-bp-artifact-meta { color: var(--z-fg-3); font-size: 11px; }

/* Responsive / Mobile (390px viewport support, zero horizontal overflow) */
@media (max-width: 960px) {
  .zen-shell { overflow-y: auto; }
  .zen-topbar { flex-wrap: wrap; padding: 8px 12px; gap: 8px; }
  .zen-topbar-summary, .zen-ref-hint, .zen-tab-full, .zen-rail-footer, .zen-group-header { display: none; }
  .zen-tab-short { display: inline; }
  .zen-style-tabs { order: 3; width: 100%; justify-content: space-between; }
  .zen-style-tab { flex: 1; padding: 0 6px; text-align: center; }
  .zen-linear, .zen-studio, .zen-blueprint, .zen-spotlight-grid, .zen-spec-consensus {
    display: flex;
    flex-direction: column;
    overflow: visible;
  }
  .zen-linear-list {
    flex: 0 0 auto;
    border-right: 0;
    border-bottom: 1px solid var(--z-line);
    overflow: visible;
  }
  .zen-linear-groups {
    padding: 6px 10px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    overflow: visible;
  }
  .zen-group { display: contents; }
  .zen-pro-row {
    padding: 5px 10px;
    border: 1px solid var(--z-line);
    border-radius: 5px;
    flex-direction: row;
    align-items: center;
    gap: 6px;
  }
  .zen-pro-row.active::before { display: none; }
  .zen-pro-row.active { border-color: var(--z-accent-text); }
  .zen-pro-row-title, .zen-pro-ws, .zen-pro-state, .zen-pro-progress-track { display: none; }
  .zen-linear-detail { padding: 14px 14px 28px; overflow: visible; }
  .zen-consensus-cell + .zen-consensus-cell { border-left: 0; border-top: 1px solid var(--z-line); }
  .zen-log-row { grid-template-columns: 38px 76px minmax(0, 1fr); font-size: 11.5px; }
  .zen-swimlane { flex-direction: column; align-items: stretch; }
  .zen-swimlane-right { flex: 0 0 auto; align-items: flex-start; }
  .zen-archive-row { grid-template-columns: 6px 58px minmax(0, 1fr) 36px; }
  .zen-archive-flow, .zen-archive-checks { display: none; }
}
</style>
