<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="backdrop"
      @click.self="close"
    >
      <form
        ref="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cross-review-title"
        class="dialog"
        @submit.prevent="submit"
      >
        <header>
          <small>New Goal</small><h2 id="cross-review-title">
            Cross-Review &amp; Slim
          </h2><p>Choose a review workflow and its deliverables.</p>
        </header>
        <div class="body">
          <fieldset :disabled="loading">
            <legend>Workflow preset</legend>
            <label
              v-for="item in presets"
              :key="item.value"
              class="card"
            ><input
              v-model="settings.default_preset"
              type="radio"
              name="preset"
              :value="item.value"
            ><span><strong>{{ item.label }}</strong><br>{{ item.description }}</span></label>
          </fieldset>
          <fieldset :disabled="loading">
            <legend>Reviewer mode</legend>
            <label><input
              v-model="settings.review_mode"
              type="radio"
              name="mode"
              value="single"
            > Single reviewer</label>
            <label><input
              v-model="settings.review_mode"
              type="radio"
              name="mode"
              value="dual"
            > Dual consensus</label>
          </fieldset>
          <fieldset :disabled="loading">
            <legend>Reviewer pool priority</legend>
            <p v-if="!settings.preferred_candidates.length">
              Automatic heterogeneous routing
            </p>
            <div
              v-for="(candidate, index) in settings.preferred_candidates"
              :key="index"
              class="candidate"
            >
              <span>{{ candidate.provider }}{{ candidate.model ? ` / ${candidate.model}` : '' }}{{ candidate.effort ? ` / ${candidate.effort}` : '' }}</span>
              <button
                type="button"
                :disabled="index === 0"
                :aria-label="`Move reviewer ${index + 1} up`"
                @click="move(index, -1)"
              >
                ↑
              </button>
              <button
                type="button"
                :disabled="index === settings.preferred_candidates.length - 1"
                :aria-label="`Move reviewer ${index + 1} down`"
                @click="move(index, 1)"
              >
                ↓
              </button>
              <button
                type="button"
                :aria-label="`Remove reviewer ${index + 1}`"
                @click="remove(index)"
              >
                Remove
              </button>
            </div>
            <label for="cross-provider">Provider</label><select
              id="cross-provider"
              v-model="provider"
            >
              <option
                v-for="item in providers"
                :key="item.provider"
                :value="item.provider"
              >
                {{ item.label }}{{ item.quota_state ? ` (${item.quota_state})` : '' }}
              </option>
            </select>
            <label>Model <input
              v-model="model"
              list="review-models"
              placeholder="Provider default"
            ><datalist id="review-models"><option
              v-for="item in providerModels"
              :key="item"
              :value="item"
            /></datalist></label>
            <label for="cross-effort">Effort</label><select
              id="cross-effort"
              v-model="effort"
              :disabled="!supportsEffort"
            >
              <option value="">
                Provider default
              </option><option
                v-for="item in effortOptions"
                :key="item"
                :value="item"
              >
                {{ item }}
              </option>
            </select>
            <p v-if="supportsEffort">
              Effort is a request, not a guarantee. Unsupported model/effort combinations may use the provider default.
            </p>
            <button
              type="button"
              :disabled="settings.preferred_candidates.length >= 8"
              @click="add"
            >
              Add reviewer
            </button>
            <button
              type="button"
              :disabled="saving || loading"
              @click="save"
            >
              Save as default
            </button><p role="status">
              {{ feedback }}
            </p>
          </fieldset>
          <fieldset :disabled="loading">
            <legend>Deliverables &amp; wrap-up</legend>
            <label><input
              v-model="settings.include_review_briefing"
              type="checkbox"
            > 3-minute Review Briefing</label>
            <label><input
              v-model="settings.generate_mr_description"
              type="checkbox"
            > Draft MR description</label>
            <label><input
              v-model="settings.push_remote_branch"
              type="checkbox"
              :disabled="settings.default_preset === 'readonly_brief'"
            > Push feature branch on PASS</label>
            <p v-if="settings.default_preset === 'readonly_brief'">
              Disabled in Read-Only Audit
            </p>
          </fieldset>
          <label for="cross-review-objective">Scope / focus notes</label><textarea
            id="cross-review-objective"
            v-model="objective"
            maxlength="4000"
            rows="4"
            required
          /><span>{{ objective.length }}/4000</span>
          <p
            v-if="error"
            role="alert"
          >
            {{ error }}
          </p>
        </div>
        <footer>
          <button
            type="button"
            @click="close"
          >
            Cancel
          </button><button
            type="submit"
            :disabled="busy || loading || !objective.trim()"
          >
            Start Cross-Review
          </button>
        </footer>
      </form>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import type { CrossReviewCandidate, StrictReviewMode } from '@/types'

