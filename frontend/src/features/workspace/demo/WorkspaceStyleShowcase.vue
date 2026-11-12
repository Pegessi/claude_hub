<template>
  <div class="ws-demo-shell">
    <!-- Top Style Switcher Bar -->
    <header class="ws-demo-bar">
      <div class="ws-demo-bar-title">
        <span class="ws-demo-pill">UX Prototype</span>
        <h2>Workspace Task Showcase</h2>
        <p class="ws-demo-subtitle">
          {{ currentStyleMeta.tagline }}
        </p>
      </div>

      <div class="ws-demo-controls">
        <div
          class="ws-demo-segmented"
          role="tablist"
          aria-label="Workspace Demo Style"
        >
          <button
            v-for="opt in DEMO_STYLES"
            :key="opt.id"
            type="button"
            role="tab"
            :aria-selected="activeStyle === opt.id"
            :class="['ws-demo-tab', { active: activeStyle === opt.id }]"
            @click="selectStyle(opt.id)"
          >
            <strong>{{ opt.code }}</strong>
            <span>{{ opt.name }}</span>
          </button>
        </div>

        <select
          v-model="workspaceFilter"
          class="ws-demo-filter"
          aria-label="Filter by workspace"
        >
          <option value="all">
            All workspaces ({{ DEMO_TASKS.length }})
          </option>
          <option
            v-for="ws in workspaceNames"
            :key="ws"
            :value="ws"
          >
            {{ ws }}
          </option>
        </select>

        <button
          type="button"
          class="ws-demo-exit"
          @click="$emit('exit')"
        >
          Classic Board
        </button>
      </div>
    </header>

    <!-- ==================== STYLE A: LINEAR TRIAGE + RUN TIMELINE ==================== -->
    <div
      v-if="activeStyle === 'triage_timeline'"
      class="ws-style-a"
    >
      <div class="ws-a-list-pane">
        <section
          v-for="group in groupedTasks"
          :key="group.key"
          class="ws-a-group"
        >
          <header class="ws-a-group-head">
            <span :class="['ws-dot', `ws-dot--${group.dotTone}`]" />
            <h3>{{ group.label }}</h3>
            <span class="ws-count">{{ group.items.length }}</span>
          </header>

          <div class="ws-a-rows">
            <article
              v-for="item in group.items"
              :key="item.id"
              :class="['ws-a-row', { selected: selectedTask.id === item.id }]"
              tabindex="0"
              @click="selectedTaskId = item.id"
              @keydown.enter="selectedTaskId = item.id"
            >
              <div class="ws-a-row-main">
                <span :class="['ws-dot', `ws-dot--${item.statusTone}`]" />
                <span class="ws-a-row-title">{{ item.title }}</span>
              </div>
              <div class="ws-a-row-sub">
                <span class="ws-mono-chip">{{ item.workspace }}</span>
                <span class="ws-status-text">{{ item.statusBadge }}</span>
                <span class="ws-muted-text">{{ item.elapsed }} · {{ item.updatedAgo }}</span>
                <span
                  v-for="link in item.links.slice(0, 2)"
                  :key="link.label"
                  class="ws-link-pill"
                >{{ link.label }} ↗</span>
              </div>
            </article>
          </div>
        </section>
      </div>

      <!-- Style A Right Detail: 3-Section Clean Run Timeline -->
      <aside
        class="ws-a-detail-pane"
        aria-label="Selected task timeline"
      >
        <div class="ws-detail-head">
          <div class="ws-detail-badges">
            <span :class="['ws-tone-pill', `ws-tone-pill--${selectedTask.statusTone}`]">
              {{ selectedTask.statusBadge }}
            </span>
            <span class="ws-mono-chip">{{ selectedTask.workspace }} · {{ selectedTask.branch }}</span>
          </div>
          <h3>{{ selectedTask.title }}</h3>
          <p class="ws-detail-outcome">
            {{ selectedTask.outcomeHeadline }}
          </p>

          <div
            v-if="selectedTask.links.length > 0"
            class="ws-link-row"
          >
            <a
              v-for="link in selectedTask.links"
              :key="link.label"
              :href="link.url"
              target="_blank"
              rel="noopener noreferrer"
              class="ws-external-capsule"
            >
              {{ link.label }} ↗
            </a>
          </div>

          <div
            v-if="selectedTask.decisionPrompt"
            class="ws-decision-banner"
          >
            <p>{{ selectedTask.decisionPrompt }}</p>
            <div class="ws-decision-actions">
              <button
                type="button"
                class="ws-btn ws-btn--primary"
              >
                Accept &amp; Clean Session
              </button>
              <button
                type="button"
                class="ws-btn"
              >
                Steer / Request Revision
              </button>
            </div>
          </div>
        </div>

        <div class="ws-timeline-section">
          <div class="ws-section-label">
            <span>Run Timeline ({{ selectedTask.timeline.length }} stages)</span>
            <span>{{ selectedTask.acceptancePassed }}/{{ selectedTask.acceptanceTotal }} checks verified</span>
          </div>

          <ol class="ws-run-timeline">
            <li
              v-for="node in selectedTask.timeline"
              :key="node.id"
              :class="['ws-run-node', `ws-run-node--${node.tone}`]"
            >
              <div class="ws-run-rail">
                <span class="ws-run-marker" />
              </div>
              <div class="ws-run-card">
                <div class="ws-run-card-top">
                  <strong>{{ node.stage }}</strong>
                  <span class="ws-model-tag">{{ node.modelBadge }}</span>
                  <span class="ws-run-time">{{ node.timeLabel }} ({{ node.deltaLabel }})</span>
                </div>
                <p>{{ node.summary }}</p>
                <div
                  v-if="node.verification"
                  class="ws-run-verify"
                >
                  ✓ {{ node.verification }}
                </div>
                <div
                  v-if="node.sessionTabId"
                  class="ws-run-links"
                >
                  <button
                    type="button"
                    class="ws-ghost-link"
                    @click="peekTranscriptTabId = peekTranscriptTabId === node.id ? null : node.id"
                  >
                    {{ peekTranscriptTabId === node.id ? 'Hide conversation excerpt' : `View ${node.sessionArchived ? 'archived ' : ''}conversation (${node.sessionTabId})` }}
                  </button>
                </div>
                <div
                  v-if="peekTranscriptTabId === node.id"
                  class="ws-inline-transcript"
                >
                  <div
                    v-for="(line, idx) in selectedTask.transcriptPreview"
                    :key="idx"
                    class="ws-transcript-line"
                  >
                    <span class="ws-transcript-speaker">{{ line.speaker }}</span>
                    <span class="ws-transcript-text">{{ line.text }}</span>
                  </div>
                </div>
              </div>
            </li>
          </ol>
        </div>

        <details class="ws-internal-drawer">
          <summary>Raw Prompt &amp; Scheduler Metadata (collapsed by default)</summary>
          <div class="ws-internal-grid">
            <div><span>Control</span><strong>{{ selectedTask.rawInternalMeta.executionControl }}</strong></div>
            <div><span>Mode</span><strong>{{ selectedTask.rawInternalMeta.taskMode }}</strong></div>
            <div><span>Epoch</span><strong>{{ selectedTask.rawInternalMeta.epoch }}</strong></div>
            <div><span>Seat</span><strong>{{ selectedTask.rawInternalMeta.sessionSeat }}</strong></div>
          </div>
          <pre class="ws-raw-prompt">{{ selectedTask.rawInternalMeta.rawPromptPreview }}</pre>
        </details>
      </aside>
    </div>

    <!-- ==================== STYLE B: COMPACT BOARD + WALKTHROUGH DELIVERABLE ==================== -->
    <div
      v-else-if="activeStyle === 'walkthrough_board'"
      class="ws-style-b"
    >
      <div class="ws-b-columns">
        <section
          v-for="group in groupedTasks"
          :key="group.key"
          class="ws-b-col"
        >
          <header class="ws-b-col-head">
            <span :class="['ws-dot', `ws-dot--${group.dotTone}`]" />
            <h3>{{ group.label }}</h3>
            <span class="ws-count">{{ group.items.length }}</span>
          </header>

          <div class="ws-b-cards">
            <article
              v-for="item in group.items"
              :key="item.id"
              :class="['ws-b-card', { selected: selectedTask.id === item.id }]"
              tabindex="0"
              @click="selectedTaskId = item.id"
              @keydown.enter="selectedTaskId = item.id"
            >
              <div class="ws-b-card-top">
                <span class="ws-mono-chip">{{ item.workspace }}</span>
                <span class="ws-muted-text">{{ item.elapsed }}</span>
              </div>
              <h4>{{ item.title }}</h4>
              <p class="ws-b-card-outcome">
                {{ item.outcomeHeadline }}
              </p>
              <div class="ws-b-card-foot">
                <span :class="['ws-tone-pill', `ws-tone-pill--${item.statusTone}`]">
                  {{ item.statusBadge }}
                </span>
                <span class="ws-check-ratio">✓ {{ item.acceptancePassed }}/{{ item.acceptanceTotal }}</span>
              </div>
            </article>
          </div>
        </section>
      </div>

      <!-- Walkthrough Deliverable Sheet -->
      <section
        class="ws-b-walkthrough"
        aria-label="Deliverable Walkthrough"
      >
        <div class="ws-b-walk-header">
          <div>
            <span class="ws-eyebrow">Deliverable Walkthrough</span>
            <h3>{{ selectedTask.title }}</h3>
          </div>
          <div
            v-if="selectedTask.bucket === 'needs_you'"
            class="ws-decision-actions"
          >
            <button
              type="button"
              class="ws-btn ws-btn--primary"
            >
              Approve Deliverable
            </button>
            <button
              type="button"
              class="ws-btn"
            >
              Send Feedback
            </button>
          </div>
        </div>

        <div class="ws-b-walk-grid">
          <div class="ws-b-walk-card">
            <h4>Executive Outcome</h4>
            <p>{{ selectedTask.outcomeHeadline }}</p>
            <div class="ws-link-row">
              <a
                v-for="link in selectedTask.links"
                :key="link.label"
                :href="link.url"
                target="_blank"
                rel="noopener noreferrer"
                class="ws-external-capsule"
              >
                {{ link.label }} ↗
              </a>
            </div>
          </div>

          <div class="ws-b-walk-card">
            <h4>Cross-Model Consensus &amp; Checks</h4>
            <p class="ws-b-consensus">
              {{ selectedTask.reviewRoundsSummary }}
            </p>
            <div
              v-if="selectedTask.changedFiles.length > 0"
              class="ws-file-chips"
            >
              <span
                v-for="file in selectedTask.changedFiles"
                :key="file"
                class="ws-mono-chip"
              >{{ file }}</span>
            </div>
          </div>
        </div>

        <div class="ws-b-stepper">
          <div
            v-for="node in selectedTask.timeline"
            :key="node.id"
            :class="['ws-b-step', `ws-b-step--${node.tone}`]"
          >
            <span class="ws-b-step-time">{{ node.timeLabel }} ({{ node.deltaLabel }})</span>
            <strong>{{ node.stage }}</strong>
            <span class="ws-model-tag">{{ node.modelBadge }}</span>
            <p>{{ node.summary }}</p>
          </div>
        </div>
      </section>
    </div>

    <!-- ==================== STYLE C: LIVE COMMAND CENTER ==================== -->
    <div
      v-else
      class="ws-style-c"
    >
      <div class="ws-c-grid">
        <article
          v-for="item in filteredTasks"
          :key="item.id"
          :class="['ws-c-unit', { selected: selectedTask.id === item.id }]"
          tabindex="0"
          @click="selectedTaskId = item.id"
          @keydown.enter="selectedTaskId = item.id"
        >
          <div class="ws-c-unit-head">
            <span :class="['ws-dot', `ws-dot--${item.statusTone}`]" />
            <strong>{{ item.title }}</strong>
            <span class="ws-c-elapsed">{{ item.elapsed }}</span>
          </div>

          <div class="ws-c-meta-strip">
            <span class="ws-mono-chip">{{ item.workspace }}</span>
            <span class="ws-mono-chip">{{ item.branch }}</span>
            <span :class="['ws-tone-pill', `ws-tone-pill--${item.statusTone}`]">{{ item.statusBadge }}</span>
          </div>

          <div class="ws-c-live-box">
            <template v-if="item.liveActivityLine">
              <div class="ws-c-live-now">
                ▸ {{ item.liveActivityLine }}
              </div>
              <div class="ws-c-live-counts">
                {{ item.liveStepCounts }}
              </div>
            </template>
            <template v-else>
              <div class="ws-c-live-done">
                {{ item.outcomeHeadline }}
              </div>
            </template>
          </div>

          <!-- Inline micro-milestone dots -->
          <div class="ws-c-milestones">
            <span
              v-for="node in item.timeline"
              :key="node.id"
              :class="['ws-c-mile', `ws-c-mile--${node.tone}`]"
              :title="`${node.stage} (${node.deltaLabel})`"
            >
              <i />
              {{ node.stage.split('·')[0]?.trim() }}
            </span>
          </div>
        </article>
      </div>

      <!-- Command Center Bottom Split: Stage Log + Live/Archived Read-Only Transcript -->
      <section
        class="ws-c-inspector"
        aria-label="Command Center Inspector"
      >
        <div class="ws-c-inspector-col">
          <div class="ws-section-label">
            <span>Stage Progression · {{ selectedTask.id }}</span>
            <div class="ws-link-row">
              <a
                v-for="link in selectedTask.links"
                :key="link.label"
                :href="link.url"
                target="_blank"
                rel="noopener noreferrer"
                class="ws-link-pill"
              >{{ link.label }} ↗</a>
            </div>
          </div>
          <ol class="ws-c-stage-list">
            <li
              v-for="node in selectedTask.timeline"
              :key="node.id"
            >
              <span class="ws-run-time">{{ node.timeLabel }}</span>
              <strong>{{ node.stage }}</strong>
              <span class="ws-model-tag">{{ node.modelBadge }}</span>
              <p>{{ node.summary }}</p>
            </li>
          </ol>
        </div>

        <div class="ws-c-inspector-col ws-c-transcript-pane">
          <div class="ws-section-label">
            <span>Read-only Session Stream Preview</span>
            <span class="ws-mono-chip">{{ selectedTask.rawInternalMeta.sessionSeat }}</span>
          </div>
          <div class="ws-c-stream-lines">
            <div
              v-for="(line, idx) in selectedTask.transcriptPreview"
              :key="idx"
              :class="['ws-c-stream-row', `ws-c-stream-row--${line.role}`]"
            >
              <span class="ws-c-stream-time">{{ line.time }}</span>
              <span class="ws-c-stream-speaker">{{ line.speaker }}</span>
              <span class="ws-c-stream-body">{{ line.text }}</span>
            </div>
          </div>
          <div
            v-if="selectedTask.decisionPrompt"
            class="ws-c-quick-bar"
          >
            <span>{{ selectedTask.decisionPrompt }}</span>
            <button
              type="button"
              class="ws-btn ws-btn--primary"
            >
              Accept
            </button>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  DEMO_STYLES,
  DEMO_TASKS,
  type DemoStyleId,
  type DemoTaskRecord,
} from './workspaceDemoData'

