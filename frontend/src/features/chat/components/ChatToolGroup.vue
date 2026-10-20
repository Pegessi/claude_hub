<template>
  <div
    class="conversation-row conversation-row--assistant conversation-row--activity"
  >
    <details
      class="tool-card tool-card--group"
      :class="[`tool-card--${groupStatus()}`, { 'tool-card--single': part.tools.length === 1 }]"
      :open="expandedToolGroups.get(turn.key)?.has(part.key)"
      @toggle="onToolGroupToggle(turn, part.key, $event)"
    >
      <summary
        class="tool-header"
        :class="{ 'tool-header--single': part.tools.length === 1 }"
      >
        <span
          class="activity-icon"
          :data-kind="headline().kind"
          aria-hidden="true"
        ><svg viewBox="0 0 16 16"><path :d="ACTIVITY_ICONS[headline().kind]" /></svg></span>
        <span class="tool-name activity-title">
          <span class="activity-verb">{{ headline().verb }}</span>
          <code
            v-if="headline().target"
            class="activity-target"
          >{{ headline().target }}</code>
        </span>
        <span
          v-if="part.tools.length > 1"
          class="tool-group-names"
        >{{ toolGroupNames(part.tools) }}</span>
        <!-- A failure inside a still-running group must not hide behind
             the spinner. -->
        <span
          v-if="groupStatus() === 'running' && failedCount() > 0"
          class="tool-status failed tool-status--count"
        ><span class="tool-status-text">{{ failedCount() }} failed</span></span>
        <span
          class="tool-status"
          :class="groupStatus()"
        ><span class="tool-status-text">{{ toolStatusLabel(groupStatus()) }}</span></span>
        <span
          class="tool-chevron"
          aria-hidden="true"
        >›</span>
      </summary>
      <template v-if="expandedToolGroups.get(turn.key)?.has(part.key)">
        <div
          v-for="{ tool, activity } in expandedItems()"
          :key="tool.key"
          class="tool-group-item"
        >
          <div
            v-if="part.tools.length > 1"
            class="tool-group-item-header"
          >
            <span
              class="activity-icon activity-icon--small"
              :data-kind="activity.kind"
              aria-hidden="true"
            ><svg viewBox="0 0 16 16"><path :d="ACTIVITY_ICONS[activity.kind]" /></svg></span>
            <span class="tool-name activity-title">
              <span class="activity-verb">{{ activity.verb }}</span>
              <code
                v-if="activity.target"
                class="activity-target"
              >{{ activity.target }}</code>
            </span>
            <span
              class="tool-status"
              :class="tool.status"
            ><span class="tool-status-text">{{ toolStatusLabel(tool.status) }}</span></span>
          </div>
          <!-- Commands read as a terminal: the prompt line, then the output. -->
          <div
            v-if="activity.command"
            class="activity-terminal"
          >
            <pre
              class="activity-terminal-command"
              role="region"
              tabindex="0"
              aria-label="Command"
            ><span
              class="activity-prompt"
              aria-hidden="true"
            >$ </span>{{ activity.command }}</pre>
            <pre
              v-if="tool.resultText"
              class="activity-terminal-output"
              role="region"
              tabindex="0"
              aria-label="Command output"
            >{{ tool.resultText }}</pre>
          </div>
          <template v-else>
            <!-- Edits read as a diff when the arguments carry one. -->
            <div
              v-if="activity.diff"
              class="activity-diff"
              role="region"
              tabindex="0"
              aria-label="Edit diff"
            >
              <div
                v-for="(line, index) in activity.diff"
                :key="index"
                class="activity-diff-line"
                :class="`activity-diff-line--${DIFF_CLASS[line.sign]}`"
              >
                <span class="activity-diff-sign">{{ line.sign === '@' || line.sign === ' ' ? '' : line.sign }}</span><span>{{ line.text }}</span>
              </div>
            </div>
            <!-- The headline already names the target, so the arguments
                 become secondary evidence. -->
            <details
              v-if="tool.argsText && (activity.diff || activity.target)"
              class="tool-block tool-block--raw"
            >
              <summary>Raw input</summary>
              <pre
                tabindex="0"
                aria-label="Tool input"
              >{{ tool.argsText }}</pre>
            </details>
            <div
              v-else-if="tool.argsText"
              class="tool-block"
            >
              <span>Input</span>
              <pre
                tabindex="0"
                aria-label="Tool input"
              >{{ tool.argsText }}</pre>
            </div>
            <div
              v-if="tool.resultText"
              class="tool-block"
            >
              <span>Result</span>
              <pre
                tabindex="0"
                aria-label="Tool result"
              >{{ tool.resultText }}</pre>
            </div>
          </template>
        </div>
        <button
          type="button"
          class="details-collapse"
          @click="collapseDetails"
        >
          Collapse
        </button>
      </template>
    </details>
  </div>
