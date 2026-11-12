<template>
  <div class="zen-shell">
    <!-- Single, quiet 44px top bar — zero double headers, zero pill clutter -->
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
          {{ opt.label }}
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
         DIRECTION 1: LINEAR SPLIT (Borderless Hairline List + Editorial Spine)
         ======================================================================= -->
    <div
      v-if="activeStyle === 'linear_zen'"
      class="zen-linear"
    >
      <aside
        class="zen-linear-list"
        aria-label="Tasks"
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
            :class="['zen-row', { active: selectedTask.id === item.id }]"
            tabindex="0"
            @click="selectedTaskId = item.id"
            @keydown.enter="selectedTaskId = item.id"
          >
            <span :class="['zen-dot', `zen-dot--${item.statusTone}`]" />
            <span class="zen-row-title">{{ item.title }}</span>
            <span class="zen-row-meta zen-num">{{ item.elapsed }}</span>
          </div>
        </section>
      </aside>

      <main class="zen-linear-detail">
        <div class="zen-editorial">
          <div class="zen-meta-line">
            <span :class="['zen-status-word', `zen-status-word--${selectedTask.statusTone}`]">
              <i :class="['zen-dot', `zen-dot--${selectedTask.statusTone}`]" />
              {{ selectedTask.statusLabel }}
            </span>
            <span>·</span>
            <span class="zen-mono">{{ selectedTask.workspace }}</span>
            <span>·</span>
            <span class="zen-mono">{{ selectedTask.branch }}</span>
            <template v-if="selectedTask.diffStat">
              <span>·</span>
              <span class="zen-diff-add zen-num">+{{ selectedTask.diffStat.added }}</span>
              <span class="zen-diff-del zen-num">−{{ selectedTask.diffStat.removed }}</span>
            </template>
          </div>

          <h1 class="zen-title">
            {{ selectedTask.title }}
          </h1>

          <p class="zen-summary">
            {{ selectedTask.outcomeSummary }}
          </p>

          <div class="zen-links-bar">
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
            <span class="zen-checks-note">✓ {{ selectedTask.checksLabel }}</span>
          </div>

          <!-- Quiet, flat decision bar only when waiting on human -->
          <div
            v-if="selectedTask.decisionLine"
            class="zen-decision-bar"
          >
            <span class="zen-decision-copy">{{ selectedTask.decisionLine }}</span>
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
              >
                Accept
              </button>
            </div>
          </div>

          <div
            v-else-if="selectedTask.liveStep"
            class="zen-live-banner"
          >
            <span class="zen-pulse-dot" />
            <span class="zen-mono">{{ selectedTask.liveStep }}</span>
          </div>

          <!-- Pure Vertical Spine Timeline (zero card-in-card boxes) -->
          <section class="zen-spine-section">
            <h2 class="zen-micro-heading">
              TIMELINE
            </h2>
            <ol class="zen-spine">
              <li
                v-for="node in selectedTask.timeline"
                :key="node.id"
                :class="['zen-spine-item', `zen-spine-item--${node.tone}`]"
              >
                <span class="zen-spine-time zen-num">{{ node.timeLabel }}</span>
                <span class="zen-spine-track">
                  <i class="zen-spine-node" />
                </span>
                <div class="zen-spine-body">
                  <div class="zen-spine-head">
                    <strong>{{ node.stage }}</strong>
                    <span class="zen-spine-actor">{{ node.actor }} · {{ node.deltaLabel }}</span>
                    <button
                      v-if="node.sessionTabId"
                      type="button"
                      class="zen-inline-link"
                      @click="openLogNodeId = openLogNodeId === node.id ? null : node.id"
                    >
                      {{ openLogNodeId === node.id ? 'Hide log' : 'Transcript ↗' }}
                    </button>
                  </div>
                  <p>{{ node.summary }}</p>
                  <div
                    v-if="node.receipt"
                    class="zen-spine-receipt zen-mono"
                  >
                    {{ node.receipt }}
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
                      <span class="zen-mono">{{ line.time }}</span>
                      <span class="zen-mono">{{ line.speaker }}</span>
                      <span>{{ line.text }}</span>
                    </div>
                  </div>
                </div>
              </li>
            </ol>
          </section>

          <details class="zen-meta-disclosure">
            <summary>System metadata</summary>
            <p class="zen-mono">
              control={{ selectedTask.internalMeta.control }} · mode={{ selectedTask.internalMeta.mode }} · epoch={{ selectedTask.internalMeta.epoch }} · seat={{ selectedTask.internalMeta.seat }}
            </p>
          </details>
        </div>
      </main>
    </div>

    <!-- =======================================================================
         DIRECTION 2: CALM READER (Centered Single-Column Inbox, Things 3 Style)
         ======================================================================= -->
    <div
      v-else-if="activeStyle === 'focused_inbox'"
      class="zen-reader-scroll"
    >
      <div class="zen-reader-column">
        <header class="zen-reader-head">
          <div>
            <h1>Tasks</h1>
            <p>Only items waiting for your judgment are expanded; active and archived work stays quiet.</p>
          </div>
          <div class="zen-filter-pills">
            <button
              v-for="f in readerFilters"
              :key="f.id"
              type="button"
              :class="['zen-filter-btn', { active: readerBucket === f.id }]"
              @click="readerBucket = f.id"
            >
              {{ f.label }}
              <span class="zen-num">{{ f.count }}</span>
            </button>
          </div>
        </header>

        <div class="zen-reader-list">
          <article
            v-for="item in readerTasks"
            :key="item.id"
            :class="['zen-sheet-item', { expanded: expandedReaderId === item.id }]"
          >
            <div
              class="zen-sheet-summary"
              tabindex="0"
              @click="toggleReaderExpand(item.id)"
              @keydown.enter="toggleReaderExpand(item.id)"
            >
              <span :class="['zen-dot', `zen-dot--${item.statusTone}`]" />
              <span class="zen-sheet-title">{{ item.title }}</span>
              <span class="zen-sheet-ws zen-mono">{{ item.workspace }}</span>
              <span class="zen-sheet-state">{{ item.statusLabel }}</span>
              <span class="zen-sheet-time zen-num">{{ item.elapsed }}</span>
            </div>

            <!-- Smooth in-place editorial sheet when expanded -->
            <div
              v-if="expandedReaderId === item.id"
              class="zen-sheet-body"
            >
              <p class="zen-sheet-outcome">
                {{ item.outcomeSummary }}
              </p>

              <!-- Minimalist horizontal step track -->
              <div class="zen-step-track">
                <div
                  v-for="(node, idx) in item.timeline"
                  :key="node.id"
                  :class="['zen-step-cell', `zen-step-cell--${node.tone}`]"
                >
                  <div class="zen-step-line">
                    <i class="zen-step-dot" />
                    <span
                      v-if="idx < item.timeline.length - 1"
                      class="zen-step-connector"
                    />
                  </div>
                  <strong>{{ node.shortStage }}</strong>
                  <span class="zen-num">{{ node.timeLabel }}</span>
                </div>
              </div>

              <div class="zen-sheet-footer">
                <div class="zen-sheet-links">
                  <a
                    v-for="link in item.links"
                    :key="link.label"
                    :href="link.url"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="zen-text-link"
                  >
                    {{ link.label }} ↗
                  </a>
                  <span class="zen-checks-note">· ✓ {{ item.checksLabel }}</span>
                </div>

                <div
                  v-if="item.bucket === 'needs_you'"
                  class="zen-decision-btns"
                >
                  <button
                    type="button"
                    class="zen-ghost-btn"
                  >
                    Steer…
                  </button>
                  <button
                    type="button"
                    class="zen-primary-btn"
                  >
                    Accept
                  </button>
                </div>
              </div>
            </div>
          </article>
        </div>
      </div>
    </div>

    <!-- =======================================================================
         DIRECTION 3: TRACE LEDGER (Vercel / Raycast Swiss Precision Table)
         ======================================================================= -->
    <div
      v-else
      class="zen-ledger-wrap"
    >
      <div class="zen-ledger-table-scroll">
        <table class="zen-ledger">
          <thead>
            <tr>
              <th class="col-status">
                STATE
              </th>
              <th class="col-task">
                TASK
              </th>
              <th class="col-repo">
                BRANCH / DIFF
              </th>
              <th class="col-trace">
                PIPELINE STAGES
              </th>
              <th class="col-checks">
                VERIFICATION
              </th>
              <th class="col-time">
                TIME
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in filteredTasks"
              :key="item.id"
              :class="{ selected: selectedTask.id === item.id }"
              tabindex="0"
              @click="selectedTaskId = item.id"
              @keydown.enter="selectedTaskId = item.id"
            >
              <td>
                <span class="zen-ledger-state">
                  <i :class="['zen-dot', `zen-dot--${item.statusTone}`]" />
                  {{ item.bucket === 'needs_you' ? 'Review' : item.bucket === 'working' ? 'Running' : 'Done' }}
                </span>
              </td>
              <td>
                <div class="zen-ledger-title">
                  {{ item.title }}
                </div>
                <div class="zen-ledger-sub">
                  {{ item.liveStep || item.outcomeSummary }}
                </div>
              </td>
              <td class="zen-mono">
                <div>{{ item.workspace }}</div>
                <div class="zen-ledger-diff">
                  <span>{{ item.branch }}</span>
                  <template v-if="item.diffStat">
                    <span class="zen-diff-add">+{{ item.diffStat.added }}</span>
                    <span class="zen-diff-del">−{{ item.diffStat.removed }}</span>
                  </template>
                </div>
              </td>
              <td>
                <div class="zen-pip-bar">
                  <span
                    v-for="node in item.timeline"
                    :key="node.id"
                    :class="['zen-pip', `zen-pip--${node.tone}`]"
                    :title="`${node.stage} (${node.deltaLabel})`"
                  />
                  <span class="zen-pip-caption">{{ item.statusLabel }}</span>
                </div>
              </td>
              <td class="zen-mono zen-ledger-checks">
                {{ item.checksLabel }}
              </td>
              <td class="zen-num zen-ledger-time">
                {{ item.elapsed }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Crisp, monochrome bottom inspector strip -->
      <footer class="zen-ledger-drawer">
        <div class="zen-ledger-drawer-col">
          <div class="zen-drawer-head">
            <strong>{{ selectedTask.title }}</strong>
            <div class="zen-sheet-links">
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
            </div>
          </div>
          <ol class="zen-compact-stages">
            <li
              v-for="node in selectedTask.timeline"
              :key="node.id"
            >
              <span class="zen-mono zen-num">{{ node.timeLabel }}</span>
              <i :class="['zen-pip-dot', `zen-pip--${node.tone}`]" />
              <strong>{{ node.shortStage }}</strong>
              <span class="zen-compact-summary">{{ node.summary }}</span>
            </li>
          </ol>
        </div>

        <div class="zen-ledger-drawer-col zen-ledger-log">
          <div class="zen-drawer-head">
            <span class="zen-micro-heading">SESSION STREAM</span>
            <button
              v-if="selectedTask.bucket === 'needs_you'"
              type="button"
              class="zen-primary-btn zen-primary-btn--sm"
            >
              Accept Deliverable
            </button>
          </div>
          <div class="zen-log-excerpt zen-log-excerpt--flush">
            <div
              v-for="(line, idx) in selectedTask.transcriptExcerpt"
              :key="idx"
              class="zen-log-row"
            >
              <span class="zen-mono">{{ line.time }}</span>
              <span class="zen-mono">{{ line.speaker }}</span>
              <span>{{ line.text }}</span>
            </div>
          </div>
        </div>
      </footer>
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
const openLogNodeId = ref<string | null>(null)
const readerBucket = ref<'all' | 'needs_you' | 'working' | 'done'>('all')
const expandedReaderId = ref<string | null>(DEMO_TASKS[0].id)

function toggleReaderExpand(id: string) {
  expandedReaderId.value = expandedReaderId.value === id ? null : id
}

const currentStyleMeta = computed(() =>
  DEMO_STYLES.find(s => s.id === activeStyle.value) ?? DEMO_STYLES[0],
)

const workspaceNames = computed(() => [...new Set(DEMO_TASKS.map(t => t.workspace))])

const filteredTasks = computed<DemoTaskRecord[]>(() =>
  workspaceFilter.value === 'all'
    ? [...DEMO_TASKS]
    : DEMO_TASKS.filter(t => t.workspace === workspaceFilter.value),
)

const groupedTasks = computed(() => [
  {
    key: 'needs_you',
    label: 'NEEDS YOU',
    items: filteredTasks.value.filter(t => t.bucket === 'needs_you'),
  },
  {
    key: 'working',
    label: 'ACTIVE',
    items: filteredTasks.value.filter(t => t.bucket === 'working'),
  },
  {
    key: 'done',
    label: 'RECENT',
    items: filteredTasks.value.filter(t => t.bucket === 'done'),
  },
])

const readerFilters = computed(() => [
  { id: 'all' as const, label: 'All', count: filteredTasks.value.length },
  { id: 'needs_you' as const, label: 'Needs you', count: filteredTasks.value.filter(t => t.bucket === 'needs_you').length },
  { id: 'working' as const, label: 'Active', count: filteredTasks.value.filter(t => t.bucket === 'working').length },
  { id: 'done' as const, label: 'Done', count: filteredTasks.value.filter(t => t.bucket === 'done').length },
])

const readerTasks = computed(() =>
  readerBucket.value === 'all'
    ? filteredTasks.value
    : filteredTasks.value.filter(t => t.bucket === readerBucket.value),
)

const selectedTask = computed<DemoTaskRecord>(() =>
  filteredTasks.value.find(t => t.id === selectedTaskId.value)
  ?? filteredTasks.value[0]
  ?? DEMO_TASKS[0],
)
</script>

<style scoped>
/* ============================================================================
   DESIGN CRAFT TOKENS (Radix Slate Dark + Geist Hairline + Linear Scarcity)
   ============================================================================ */
.zen-shell {
  --z-bg-canvas: #111113;
  --z-bg-panel: #16171a;
  --z-bg-elevated: #1c1d21;
  --z-bg-hover: rgba(255, 255, 255, 0.04);
  --z-bg-selected: rgba(255, 255, 255, 0.065);
  --z-line: rgba(255, 255, 255, 0.075);
  --z-line-strong: rgba(255, 255, 255, 0.14);
  --z-fg-1: #edeef0;
  --z-fg-2: #a0a5ad;
  --z-fg-3: #696e77;
  --z-accent: #5e6ad2;
  --z-accent-text: #9aa6ff;
  --z-amber: #e5a44a;
  --z-green: #4cb782;

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
  --z-bg-canvas: #fafafa;
  --z-bg-panel: #f4f4f5;
  --z-bg-elevated: #ffffff;
  --z-bg-hover: rgba(0, 0, 0, 0.035);
  --z-bg-selected: rgba(0, 0, 0, 0.06);
  --z-line: rgba(0, 0, 0, 0.08);
  --z-line-strong: rgba(0, 0, 0, 0.16);
  --z-fg-1: #18181b;
  --z-fg-2: #52525b;
  --z-fg-3: #8a8f98;
  --z-accent: #4f5bd5;
  --z-accent-text: #434ec2;
  --z-amber: #c97a16;
  --z-green: #1e8e5a;
}

.zen-num {
  font-variant-numeric: tabular-nums;
}

.zen-mono {
  font-family: var(--ch-font-mono);
  font-size: 12px;
}

/* Top Bar — 44px single quiet navigation line */
.zen-topbar {
  height: 44px;
  flex: 0 0 44px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 0 20px;
  border-bottom: 1px solid var(--z-line);
  background: var(--z-bg-canvas);
}

.zen-topbar-left,
.zen-topbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.zen-brand {
  font-weight: 600;
  color: var(--z-fg-1);
  letter-spacing: -0.01em;
}

.zen-sep {
  color: var(--z-fg-3);
}

.zen-ws-select {
  height: 26px;
  padding: 0 6px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--z-fg-2);
  font-size: 13px;
  cursor: pointer;
}
.zen-ws-select:hover {
  background: var(--z-bg-hover);
  color: var(--z-fg-1);
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
  transition: color 120ms ease, background-color 120ms ease;
}
.zen-style-tab:hover {
  color: var(--z-fg-1);
}
.zen-style-tab.active {
  background: var(--z-bg-elevated);
  color: var(--z-fg-1);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
}

