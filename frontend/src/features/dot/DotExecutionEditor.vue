<template>
  <section
    class="dot-execution"
    data-testid="dot-execution-editor"
  >
    <div class="dot-execution__heading">
      <div>
        <h3>Execution backend</h3>
        <p v-if="execution?.agent_type">
          Currently {{ providerLabel(execution.agent_type) }}
        </p>
      </div>
    </div>

    <div
      v-if="loading"
      class="dot-execution__state"
      role="status"
      aria-live="polite"
    >
      Loading execution settings…
    </div>
    <div
      v-else-if="loadError"
      class="dot-execution__error"
      role="alert"
    >
      <span>{{ loadError }}</span>
      <LoadingButton
        type="button"
        class="ch-btn ch-btn--sm"
        :disabled="props.disabled"
        :loading="loading"
        loading-label="Reloading settings"
        @click="loadExecution"
      >
        Retry
      </LoadingButton>
    </div>
    <form
      v-else-if="execution"
      class="dot-execution__form"
      @submit.prevent="applyExecution"
    >
      <label class="dot-execution__field">
        <span>Backend</span>
        <select
          class="ch-select"
          :value="draftAgentType"
          :disabled="saving || refreshingTabs || props.disabled"
          aria-label="Execution backend"
          @change="changeAgentType(($event.target as HTMLSelectElement).value)"
        >
          <option
            v-for="agentType in execution.supported_agent_types"
            :key="agentType"
            :value="agentType"
          >
            {{ providerLabel(agentType) }}
          </option>
        </select>
      </label>

      <div class="dot-execution__field">
        <span>Environment preset</span>
        <div class="dot-execution__preset-row">
          <select
            class="ch-select"
            :value="draftPreset"
            :disabled="saving || refreshingTabs || props.disabled"
            aria-label="Dot environment preset"
            @change="selectPreset(($event.target as HTMLSelectElement).value)"
          >
            <option
              v-if="draftPreset === 'custom'"
              value="custom"
            >
              Custom (current values)
            </option>
            <option
              v-for="preset in envPresets"
              :key="preset.id"
              :value="preset.id"
            >
              {{ preset.name }}
            </option>
          </select>
          <button
            type="button"
            class="ch-btn ch-btn--sm"
            :disabled="saving || refreshingTabs || props.disabled"
            @click="showEnvManager = true"
          >
            Manage
          </button>
        </div>
        <small>
          A preset supplies this backend's endpoint, credentials, and default model.
          Values are not printed in logs.
        </small>
      </div>

      <div
        v-if="draftAgentType === 'maso'"
        class="dot-execution__preflight"
        :class="{ 'dot-execution__preflight--error': masoState === 'unavailable' }"
        :role="masoState === 'unavailable' ? 'alert' : 'status'"
        aria-live="polite"
      >
        <span>{{ masoMessage }}</span>
        <small v-if="masoEndpoint">Service: {{ masoEndpoint }}</small>
        <button
          v-if="masoState === 'unavailable'"
          type="button"
          class="ch-btn ch-btn--sm"
          :disabled="saving || refreshingTabs || props.disabled"
          @click="checkMasoService"
        >
          Retry check
        </button>
      </div>

      <p
        v-if="isDirty"
        class="dot-execution__callout"
      >
        <template v-if="execution.tab_id">
          Applying this creates a fresh Dot chat and archives the current chat.
          Dot's memory, focus, and tracked work stay available, and the archived
          conversation remains readable. Any Feishu Bot must be unbound first and
          rebound to the new chat. Retarget, disable, or remove user-created schedules
          that still point at the current chat.
        </template>
        <template v-else>
          Applying this creates Dot's first chat. Dot's memory, focus, and tracked
          work remain independent of the selected backend.
        </template>
      </p>

      <div
        v-if="applyError"
        class="dot-execution__error"
        role="alert"
      >
        <span>{{ applyError }}</span>
        <div class="dot-execution__error-actions">
          <LoadingButton
            v-if="canRetryApply"
            type="button"
            class="ch-btn ch-btn--sm"
            :disabled="props.disabled"
            :loading="saving"
            loading-label="Retrying configuration"
            @click="applyExecution"
          >
            Retry
          </LoadingButton>
          <LoadingButton
            v-if="applyErrorStatus === 409"
            type="button"
            class="ch-btn ch-btn--ghost ch-btn--sm"
            :disabled="props.disabled"
            :loading="loading"
            loading-label="Reloading configuration"
            @click="loadExecution"
          >
            Reload configuration
          </LoadingButton>
        </div>
      </div>
      <p
        v-else-if="successText"
        class="dot-execution__success"
        role="status"
      >
        {{ successText }}
      </p>
      <div
        v-if="refreshError"
        class="dot-execution__error"
        role="alert"
      >
        <span>{{ refreshError }}</span>
        <div class="dot-execution__error-actions">
          <LoadingButton
            type="button"
            class="ch-btn ch-btn--sm"
            :disabled="props.disabled"
            :loading="refreshingTabs"
            loading-label="Refreshing Chat list"
            @click="refreshCommittedTabs"
          >
            Retry tab refresh
          </LoadingButton>
        </div>
      </div>

      <div class="dot-execution__actions">
        <LoadingButton
          type="submit"
          class="ch-btn ch-btn--primary ch-btn--sm"
          :disabled="applyDisabled"
          :loading="saving"
          :loading-label="execution.tab_id ? 'Creating new Dot chat' : 'Creating Dot chat'"
        >
          {{ applyLabel }}
        </LoadingButton>
      </div>
    </form>

    <EnvPresetManager
      v-if="showEnvManager"
      :model-value="draftPreset"
      :visible="showEnvManager"
      @update:model-value="selectPreset"
      @close="closeEnvManager"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import EnvPresetManager from '@/components/EnvPresetManager.vue'
