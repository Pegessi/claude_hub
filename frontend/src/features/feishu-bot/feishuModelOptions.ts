import type { FeishuAiCommandCapabilities, FeishuAiCommandModelOption, FeishuAiCommandProvider } from '@/utils/feishuBotConfig'

const EFFORT_DISPLAY_LABELS: Record<string, string> = {
  default: 'Default (default)',
  none: 'None (none)',
  minimal: 'Minimal (minimal)',
  low: 'Low (low)',
  medium: 'Medium (medium)',
  high: 'High (high)',
  xhigh: 'Extra High (xhigh)',
  max: 'Max (max)',
  ultra: 'Ultra (ultra)',
}
export function aiEffortLabel(effort: string): string {
  return EFFORT_DISPLAY_LABELS[effort] ?? effort
}
export function matchesAiModelOption(option: FeishuAiCommandModelOption, modelId: string): boolean {
  const clean = modelId.trim()
  if (!clean) return false
  if (option.id === clean || option.provider_model_id === clean) return true
  return Boolean(option.supported_reasoning_efforts?.some(eff => eff.provider_model_id === clean))
}
export function resolveVariantModelId(
  provider: FeishuAiCommandProvider,
  option: FeishuAiCommandModelOption,
  effort: string,
): string {
  if (provider === 'maso' || provider === 'cursor') {
    const matchedEffort = option.supported_reasoning_efforts?.find(item => item.id === effort)
    if (matchedEffort?.provider_model_id) return matchedEffort.provider_model_id
    const defaultEff = option.default_reasoning_effort
    const fallbackEffort = defaultEff
      ? option.supported_reasoning_efforts?.find(item => item.id === defaultEff)
      : undefined
    if (fallbackEffort?.provider_model_id) return fallbackEffort.provider_model_id
  }
  return option.provider_model_id || option.id
}

const DEFAULT_PROVIDER_EFFORTS: Record<FeishuAiCommandProvider, string[]> = {
  claude: [],
  codex: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
  traex: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
  maso: ['default', 'none', 'low', 'medium', 'high', 'xhigh', 'max'],
  cursor: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'],
  antigravity: [],
}
// Provider models are supplied by the authenticated capabilities response.
const DEFAULT_PROVIDER_MODEL_OPTIONS: Record<FeishuAiCommandProvider, FeishuAiCommandModelOption[]> = {
  claude: [], codex: [], traex: [], maso: [], cursor: [], antigravity: [],
}
export function resolveAiModelOptionsForProvider(capabilities: FeishuAiCommandCapabilities | null, provider: FeishuAiCommandProvider): FeishuAiCommandModelOption[] {
  const rich = capabilities?.provider_model_options?.[provider]
  if (Array.isArray(rich) && rich.length > 0) return rich
  const legacyIds = capabilities?.provider_models?.[provider]
  if (Array.isArray(legacyIds) && legacyIds.length > 0) {
    return legacyIds.map(id => ({ id, label: id, provider_model_id: id }))
  }
  return DEFAULT_PROVIDER_MODEL_OPTIONS[provider] ?? []
}
export function resolveEffortsForModel(
  capabilities: FeishuAiCommandCapabilities | null,
  provider: FeishuAiCommandProvider,
  modelId: string,
  option?: FeishuAiCommandModelOption | null,
): string[] {
  const clean = modelId.trim()
  if (!clean) {
    // Only ENV_KEY providers (codex, traex) have a separate effort channel when Provider default
    // (model === '') is chosen; model-bound effort providers (maso, cursor) and non-effort
    // providers (claude, antigravity) require a concrete model ID.
    if (provider === 'maso' || provider === 'cursor' || provider === 'claude' || provider === 'antigravity') {
      return []
    }
    const perProviderDefault = capabilities?.provider_reasoning_efforts?.[provider]
    if (Array.isArray(perProviderDefault)) return perProviderDefault
    return DEFAULT_PROVIDER_EFFORTS[provider] ?? []
  }
  const matched = option ?? resolveAiModelOptionsForProvider(capabilities, provider).find(opt => matchesAiModelOption(opt, clean)) ?? null
  if (matched && Array.isArray(matched.supported_reasoning_efforts)) {
    if (matched.supported_reasoning_efforts.length > 0) {
      return matched.supported_reasoning_efforts.map(item => item.id)
    }
    if (provider === 'maso' || provider === 'cursor' || provider === 'claude' || provider === 'antigravity') {
      return []
    }
  }
  const perModel = capabilities?.model_reasoning_efforts?.[clean]
  if (Array.isArray(perModel)) return perModel
  const perProvider = capabilities?.provider_reasoning_efforts?.[provider]
  if (Array.isArray(perProvider)) return perProvider
  return DEFAULT_PROVIDER_EFFORTS[provider] ?? []
}