.zen-ref-hint {
  font-size: 12px;
  color: var(--z-fg-3);
}

.zen-ghost-btn {
  height: 28px;
  padding: 0 10px;
  border: 1px solid var(--z-line-strong);
  border-radius: 6px;
  background: transparent;
  color: var(--z-fg-2);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}
.zen-ghost-btn:hover {
  color: var(--z-fg-1);
  background: var(--z-bg-hover);
}

.zen-primary-btn {
  height: 28px;
  padding: 0 12px;
  border: 0;
  border-radius: 6px;
  background: var(--z-accent);
  color: #fff;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
}
.zen-primary-btn--sm {
  height: 26px;
  padding: 0 10px;
}

/* Status Dots — 6px restrained punctuation */
.zen-dot {
  width: 6px;
  height: 6px;
  flex: 0 0 6px;
  border-radius: 50%;
  background: var(--z-fg-3);
}
.zen-dot--attention { background: var(--z-amber); }
.zen-dot--working { background: var(--z-accent-text); }
.zen-dot--done { background: var(--z-green); opacity: 0.75; }

/* ============================================================================
   DIRECTION 1: LINEAR SPLIT
   ============================================================================ */
.zen-linear {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 380px minmax(0, 1fr);
  overflow: hidden;
}

.zen-linear-list {
  border-right: 1px solid var(--z-line);
  background: var(--z-bg-panel);
  overflow-y: auto;
  padding: 16px 0;
}