type Preset = 'mr_slim' | 'bug_only' | 'readonly_brief'
type Settings = { default_preset: Preset; review_mode: StrictReviewMode; preferred_candidates: CrossReviewCandidate[]; include_review_briefing: boolean; generate_mr_description: boolean; push_remote_branch: boolean }
type Provider = { provider: CrossReviewCandidate['provider']; label: string; models: string[]; supports_effort: boolean; effort_options: string[]; quota_state: string | null }
const props = defineProps<{ open: boolean; busy: boolean; error: string | null }>()
const emit = defineEmits<{ close: []; submit: [input: { objective: string; strictReview: boolean; strict_review_preset: Preset; strict_review_mode: StrictReviewMode; strict_review_candidates: CrossReviewCandidate[]; strict_review_briefing: boolean; strict_review_mr_description: boolean; strict_review_push_branch: boolean }] }>()
const defaults = (): Settings => ({ default_preset: 'mr_slim', review_mode: 'single', preferred_candidates: [], include_review_briefing: true, generate_mr_description: false, push_remote_branch: false })
const settings = ref(defaults())
const providers = ref<Provider[]>(['maso', 'codex', 'claude', 'cursor', 'traex'].map(name => ({ provider: name as Provider['provider'], label: name, models: [], supports_effort: name === 'codex' || name === 'traex', effort_options: [], quota_state: null })))
const presets: { value: Preset; label: string; description: string }[] = [
  { value: 'mr_slim', label: 'MR Slim & Gate', description: 'Review the full change and remove unnecessary additions.' },
  { value: 'bug_only', label: 'Bug Gate Only', description: 'Review reachable blocking defects and verification.' },
  { value: 'readonly_brief', label: 'Read-Only Audit', description: 'Inspect and report without changing repository files.' },
]
const objectives: Record<Preset, string> = { mr_slim: 'Review and slim the current change, then verify the result.', bug_only: 'Review the current change for reachable blocking defects and verify the result.', readonly_brief: 'Inspect the current change without modifying files and report defects and evidence.' }
const objective = ref(objectives.mr_slim)
const provider = ref<CrossReviewCandidate['provider']>('codex')
const model = ref('')
const effort = ref('')
const supportsEffort = computed(() => providers.value.find(item => item.provider === provider.value)?.supports_effort ?? false)
const effortOptions = computed(() => providers.value.find(item => item.provider === provider.value)?.effort_options ?? [])
watch([supportsEffort, effortOptions], () => { if (!supportsEffort.value || !effortOptions.value.includes(effort.value)) effort.value = '' })
const providerModels = computed(() => providers.value.find(item => item.provider === provider.value)?.models ?? [])
const feedback = ref('')
const dialog = ref<HTMLElement | null>(null)
let returnFocus: HTMLElement | null = null
let epoch = 0
let saveSeq = 0
const loading = ref(false)
const saving = ref(false)
watch(() => props.open, async open => {
  if (!open) { epoch++; saveSeq++; loading.value = false; saving.value = false; document.removeEventListener('keydown', onDocumentKeydown); returnFocus?.focus(); return }
  returnFocus = document.activeElement as HTMLElement
  const request = ++epoch
  loading.value = true
  document.addEventListener('keydown', onDocumentKeydown)
  settings.value = defaults(); objective.value = objectives.mr_slim; feedback.value = ''
  await nextTick(); dialog.value?.querySelector<HTMLElement>('input:not(:disabled), textarea')?.focus()
  try {
    const response = await fetch('/api/goals/cross-review-settings', { credentials: 'same-origin' })
    if (!response.ok) throw new Error('Settings unavailable')
    const data = await response.json() as { settings: Settings; providers: Provider[] }
    if (request !== epoch) return
    providers.value = data.providers
    settings.value = data.settings
  } catch { if (request === epoch) feedback.value = 'Defaults unavailable. You can still start a review.' }
  finally { if (request === epoch) loading.value = false }
})
watch(() => settings.value.default_preset, (value, previous) => {
  if (value === 'readonly_brief') settings.value.push_remote_branch = false
  if (objective.value === objectives[previous]) objective.value = objectives[value]
})
function close() { if (!props.busy) emit('close') }
function onDocumentKeydown(event: KeyboardEvent) {
  if (event.defaultPrevented) return
  if (event.key === 'Escape') { event.preventDefault(); close() }
  else if (event.key === 'Tab') trapFocus(event)
}
onBeforeUnmount(() => { epoch++; saveSeq++; document.removeEventListener('keydown', onDocumentKeydown) })
function trapFocus(event: KeyboardEvent) {
  const all = [...(dialog.value?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)') ?? [])]
  const items = all.filter(item => {
    if (!(item instanceof HTMLInputElement) || item.type !== 'radio' || !item.name) return true
    const group = all.filter((other): other is HTMLInputElement => other instanceof HTMLInputElement && other.type === 'radio' && other.name === item.name)
    return item.checked || !group.some(other => other.checked)
  })
  if (!items.length) return
  if (!items.includes(document.activeElement as HTMLElement)) { event.preventDefault(); (event.shiftKey ? items[items.length - 1] : items[0]).focus() }
  else if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items[items.length - 1].focus() }
  else if (!event.shiftKey && document.activeElement === items[items.length - 1]) { event.preventDefault(); items[0].focus() }
}
function add() { settings.value.preferred_candidates.push({ provider: provider.value, model: model.value.trim() || null, effort: (effort.value || null) as CrossReviewCandidate['effort'] }) }
function remove(index: number) { settings.value.preferred_candidates.splice(index, 1) }
function move(index: number, delta: number) { const list = settings.value.preferred_candidates; const [item] = list.splice(index, 1); list.splice(index + delta, 0, item) }
async function save() {
  if (saving.value || loading.value) return
  saving.value = true
  const request = epoch
  const sequence = ++saveSeq
  const saved = JSON.stringify(settings.value)
  try {
    const response = await fetch('/api/goals/cross-review-settings', { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: saved })
    if (!response.ok) throw new Error('Save failed')
    const data = await response.json() as { settings: Settings; providers: Provider[] }
    if (request !== epoch || sequence !== saveSeq) return
    if (JSON.stringify(settings.value) === saved) settings.value = data.settings
    providers.value = data.providers
    feedback.value = 'Defaults saved.'
  } catch { if (request === epoch && sequence === saveSeq) feedback.value = 'Could not save defaults. Try again.' }
  finally { if (request === epoch && sequence === saveSeq) saving.value = false }
}
function submit() { if (props.busy || loading.value || !objective.value.trim()) return; emit('submit', { objective: objective.value, strictReview: true, strict_review_preset: settings.value.default_preset, strict_review_mode: settings.value.review_mode, strict_review_candidates: settings.value.preferred_candidates, strict_review_briefing: settings.value.include_review_briefing, strict_review_mr_description: settings.value.generate_mr_description, strict_review_push_branch: settings.value.push_remote_branch }) }
</script>

