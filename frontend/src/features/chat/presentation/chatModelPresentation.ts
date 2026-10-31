import type { StreamCapabilities, StreamModelOption, TerminalTab } from '@/types'

// Providers select models through different environment variables. These keys
// are also used by the pane's existing switch-env action, not just its labels.
const MODEL_ENV_VAR: Record<string, string> = {
  claude: 'ANTHROPIC_MODEL',
  codex: 'CODEX_MODEL',
  // TraeX uses the Codex app-server collaborationMode.settings.model channel.
  traex: 'CODEX_MODEL',
  cursor: 'CURSOR_MODEL',
  maso: 'MASO_MODEL',
  antigravity: 'ANTIGRAVITY_MODEL',
}
const REASONING_EFFORT_ENV: Record<string, string> = {
  codex: 'CODEX_REASONING_EFFORT',
  traex: 'CODEX_REASONING_EFFORT',
}
export const LEGACY_TRAEX_REASONING_EFFORT_ENV = 'TRAEX_REASONING_EFFORT'

type ModelTab = Pick<TerminalTab, 'agent_type' | 'env'>
type ModelCapabilities = Pick<StreamCapabilities, 'available_models' | 'current_model' | 'current_reasoning_effort'>

export interface ChatModelPresentation {
  modelEnvVar: string | null
  reasoningEffortEnvVar: string | null
  currentModel: string
  modelOptions: StreamModelOption[]
  currentModelOption: StreamModelOption | null
  currentReasoningEffort: string
  combinedModelLabel: string
}

export function effortLabel(effort?: string | null): string {
  if (!effort) return 'Default'
  return ({ xhigh: 'Extra High' } as Record<string, string>)[effort]
    ?? effort.replace(/(^|[-_])\w/g, part => part.replace(/[-_]/, ' ').toUpperCase())
}

function selectedReasoningEffort(
  tab: ModelTab | null,
  model: StreamModelOption | null,
  currentModel: string,
  effectiveCurrentModel: string,
  effortEnvVar: string | null,
): string {
  if (tab?.agent_type === 'maso') {
    return model?.supported_reasoning_efforts.find(
      effort => effort.provider_model_id === effectiveCurrentModel,
    )?.id ?? ''
  }
  if (tab?.agent_type === 'cursor') {
    if (currentModel === model?.provider_model_id) return ''
    return model?.supported_reasoning_efforts.find(
      effort => effort.provider_model_id === currentModel,
    )?.id ?? ''
  }
  if (!effortEnvVar) return ''
  const selected = tab?.env?.[effortEnvVar]
    ?? (tab?.agent_type === 'traex' ? tab.env?.[LEGACY_TRAEX_REASONING_EFFORT_ENV] : '')
    ?? ''
  if (selected && model && !model.supported_reasoning_efforts.some(effort => effort.id === selected)) return ''
  return selected
}

/** Derive picker text and selection without owning tab, menu or request state. */
export function presentChatModel(
  tab: ModelTab | null,
  capabilities: ModelCapabilities | null,
): ChatModelPresentation {
  const agentType = tab?.agent_type
  const modelEnvVar = agentType ? MODEL_ENV_VAR[agentType] ?? null : null
  const reasoningEffortEnvVar = agentType ? REASONING_EFFORT_ENV[agentType] ?? null : null
  const currentModel = modelEnvVar ? tab?.env?.[modelEnvVar] ?? '' : ''
  const modelOptions = capabilities?.available_models ?? []
  const effectiveCurrentModel = currentModel && currentModel !== 'auto'
    ? currentModel : capabilities?.current_model ?? ''
  const currentModelOption = modelOptions.find(model =>
    model.id === effectiveCurrentModel
    || model.provider_model_id === effectiveCurrentModel
    || model.supported_reasoning_efforts.some(effort => effort.provider_model_id === effectiveCurrentModel),
  ) ?? null
  // Codex-family providers report an effective thread model. Cursor instead
  // exposes an explicit Auto row; unknown custom model ids remain visible.
  const currentModelLabel = currentModelOption?.label
    ?? (currentModel || (modelOptions.some(model => model.id === 'auto') ? 'Auto' : 'default'))
  const currentReasoningEffort = selectedReasoningEffort(
    tab, currentModelOption, currentModel, effectiveCurrentModel, reasoningEffortEnvVar,
  )
  const currentReasoningEffortLabel = effortLabel(
    currentReasoningEffort || capabilities?.current_reasoning_effort || currentModelOption?.default_reasoning_effort,
  )
  const combinedModelLabel = currentModelOption?.supported_reasoning_efforts.length
    ? `${currentModelLabel} · ${currentReasoningEffortLabel}` : currentModelLabel
  return {
    modelEnvVar,
    reasoningEffortEnvVar,
    currentModel,
    modelOptions,
    currentModelOption,
    currentReasoningEffort,
    combinedModelLabel,
  }
}

/** Empty explicit selection continues to match the provider's Auto row. */
export function isChatModelActive(model: StreamModelOption, presentation: ChatModelPresentation): boolean {
  if (model.id === 'auto') return presentation.currentModel === '' || presentation.currentModel === 'auto'
  return presentation.currentModelOption?.id === model.id
}

export function isChatModelEffortActive(
  model: StreamModelOption,
  effort: string,
  presentation: ChatModelPresentation,
): boolean {
  return isChatModelActive(model, presentation) && presentation.currentReasoningEffort === effort
}

export function defaultModelReasoningEffortLabel(
  model: StreamModelOption,
  currentReasoningEffort?: string | null,
): string {
  return effortLabel(currentReasoningEffort ?? model.default_reasoning_effort)
}

export function filterChatModels(models: StreamModelOption[], query: string): StreamModelOption[] {
  const q = query.trim().toLowerCase()
  if (!q) return models
  return models.filter(model => model.id.toLowerCase().includes(q) || model.label.toLowerCase().includes(q))
}