.zen-group + .zen-group {
  margin-top: 20px;
}

.zen-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 18px 6px;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.06em;
  color: var(--z-fg-3);
}

.zen-row {
  position: relative;
  height: 38px;
  padding: 0 18px;
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  transition: background-color 100ms ease;
}
.zen-row:hover {
  background: var(--z-bg-hover);
}
.zen-row.active {
  background: var(--z-bg-selected);
}
.zen-row.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 7px;
  bottom: 7px;
  width: 2px;
  border-radius: 0 2px 2px 0;
  background: var(--z-accent-text);
}

.zen-row-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--z-fg-2);
  font-size: 13px;
}
.zen-row.active .zen-row-title {
  color: var(--z-fg-1);
  font-weight: 500;
}

.zen-row-meta {
  flex: 0 0 auto;
  font-size: 12px;
  color: var(--z-fg-3);
}

.zen-linear-detail {
  overflow-y: auto;
  padding: 36px 48px 56px;
}

.zen-editorial {
  max-width: 660px;
  margin: 0 auto;
}

.zen-meta-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--z-fg-3);
  margin-bottom: 10px;
}

.zen-status-word {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
  color: var(--z-fg-2);
}
.zen-status-word--attention { color: var(--z-amber); }
.zen-status-word--working { color: var(--z-accent-text); }

