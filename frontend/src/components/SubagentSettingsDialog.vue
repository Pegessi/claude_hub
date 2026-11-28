<template>
  <Teleport to="body">
    <div
      class="goal-dialog-backdrop"
      @click.self="close"
    >
      <form
        ref="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="subagent-settings-title"
        aria-describedby="subagent-settings-subtitle"
        tabindex="-1"
        class="goal-dialog dialog subagent-settings-dialog"
        @submit.prevent="save"
      >
        <header class="goal-dialog-head sa-head">
          <div class="sa-head-top">
            <span class="goal-dialog-eyebrow">Global Settings</span>
            <span
              v-if="loading"
              class="sa-head-pill"
            >Loading…</span>
          </div>
          <h2 id="subagent-settings-title">
            Subagent Priority
          </h2>
          <p
            id="subagent-settings-subtitle"
            class="goal-dialog-subtitle"
          >
            Shared default provider and model priority for normal subagent delegation (<code>claude-hub agent run</code>) and Cross-Review &amp; Slim. Explicit provider/model flags always override.
          </p>
        </header>

        <div class="goal-dialog-body sa-body">
          <fieldset
            class="sa-section"
            :disabled="loading || saving || loadError"
          >
            <legend class="sa-section-title">
              Default subagent priority
            </legend>
            <p class="sa-note">
              When an agent delegates without specifying a provider or runs Cross-Review, Hub tries these entries in order (skipping exhausted quota) before falling back to automatic heterogeneous routing.
            </p>

            <div
              v-if="settings.preferred_candidates.length"
              class="sa-candidate-list"
            >
              <div
                v-for="(candidate, index) in settings.preferred_candidates"
                :key="index"
                class="candidate"
              >
                <span class="sa-candidate-rank">#{{ index + 1 }}</span>
                <span class="sa-candidate-label">{{ formatCandidateLabel(candidate) }}</span>
                <div class="sa-candidate-controls">
                  <button
                    type="button"
                    class="sa-icon-btn"
                    :disabled="index === 0"
                    :aria-label="`Move subagent ${index + 1} up`"
                    @click="move(index, -1)"
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    class="sa-icon-btn"
                    :disabled="index === settings.preferred_candidates.length - 1"
                    :aria-label="`Move subagent ${index + 1} down`"
                    @click="move(index, 1)"
                  >
                    ↓
                  </button>
                  <button
                    type="button"
                    class="sa-icon-btn sa-icon-btn--remove"
                    :aria-label="`Remove subagent ${index + 1}`"
                    @click="remove(index)"
                  >
                    Remove
                  </button>
                </div>
              </div>
            </div>
            <p
              v-else
              class="sa-empty"
            >
              No custom priority set — using automatic heterogeneous routing.
            </p>

            <div class="sa-builder">
              <div class="sa-builder-row">
                <div class="sa-field">
                  <label
                    class="sa-field-label"
                    for="subagent-provider"
                  >Provider</label>
                  <select
                    id="subagent-provider"
                    v-model="provider"
                    class="sa-control"
                  >
                    <option
                      v-for="item in providers"
                      :key="item.provider"
                      :value="item.provider"
                    >
                      {{ formatProviderOption(item) }}
                    </option>
                  </select>
                </div>

                <div class="sa-field">
                  <label
                    class="sa-field-label"
                    for="subagent-model"
                  >Model</label>
                  <select
                    id="subagent-model"
                    v-model="selectedModelChoice"
                    class="sa-control"
                  >
                    <option value="">
                      Provider default
                    </option>
                    <option
                      v-for="item in providerModels"
                      :key="item"
                      :value="item"
                    >
                      {{ item }}
                    </option>
                    <option value="__custom__">
                      Custom model ID…
                    </option>
                  </select>
                </div>

                <div class="sa-field">
                  <label
                    class="sa-field-label"
                    for="subagent-effort"
                  >Effort</label>
                  <select
                    id="subagent-effort"
                    v-model="effort"
                    class="sa-control"
                    :disabled="!supportsEffort"
                  >
                    <option value="">
                      Provider default
                    </option>
                    <option
                      v-for="item in effortOptions"
                      :key="item"
                      :value="item"
                    >
                      {{ formatEffortLabel(item) }}
                    </option>
                  </select>
                </div>

                <button
                  type="button"
                  class="sa-btn sa-add-btn"
                  :disabled="settings.preferred_candidates.length >= 8 || (selectedModelChoice === '__custom__' && !customModel.trim())"
                  @click="add"
                >
                  Add priority
                </button>
              </div>

              <div
                v-if="selectedModelChoice === '__custom__'"
                class="sa-custom-model-row"
              >
                <label
                  class="sa-field-label"
                  for="subagent-custom-model"
                >Custom model ID</label>
                <input
                  id="subagent-custom-model"
                  v-model="customModel"
                  class="sa-control"
                  maxlength="120"
                  placeholder="Enter provider model identifier…"
                >
              </div>
            </div>
          </fieldset>

          <div
            v-if="statusMessage"
            class="sa-status-row"
          >
            <p
              class="sa-status"
              :data-error="statusError ? 'true' : 'false'"
              :role="statusError ? 'alert' : 'status'"
            >
              {{ statusMessage }}
            </p>
            <button
              v-if="loadError"
              type="button"
              class="sa-btn"
              :disabled="loading"
              @click="load"
            >
              Retry load
            </button>
          </div>
        </div>

        <footer class="goal-dialog-actions sa-footer">
          <button
            type="button"
            class="goal-dialog-button"
            :disabled="saving"
            @click="close"
          >
            Close
          </button>
          <button
            type="submit"
            class="goal-dialog-button goal-dialog-button--primary"
            :disabled="loading || saving || loadError"
          >
            {{ saving ? 'Saving…' : 'Save priority' }}
          </button>
        </footer>
      </form>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { SubagentModelPreference } from '@/types'

