<template>
  <div class="codex-session-selector">
    <div class="codex-session-selector__header">
      <label class="codex-session-selector__label">Codex Session</label>
      <button
        type="button"
        class="codex-session-selector__refresh"
        :disabled="loading"
        @click="fetchSessions"
      >
        {{ loading ? 'Refreshing…' : 'Refresh' }}
      </button>
    </div>

    <div
      v-if="error"
      class="codex-session-selector__error"
    >
      {{ error }}
    </div>

    <div
      v-if="!loading && !error && groups.length === 0"
      class="codex-session-selector__empty"
    >
      No local Codex sessions found.
    </div>

    <div
      v-else
      class="codex-session-selector__body"
    >
      <!-- Left: workspace list -->
      <div class="codex-session-selector__workspaces">
        <label
          class="codex-session-selector__workspace codex-session-selector__workspace--fresh"
          :class="{ 'is-selected': selectedId === '' }"
        >
          <input
            type="radio"
            name="codex-session"
            :checked="selectedId === ''"
            value=""
            @change="$emit('update:sessionId', '')"
          >
          <span class="codex-session-selector__workspace-body">
            <span class="codex-session-selector__workspace-name">Start a fresh session</span>
          </span>
        </label>

        <label
          v-for="group in groups"
          :key="group.cwd"
          class="codex-session-selector__workspace"
          :class="{ 'is-selected': isGroupActive(group) }"
          @click="selectGroup(group)"
        >
          <span class="codex-session-selector__workspace-body">
            <span class="codex-session-selector__workspace-name">{{ workspaceLabel(group.cwd) }}</span>
            <span class="codex-session-selector__workspace-path">{{ group.cwd }}</span>
          </span>
          <span class="codex-session-selector__workspace-count">{{ group.sessions.length }}</span>
        </label>
      </div>

      <!-- Right: sessions for the selected workspace -->
      <div class="codex-session-selector__sessions">
        <div
          v-if="selectedId === ''"
          class="codex-session-selector__hint"
        >
          Starts a brand-new Codex session in the tab's working directory.
        </div>

        <template v-else>
          <label
            v-for="session in activeGroup.sessions"
            :key="session.session_id"
            class="codex-session-selector__session"
            :class="{ 'is-selected': selectedId === session.session_id }"
          >
            <input
              type="radio"
              name="codex-session"
              :checked="selectedId === session.session_id"
              :value="session.session_id"
              @change="$emit('update:sessionId', session.session_id)"
            >
            <span class="codex-session-selector__session-body">
              <span class="codex-session-selector__session-title">
                {{ session.title || '(untitled session)' }}
              </span>
              <span class="codex-session-selector__session-meta">
                {{ formatTime(session.start_time) }}
              </span>
            </span>
          </label>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

interface CodexSession {
  session_id: string
  cwd: string
  start_time: string
  title: string
}

interface CodexSessionGroup {
  cwd: string
  sessions: CodexSession[]
}

const props = defineProps<{
  sessionId: string
}>()

const emit = defineEmits<{
  'update:sessionId': [id: string]
}>()

const API_BASE = '/api'

