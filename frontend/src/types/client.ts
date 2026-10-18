export type LayoutType = '1x1' | '2x1' | '1x2' | '3x1' | '1x3' | '2x2' | '3x3'

export interface Pane {
  id: string
  tabId: string | null
  isActive: boolean
}

export interface LayoutConfig {
  type: LayoutType
  rows: number
  cols: number
}

export interface User {
  open_id: string
  name: string
  email: string
  avatar_url?: string
}

export interface AuthCheckResponse {
  authenticated: boolean
  auth_required: boolean
  user: User | null
}

// Namespaced globals hung off window.__claudeHub to avoid top-level pollution.
// See: F8 cleanup — consolidate stray window globals.
export interface TerminalKeyItem {
  key: string
  ctrl: boolean
  shift: boolean
}

export interface TerminalKeyState {
  iframes: Record<string, HTMLIFrameElement | null>
  ready: Record<string, boolean>
  queues: Record<string, TerminalKeyItem[]>
  inputRing: Record<string, unknown>
}

/** Returns false only when no terminal target is available to accept/queue the key. */
export type TerminalKeySender = (key: string, ctrl?: boolean, shift?: boolean) => boolean
/** Sends one ordered text frame to a specific terminal. */
export type TerminalTextSender = (text: string, tabId?: string) => boolean
export type TerminalHistoryRefresher = (tabId?: string) => void
export type TerminalIframeRegistrar = (el: HTMLIFrameElement | null, tabId: string) => void
export type TerminalSelectModeSetter = (enabled: boolean, tabId?: string) => void

export interface ClaudeHubNamespace {
  activePaneTabId?: string | null
  terminalState?: TerminalKeyState
  registerTerminalIframe?: TerminalIframeRegistrar
  refreshTerminalHistory?: TerminalHistoryRefresher
  sendTerminalKey?: TerminalKeySender
  sendTerminalText?: TerminalTextSender
  setTerminalSelectMode?: TerminalSelectModeSetter
}

declare global {
  interface Window {
    __claudeHub: ClaudeHubNamespace
  }
}

// Cross-store notification (toast) shape — replaces single mutable `error: string`
// anti-pattern so concurrent failures stack instead of overwrite each other.
export type NotificationType = 'error' | 'success' | 'warning' | 'info'

export interface StoreNotification {
  id: string
  type: NotificationType
  message: string
  /** If set, the toast auto-dismisses after this many milliseconds. */
  autoDismissMs?: number
}