.zen-diff-add { color: var(--z-green); }
.zen-diff-del { color: #e5726b; }

.zen-title {
  margin: 0 0 12px;
  font-size: 20px;
  font-weight: 600;
  line-height: 1.35;
  letter-spacing: -0.015em;
  color: var(--z-fg-1);
}

.zen-summary {
  margin: 0 0 16px;
  font-size: 14px;
  line-height: 1.65;
  color: var(--z-fg-2);
}

.zen-links-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 16px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--z-line);
}

.zen-text-link {
  color: var(--z-accent-text);
  font-size: 12.5px;
  font-weight: 500;
  text-decoration: none;
}
.zen-text-link:hover {
  text-decoration: underline;
  text-underline-offset: 3px;
}

.zen-checks-note {
  font-size: 12px;
  color: var(--z-fg-3);
}

.zen-decision-bar {
  margin-top: 20px;
  padding: 12px 16px;
  border-radius: 8px;
  background: var(--z-bg-panel);
  border: 1px solid var(--z-line-strong);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.zen-decision-copy {
  font-size: 13px;
  color: var(--z-fg-1);
  line-height: 1.45;
}

.zen-decision-btns {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
}

.zen-live-banner {
  margin-top: 20px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--z-bg-panel);
  border: 1px solid var(--z-line);
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--z-fg-2);
}

