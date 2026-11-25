<template>
  <section
    class="realtime-agent"
    data-testid="feishu-realtime-agent-settings"
  >
    <h4>Background execution agent</h4>
    <p>Checks messages in temporary, read-only sessions. Sends useful summaries and reply drafts without interrupting Dot.</p>
    <div
      v-if="error"
      role="alert"
    >
      {{ error }}
    </div>
    <div class="realtime-agent__fields">
      <label>
        <span>Provider</span>
        <select
          :value="draft.provider"
          class="ch-input"
          :disabled="disabled"
          aria-label="Provider"
          data-testid="feishu-realtime-provider"
          @change="changeProvider(($event.target as HTMLSelectElement).value as FeishuAiCommandProvider)"
        >
          <option
            v-for="provider in providers"
            :key="provider"
            :value="provider"
          >{{ provider }}</option>
        </select>
      </label>
      <label>
        <span>Model</span>
        <select
          :value="selectedModel"
          class="ch-input"
          :disabled="disabled"
          aria-label="Model"
          data-testid="feishu-realtime-model"
          @change="changeModel(($event.target as HTMLSelectElement).value)"
        >
          <option value="">Automatic (live catalog)</option>
          <option
            v-if="draft.model && !currentModel"
            :value="draft.model"
          >{{ draft.model }} (current)</option>
          <option
            v-for="model in models"
            :key="model.id"
            :value="model.id"
          >{{ model.label || model.id }}</option>
        </select>
      </label>
      <label>
        <span>Thinking intensity</span>
        <select
          v-model="draft.reasoning_effort"
          class="ch-input"
          :disabled="disabled || !efforts.length"
          aria-label="Thinking intensity"
          data-testid="feishu-realtime-effort"
        >
          <option value="">Provider default</option>
          <option
            v-for="effort in efforts"
            :key="effort"
            :value="effort"
          >{{ aiEffortLabel(effort) }}</option>
        </select>
      </label>
      <label>
        <span>Workspace</span>
        <select
          v-model="draft.workspace_id"
          class="ch-input"
          :disabled="disabled"
          aria-label="Workspace"
          data-testid="feishu-realtime-workspace"
        >
          <option value="">Configured default workspace</option>
          <option
            v-if="draft.workspace_id && !workspaces.some(item => item.id === draft.workspace_id)"
            :value="draft.workspace_id"
          >{{ draft.workspace_id }} (current)</option>
          <option
            v-for="workspace in workspaces"
            :key="workspace.id"
            :value="workspace.id"
          >{{ workspace.name }}</option>
        </select>
      </label>
    </div>
    <div class="realtime-agent__actions">
      <button
        type="button"
        class="ch-btn"
        :disabled="disabled || uncertain"
        @click="save"
      >
        Save execution agent
      </button>
      <button
        type="button"
        class="ch-btn ch-btn--ghost"
        :disabled="busy"
        @click="load"
      >
        Reload execution settings
      </button>
      <span
        v-if="saved"
        data-testid="feishu-realtime-agent-saved"
      >Execution agent saved.</span>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  loadFeishuRealtimeConfig, updateFeishuRealtimeConfig,
  type FeishuAiCommandCapabilities, type FeishuAiCommandProvider,
  type FeishuAiCommandReasoningEffort, type FeishuRealtimeEventConfig,
} from '@/utils/feishuBotConfig'
import { aiEffortLabel, matchesAiModelOption, resolveAiModelOptionsForProvider, resolveEffortsForModel } from './feishuModelOptions'

const props = defineProps<{ capabilities: FeishuAiCommandCapabilities | null }>()
const draft = reactive<FeishuRealtimeEventConfig>({ enabled: false, provider: 'maso', model: '', reasoning_effort: 'high', workspace_id: '' })
const workspaces = ref<Array<{ id: string; name: string }>>([])
const loaded = ref(false), busy = ref(false), uncertain = ref(false), saved = ref(false), error = ref('')
const controller = new AbortController()
const disabled = computed(() => busy.value || !loaded.value || !props.capabilities)
const providers = computed(() => [...new Set([...(props.capabilities?.providers ?? []), draft.provider])])
const models = computed(() => resolveAiModelOptionsForProvider(props.capabilities, draft.provider))
const currentModel = computed(() => models.value.find(model => matchesAiModelOption(model, draft.model)))
const selectedModel = computed(() => currentModel.value?.id ?? draft.model)
const efforts = computed(() => {
  // Automatic realtime selection resolves a reasoning model on the server.
  if (!draft.model && draft.provider === 'maso') {
    return props.capabilities?.provider_reasoning_efforts?.maso ?? ['low', 'medium', 'high']
  }
  return resolveEffortsForModel(props.capabilities, draft.provider, draft.model, currentModel.value)
})
function normalizeEffort() {
  if (!efforts.value.includes(draft.reasoning_effort)) {
    draft.reasoning_effort = (efforts.value.includes('high') ? 'high' : efforts.value[0] ?? '') as FeishuAiCommandReasoningEffort
  }
}
function changeProvider(provider: FeishuAiCommandProvider) {
  draft.provider = provider
  draft.model = ''
  normalizeEffort()
  saved.value = false
}
function changeModel(model: string) {
  draft.model = model
  normalizeEffort()
  saved.value = false
}
async function load() {
  if (busy.value) return
  busy.value = true
  saved.value = false
  error.value = ''
  try {
    const [result, response] = await Promise.all([
      loadFeishuRealtimeConfig(controller.signal),
      fetch('/api/workspaces', { credentials: 'same-origin', signal: controller.signal }),
    ])
    if (!response.ok) throw new Error('Could not load workspaces.')
    const choices = await response.json() as Array<{ id: string; name: string }>
    Object.assign(draft, result.config, { workspace_id: result.config.workspace_id ?? '' })
    workspaces.value = choices
    loaded.value = true
    uncertain.value = false
  } catch {
    if (!controller.signal.aborted) error.value = 'Could not load execution settings. Reload to try again.'
  } finally {
    busy.value = false
  }
}
async function save() {
  if (disabled.value || uncertain.value) return
  busy.value = true
  saved.value = false
  error.value = ''
  try {
    const { config } = await updateFeishuRealtimeConfig({
      provider: draft.provider,
      model: currentModel.value?.id ?? draft.model,
      reasoning_effort: draft.reasoning_effort,
      workspace_id: draft.workspace_id,
    }, controller.signal)
    Object.assign(draft, config, { workspace_id: config.workspace_id ?? '' })
    saved.value = true
  } catch {
    if (!controller.signal.aborted) {
      uncertain.value = true
      error.value = 'Could not confirm the save. Your draft is retained. Reload execution settings before retrying.'
    }
  } finally {
    busy.value = false
  }
}
watch(draft, () => { saved.value = false }, { flush: 'sync' })
onMounted(load)
onUnmounted(() => controller.abort())
</script>

<style scoped>
.realtime-agent { margin-top: 0.8rem; }
.realtime-agent h4 { margin: 0 0 0.4rem; }
.realtime-agent p { margin: 0 0 0.8rem; color: var(--text-muted); }
.realtime-agent__fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.7rem; }
.realtime-agent__fields label { display: grid; gap: 0.3rem; min-width: 0; }
.realtime-agent__fields select { min-width: 0; width: 100%; }
.realtime-agent__actions { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; margin-top: 0.8rem; }
@media (max-width: 600px) {
  .realtime-agent__fields { grid-template-columns: minmax(0, 1fr); }
  .realtime-agent__fields select, .realtime-agent__actions button { min-height: 44px; }
}
</style>