const props = defineProps<{
  initialStyle?: DemoStyleId
}>()

defineEmits<{
  exit: []
}>()

const activeStyle = ref<DemoStyleId>(props.initialStyle ?? 'triage_timeline')
const workspaceFilter = ref<string>('all')
const selectedTaskId = ref<string>(DEMO_TASKS[0].id)
const peekTranscriptTabId = ref<string | null>('t3')

function selectStyle(id: DemoStyleId) {
  activeStyle.value = id
}

const currentStyleMeta = computed(() =>
  DEMO_STYLES.find(s => s.id === activeStyle.value) ?? DEMO_STYLES[0],
)

const workspaceNames = computed(() => {
  const set = new Set<string>()
  for (const t of DEMO_TASKS) set.add(t.workspace)
  return [...set]
})

const filteredTasks = computed<DemoTaskRecord[]>(() => {
  if (workspaceFilter.value === 'all') return [...DEMO_TASKS]
  return DEMO_TASKS.filter(t => t.workspace === workspaceFilter.value)
})

const groupedTasks = computed(() => [
  {
    key: 'needs_you',
    label: 'Waiting on you',
    dotTone: 'attention',
    items: filteredTasks.value.filter(t => t.bucket === 'needs_you'),
  },
  {
    key: 'working',
    label: 'Working',
    dotTone: 'working',
    items: filteredTasks.value.filter(t => t.bucket === 'working'),
  },
  {
    key: 'done',
    label: 'Recently done',
    dotTone: 'done',
    items: filteredTasks.value.filter(t => t.bucket === 'done'),
  },
])