import LoadingButton from '@/components/LoadingButton.vue'
import {
  defaultLaunchEnvPresetForAgent,
  parseLaunchEnv,
  useLaunchEnvPresets,
} from '@/composables/useLaunchEnvPresets'
import { useMasoPreflight } from '@/composables/useMasoPreflight'
import { useTerminalStore } from '@/stores/terminalStore'
import {
  DotRequestError,
  loadDotExecution,
  updateDotExecution,
  type DotExecution,
  type DotProvider,
} from '@/utils/dotApi'

const props = withDefaults(defineProps<{
  disabled?: boolean
}>(), {
  disabled: false,
})

const emit = defineEmits<{
  updated: [execution: DotExecution]
  busyChange: [busy: boolean]
}>()

const terminalStore = useTerminalStore()
const {
  envPresets,
  getPresetText,
  defaultPresetTextForAgent,
} = useLaunchEnvPresets()
const execution = ref<DotExecution | null>(null)
const draftAgentType = ref<DotProvider | ''>('')
const draftPreset = ref('none')
const draftEnv = ref<Record<string, string>>({})
const loading = ref(false)
const saving = ref(false)
const refreshingTabs = ref(false)
const loadError = ref('')
const applyError = ref('')
const applyErrorStatus = ref<number | null>(null)
const refreshError = ref('')
const successText = ref('')
const showEnvManager = ref(false)
const committedRefresh = ref<{
  oldTabId: string | null
  newTabId: string | null
  oldDotWasActive: boolean
} | null>(null)

const {
  state: masoState,
  message: masoMessage,
  endpoint: masoEndpoint,
  check: checkMasoService,
} = useMasoPreflight(
  () => execution.value !== null && draftAgentType.value === 'maso',
  () => draftEnv.value.MASO_BASE_URL,
)

function providerLabel(agentType: string): string {
  return ({
    claude: 'Claude',
    codex: 'Codex',
    cursor: 'Cursor',
    traex: 'TraeX',
    maso: 'MASO',
  } satisfies Record<DotProvider, string>)[agentType as DotProvider] ?? agentType
}

