import type { LayoutType, TerminalAgentStatus } from '@/types'

export const API_BASE = '/api'
export const STORAGE_KEY_LAYOUT = 'claude_hub_layout_type'
export const STORAGE_KEY_SIDEBAR = 'claude_hub_sidebar_collapsed'
export const STORAGE_KEY_PINNED_CHATS = 'claude_hub_pinned_chat_ids'
export const STATUS_POLL_INTERVAL_MS = 5000

export const LAYOUT_CONFIGS: Record<LayoutType, { rows: number; cols: number }> = {
  '1x1': { rows: 1, cols: 1 },
  '2x1': { rows: 1, cols: 2 },
  '1x2': { rows: 2, cols: 1 },
  '3x1': { rows: 1, cols: 3 },
  '1x3': { rows: 3, cols: 1 },
  '2x2': { rows: 2, cols: 2 },
  '3x3': { rows: 3, cols: 3 },
}

export async function tabRequestError(response: Response, fallback: string): Promise<Error> {
  try {
    const detail = (await response.json())?.detail
    if (typeof detail === 'string' && detail) return new Error(`${fallback}: ${detail}`)
  } catch {
    // Non-JSON error bodies keep the generic message.
  }
  return new Error(fallback)
}

export function generatePaneId(): string {
  return 'pane-' + Math.random().toString(36).substring(2, 11)
}

export function statusesEqual(a: TerminalAgentStatus[], b: TerminalAgentStatus[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    const x = a[i]
    const y = b[i]
    if (
      x.tab_id !== y.tab_id ||
      x.tab_name !== y.tab_name ||
      x.agent_type !== y.agent_type ||
      x.status !== y.status ||
      x.status_text !== y.status_text ||
      x.detail !== y.detail ||
      x.tmux_session !== y.tmux_session ||
      x.last_changed_at !== y.last_changed_at ||
      x.sampled_at !== y.sampled_at
    ) {
      return false
    }
  }
  return true
}
