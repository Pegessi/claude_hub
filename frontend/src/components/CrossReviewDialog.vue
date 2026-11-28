<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="goal-dialog-backdrop"
      @click.self="close"
    >
      <form
        ref="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cross-review-title"
        aria-describedby="cross-review-subtitle"
        tabindex="-1"
        class="goal-dialog dialog cross-review-dialog"
        @submit.prevent="submit"
      >
        <header class="goal-dialog-head cr-head">
          <div class="cr-head-top">
            <span class="goal-dialog-eyebrow">New Goal</span>
            <span
              v-if="loading"
              class="cr-head-pill"
            >Loading defaults…</span>
          </div>
          <h2 id="cross-review-title">
            Cross-Review &amp; Slim
          </h2>
          <p
            id="cross-review-subtitle"
            class="goal-dialog-subtitle"
          >
            Lightweight code cross-review and justified simplification before completion.
          </p>
        </header>

        <div
          ref="bodyEl"
          class="goal-dialog-body cr-body"
        >
          <!-- 1. Four review pillars -->
          <fieldset class="cr-section">
            <legend class="cr-section-title">
              Review focus
            </legend>
            <div class="cr-pillars">
              <div
                v-for="pillar in pillars"
                :key="pillar.title"
                class="cr-pillar"
              >
                <strong class="cr-pillar-title">{{ pillar.title }}</strong>
                <span class="cr-pillar-desc">{{ pillar.desc }}</span>
              </div>
            </div>
          </fieldset>

          <!-- 2. Shared subagent priority & wrap-up -->
          <div class="cr-config-card">
            <div class="cr-priority-row">
              <div class="cr-priority-copy">
                <span class="cr-section-title">Shared subagent priority</span>
                <p class="cr-priority-summary">
                  {{ prioritySummaryText }}
                </p>
              </div>
              <button
                type="button"
                class="cr-btn"
                :disabled="loading"
                @click="openGlobalSubagentSettings"
              >
                Configure in Settings
              </button>
            </div>

            <div
              v-if="settings.default_preset === 'readonly_brief'"
              class="cr-readonly-banner"
              role="status"
            >
              <span>Saved read-only audit default active (no file edits or branch push).</span>
              <button
                type="button"
                class="cr-reset-btn"
                :disabled="loading"
                @click="switchToSlimMode"
              >
                Switch to Cross-Review &amp; Slim
              </button>
            </div>

            <fieldset
              class="cr-wrapup-row"
              :disabled="loading || loadError || !settingsLoaded || settings.default_preset === 'readonly_brief'"
            >
              <legend class="cr-section-title">
                Wrap-up option
              </legend>
              <label
                class="cr-chip"
                :data-checked="settings.push_remote_branch ? 'true' : 'false'"
              >
                <input
                  v-model="settings.push_remote_branch"
                  type="checkbox"
                  :disabled="loading || loadError || !settingsLoaded || settings.default_preset === 'readonly_brief'"
                  @change="persistPushPreference"
                >
                <span>Push feature branch on PASS</span>
              </label>
            </fieldset>
          </div>

          <!-- 3. Scope / focus notes -->
          <section class="cr-scope-section">
            <div class="goal-dialog-section-head">
              <label
                class="goal-dialog-field-label"
                for="cross-review-objective"
              >Scope / focus notes</label>
              <div class="cr-scope-meta">
                <button
                  v-if="isObjectiveCustomized"
                  type="button"
                  class="cr-reset-btn"
                  @click="resetObjective"
                >
                  Reset to default
                </button>
                <span
                  class="goal-dialog-count"
                  :data-near-limit="objective.length > MAX_OBJECTIVE * 0.9 ? 'true' : 'false'"
                >{{ objective.length }}/{{ MAX_OBJECTIVE }}</span>
              </div>
            </div>
            <div class="goal-dialog-field">
              <textarea
                id="cross-review-objective"
                v-model="objective"
                :maxlength="MAX_OBJECTIVE"
                rows="3"
                placeholder="Describe specific files, invariants, or risks to focus on…"
                aria-describedby="cross-review-objective-hint"
                required
              />
              <p
                id="cross-review-objective-hint"
                class="goal-dialog-field-hint"
                :data-warning="!objective.trim() ? 'true' : 'false'"
              >
                <template v-if="!objective.trim()">
                  Scope / focus notes are required to start a review. Enter instructions above or reset to the default focus.
                </template>
                <template v-else>
                  Guides the worker and independent reviewer; agents keep room to exercise judgment.
                </template>
              </p>
            </div>
          </section>

          <div
            v-if="loadError"
            class="cr-load-error-row"
            role="alert"
          >
            <p class="goal-dialog-error">
              Could not load saved subagent settings. Retry before starting a review.
            </p>
            <button
              type="button"
              class="cr-btn"
              :disabled="loading"
              @click="load"
            >
              Retry load
            </button>
          </div>
          <p
            v-if="error"
            class="goal-dialog-error"
            role="alert"
          >
            {{ error }}
          </p>
        </div>

        <footer class="goal-dialog-actions cr-footer">
          <p
            class="cr-run-summary"
            aria-live="polite"
          >
            <span class="cr-run-summary-prefix">Will run:</span>
            <strong>{{ runSummaryText }}</strong>
          </p>
          <div class="cr-footer-actions">
            <button
              type="button"
              class="goal-dialog-button"
              :disabled="busy"
              @click="close"
            >
              Cancel
            </button>
            <button
              type="submit"
              class="goal-dialog-button goal-dialog-button--primary"
              :disabled="busy || loading || loadError || !settingsLoaded || !objective.trim()"
            >
              {{ busy ? 'Starting…' : 'Start Cross-Review' }}
            </button>
          </div>
        </footer>
      </form>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useAppStore } from '@/stores/appStore'