</template>

<script setup lang="ts">
import { requireChatOwner, chatTimelineKey } from '../owners'
import { toolGroupNames, toolStatusLabel } from '@/utils/toolPresentation'
import { ACTIVITY_ICONS, describeToolActivity, toolGroupSummary, type ToolActivity, type ToolDiffLine, type ToolGroupSummary } from '@/utils/toolActivity'
import { toolGroupStatus } from '@/features/chat/presentation/chatTurnPresentation'
import type { TimelineTool, TimelineTurn } from '../timeline/types'
import type { TimelinePart } from '../timeline/types'
const { expandedToolGroups, onToolGroupToggle, collapseDetails } = requireChatOwner(chatTimelineKey)
const props = defineProps<{
  turn: TimelineTurn
  part: Extract<TimelinePart, { kind: 'tool_group' }>
}>()

const DIFF_CLASS: Record<ToolDiffLine['sign'], string> = { '+': 'add', '-': 'del', ' ': 'ctx', '@': 'hunk' }

// Plain render-time functions, not computeds: the timeline mutates tool rows
// in place and signals change through the turn's renderRevision, which a
// computed cannot observe.
function groupStatus(): ReturnType<typeof toolGroupStatus> {
  return toolGroupStatus(props.part.tools)
}

function failedCount(): number {
  return props.part.tools.reduce((count, tool) => count + (tool.status === 'failed' ? 1 : 0), 0)
}

/** Cached per group, so reading it once per header field costs nothing. */
function headline(): ToolGroupSummary {
  return toolGroupSummary(props.part.tools)
}

/** Arguments are parsed only while the card is open. */
function expandedItems(): { tool: TimelineTool, activity: ToolActivity }[] {
  if (!expandedToolGroups.get(props.turn.key)?.has(props.part.key)) return []
  return props.part.tools.map(tool => ({ tool, activity: describeToolActivity(tool) }))
}
</script>

<style scoped>
/* Tool calls read as a quiet activity log between the prose: one line per
   group ("Ran `pnpm test`"), a card only once someone opens it. */
.conversation-row {
  display: flex;
  align-items: flex-start;
  min-width: 0;
}

.tool-card {
  width: 100%;
  min-width: 0;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-md);
  background: transparent;
  overflow: clip;
  transition: border-color 160ms ease, background-color 160ms ease, box-shadow 160ms ease;
}

.tool-card[open] {
  border-color: var(--ch-color-border-muted);
  background: var(--ch-color-surface);
  box-shadow: 0 10px 28px var(--ch-shadow-color-soft);
}

.tool-card > summary {
  cursor: pointer;
  list-style: none;
  position: sticky;
  top: 0;
  z-index: 1;
}

.tool-card > summary::-webkit-details-marker {
  display: none;
}

.tool-card[open] > summary {
  background: var(--ch-color-surface);
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.tool-card > summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

.tool-header {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  min-height: 30px;
  padding: 4px 8px;
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text-subtle);
  font-size: 13px;
  transition: background-color 120ms ease, color 120ms ease;
}

.tool-card:not([open]) > .tool-header:hover {
  background: var(--ch-color-row-hover, var(--ch-color-surface-soft));
  color: var(--ch-color-text-muted);
}

.tool-card[open] > .tool-header {
  border-radius: 0;
  color: var(--ch-color-text-muted);
}

.tool-name {
  flex-shrink: 0;
  min-width: 0;
  white-space: nowrap;
}

