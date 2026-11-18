<template>
  <div class="codex-model-selector">
    <div class="codex-model-selector__field">
      <label for="create-codex-model">Codex Model</label>
      <select
        id="create-codex-model"
        :value="modelId"
        @change="selectModel(($event.target as HTMLSelectElement).value)"
      >
        <option value="">
          Codex default
        </option>
        <option
          v-if="modelId && !selectedModel"
          :value="modelId"
        >
          {{ modelId }} (custom/current)
        </option>
        <option
          v-for="model in models"
          :key="model.id"
          :value="model.id"
        >
          {{ model.label }}
        </option>
      </select>
    </div>
    <div class="codex-model-selector__field">
      <label for="create-codex-effort">Codex Reasoning Effort</label>
      <select
        id="create-codex-effort"
        :value="effortId"
        @change="updateEnv({ CODEX_REASONING_EFFORT: ($event.target as HTMLSelectElement).value })"
      >
        <option value="">
          {{ selectedModel?.default_reasoning_effort
            ? `Model default (${selectedModel.default_reasoning_effort})` : 'Codex default' }}
        </option>
        <option
          v-if="effortId && !efforts.some(effort => effort.id === effortId)"
          :value="effortId"
        >
          {{ effortId }} (custom/current)
        </option>
        <option
          v-for="effort in efforts"
          :key="effort.id"
          :value="effort.id"
        >
          {{ effort.label || effort.id }}
        </option>
      </select>
    </div>
    <p
      v-if="loading"
      role="status"
    >
      Loading local Codex models…
    </p>
    <p
      v-else-if="error"
      role="alert"
    >
      {{ error }}
      <button
        type="button"
        class="ch-btn ch-btn--sm"
        @click="loadModels"
      >
        Retry models
      </button>
    </p>
    <p>
      Overrides apply to this launch without changing the saved preset.
      Custom/current values are preserved; model availability depends on your Codex account.
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { parseLaunchEnv } from '@/composables/useLaunchEnvPresets'
import type { StreamModelOption } from '@/types'

const props = defineProps<{ envText: string }>()
const emit = defineEmits<{ (e: 'update:envText', value: string): void }>()
const models = ref<StreamModelOption[]>([])
const loading = ref(false)
const error = ref('')
let request: AbortController | null = null
const env = computed(() => parseLaunchEnv(props.envText) || {})
const modelId = computed(() => env.value.CODEX_MODEL || '')
const effortId = computed(() => env.value.CODEX_REASONING_EFFORT || '')
const selectedModel = computed(() => models.value.find(model => model.id === modelId.value))
const efforts = computed(() => selectedModel.value?.supported_reasoning_efforts || [])

function updateEnv(updates: Record<string, string>) {
  // Keep unrelated preset lines verbatim: parsing and serializing them again
  // can strip a second layer of quotes from an unrelated environment value.
  const lines = (props.envText ? props.envText.split('\n') : []).filter(raw => {
    const line = raw.trim()
    if (!line || line.startsWith('#')) return true
    const normalized = line.startsWith('export ') ? line.slice(7).trim() : line
    const equals = normalized.indexOf('=')
    return equals <= 0 || !Object.hasOwn(updates, normalized.slice(0, equals).trim())
  })
  for (const [key, value] of Object.entries(updates)) {
    if (value) lines.push(`${key}=${value}`)
  }
  emit('update:envText', lines.join('\n'))
}

function selectModel(id: string) {
  const model = models.value.find(option => option.id === id)
  const updates: Record<string, string> = { CODEX_MODEL: id }
  // A deliberate model switch must not carry an incompatible effort to the
  // new model. Discovery and preset changes never rewrite the user's values.
  if (id !== modelId.value && !model?.supported_reasoning_efforts.some(effort => effort.id === effortId.value)) {
    updates.CODEX_REASONING_EFFORT = ''
  }
  updateEnv(updates)
}

async function loadModels() {
  request?.abort()
  const current = new AbortController()
  request = current
  loading.value = true
  error.value = ''
  try {
    const response = await fetch('/api/codex/models', { signal: current.signal })
    if (!response.ok) throw new Error('Could not load local Codex models. Keep the current/default model or retry.')
    const result: StreamModelOption[] = await response.json()
    if (!current.signal.aborted) models.value = result
  } catch {
    if (!current.signal.aborted) error.value = 'Could not load local Codex models. Keep the current/default model or retry.'
  } finally {
    if (!current.signal.aborted) loading.value = false
  }
}

onMounted(loadModels)
onUnmounted(() => request?.abort())
</script>

<style scoped>
.codex-model-selector {
  margin-bottom: 16px;
}

.codex-model-selector__field {
  margin-bottom: 12px;
}

label {
  display: block;
  margin-bottom: 6px;
  color: var(--ch-color-text);
  font-size: var(--ch-font-size-sm);
}

select {
  box-sizing: border-box;
  width: 100%;
  min-height: 44px;
  padding: 8px 12px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control);
  font-size: 16px;
}

select:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 2px;
}

p {
  margin: 8px 0;
  color: var(--ch-color-text-muted);
  font-size: var(--ch-font-size-sm);
  overflow-wrap: anywhere;
}
</style>