.zen-pulse-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--z-accent-text);
}

/* Spine Timeline — pure typography & single 1px vertical thread */
.zen-spine-section {
  margin-top: 32px;
}

.zen-micro-heading {
  margin: 0 0 16px;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.07em;
  color: var(--z-fg-3);
}

.zen-spine {
  list-style: none;
  margin: 0;
  padding: 0;
}

.zen-spine-item {
  display: grid;
  grid-template-columns: 44px 20px minmax(0, 1fr);
  gap: 0 8px;
}

.zen-spine-time {
  font-size: 12px;
  color: var(--z-fg-3);
  padding-top: 1px;
}

.zen-spine-track {
  position: relative;
  display: flex;
  justify-content: center;
}
.zen-spine-track::after {
  content: '';
  position: absolute;
  top: 14px;
  bottom: -6px;
  width: 1px;
  background: var(--z-line);
}
.zen-spine-item:last-child .zen-spine-track::after {
  display: none;
}

.zen-spine-node {
  margin-top: 6px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--z-bg-canvas);
  border: 1.5px solid var(--z-fg-3);
  z-index: 1;
}
.zen-spine-item--warn .zen-spine-node {
  border-color: var(--z-amber);
  background: var(--z-amber);
}
.zen-spine-item--pass .zen-spine-node {
  border-color: var(--z-green);
  background: var(--z-green);
}
.zen-spine-item--live .zen-spine-node {
  border-color: var(--z-accent-text);
  background: var(--z-accent-text);
}