.activity-title {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
}

.tool-header--single .activity-title {
  flex: 1 1 auto;
}

.activity-verb {
  flex: 0 0 auto;
  color: inherit;
  font-weight: 500;
}

.activity-target {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 1px 6px;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken, var(--ch-color-surface-soft));
  color: var(--ch-color-text-code, var(--ch-color-text));
  font-family: var(--ch-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12px;
}

.activity-icon {
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  border-radius: 6px;
  color: var(--ch-color-text-subtle);
}

.activity-icon svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.activity-icon--small {
  width: 16px;
  height: 16px;
}

.activity-icon--small svg {
  width: 12px;
  height: 12px;
}

.activity-icon[data-kind='command'] { color: var(--ch-color-success-strong, var(--ch-color-success)); }
.activity-icon[data-kind='edit'],
.activity-icon[data-kind='write'] { color: var(--ch-color-accent); }
.activity-icon[data-kind='search'],
.activity-icon[data-kind='web'] { color: var(--ch-color-info, var(--ch-color-accent)); }

.tool-card--running .activity-verb {
  background: linear-gradient(
    90deg,
    var(--ch-color-text-subtle) 0%,
    var(--ch-color-text-strong, var(--ch-color-text)) 50%,
    var(--ch-color-text-subtle) 100%
  );
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: activity-shimmer 1.8s linear infinite;
}

@keyframes activity-shimmer {
  from { background-position: 100% 0; }
  to { background-position: -100% 0; }
}