type Settings = {
  preferred_candidates: SubagentModelPreference[]
  push_remote_branch: boolean
  default_preset: 'cross_review_slim' | 'readonly_brief'
}
type Provider = {
  provider: SubagentModelPreference['provider']
  label: string
  models: string[]
  supports_effort: boolean
  effort_options: string[]
  quota_state: string | null
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

const QUOTA_LABELS: Record<string, string> = {
  near: 'Near limit',
  over: 'Limit reached',
  unavailable: 'Unavailable',
  not_configured: 'Not configured',
}

const EFFORT_LABELS: Record<string, string> = {
  default: 'Default',
  none: 'None',
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra high (xhigh)',
  max: 'Max',
  ultra: 'Ultra',
}

const emit = defineEmits<{ close: []; saved: [settings: Settings] }>()

const dialog = ref<HTMLFormElement | null>(null)
const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
const returnFocus = opener?.closest('details')?.querySelector('summary') ?? opener

const loading = ref(false)
const saving = ref(false)
const loadError = ref(false)
const statusMessage = ref('')
const statusError = ref(false)
const providers = ref<Provider[]>([
  { provider: 'maso', label: 'Maso', models: [], supports_effort: false, effort_options: [], quota_state: null },
  { provider: 'codex', label: 'Codex', models: ['gpt-5.4', 'gpt-5.4-mini'], supports_effort: true, effort_options: ['minimal', 'low', 'medium', 'high', 'xhigh'], quota_state: null },
  { provider: 'claude', label: 'Claude', models: ['sonnet', 'opus', 'haiku'], supports_effort: false, effort_options: [], quota_state: null },
  { provider: 'cursor', label: 'Cursor', models: ['auto', 'claude-4.6-opus-high-thinking'], supports_effort: false, effort_options: [], quota_state: null },
  { provider: 'traex', label: 'Traex', models: ['gpt-5.4'], supports_effort: true, effort_options: ['minimal', 'low', 'medium', 'high', 'xhigh'], quota_state: null },
])
const settings = ref<Settings>(cloneSettings(null))
const provider = ref<SubagentModelPreference['provider']>('codex')
const selectedModelChoice = ref('')
const customModel = ref('')
const effort = ref('')
let activeLoad: AbortController | null = null
let activeSave: AbortController | null = null

const currentProvider = computed(() => providers.value.find(item => item.provider === provider.value))
const providerModels = computed(() => currentProvider.value?.models ?? [])
const supportsEffort = computed(() => Boolean(currentProvider.value?.supports_effort))
const effortOptions = computed(() => currentProvider.value?.effort_options ?? [])

watch(provider, () => {
  selectedModelChoice.value = ''
  customModel.value = ''
  if (!supportsEffort.value) effort.value = ''
})

function formatProviderOption(item: Provider): string {
  const status = item.quota_state ? QUOTA_LABELS[item.quota_state] : ''
  return status ? `${item.label} (${status})` : item.label
}

function formatEffortLabel(value: string): string {
  return EFFORT_LABELS[value] ?? value
}

function formatCandidateLabel(candidate: SubagentModelPreference): string {
  const parts = [PROVIDER_LABELS[candidate.provider] ?? candidate.provider]
  parts.push(candidate.model || 'default model')
  if (candidate.effort) parts.push(`effort: ${candidate.effort}`)
  return parts.join(' · ')
}

function focusableControls() {
  const root = dialog.value
  if (!root) return []
  return [...root.querySelectorAll<HTMLElement>(
    'button:not(:disabled), textarea:not(:disabled), input:not(:disabled), select:not(:disabled)',
  )].filter(el => el.getClientRects().length > 0)
}

function onDocumentKeydown(event: KeyboardEvent) {
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

async function load() {
  activeLoad?.abort()
  const controller = new AbortController()
  activeLoad = controller
  loading.value = true
  loadError.value = false
  statusMessage.value = ''
  statusError.value = false
  try {
    const response = await fetch('/api/subagents/settings', { signal: controller.signal })
    if (controller.signal.aborted) return
    if (!response.ok) {
      loadError.value = true
      statusError.value = true
      statusMessage.value = 'Could not load saved subagent priority. Retry before saving changes.'
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
    } else {
      loadError.value = true
      statusError.value = true
      statusMessage.value = 'Could not load saved subagent priority. Retry before saving changes.'
      return
    }
    if (Array.isArray(body.providers) && body.providers.length) {
      providers.value = body.providers
    }
  } catch {
    if (!controller.signal.aborted) {
      loadError.value = true
      statusError.value = true
      statusMessage.value = 'Could not load saved subagent priority. Retry before saving changes.'
    }
  } finally {
    if (activeLoad === controller) loading.value = false
  }
}

function add() {
  if (loadError.value || settings.value.preferred_candidates.length >= 8) return
  const resolvedModel = selectedModelChoice.value === '__custom__'
    ? customModel.value.trim()
    : selectedModelChoice.value.trim()
  if (selectedModelChoice.value === '__custom__' && !resolvedModel) return
  settings.value.preferred_candidates.push({
    provider: provider.value,
    model: resolvedModel || null,
    effort: supportsEffort.value && effort.value ? effort.value : null,
  })
  selectedModelChoice.value = ''
  customModel.value = ''
  effort.value = ''
  statusMessage.value = ''
}

function move(index: number, delta: number) {
  if (loadError.value) return
  const target = index + delta
  const list = settings.value.preferred_candidates
  if (target < 0 || target >= list.length) return
  const [item] = list.splice(index, 1)
  list.splice(target, 0, item)
  statusMessage.value = ''
}

function remove(index: number) {
  if (loadError.value) return
  settings.value.preferred_candidates.splice(index, 1)
  statusMessage.value = ''
}

async function save() {
  if (loading.value || saving.value || loadError.value) return
  activeSave?.abort()
  const controller = new AbortController()
  activeSave = controller
  saving.value = true
  statusMessage.value = ''
  statusError.value = false
  const payload = cloneSettings(settings.value)
  try {
    const response = await fetch('/api/subagents/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    if (controller.signal.aborted) return
    if (!response.ok) {
      statusError.value = true
      statusMessage.value = 'Could not save subagent priority.'
      return
    }
    const body = await response.json().catch(() => null)
    const committed = cloneSettings(body?.settings ?? payload)
    settings.value = cloneSettings(committed)
    statusError.value = false
    statusMessage.value = 'Subagent priority saved.'
    window.dispatchEvent(
      new CustomEvent('claude-hub:subagent-settings-updated', { detail: cloneSettings(committed) }),
    )
    emit('saved', cloneSettings(committed))
  } catch {
    if (!controller.signal.aborted) {
      statusError.value = true
      statusMessage.value = 'Could not save subagent priority.'
    }
  } finally {
    if (activeSave === controller) saving.value = false
  }
}

function close() {
  if (saving.value) return
  emit('close')
}

onMounted(() => {
  document.addEventListener('keydown', onDocumentKeydown, true)
  void load()
  void nextTick(() => dialog.value?.focus())
})

onBeforeUnmount(() => {
  activeLoad?.abort()
  activeSave?.abort()
  document.removeEventListener('keydown', onDocumentKeydown, true)
  if (returnFocus instanceof HTMLElement && returnFocus.isConnected) {
    returnFocus.focus({ preventScroll: true })
  }
})
</script>

<style scoped>
.goal-dialog-backdrop { position: fixed; inset: 0; z-index: 900; display: grid; place-items: center; padding: 16px; background: var(--ch-color-backdrop, rgba(9, 10, 14, 0.68)); }
.dialog { width: min(640px, 100%); max-height: min(88dvh, 760px); display: flex; flex-direction: column; overflow: hidden; border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-xl, 12px); background: var(--ch-color-surface); color: var(--ch-color-text); box-shadow: var(--ch-shadow-lg, 0 24px 64px rgba(0, 0, 0, 0.45)); }
.sa-head { display: grid; gap: 4px; padding: 16px 20px 12px; border-bottom: 1px solid var(--ch-color-border-muted); flex-shrink: 0; }
.sa-head-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.goal-dialog-eyebrow { font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ch-color-accent); }
.sa-head-pill { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px; background: var(--ch-color-surface-raised); color: var(--ch-color-text-muted); border: 1px solid var(--ch-color-border-muted); }
.sa-head h2 { margin: 0; font-size: 18px; line-height: 1.25; color: var(--ch-color-text-strong); }
.goal-dialog-subtitle { margin: 0; color: var(--ch-color-text-muted); font-size: 12.5px; line-height: 1.45; }
.sa-body { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; padding: 14px 20px; overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain; min-height: 0; }
.sa-section { margin: 0; padding: 12px; border: 1px solid var(--ch-color-border-muted); border-radius: var(--ch-radius-lg, 10px); background: var(--ch-color-surface-sunken); display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; min-width: 0; }
.sa-section-title { float: left; width: 100%; margin: 0; padding: 0; font-size: 12px; font-weight: 650; color: var(--ch-color-text-strong); }
.sa-note, .sa-empty { margin: 0; font-size: 12px; line-height: 1.45; color: var(--ch-color-text-muted); }
.sa-candidate-list { display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px; min-width: 0; }
.candidate { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 8px; align-items: center; padding: 6px 10px; border: 1px solid var(--ch-color-border-muted); border-radius: var(--ch-radius-md, 8px); background: var(--ch-color-surface); font-size: 12px; min-width: 0; }
.sa-candidate-rank { font-family: var(--ch-font-mono); font-size: 11px; font-weight: 700; color: var(--ch-color-accent); padding: 2px 6px; border-radius: 4px; background: var(--ch-color-accent-soft); }
.sa-candidate-label { min-width: 0; overflow-wrap: anywhere; word-break: break-word; font-weight: 500; color: var(--ch-color-text-strong); }
.sa-candidate-controls { display: flex; align-items: center; gap: 6px; }
.sa-icon-btn { min-height: 30px; min-width: 30px; padding: 0 8px; border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-sm, 6px); background: var(--ch-color-surface-control); color: var(--ch-color-text); font: inherit; font-size: 11.5px; font-weight: 600; cursor: pointer; }
.sa-icon-btn:hover:not(:disabled) { background: var(--ch-color-surface-control-hover); }
.sa-icon-btn--remove:hover:not(:disabled) { border-color: var(--ch-color-danger); color: var(--ch-color-danger); }
.sa-icon-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.sa-builder { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; padding-top: 8px; border-top: 1px solid var(--ch-color-border-muted); min-width: 0; }
.sa-builder-row { display: grid; grid-template-columns: minmax(110px, 1fr) minmax(140px, 1.3fr) minmax(110px, 1fr) auto; gap: 8px; align-items: end; min-width: 0; }
.sa-field { display: grid; gap: 4px; min-width: 0; }
.sa-field-label { font-size: 11px; font-weight: 600; color: var(--ch-color-text-muted); }
.sa-control { width: 100%; min-width: 0; min-height: 36px; box-sizing: border-box; padding: 6px 10px; border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-md, 8px); background: var(--ch-color-surface); color: var(--ch-color-text); font: inherit; font-size: 12.5px; }
.sa-btn { min-height: 36px; padding: 6px 12px; border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-md, 8px); background: var(--ch-color-surface-control); color: var(--ch-color-text); font: inherit; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap; }
.sa-btn:hover:not(:disabled) { background: var(--ch-color-surface-control-hover); }
.sa-btn:disabled { opacity: 0.55; cursor: not-allowed; }
.sa-custom-model-row { display: grid; gap: 4px; min-width: 0; }
.sa-status-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; min-width: 0; }
.sa-status { margin: 0; font-size: 12px; font-weight: 600; color: var(--ch-color-text-muted); min-width: 0; overflow-wrap: anywhere; }
.sa-status[data-error='true'] { color: var(--ch-color-danger-text, var(--ch-color-danger)); }
.sa-footer { display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding: 12px 20px; border-top: 1px solid var(--ch-color-border-muted); background: var(--ch-color-surface-raised); flex-shrink: 0; }
.goal-dialog-button { min-height: 38px; padding: 8px 14px; border: 1px solid var(--ch-color-border-strong); border-radius: var(--ch-radius-md, 8px); background: var(--ch-color-surface-control); color: var(--ch-color-text); font: inherit; font-size: 12.5px; font-weight: 600; cursor: pointer; }
.goal-dialog-button--primary { border-color: var(--ch-color-accent-strong); background: var(--ch-color-accent-strong); color: var(--ch-color-on-accent, var(--ch-color-text-inverse)); }
.goal-dialog-button:disabled { opacity: 0.55; cursor: not-allowed; }
@media (max-width: 640px), (pointer: coarse) {
  .sa-head { padding: 12px 16px 8px; gap: 2px; }
  .sa-body { padding: 10px 16px; gap: 8px; }
  .sa-section { padding: 10px; gap: 8px; }
  .sa-builder { gap: 8px; padding-top: 6px; }
  .sa-builder-row { grid-template-columns: minmax(0, 1fr); gap: 6px; }
  .candidate { grid-template-columns: auto minmax(0, 1fr); padding: 6px 8px; gap: 6px; }
  .sa-candidate-controls { grid-column: 1 / -1; justify-content: flex-end; }
  .sa-footer { padding: 10px 16px calc(10px + env(safe-area-inset-bottom, 0px)); }
  .goal-dialog-button, .sa-btn, .sa-control, .sa-icon-btn { min-height: 44px; min-width: 44px; }
}
</style>