.zen-spine-body {
  padding-bottom: 22px;
  min-width: 0;
}

.zen-spine-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
}
.zen-spine-head strong {
  font-size: 13.5px;
  font-weight: 500;
  color: var(--z-fg-1);
}
.zen-spine-actor {
  font-size: 12px;
  color: var(--z-fg-3);
}
.zen-inline-link {
  margin-left: auto;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--z-fg-3);
  font-size: 12px;
  cursor: pointer;
}
.zen-inline-link:hover {
  color: var(--z-accent-text);
}

.zen-spine-body p {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--z-fg-2);
  line-height: 1.55;
}

.zen-spine-receipt {
  margin-top: 6px;
  color: var(--z-fg-3);
}

.zen-log-excerpt {
  margin-top: 10px;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--z-bg-panel);
  border: 1px solid var(--z-line);
  display: grid;
  gap: 6px;
}
.zen-log-excerpt--flush {
  margin-top: 0;
}
.zen-log-row {
  display: grid;
  grid-template-columns: 44px 96px minmax(0, 1fr);
  gap: 10px;
  font-size: 12px;
  color: var(--z-fg-2);
}
.zen-log-row > span:first-child,
.zen-log-row > span:nth-child(2) {
  color: var(--z-fg-3);
}

.zen-meta-disclosure {
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--z-line);
  color: var(--z-fg-3);
  font-size: 12px;
}
.zen-meta-disclosure summary {
  cursor: pointer;
  user-select: none;
}
.zen-meta-disclosure p {
  margin: 8px 0 0;
}