function envFromText(text: string): Record<string, string> {
  return parseLaunchEnv(text) ?? {}
}

function sameEnv(left: Record<string, string>, right: Record<string, string>): boolean {
  const leftKeys = Object.keys(left)
  const rightKeys = Object.keys(right)
  return leftKeys.length === rightKeys.length
    && leftKeys.every(key => left[key] === right[key])
}

function matchingPreset(env: Record<string, string>): string {
  return envPresets.value.find(preset => sameEnv(envFromText(preset.text), env))?.id ?? 'custom'
}

function initialProvider(next: DotExecution): DotProvider | '' {
  if (
    next.agent_type
    && next.supported_agent_types.includes(next.agent_type as DotProvider)
  ) return next.agent_type as DotProvider
  if (next.supported_agent_types.includes('claude')) return 'claude'
  return next.supported_agent_types[0] ?? ''
}

function hydrateDraft(next: DotExecution) {
  const provider = initialProvider(next)
  execution.value = next
  draftAgentType.value = provider
  if (provider && next.agent_type === provider) {
    draftEnv.value = { ...next.env }
    draftPreset.value = matchingPreset(next.env)
  } else if (provider) {
    draftPreset.value = defaultLaunchEnvPresetForAgent(provider)
    draftEnv.value = envFromText(defaultPresetTextForAgent(provider))
  } else {
    draftPreset.value = 'none'
    draftEnv.value = {}
  }
  applyError.value = ''
  applyErrorStatus.value = null
  refreshError.value = ''
  committedRefresh.value = null
  successText.value = ''
}

async function loadExecution() {
  if (loading.value || saving.value || refreshingTabs.value) return
  loading.value = true
  loadError.value = ''
  try {
    hydrateDraft(await loadDotExecution())
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'Could not load execution settings.'
  } finally {
    loading.value = false
  }
}

function changeAgentType(value: string) {
  if (!execution.value?.supported_agent_types.includes(value as DotProvider)) return
  const provider = value as DotProvider
  draftAgentType.value = provider
  // Security boundary: a backend change starts from that backend's defaults.
  // Never merge or carry the old provider's endpoint, model, or credentials.
  draftPreset.value = defaultLaunchEnvPresetForAgent(provider)
  draftEnv.value = envFromText(defaultPresetTextForAgent(provider))
  applyError.value = ''
  applyErrorStatus.value = null
  successText.value = ''
}

function selectPreset(presetId: string) {
  if (presetId === 'custom') return
  const text = getPresetText(presetId)
  if (text === null) return
  draftPreset.value = presetId
  draftEnv.value = envFromText(text)
  applyError.value = ''
  applyErrorStatus.value = null
  successText.value = ''
}

function closeEnvManager() {
  showEnvManager.value = false
  if (draftPreset.value !== 'custom') selectPreset(draftPreset.value)
}

const isDirty = computed(() => {
  const current = execution.value
  if (!current || !draftAgentType.value) return false
  return current.agent_type !== draftAgentType.value || !sameEnv(current.env, draftEnv.value)
})
const applyDisabled = computed(() =>
  props.disabled
  || loading.value
  || saving.value
  || refreshingTabs.value
  || !draftAgentType.value
  || !isDirty.value
  || (draftAgentType.value === 'maso' && masoState.value !== 'available'),
)
const applyLabel = computed(() => {
  if (!execution.value?.tab_id) return 'Create Dot chat'
  if (execution.value.agent_type !== draftAgentType.value) return 'Switch backend'
  return 'Apply configuration'
})
const canRetryApply = computed(() =>
  applyErrorStatus.value === null || [409, 429, 503].includes(applyErrorStatus.value),
)