.tool-group-names {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text-subtle);
  font-family: var(--ch-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  opacity: 0.75;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Status stays as text for assistive tech; sighted readers get a glyph,
   and only a failure is loud. */
.tool-status {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  gap: 4px;
  min-width: 16px;
  height: 18px;
  color: var(--ch-color-text-subtle);
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
}

.tool-status-text {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}

.tool-status::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.tool-status.completed {
  color: var(--ch-color-success-strong, #2ea043);
}

.tool-status.completed::before {
  content: '✓';
  width: auto;
  height: auto;
  background: none;
  font-size: 11px;
}

.tool-status.cancelled {
  color: var(--ch-color-text-subtle);
}

.tool-status.cancelled::before {
  width: 7px;
  height: 1.5px;
  border-radius: 1px;
}

.tool-status.running {
  color: var(--ch-color-warning, #e0a800);
}

.tool-status.running::before {
  width: 10px;
  height: 10px;
  border: 1.5px solid currentColor;
  border-right-color: transparent;
  background: none;
  animation: activity-spin 0.8s linear infinite;
}

.tool-status.failed {
  height: 18px;
  padding: 0 7px;
  border-radius: 999px;
  background: var(--ch-color-danger-bg, rgba(248, 81, 73, 0.12));
  color: var(--ch-color-danger-strong, #f85149);
}

.tool-status.failed::before {
  content: '!';
  width: auto;
  height: auto;
  background: none;
  font-weight: 700;
}

.tool-status.failed .tool-status-text {
  position: static;
  width: auto;
  height: auto;
  clip-path: none;
}

@keyframes activity-spin {
  to { transform: rotate(360deg); }
}

.tool-chevron {
  flex: 0 0 auto;
  margin-left: auto;
  color: var(--ch-color-text-subtle);
  font-size: 15px;
  line-height: 1;
  opacity: 0;
  transition: transform 140ms ease, opacity 120ms ease;
}

.tool-header:hover .tool-chevron,
.tool-card[open] > .tool-header .tool-chevron,
.tool-header:focus-visible .tool-chevron {
  opacity: 1;
}

.tool-card[open] > .tool-header .tool-chevron {
  transform: rotate(90deg);
}

.tool-group-item {
  padding: 10px 12px;
  border-top: 1px solid var(--ch-color-border-muted);
}

.tool-group-item:first-of-type {
  border-top: 0;
}

.tool-group-item-header {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 8px;
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.tool-group-item-header .activity-title {
  flex: 1 1 auto;
}

.activity-terminal {
  overflow: hidden;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-terminal-bg, var(--ch-color-canvas));
}

.activity-terminal pre,
.tool-block pre {
  margin: 0;
  padding: 8px 10px;
  overflow: auto;
  overscroll-behavior: contain;
  font-family: var(--ch-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11.5px;
  line-height: 1.5;
  tab-size: 2;
  white-space: pre;
  word-break: normal;
}

.activity-terminal-command {
  color: var(--ch-terminal-fg, var(--ch-color-text));
  white-space: pre-wrap !important;
  word-break: break-word !important;
}

.activity-prompt {
  color: var(--ch-color-success-strong, var(--ch-color-success));
  user-select: none;
}

.activity-terminal-output {
  max-height: 260px;
  border-top: 1px dashed var(--ch-color-border-muted);
  color: var(--ch-terminal-fg, var(--ch-color-text-code));
  opacity: 0.86;
}

.activity-diff {
  max-height: 320px;
  overflow: auto;
  overscroll-behavior: contain;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-canvas);
  font-family: var(--ch-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11.5px;
  line-height: 1.5;
}

.activity-diff-line {
  display: flex;
  min-width: max-content;
  padding-right: 10px;
  white-space: pre;
}

.activity-diff-sign {
  width: 22px;
  flex: 0 0 auto;
  text-align: center;
  opacity: 0.7;
  user-select: none;
}

.activity-diff-line--add {
  background: color-mix(in srgb, var(--ch-code-addition, #2ea043) 14%, transparent);
  color: var(--ch-code-addition, var(--ch-color-success-strong));
}

.activity-diff-line--del {
  background: color-mix(in srgb, var(--ch-code-deletion, #f85149) 12%, transparent);
  color: var(--ch-code-deletion, var(--ch-color-danger-strong));
}

.activity-diff-line--ctx {
  color: var(--ch-color-text-code);
}

.activity-diff-line--hunk {
  padding-top: 3px;
  padding-bottom: 3px;
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-subtle);
}

.tool-block {
  margin-top: 8px;
}

.tool-block:first-child {
  margin-top: 0;
}

.tool-block > span,
.tool-block--raw > summary {
  display: block;
  margin-bottom: 5px;
  color: var(--ch-color-text-subtle);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.tool-block--raw > summary {
  cursor: pointer;
  list-style: none;
}

.tool-block--raw > summary::-webkit-details-marker {
  display: none;
}

.tool-block--raw > summary::before {
  content: '▸';
  display: inline-block;
  width: 12px;
}

.tool-block--raw[open] > summary::before {
  content: '▾';
}

.tool-block--raw > summary:hover {
  color: var(--ch-color-text-muted);
}

.tool-block pre {
  max-height: 220px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-canvas);
  color: var(--ch-color-text-code);
}

.tool-block pre:focus-visible,
.activity-terminal pre:focus-visible,
.activity-diff:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

.details-collapse {
  display: block;
  width: 100%;
  padding: 6px 0;
  border: none;
  border-top: 1px solid var(--ch-color-border-muted);
  background: none;
  color: var(--ch-color-text-subtle);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.details-collapse:hover {
  color: var(--ch-color-text-muted);
}

.details-collapse:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

/* Touch: there is no hover to reveal affordances, and a fingertip needs a
   taller target than a pointer. */
@media (hover: none), (pointer: coarse) {
  .tool-chevron {
    opacity: 0.7;
  }

  .tool-header {
    min-height: 40px;
  }

  .tool-block--raw > summary {
    padding: 6px 0;
  }
}

@media (max-width: 640px) {
  .tool-group-names {
    display: none;
  }

  .tool-header {
    gap: 7px;
    padding: 4px 6px;
  }

  .tool-group-item {
    padding: 9px 8px;
  }

  .activity-terminal pre,
  .tool-block pre,
  .activity-diff {
    font-size: 11px;
  }

  .activity-terminal-output,
  .tool-block pre {
    max-height: 180px;
  }

  .activity-diff {
    max-height: 260px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .tool-card,
  .tool-chevron,
  .tool-header {
    transition: none;
  }

  .tool-card--running .activity-verb,
  .tool-status.running::before {
    animation: none;
  }
}
</style>