const groups = ref<CodexSessionGroup[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const selectedId = ref(props.sessionId)
const activeCwd = ref<string>('')

// Keep local selection in sync if the parent resets the value (e.g. modal close).
watch(
  () => props.sessionId,
  id => {
    selectedId.value = id
    syncActiveCwd()
  }
)

function syncActiveCwd() {
  if (selectedId.value === '') {
    activeCwd.value = ''
    return
  }
  const found = groups.value.find(g =>
    g.sessions.some(s => s.session_id === selectedId.value)
  )
  if (found) {
    activeCwd.value = found.cwd
  } else if (!activeCwd.value && groups.value.length) {
    activeCwd.value = groups.value[0].cwd
  }
}

const activeGroup = computed(
  () => groups.value.find(g => g.cwd === activeCwd.value) || groups.value[0]
)

function isGroupActive(group: CodexSessionGroup): boolean {
  if (selectedId.value === '') return false
  return activeCwd.value === group.cwd
}

function selectGroup(group: CodexSessionGroup) {
  activeCwd.value = group.cwd
  // Pick the most recent session in the group as the default selection.
  if (group.sessions.length) {
    const next = group.sessions[0].session_id
    selectedId.value = next
    emit('update:sessionId', next)
  }
}

function workspaceLabel(cwd: string): string {
  const cleaned = cwd.replace(/\/$/, '')
  const parts = cleaned.split('/')
  const last = parts[parts.length - 1]
  return last || cleaned || '/'
}

async function fetchSessions() {
  loading.value = true
  error.value = null
  try {
    const response = await fetch(`${API_BASE}/codex/sessions`)
    if (!response.ok) throw new Error('Failed to list Codex sessions')
    groups.value = (await response.json()) as CodexSessionGroup[]
    syncActiveCwd()
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to list Codex sessions'
  } finally {
    loading.value = false
  }
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString()
}

onMounted(fetchSessions)
</script>

<style scoped>
.codex-session-selector {
  margin-top: 4px;
}

.codex-session-selector__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.codex-session-selector__label {
  font-size: 13px;
  font-weight: 600;
  color: var(--ch-color-text);
}

.codex-session-selector__refresh {
  font-size: 12px;
  padding: 2px 8px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
}

.codex-session-selector__refresh:disabled {
  opacity: 0.6;
  cursor: default;
}

.codex-session-selector__error {
  font-size: 12px;
  color: var(--ch-color-danger, #ef4444);
  margin-bottom: 6px;
}

.codex-session-selector__empty {
  font-size: 12px;
  color: var(--ch-color-text-muted);
  padding: 8px 4px;
}

.codex-session-selector__body {
  display: flex;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface, #1e1e1e);
  min-height: 120px;
  max-height: 320px;
  overflow: hidden;
}

/* Left column: workspaces */
.codex-session-selector__workspaces {
  width: 42%;
  max-width: 220px;
  border-right: 1px solid var(--ch-color-border-muted);
  overflow-y: auto;
}

.codex-session-selector__workspace {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  cursor: pointer;
  border-bottom: 1px solid var(--ch-color-border-muted);
  transition: background var(--ch-motion-fast, 120ms ease);
}

.codex-session-selector__workspace:last-child {
  border-bottom: none;
}

.codex-session-selector__workspace:hover {
  background: var(--ch-color-surface-hover, rgba(255, 255, 255, 0.04));
}

.codex-session-selector__workspace.is-selected {
  background: var(--ch-color-accent-subtle, rgba(99, 102, 241, 0.12));
}

.codex-session-selector__workspace input[type='radio'] {
  margin-top: 3px;
  flex-shrink: 0;
}

.codex-session-selector__workspace--fresh .codex-session-selector__workspace-name {
  font-style: normal;
}

.codex-session-selector__workspace-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}

.codex-session-selector__workspace-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--ch-color-text);
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.codex-session-selector__workspace-path {
  font-size: 11px;
  color: var(--ch-color-text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.codex-session-selector__workspace-count {
  font-size: 11px;
  color: var(--ch-color-text-muted);
  background: var(--ch-color-surface-control, rgba(255, 255, 255, 0.06));
  border-radius: 999px;
  padding: 1px 7px;
  flex-shrink: 0;
}

/* Right column: sessions */
.codex-session-selector__sessions {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
}

.codex-session-selector__session {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  transition: background var(--ch-motion-fast, 120ms ease);
}

.codex-session-selector__session:hover {
  background: var(--ch-color-surface-hover, rgba(255, 255, 255, 0.04));
}

.codex-session-selector__session.is-selected {
  background: var(--ch-color-accent-subtle, rgba(99, 102, 241, 0.12));
}

.codex-session-selector__session input[type='radio'] {
  margin-top: 3px;
  flex-shrink: 0;
}

.codex-session-selector__session-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.codex-session-selector__session-title {
  font-size: 13px;
  color: var(--ch-color-text);
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.codex-session-selector__session-meta {
  font-size: 11px;
  color: var(--ch-color-text-muted);
}

.codex-session-selector__hint {
  font-size: 12px;
  color: var(--ch-color-text-muted);
  padding: 12px;
}
</style>