import type { SubagentModelPreference } from '@/types'

const MAX_OBJECTIVE = 4000
const DEFAULT_OBJECTIVE =
  'Cross-review the current change for bugs and correctness, elegant and reasonable design, restraint against overengineering or excessive defensive code, and performance; simplify where justified and verify.'

type Settings = {
  preferred_candidates: SubagentModelPreference[]
  push_remote_branch: boolean
  default_preset: 'cross_review_slim' | 'readonly_brief'
}

function cloneSettings(src: Partial<Settings> | null | undefined): Settings {
  const rawPreset = src?.default_preset === 'readonly_brief' ? 'readonly_brief' : 'cross_review_slim'
  const push = rawPreset === 'readonly_brief' ? false : Boolean(src?.push_remote_branch)
  return {
    preferred_candidates: Array.isArray(src?.preferred_candidates)
      ? src.preferred_candidates.map(c => ({
          provider: c.provider,
          model: c.model ?? null,
          effort: c.effort ?? null,
        }))
      : [],
    push_remote_branch: push,
    default_preset: rawPreset,
  }
}

const PROVIDER_LABELS: Record<string, string> = {
  maso: 'Maso',
  codex: 'Codex',
  claude: 'Claude',
  cursor: 'Cursor',
  traex: 'Traex',
}

const pillars = [
  { title: 'Bugs & correctness', desc: 'Reachable defects, data integrity, security, and verification.' },
  { title: 'Elegant design', desc: 'Clear ownership and reuse of existing patterns.' },
  { title: 'Restraint & slim', desc: 'Cut overengineering, redundant wrappers, and defensive bloat.' },
  { title: 'Performance', desc: 'Hot paths, unnecessary work, and resource efficiency.' },
] as const

const props = defineProps<{ open: boolean; busy: boolean; error: string | null }>()
const emit = defineEmits<{
  close: []
  submit: [input: {
    objective: string
    strictReview: boolean
    strict_review_preset: 'cross_review_slim' | 'readonly_brief'
    strict_review_push_branch: boolean
  }]
}>()