/* ============================================================================
   DIRECTION 2: CALM READER (Single-Column Centered Editorial Inbox)
   ============================================================================ */
.zen-reader-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 40px 24px 64px;
}

.zen-reader-column {
  max-width: 740px;
  margin: 0 auto;
}

.zen-reader-head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--z-line);
}
.zen-reader-head h1 {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
}
.zen-reader-head p {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--z-fg-3);
}

.zen-filter-pills {
  display: flex;
  gap: 4px;
}
.zen-filter-btn {
  height: 28px;
  padding: 0 10px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--z-fg-2);
  font-size: 12.5px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}
.zen-filter-btn span {
  color: var(--z-fg-3);
}
.zen-filter-btn.active {
  background: var(--z-bg-selected);
  color: var(--z-fg-1);
  font-weight: 500;
}

.zen-reader-list {
  display: flex;
  flex-direction: column;
}

.zen-sheet-item {
  border-bottom: 1px solid var(--z-line);
}
.zen-sheet-item.expanded {
  margin: 12px -16px;
  padding: 4px 16px 18px;
  border: 1px solid var(--z-line-strong);
  border-radius: 10px;
  background: var(--z-bg-panel);
}

.zen-sheet-summary {
  min-height: 46px;
  display: grid;
  grid-template-columns: 6px minmax(0, 1fr) auto auto 44px;
  align-items: center;
  gap: 14px;
  cursor: pointer;
}

.zen-sheet-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--z-fg-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.zen-sheet-ws {
  color: var(--z-fg-3);
}

.zen-sheet-state {
  font-size: 12px;
  color: var(--z-fg-2);
}

.zen-sheet-time {
  text-align: right;
  font-size: 12px;
  color: var(--z-fg-3);
}

.zen-sheet-body {
  padding: 6px 0 0 20px;
  display: grid;
  gap: 18px;
}

.zen-sheet-outcome {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.65;
  color: var(--z-fg-2);
  max-width: 64ch;
}

.zen-step-track {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
  gap: 8px;
  padding: 12px 0;
  border-top: 1px solid var(--z-line);
  border-bottom: 1px solid var(--z-line);
}

.zen-step-cell {
  display: grid;
  gap: 3px;
}
.zen-step-line {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 3px;
}
.zen-step-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--z-fg-3);
  flex: 0 0 7px;
}
.zen-step-cell--warn .zen-step-dot { background: var(--z-amber); }
.zen-step-cell--pass .zen-step-dot { background: var(--z-green); }
.zen-step-cell--live .zen-step-dot { background: var(--z-accent-text); }

.zen-step-connector {
  flex: 1;
  height: 1px;
  background: var(--z-line-strong);
}

.zen-step-cell strong {
  font-size: 12px;
  font-weight: 500;
  color: var(--z-fg-1);
}
.zen-step-cell > span {
  font-size: 11px;
  color: var(--z-fg-3);
}

.zen-sheet-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.zen-sheet-links {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
}

/* ============================================================================
   DIRECTION 3: TRACE LEDGER (Swiss Grid Table + Clean Bottom Inspector)
   ============================================================================ */
.zen-ledger-wrap {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-rows: minmax(0, 1fr) 230px;
  overflow: hidden;
}

