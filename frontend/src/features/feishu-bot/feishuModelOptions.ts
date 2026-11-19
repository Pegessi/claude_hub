import type { FeishuAiCommandModelOption, FeishuAiCommandProvider } from '@/utils/feishuBotConfig'

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