const selectedTask = computed<DemoTaskRecord>(() =>
  filteredTasks.value.find(t => t.id === selectedTaskId.value)
  ?? filteredTasks.value[0]
  ?? DEMO_TASKS[0],
)
</script>

<style scoped>
.ws-demo-shell {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--ch-color-app-bg);
  color: var(--ch-color-text);
  overflow: hidden;
}

.ws-demo-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 18px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface-raised);
}

.ws-demo-bar-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
  min-width: 0;
}

.ws-demo-pill {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--ch-color-accent-soft, rgba(74, 140, 224, 0.14));
  border: 1px solid var(--ch-color-accent-ring-strong, rgba(74, 140, 224, 0.35));
  color: var(--ch-color-accent);
  font-size: 11px;
  font-weight: 600;
}

.ws-demo-bar-title h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--ch-color-text-strong);
}

.ws-demo-subtitle {
  flex: 1 1 100%;
  margin: 0;
  font-size: 12px;
  color: var(--ch-color-text-muted);
}

.ws-demo-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.ws-demo-segmented {
  display: inline-flex;
  padding: 3px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
  gap: 3px;
}

.ws-demo-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 30px;
  padding: 4px 10px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text-muted);
  font-size: 12px;
  cursor: pointer;
}

.ws-demo-tab strong {
  font-weight: 600;
  color: var(--ch-color-text);
}

