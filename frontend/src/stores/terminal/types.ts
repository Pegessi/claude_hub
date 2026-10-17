import type { WorkspaceAttachmentCreate } from '@/types'

export interface QueuedChatDraftAttachment extends WorkspaceAttachmentCreate {
  id: string
  preview_url: string
  size_bytes: number
}

export interface QueuedChatDraft {
  id: string
  error?: string
  clientTurnId?: string
  message: string
  attachments: QueuedChatDraftAttachment[]
}

export interface QueuedChatState {
  paneId: string
  tabId: string
  drafts: QueuedChatDraft[]
  paused: boolean
  editing: boolean
  editingDraftId: string | null
  editingMessage: string
  sendInProgress: boolean
  autoActivationBlock: 'stream-failed' | 'goal-locked' | null
}

export interface QueuedChatActivationRequest {
  id: number
  paneId: string
  tabId: string
}