async function refreshCommittedTabs() {
  const pending = committedRefresh.value
  if (!pending || refreshingTabs.value) return
  const standalone = !saving.value
  const shouldActivate = Boolean(
    pending.oldDotWasActive
    && pending.oldTabId
    && terminalStore.activeTabId === pending.oldTabId,
  )
  refreshingTabs.value = true
  if (standalone) emit('busyChange', true)
  refreshError.value = ''
  try {
    await terminalStore.fetchTabs({ force: true })
    if (
      pending.newTabId
      && !terminalStore.tabs.some(tab => tab.id === pending.newTabId)
    ) {
      throw new Error('The new Dot chat is not visible yet.')
    }
    if (shouldActivate && pending.newTabId) terminalStore.setActiveTab(pending.newTabId)
    committedRefresh.value = null
  } catch (error) {
    const detail = error instanceof Error ? error.message : 'Could not refresh the Chat list.'
    refreshError.value = `Dot was updated, but the Chat list could not refresh. ${detail}`
  } finally {
    refreshingTabs.value = false
    if (standalone) emit('busyChange', false)
  }
}

async function applyExecution() {
  const current = execution.value
  const provider = draftAgentType.value
  if (!current || !provider || applyDisabled.value) return
  const oldTabId = current.tab_id
  const oldDotWasActive = Boolean(oldTabId && terminalStore.activeTabId === oldTabId)
  saving.value = true
  emit('busyChange', true)
  applyError.value = ''
  applyErrorStatus.value = null
  successText.value = ''
  try {
    const updated = await updateDotExecution({
      expected_revision: current.revision,
      agent_type: provider,
      env: draftEnv.value,
    })
    hydrateDraft(updated)
    committedRefresh.value = {
      oldTabId,
      newTabId: updated.tab_id,
      oldDotWasActive,
    }
    successText.value = oldTabId
      ? `Dot now uses ${providerLabel(provider)}. The previous chat was archived.`
      : `Dot chat created with ${providerLabel(provider)}.`
    emit('updated', updated)
    await refreshCommittedTabs()
  } catch (error) {
    applyErrorStatus.value = error instanceof DotRequestError ? error.status : null
    applyError.value = error instanceof Error ? error.message : 'Could not update Dot execution.'
  } finally {
    saving.value = false
    emit('busyChange', false)
  }
}

onMounted(() => { void loadExecution() })
</script>

<style scoped>
.dot-execution {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0 16px 10px;
  padding: 10px 12px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
}

.dot-execution__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.dot-execution__heading h3,
.dot-execution__heading p {
  margin: 0;
}

.dot-execution__heading h3 {
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
}

.dot-execution__heading p,
.dot-execution__field small,
.dot-execution__preflight small {
  color: var(--ch-color-text-subtle);
  font-size: var(--ch-font-size-xs);
}

.dot-execution__form,
.dot-execution__field,
.dot-execution__preflight,
.dot-execution__error {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.dot-execution__field > span {
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-xs);
  font-weight: 600;
}

.dot-execution__preset-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.dot-execution__state,
.dot-execution__callout,
.dot-execution__preflight,
.dot-execution__error,
.dot-execution__success {
  margin: 0;
  padding: 8px 10px;
  border-radius: var(--ch-radius-md);
  font-size: var(--ch-font-size-xs);
  line-height: 1.45;
}

.dot-execution__state,
.dot-execution__callout,
.dot-execution__preflight {
  border: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-muted);
}

.dot-execution__preflight--error,
.dot-execution__error {
  background: var(--ch-color-danger-bg);
  color: var(--ch-color-danger-text);
}

.dot-execution__success {
  background: var(--ch-color-success-bg);
  color: var(--ch-color-success-text, var(--ch-color-success));
}

.dot-execution__error-actions,
.dot-execution__actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}

@media (max-width: 768px) {
  .dot-execution {
    margin: 0 12px 10px;
  }

  .dot-execution .ch-select,
  .dot-execution .ch-btn {
    min-height: 40px;
    font-size: 16px;
  }
}
</style>