<style scoped>
.backdrop { position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center; background: var(--ch-color-overlay); }
.dialog { display: flex; flex-direction: column; width: min(620px, calc(100vw - 32px)); max-height: min(90vh, var(--visual-viewport-height, 90vh)); color: var(--ch-color-text); background: var(--ch-color-surface); border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-md); box-shadow: var(--ch-shadow-md); }
header, footer { padding: 20px; } h2, p { margin: 4px 0; } .body { padding: 0 20px 20px; overflow-y: auto; display: grid; gap: 10px; } fieldset { display: grid; gap: 8px; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-sm); padding: 12px; } legend { font-weight: 600; } label { display: flex; align-items: center; gap: 8px; min-height: 44px; } .card { padding: 8px; background: var(--ch-color-surface-soft); border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-sm); } .candidate { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; } .candidate span { flex: 1; } input, select, textarea, button { font: inherit; color: inherit; } select, textarea, input:not([type]) { min-height: 44px; background: var(--ch-color-surface); border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-sm); padding: 8px; } button { min-height: 44px; padding: 8px 12px; border: 1px solid var(--ch-color-border); border-radius: var(--ch-radius-sm); background: var(--ch-color-surface); cursor: pointer; } footer button[type="submit"] { background: var(--ch-color-accent-strong); color: var(--ch-color-on-accent); } button:disabled { opacity: .55; } footer { display: flex; justify-content: flex-end; gap: 8px; border-top: 1px solid var(--ch-color-border); } button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible { outline: 2px solid var(--ch-color-accent-ring); }
@media (max-width: 640px) { .backdrop { align-items: flex-end; padding-bottom: var(--keyboard-height, 0px); } .dialog { width: 100%; max-height: var(--visual-viewport-height, 90vh); border-radius: 16px 16px 0 0; } footer { padding-bottom: max(16px, env(safe-area-inset-bottom)); } }
</style>
