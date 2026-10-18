import type { QueuedChatDraftAttachment } from '@/stores/terminalStore'

export interface ChatPaneProps {
  tabId: string
  paneId: string
  managedChat?: boolean
  managedSessionId?: string
}

export type PendingTurn = {
  key: string
  turnId: string
  userText: string
  attachments: { preview_url: string; mime_type: string }[]
}

export type QuestionIdentity = { turnId: string; callId: string }

export type RetriableSend = {
  clientTurnId: string
  message: string
  attachments: DraftAttachment[]
  questionIdentity: QuestionIdentity | null
  goalRecovery?: boolean
}

export type DraftAttachment = QueuedChatDraftAttachment
