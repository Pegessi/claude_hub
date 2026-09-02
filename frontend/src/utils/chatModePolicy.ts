export interface ChatModeOption {
  id: string
  label: string
  description?: string | null
}

export interface ChatModeCapabilities {
  supports_dynamic_modes?: boolean
  available_modes?: Array<ChatModeOption | null>
  current_mode?: string | null
}

/**
 * Return only usable, unique modes advertised by the active backend stream.
 *
 * Provider names are deliberately absent: the backend owns capability
 * detection, so Claude, Codex, Cursor, and future providers all follow the
 * same rendering policy without frontend feature guesses.
 */
export function getAvailableChatModes(
  capabilities: ChatModeCapabilities | null | undefined,
): ChatModeOption[] {
  if (!capabilities?.supports_dynamic_modes || !Array.isArray(capabilities.available_modes)) {
    return []
  }

  const seen = new Set<string>()
  const modes: ChatModeOption[] = []
  for (const option of capabilities.available_modes) {
    if (!option || typeof option.id !== 'string' || typeof option.label !== 'string') continue
    const id = option.id.trim()
    const label = option.label.trim()
    if (!id || !label || seen.has(id)) continue
    seen.add(id)
    modes.push({
      id,
      label,
      ...(option.description ? { description: option.description } : {}),
    })
  }
  return modes
}

export function getCurrentChatModeId(
  capabilities: ChatModeCapabilities | null | undefined,
): string | null {
  const currentMode = capabilities?.current_mode
  if (!currentMode) return null
  return getAvailableChatModes(capabilities).some(option => option.id === currentMode)
    ? currentMode
    : null
}