.zen-ledger-table-scroll {
  overflow-y: auto;
}

.zen-ledger {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
}

.zen-ledger thead th {
  position: sticky;
  top: 0;
  z-index: 2;
  height: 34px;
  padding: 0 18px;
  background: var(--z-bg-panel);
  border-bottom: 1px solid var(--z-line);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.06em;
  color: var(--z-fg-3);
}

.zen-ledger tbody tr {
  border-bottom: 1px solid var(--z-line);
  cursor: pointer;
  transition: background-color 100ms ease;
}
.zen-ledger tbody tr:hover {
  background: var(--z-bg-hover);
}
.zen-ledger tbody tr.selected {
  background: var(--z-bg-selected);
}

.zen-ledger td {
  padding: 12px 18px;
  vertical-align: middle;
}

.col-status { width: 100px; }
.col-repo { width: 210px; }
.col-trace { width: 230px; }
.col-time { width: 76px; text-align: right; }

.zen-ledger-state {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--z-fg-2);
}

.zen-ledger-title {
  font-size: 13.5px;
  font-weight: 500;
  color: var(--z-fg-1);
}

.zen-ledger-sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--z-fg-3);
  max-width: 54ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.zen-ledger-diff {
  display: flex;
  gap: 8px;
  color: var(--z-fg-3);
  font-size: 11.5px;
}

.zen-pip-bar {
  display: flex;
  align-items: center;
  gap: 4px;
}
.zen-pip {
  width: 14px;
  height: 6px;
  border-radius: 2px;
  background: var(--z-line-strong);
}
.zen-pip--warn { background: var(--z-amber); }
.zen-pip--pass { background: var(--z-green); }
.zen-pip--live { background: var(--z-accent-text); }

.zen-pip-caption {
  margin-left: 8px;
  font-size: 12px;
  color: var(--z-fg-2);
}

.zen-ledger-checks {
  color: var(--z-fg-2);
}
.zen-ledger-time {
  text-align: right;
  color: var(--z-fg-3);
}

.zen-ledger-drawer {
  border-top: 1px solid var(--z-line-strong);
  background: var(--z-bg-panel);
  display: grid;
  grid-template-columns: 1.15fr 0.85fr;
  min-height: 0;
  overflow: hidden;
}

.zen-ledger-drawer-col {
  padding: 16px 22px;
  overflow-y: auto;
}
.zen-ledger-log {
  border-left: 1px solid var(--z-line);
}

.zen-drawer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.zen-drawer-head strong {
  font-size: 13.5px;
  font-weight: 600;
}

.zen-compact-stages {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 8px;
}
.zen-compact-stages li {
  display: grid;
  grid-template-columns: 42px 8px 92px minmax(0, 1fr);
  align-items: baseline;
  gap: 8px;
  font-size: 12.5px;
}
.zen-compact-stages li > span:first-child {
  color: var(--z-fg-3);
}
.zen-pip-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--z-fg-3);
  align-self: center;
}
.zen-compact-summary {
  color: var(--z-fg-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 820px) {
  .zen-ref-hint { display: none; }
  .zen-linear {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }
  .zen-linear-list {
    border-right: 0;
    border-bottom: 1px solid var(--z-line);
    overflow: visible;
  }
  .zen-linear-detail {
    padding: 24px 18px 40px;
    overflow: visible;
  }
  .zen-sheet-summary {
    grid-template-columns: 6px minmax(0, 1fr) 40px;
  }
  .zen-sheet-ws,
  .zen-sheet-state {
    display: none;
  }
  .zen-ledger-wrap {
    grid-template-rows: auto auto;
    overflow-y: auto;
  }
  .zen-ledger-drawer {
    grid-template-columns: 1fr;
  }
  .zen-ledger-log {
    border-left: 0;
    border-top: 1px solid var(--z-line);
  }
}
</style>