.ws-demo-tab.active {
  background: var(--ch-color-surface-raised);
  border-color: var(--ch-color-accent);
  color: var(--ch-color-text-strong);
}

.ws-demo-filter,
.ws-demo-exit {
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: 12px;
  cursor: pointer;
}

/* Shared tokens */
.ws-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 999px;
  background: var(--ch-color-text-muted);
}
.ws-dot--attention { background: var(--ch-color-warning, #e5a44a); }
.ws-dot--working { background: var(--ch-color-accent, #4a8ce0); }
.ws-dot--done { background: var(--ch-color-success, #30a46c); }
.ws-dot--failed { background: var(--ch-color-danger, #e5484d); }

.ws-count {
  font-size: 11px;
  color: var(--ch-color-text-muted);
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--ch-color-surface-control);
}

.ws-mono-chip {
  display: inline-flex;
  align-items: center;
  padding: 2px 7px;
  border-radius: 6px;
  background: var(--ch-color-surface-control);
  border: 1px solid var(--ch-color-border-muted);
  color: var(--ch-color-text-muted);
  font-family: var(--ch-font-mono);
  font-size: 11px;
}

.ws-link-pill,
.ws-external-capsule {
  display: inline-flex;
  align-items: center;
  padding: 3px 9px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--ch-color-accent) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--ch-color-accent) 32%, transparent);
  color: var(--ch-color-accent);
  font-size: 11px;
  font-weight: 500;
  text-decoration: none;
}

.ws-tone-pill {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
}
.ws-tone-pill--attention {
  background: color-mix(in srgb, var(--ch-color-warning, #e5a44a) 15%, transparent);
  color: var(--ch-color-warning, #d9822b);
}
.ws-tone-pill--working {
  background: color-mix(in srgb, var(--ch-color-accent) 15%, transparent);
  color: var(--ch-color-accent);
}
.ws-tone-pill--done {
  background: color-mix(in srgb, var(--ch-color-success, #30a46c) 15%, transparent);
  color: var(--ch-color-success, #30a46c);
}

.ws-btn {
  min-height: 32px;
  padding: 0 12px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
.ws-btn--primary {
  background: var(--ch-color-accent-strong);
  border-color: var(--ch-color-accent-strong);
  color: #fff;
}

/* ==================== STYLE A ==================== */
.ws-style-a {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(340px, 46%) minmax(380px, 54%);
  overflow: hidden;
}

.ws-a-list-pane {
  overflow-y: auto;
  padding: 14px 16px;
  border-right: 1px solid var(--ch-color-border-muted);
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ws-a-group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.ws-a-group-head h3 {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--ch-color-text-muted);
}

.ws-a-rows {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.ws-a-row {
  padding: 9px 11px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  cursor: pointer;
  display: grid;
  gap: 5px;
}
.ws-a-row:hover {
  background: var(--ch-color-surface-raised);
}
.ws-a-row.selected {
  border-color: var(--ch-color-accent);
  background: color-mix(in srgb, var(--ch-color-accent) 8%, var(--ch-color-surface));
}

.ws-a-row-main {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ws-a-row-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--ch-color-text-strong);
  line-height: 1.35;
}
.ws-a-row-sub {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding-left: 16px;
  font-size: 11px;
}
.ws-status-text {
  color: var(--ch-color-text);
  font-weight: 500;
}
.ws-muted-text {
  color: var(--ch-color-text-muted);
}

.ws-a-detail-pane {
  overflow-y: auto;
  padding: 18px 22px;
  display: flex;
  flex-direction: column;
  gap: 18px;
  background: var(--ch-color-surface);
}

.ws-detail-head {
  display: grid;
  gap: 8px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--ch-color-border-muted);
}
.ws-detail-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.ws-detail-head h3 {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
  color: var(--ch-color-text-strong);
  line-height: 1.35;
}
.ws-detail-outcome {
  margin: 0;
  font-size: 13px;
  line-height: 1.55;
  color: var(--ch-color-text);
}
.ws-link-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.ws-decision-banner {
  margin-top: 4px;
  padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--ch-color-warning, #e5a44a) 45%, var(--ch-color-border));
  border-radius: var(--ch-radius-md);
  background: color-mix(in srgb, var(--ch-color-warning, #e5a44a) 8%, var(--ch-color-surface-raised));
  display: grid;
  gap: 10px;
}
.ws-decision-banner p {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.5;
}
.ws-decision-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.ws-section-label {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  font-weight: 600;
  color: var(--ch-color-text-muted);
  margin-bottom: 10px;
}

.ws-run-timeline {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ws-run-node {
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr);
  gap: 10px;
}

.ws-run-rail {
  position: relative;
  display: flex;
  justify-content: center;
}
.ws-run-rail::after {
  content: '';
  position: absolute;
  top: 14px;
  bottom: -12px;
  width: 1px;
  background: var(--ch-color-border-muted);
}
.ws-run-node:last-child .ws-run-rail::after {
  display: none;
}
.ws-run-marker {
  margin-top: 5px;
  width: 9px;
  height: 9px;
  border-radius: 999px;
  background: var(--ch-color-accent);
}
.ws-run-node--review_block .ws-run-marker { background: var(--ch-color-warning, #e5a44a); }
.ws-run-node--review_pass .ws-run-marker,
.ws-run-node--done .ws-run-marker { background: var(--ch-color-success, #30a46c); }

.ws-run-card {
  padding: 10px 12px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-raised);
  display: grid;
  gap: 6px;
}
.ws-run-card-top {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
}
.ws-model-tag {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--ch-color-surface-control);
  font-family: var(--ch-font-mono);
  font-size: 11px;
  color: var(--ch-color-text-muted);
}
.ws-run-time {
  margin-left: auto;
  font-family: var(--ch-font-mono);
  font-size: 11px;
  color: var(--ch-color-text-muted);
}
.ws-run-card p {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--ch-color-text);
}
.ws-run-verify {
  font-family: var(--ch-font-mono);
  font-size: 11px;
  color: var(--ch-color-success, #30a46c);
}
.ws-ghost-link {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--ch-color-accent);
  font-size: 11.5px;
  cursor: pointer;
  text-decoration: underline;
}
.ws-inline-transcript {
  margin-top: 4px;
  padding: 8px 10px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken);
  display: grid;
  gap: 5px;
  font-size: 11.5px;
}
.ws-transcript-line {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  gap: 8px;
}
.ws-transcript-speaker {
  font-family: var(--ch-font-mono);
  color: var(--ch-color-text-muted);
}

.ws-internal-drawer {
  border-top: 1px solid var(--ch-color-border-muted);
  padding-top: 10px;
  font-size: 12px;
  color: var(--ch-color-text-muted);
}
.ws-internal-drawer summary {
  cursor: pointer;
  font-weight: 500;
}
.ws-internal-grid {
  margin-top: 8px;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}
.ws-internal-grid div {
  padding: 6px 8px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken);
}
.ws-internal-grid span {
  display: block;
  font-size: 11px;
}
.ws-internal-grid strong {
  color: var(--ch-color-text);
  font-size: 12px;
}
.ws-raw-prompt {
  margin: 8px 0 0;
  padding: 8px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken);
  font-family: var(--ch-font-mono);
  font-size: 11px;
  white-space: pre-wrap;
}

/* ==================== STYLE B ==================== */
.ws-style-b {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ws-b-columns {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.ws-b-col {
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ws-b-col-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ws-b-col-head h3 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
}
.ws-b-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ws-b-card {
  padding: 10px 12px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-raised);
  cursor: pointer;
  display: grid;
  gap: 6px;
}
.ws-b-card.selected {
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 1px var(--ch-color-accent);
}
.ws-b-card-top,
.ws-b-card-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
}
.ws-b-card h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--ch-color-text-strong);
}
.ws-b-card-outcome {
  margin: 0;
  font-size: 12px;
  color: var(--ch-color-text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.ws-check-ratio {
  font-family: var(--ch-font-mono);
  color: var(--ch-color-success, #30a46c);
}

.ws-b-walkthrough {
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-raised);
  padding: 16px 18px;
  display: grid;
  gap: 14px;
}
.ws-b-walk-header {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.ws-eyebrow {
  font-size: 11px;
  font-weight: 600;
  color: var(--ch-color-accent);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.ws-b-walk-header h3 {
  margin: 2px 0 0;
  font-size: 16px;
}
.ws-b-walk-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.ws-b-walk-card {
  padding: 12px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  display: grid;
  gap: 8px;
}
.ws-b-walk-card h4 {
  margin: 0;
  font-size: 12px;
  color: var(--ch-color-text-muted);
}
.ws-b-walk-card p {
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
}
.ws-file-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.ws-b-stepper {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 10px;
}
.ws-b-step {
  padding: 10px;
  border-top: 2px solid var(--ch-color-accent);
  background: var(--ch-color-surface);
  border-radius: 0 0 var(--ch-radius-sm) var(--ch-radius-sm);
  display: grid;
  gap: 4px;
  font-size: 12px;
}
.ws-b-step--review_block { border-top-color: var(--ch-color-warning, #e5a44a); }
.ws-b-step--review_pass,
.ws-b-step--done { border-top-color: var(--ch-color-success, #30a46c); }
.ws-b-step-time {
  font-family: var(--ch-font-mono);
  font-size: 11px;
  color: var(--ch-color-text-muted);
}
.ws-b-step p {
  margin: 0;
  color: var(--ch-color-text-muted);
  font-size: 11.5px;
  line-height: 1.45;
}

/* ==================== STYLE C ==================== */
.ws-style-c {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-rows: minmax(220px, 52%) minmax(220px, 48%);
  overflow: hidden;
}

.ws-c-grid {
  overflow-y: auto;
  padding: 14px 18px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 10px;
  align-content: start;
}

.ws-c-unit {
  padding: 11px 13px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  cursor: pointer;
  display: grid;
  gap: 8px;
}
.ws-c-unit.selected {
  border-color: var(--ch-color-accent);
  background: var(--ch-color-surface-raised);
}
.ws-c-unit-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.ws-c-unit-head strong {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-c-elapsed {
  font-family: var(--ch-font-mono);
  font-size: 11px;
  color: var(--ch-color-text-muted);
}
.ws-c-meta-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.ws-c-live-box {
  padding: 7px 9px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken);
  font-family: var(--ch-font-mono);
  font-size: 11.5px;
  line-height: 1.45;
}
.ws-c-live-now { color: var(--ch-color-text-strong); }
.ws-c-live-counts { color: var(--ch-color-text-muted); font-size: 11px; margin-top: 2px; }
.ws-c-live-done { font-family: var(--ch-font-sans); color: var(--ch-color-text); }

.ws-c-milestones {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 11px;
  color: var(--ch-color-text-muted);
}
.ws-c-mile {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.ws-c-mile i {
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: var(--ch-color-accent);
}
.ws-c-mile--review_block i { background: var(--ch-color-warning, #e5a44a); }
.ws-c-mile--review_pass i,
.ws-c-mile--done i { background: var(--ch-color-success, #30a46c); }

.ws-c-inspector {
  border-top: 1px solid var(--ch-color-border);
  background: var(--ch-color-surface-raised);
  display: grid;
  grid-template-columns: 1fr 1fr;
  min-height: 0;
  overflow: hidden;
}
.ws-c-inspector-col {
  padding: 12px 16px;
  overflow-y: auto;
}
.ws-c-transcript-pane {
  border-left: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface);
  display: flex;
  flex-direction: column;
}
.ws-c-stage-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 8px;
  font-size: 12px;
}
.ws-c-stage-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--ch-color-border-muted);
}
.ws-c-stage-list p {
  flex: 1 1 100%;
  margin: 2px 0 0;
  color: var(--ch-color-text-muted);
}
.ws-c-stream-lines {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
}
.ws-c-stream-row {
  padding: 6px 8px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken);
  display: grid;
  grid-template-columns: 44px 120px minmax(0, 1fr);
  gap: 8px;
}
.ws-c-stream-time,
.ws-c-stream-speaker {
  font-family: var(--ch-font-mono);
  font-size: 11px;
  color: var(--ch-color-text-muted);
}
.ws-c-quick-bar {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--ch-color-border-muted);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
}

@media (max-width: 820px) {
  .ws-style-a {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }
  .ws-a-list-pane {
    border-right: 0;
    border-bottom: 1px solid var(--ch-color-border-muted);
    overflow: visible;
  }
  .ws-a-detail-pane {
    overflow: visible;
  }
  .ws-b-columns,
  .ws-b-walk-grid {
    grid-template-columns: 1fr;
  }
  .ws-style-c {
    grid-template-rows: auto auto;
    overflow-y: auto;
  }
  .ws-c-grid {
    grid-template-columns: 1fr;
    overflow: visible;
  }
  .ws-c-inspector {
    grid-template-columns: 1fr;
    overflow: visible;
  }
  .ws-c-transcript-pane {
    border-left: 0;
    border-top: 1px solid var(--ch-color-border-muted);
  }
  .ws-demo-tab span {
    display: none;
  }
  .ws-btn,
  .ws-demo-tab,
  .ws-demo-exit {
    min-height: 44px;
  }
}
</style>