const appStore = useAppStore()
const dialog = ref<HTMLFormElement | null>(null)
const bodyEl = ref<HTMLElement | null>(null)
const loading = ref(false)
const loadError = ref(false)
const settingsLoaded = ref(false)
const settings = ref<Settings>(cloneSettings(null))
const objective = ref(DEFAULT_OBJECTIVE)
let activeLoad: AbortController | null = null
let activePersist: AbortController | null = null

const isObjectiveCustomized = computed(() => objective.value.trim() !== DEFAULT_OBJECTIVE)

const prioritySummaryText = computed(() => {
  const list = settings.value.preferred_candidates
  if (!list.length) {
    return 'Automatic heterogeneous routing (follows shared Settings).'
  }
  const formatted = list.slice(0, 3).map((item, idx) => {
    const prov = PROVIDER_LABELS[item.provider] ?? item.provider
    const model = item.model ? `/${item.model}` : ''
    const effort = item.effort ? ` (${item.effort})` : ''
    return `#${idx + 1} ${prov}${model}${effort}`
  })
  const suffix = list.length > 3 ? ` +${list.length - 3} more` : ''
  return `${formatted.join(' · ')}${suffix}`
})

const runSummaryText = computed(() => {
  if (loading.value) return 'Loading saved defaults…'
  if (loadError.value || !settingsLoaded.value) return 'Saved defaults unavailable — retry load to start'
  const count = settings.value.preferred_candidates.length
  const routing = count > 0
    ? `${count} shared priority ${count === 1 ? 'subagent' : 'subagents'}`
    : 'Auto heterogeneous routing'
  const modeLabel = settings.value.default_preset === 'readonly_brief'
    ? 'Read-Only Audit'
    : 'Cross-Review & Slim'
  const parts = [modeLabel, routing]
  if (settings.value.push_remote_branch && settings.value.default_preset !== 'readonly_brief') {
    parts.push('Push on PASS')
  }
  return parts.join(' · ')
})

function resetObjective() {
  objective.value = DEFAULT_OBJECTIVE
}

function openGlobalSubagentSettings() {
  appStore.openSubagentSettings()
}

function onGlobalSettingsUpdated(event: Event) {
  const detail = (event as CustomEvent<Settings>).detail
  if (detail && Array.isArray(detail.preferred_candidates)) {
    settings.value = cloneSettings(detail)
    settingsLoaded.value = true
    loadError.value = false
  }
}

async function switchToSlimMode() {
  settings.value.default_preset = 'cross_review_slim'
  await persistSettingsPatch({ default_preset: 'cross_review_slim' })
}

async function persistPushPreference() {
  await persistSettingsPatch({ push_remote_branch: settings.value.push_remote_branch })
}

async function persistSettingsPatch(patch: Partial<Pick<Settings, 'push_remote_branch' | 'default_preset'>>) {
  if (!settingsLoaded.value || loadError.value) return
  activePersist?.abort()
  const controller = new AbortController()
  activePersist = controller
  try {
    const patchRes = await fetch('/api/goals/cross-review-settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
      signal: controller.signal,
    })
    if (controller.signal.aborted) return
    if (patchRes.ok) {
      const body = await patchRes.json().catch(() => null)
      if (controller.signal.aborted) return
      if (body?.settings && Array.isArray(body.settings.preferred_candidates)) {
        const committed = cloneSettings(body.settings)
        settings.value = committed
        window.dispatchEvent(
          new CustomEvent('claude-hub:subagent-settings-updated', { detail: cloneSettings(committed) }),
        )
      }
    }
  } catch {
    // Keep in-dialog toggle state even if background persistence fails.
  } finally {
    if (activePersist === controller) activePersist = null
  }
}

function focusableControls() {
  const root = dialog.value
  if (!root) return []
  return [...root.querySelectorAll<HTMLElement>(
    'button:not(:disabled), textarea:not(:disabled), input:not(:disabled), select:not(:disabled)',
  )].filter(el => el.getClientRects().length > 0)
}

