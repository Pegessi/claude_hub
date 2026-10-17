import { inject, type InjectionKey, type Ref, type ComputedRef } from 'vue'
import type { TerminalTab, StreamCapabilities } from '@/types'
import type { useFeishuBotPoolStore } from '@/stores/feishuBotPoolStore'
import type { useAppStore } from '@/stores/appStore'
import type { ChatPaneProps } from './controllers/types'
import type { useChatGoal } from './controllers/useChatGoal'
import type { useChatTimeline } from './controllers/useChatTimeline'
import type { useChatActions } from './controllers/useChatActions'
import type { useChatDelivery } from './controllers/useChatDelivery'
import type { useChatQueue } from './controllers/useChatQueue'
import type { useChatAttachments } from './controllers/useChatAttachments'
import type { useChatImages } from './controllers/useChatImages'
import type { useChatSettings } from './controllers/useChatSettings'
import type { useChatComposer } from './controllers/useChatComposer'

export interface ChatIdentity {
  connectionState: Ref<'idle' | 'hydrating' | 'live' | 'reconciling' | 'failed'>
  capabilities: Ref<StreamCapabilities | null>
  props: ChatPaneProps
  isManagedChat: ComputedRef<boolean>
  managedIdentityMissing: ComputedRef<boolean>
  markdownImageScope: ComputedRef<string>
  supportsEditResend: ComputedRef<boolean>
  isHistoryVisible: ComputedRef<boolean>
  currentTab: ComputedRef<TerminalTab | null>
  feishuBotPoolStore: ReturnType<typeof useFeishuBotPoolStore>
  appStore: ReturnType<typeof useAppStore>
  isPaneActivated: Ref<boolean>
}

export const chatIdentityKey: InjectionKey<ChatIdentity> = Symbol('chatIdentity')
export const chatGoalKey: InjectionKey<ReturnType<typeof useChatGoal>> = Symbol('chatGoal')
export const chatTimelineKey: InjectionKey<ReturnType<typeof useChatTimeline>> = Symbol('chatTimeline')
export const chatActionsKey: InjectionKey<ReturnType<typeof useChatActions>> = Symbol('chatActions')
export const chatDeliveryKey: InjectionKey<ReturnType<typeof useChatDelivery>> = Symbol('chatDelivery')
export const chatQueueKey: InjectionKey<ReturnType<typeof useChatQueue>> = Symbol('chatQueue')
export const chatAttachmentsKey: InjectionKey<ReturnType<typeof useChatAttachments>> = Symbol('chatAttachments')
export const chatImagesKey: InjectionKey<ReturnType<typeof useChatImages>> = Symbol('chatImages')
export const chatSettingsKey: InjectionKey<ReturnType<typeof useChatSettings>> = Symbol('chatSettings')
export const chatComposerKey: InjectionKey<ReturnType<typeof useChatComposer>> = Symbol('chatComposer')

export function requireChatOwner<T>(key: InjectionKey<T>): T {
  const owner = inject(key)
  if (!owner) throw new Error('Chat component must be mounted inside its pane owner')
  return owner
}