function onDocumentKeydown(event: KeyboardEvent) {
  if (!props.open || appStore.subagentSettingsVisible) return
  const root = dialog.value
  if (!root) return
  if (event.key === 'Escape') {
    event.preventDefault()
    close()
    return
  }
  if (event.key !== 'Tab') return
  const controls = focusableControls()
  const first = controls[0]
  const last = controls[controls.length - 1]
  if (!first || !last) return
  const active = document.activeElement as HTMLElement | null
  if (!active || !root.contains(active)) {
    event.preventDefault()
    ;(event.shiftKey ? last : first).focus()
    return
  }
  if (event.shiftKey && active === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

watch(() => props.open, async open => {
  if (!open) {
    activeLoad?.abort()
    activeLoad = null
    activePersist?.abort()
    activePersist = null
    loading.value = false
    document.removeEventListener('keydown', onDocumentKeydown, true)
    window.removeEventListener('claude-hub:subagent-settings-updated', onGlobalSettingsUpdated)
    return
  }
  document.addEventListener('keydown', onDocumentKeydown, true)
  window.addEventListener('claude-hub:subagent-settings-updated', onGlobalSettingsUpdated)
  objective.value = DEFAULT_OBJECTIVE
  await load()
  await nextTick()
  if (bodyEl.value) bodyEl.value.scrollTop = 0
  dialog.value?.focus()
})

onBeforeUnmount(() => {
  activeLoad?.abort()
  activePersist?.abort()
  document.removeEventListener('keydown', onDocumentKeydown, true)
  window.removeEventListener('claude-hub:subagent-settings-updated', onGlobalSettingsUpdated)
})

async function load() {
  activeLoad?.abort()
  const controller = new AbortController()
  activeLoad = controller
  loading.value = true
  loadError.value = false
  settingsLoaded.value = false
  try {
    const response = await fetch('/api/goals/cross-review-settings', { signal: controller.signal })
    if (controller.signal.aborted) return
    if (!response.ok) {
      loadError.value = true
      return
    }
    const body = await response.json()
    if (controller.signal.aborted) return
    const rawSettings = body?.settings
    if (
      rawSettings &&
      typeof rawSettings === 'object' &&
      Array.isArray(rawSettings.preferred_candidates)
    ) {
      settings.value = cloneSettings(rawSettings)
      settingsLoaded.value = true
    } else {
      loadError.value = true
    }
  } catch {
    if (!controller.signal.aborted) {
      loadError.value = true
    }
  } finally {
    if (activeLoad === controller) loading.value = false
  }
}

function close() {
  if (!props.busy) emit('close')
}

function submit() {
  if (props.busy || loading.value || loadError.value || !settingsLoaded.value || !objective.value.trim()) return
  const preset = settings.value.default_preset === 'readonly_brief'
    ? 'readonly_brief'
    : 'cross_review_slim'
  emit('submit', {
    objective: objective.value.trim(),
    strictReview: true,
    strict_review_preset: preset,
    strict_review_push_branch: preset === 'readonly_brief' ? false : settings.value.push_remote_branch,
  })
}
</script>

<style scoped>
.goal-dialog-backdrop { position: fixed; inset: 0; z-index: 880; display: grid; place-items: center; padding: 16px; background: var(--ch-color-backdrop, rgba(9, 10, 14, 0.68)); }
.dialog { width: min(620px, 100%); max-height: min(88dvh, 760px); display: flex; flex-direction: column; overflow: hidden; border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-xl, 12px); background: var(--ch-color-surface); color: var(--ch-color-text); box-shadow: var(--ch-shadow-lg, 0 24px 64px rgba(0, 0, 0, 0.45)); }
.cr-head { display: grid; gap: 3px; padding: 14px 20px 10px; border-bottom: 1px solid var(--ch-color-border-muted); flex-shrink: 0; }
.cr-head-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.goal-dialog-eyebrow { font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ch-color-accent); }
.cr-head-pill { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px; background: var(--ch-color-surface-raised); color: var(--ch-color-text-muted); border: 1px solid var(--ch-color-border-muted); }
.cr-head h2 { margin: 0; font-size: 18px; line-height: 1.25; color: var(--ch-color-text-strong); }
.goal-dialog-subtitle { margin: 0; color: var(--ch-color-text-muted); font-size: 12.5px; line-height: 1.4; }
.cr-body { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; padding: 12px 20px; overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain; min-height: 0; }
.cr-section { margin: 0; padding: 0; border: 0; display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px; min-width: 0; }
.cr-section-title { float: left; width: 100%; margin: 0; padding: 0; font-size: 12px; font-weight: 650; color: var(--ch-color-text-strong); letter-spacing: -0.01em; }
.cr-pillars { clear: both; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; min-width: 0; }
.cr-pillar { display: grid; gap: 2px; padding: 8px 10px; border: 1px solid var(--ch-color-border-muted); border-radius: var(--ch-radius-md, 8px); background: var(--ch-color-surface-sunken); min-width: 0; }
.cr-pillar-title { font-size: 12px; font-weight: 650; color: var(--ch-color-text-strong); }
.cr-pillar-desc { font-size: 11.5px; line-height: 1.35; color: var(--ch-color-text-muted); }
.cr-config-card { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; padding: 10px 12px; border: 1px solid var(--ch-color-border-muted); border-radius: var(--ch-radius-lg, 10px); background: var(--ch-color-surface-sunken); min-width: 0; }
.cr-priority-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; min-width: 0; }
.cr-priority-copy { display: grid; grid-template-columns: minmax(0, 1fr); gap: 2px; min-width: 0; flex: 1 1 200px; }
.cr-priority-summary { margin: 0; font-size: 12px; line-height: 1.4; color: var(--ch-color-text-muted); min-width: 0; overflow-wrap: anywhere; word-break: break-word; }
.cr-readonly-banner { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; padding: 8px 10px; border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-md, 8px); background: var(--ch-color-surface); font-size: 12px; color: var(--ch-color-text-strong); min-width: 0; }
.cr-wrapup-row { margin: 0; padding: 8px 0 0; border: 0; border-top: 1px solid var(--ch-color-border-muted); display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; min-width: 0; }
.cr-wrapup-row .cr-section-title { float: none; width: auto; }
.cr-chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; min-height: 32px; box-sizing: border-box; border: 1px solid var(--ch-color-border); border-radius: 999px; background: var(--ch-color-surface); color: var(--ch-color-text); font-size: 12px; font-weight: 550; cursor: pointer; }
.cr-chip[data-checked='true'] { border-color: var(--ch-color-border-selected); background: var(--ch-color-surface-selected); color: var(--ch-color-text-selected); }
.cr-chip input { margin: 0; accent-color: var(--ch-color-accent); }
.cr-btn { min-height: 34px; padding: 6px 12px; border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-md, 8px); background: var(--ch-color-surface-control); color: var(--ch-color-text); font: inherit; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap; }
.cr-btn:hover:not(:disabled) { background: var(--ch-color-surface-control-hover); }
.cr-btn:disabled { opacity: 0.55; cursor: not-allowed; }
.cr-scope-section { display: grid; grid-template-columns: minmax(0, 1fr); gap: 5px; min-width: 0; }
.goal-dialog-section-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; min-width: 0; }
.goal-dialog-field-label { font-size: 12px; font-weight: 650; color: var(--ch-color-text-strong); }
.cr-scope-meta { display: flex; align-items: center; gap: 10px; }
.cr-reset-btn { min-height: 28px; padding: 2px 8px; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-sm, 6px); background: var(--ch-color-surface-control); color: var(--ch-color-accent); font: inherit; font-size: 11.5px; font-weight: 600; cursor: pointer; }
.cr-reset-btn:hover { background: var(--ch-color-surface-control-hover); }
.goal-dialog-count { font-size: 11px; font-family: var(--ch-font-mono); color: var(--ch-color-text-muted); }
.goal-dialog-count[data-near-limit='true'] { color: var(--ch-color-accent); font-weight: 700; }
.goal-dialog-field { display: grid; grid-template-columns: minmax(0, 1fr); gap: 4px; min-width: 0; }
.goal-dialog-field textarea { width: 100%; min-height: 62px; max-height: 150px; box-sizing: border-box; resize: vertical; padding: 8px 10px; border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-md, 8px); background: var(--ch-color-surface-sunken); color: var(--ch-color-text); font: inherit; font-size: 12.5px; line-height: 1.45; }
.goal-dialog-field textarea:focus { outline: 2px solid var(--ch-color-accent-ring); outline-offset: 1px; border-color: var(--ch-color-border-selected); }
.goal-dialog-field-hint { margin: 0; font-size: 11.5px; line-height: 1.4; color: var(--ch-color-text-muted); }
.goal-dialog-field-hint[data-warning='true'] { color: var(--ch-color-text-strong); font-weight: 600; }
.cr-load-error-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; min-width: 0; }
.cr-load-error-row .goal-dialog-error { flex: 1 1 200px; }
.goal-dialog-error { margin: 0; padding: 8px 10px; border: 1px solid var(--ch-color-danger); border-radius: var(--ch-radius-md, 8px); background: rgba(248, 113, 113, 0.12); color: var(--ch-color-danger-text, var(--ch-color-danger)); font-size: 12px; overflow-wrap: anywhere; }
.cr-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 20px; border-top: 1px solid var(--ch-color-border-muted); background: var(--ch-color-surface-raised); flex-shrink: 0; flex-wrap: wrap; min-width: 0; }
.cr-run-summary { margin: 0; font-size: 12px; color: var(--ch-color-text-muted); display: flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0; overflow-wrap: anywhere; }
.cr-run-summary strong { color: var(--ch-color-text-strong); font-weight: 600; min-width: 0; overflow-wrap: anywhere; }
.cr-footer-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.goal-dialog-button { min-height: 36px; padding: 7px 14px; border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-md, 8px); background: var(--ch-color-surface-control); color: var(--ch-color-text); font: inherit; font-size: 12.5px; font-weight: 600; cursor: pointer; }
.goal-dialog-button:hover:not(:disabled) { background: var(--ch-color-surface-control-hover); }
.goal-dialog-button--primary { border-color: var(--ch-color-accent-strong); background: var(--ch-color-accent-strong); color: var(--ch-color-on-accent, var(--ch-color-text-inverse)); }
.goal-dialog-button--primary:hover:not(:disabled) { background: var(--ch-color-accent-fill-hover, var(--ch-color-accent-hover)); }
.goal-dialog-button:disabled { opacity: 0.55; cursor: not-allowed; }
.dialog :is(button, input, select, textarea):focus-visible { outline: 2px solid var(--ch-color-accent-ring); outline-offset: 2px; }
@media (max-width: 640px), (pointer: coarse) {
  .cr-head { padding: 12px 16px 8px; gap: 2px; }
  .cr-body { padding: 10px 16px; gap: 8px; }
  .cr-pillars { grid-template-columns: minmax(0, 1fr); gap: 6px; }
  .cr-pillar { padding: 6px 10px; }
  .cr-config-card { padding: 8px 10px; gap: 8px; }
  .cr-priority-copy { flex: 1 1 100%; }
  .goal-dialog-field textarea { min-height: 54px; padding: 6px 10px; }
  .cr-footer { padding: 10px 16px calc(10px + env(safe-area-inset-bottom, 0px)); gap: 8px; flex-direction: column; align-items: stretch; }
  .cr-footer-actions { width: 100%; justify-content: flex-end; }
  .cr-footer-actions .goal-dialog-button { flex: 1; }
  .goal-dialog-button, .cr-btn, .cr-chip, .cr-reset-btn { min-height: 44px; min-width: 44px; }
  .cr-reset-btn { padding: 8px 12px; }
}
</style>
