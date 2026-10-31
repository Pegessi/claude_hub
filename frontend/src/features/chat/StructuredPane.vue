<template>
  <div
    class="structured-pane"
    :class="{ 'is-dragging': isDragOver }"
    @dragover.prevent="handleDragOver"
    @dragleave="handleDragLeave"
    @drop="handleDrop"
  >
    <!-- Chat sessions fail closed on this surface. A stream failure never
         mounts a hidden raw terminal; users can retry or create a Terminal. -->
    <div
      v-if="timelineLoadingMessage || connectionState === 'failed'"
      class="structured-banner"
      :role="connectionState === 'failed' ? 'alert' : 'status'"
      :aria-live="connectionState === 'failed' ? 'assertive' : 'polite'"
    >
      <template v-if="timelineLoadingMessage">
        <span
          class="banner-spinner"
          aria-hidden="true"
        />
        <span>{{ timelineLoadingMessage }}</span>
        <button
          v-if="stopArmed"
          type="button"
          class="banner-stop"
          :disabled="isCancelling"
          title="Stop the current turn even while the conversation is reconnecting"
          @click="cancelActiveTurn"
        >
          {{ isCancelling ? 'Stopping…' : 'Stop turn' }}
        </button>
      </template>
      <template v-else-if="connectionState === 'failed'">
        <span
          class="banner-icon"
          aria-hidden="true"
        >⚠</span>
        <div
          class="banner-message"
          role="region"
          aria-label="Stream error details"
          tabindex="0"
        >
          {{ errorMessage || 'Structured view unavailable.' }}
        </div>
        <button
          v-if="stopArmed"
          type="button"
          class="banner-stop"
          :disabled="isCancelling"
          title="Stop the wedged turn and unlock the composer without waiting for reconnect"
          @click="cancelActiveTurn"
        >
          {{ isCancelling ? 'Stopping…' : 'Stop turn' }}
        </button>
        <button
          type="button"
          class="banner-retry"
          @click="retry"
        >
          Retry
        </button>
        <span class="banner-guidance">{{ managedIdentityMissing
          ? 'Return to Workspace and refresh managed sessions.'
          : currentTab?.agent_type === 'maso'
            ? 'Start the MASO HTTP service, then retry.'
            : currentTab?.agent_type === 'antigravity'
              ? 'Check the local agyx network and sign-in, then retry. Retry does not resend your message.'
              : 'Create a Terminal session for native TUI access.' }}</span>
      </template>
    </div>

    <!-- Timeline -->
    <ChatTimeline />

    <button
      v-if="!isFollowingLatest"
      type="button"
      class="structured-jump-latest"
      aria-label="Scroll to latest message"
      @click="jumpToLatest"
    >
      <span aria-hidden="true">↓</span>
      Latest
    </button>


    <GoalStatusBar
      v-if="goal && !isManagedChat"
      :goal="goal"
      :busy="isGoalMutating"
      :error="goalError"
      @pause="pauseGoal"
      @resume="resumeGoal"
      @complete="completeGoal"
      @clear="clearGoal"
      @edit="isGoalEditing = true; isGoalSetupOpen = true"
      @refresh="hydrateGoal"
    />

    <ChatMessageInbox
      v-if="!isManagedChat && currentTab && !currentTab.archived && ['claude', 'codex', 'cursor', 'maso', 'antigravity'].includes(currentTab.agent_type ?? '')"
      :tab-id="props.tabId"
    />

    <!-- Composer -->
    <ChatComposer />

    <ChatGoalSetup
      v-if="!isManagedChat"
    />

    <AttachmentPreviewDialog
      :target="preview.target.value"
      :can-go-back="preview.canGoBack.value"
      @close="preview.close()"
      @back="preview.back"
    />
  </div>
</template>

<script setup lang="ts">
import { provide } from 'vue'
import { chatIdentityKey, chatGoalKey, chatTimelineKey, chatActionsKey, chatDeliveryKey, chatQueueKey, chatAttachmentsKey, chatImagesKey, chatSettingsKey, chatComposerKey } from './owners'
import ChatTimeline from './components/ChatTimeline.vue'
import ChatMessageInbox from './components/ChatMessageInbox.vue'
import ChatComposer from './components/ChatComposer.vue'
import ChatGoalSetup from './components/ChatGoalSetup.vue'
import AttachmentPreviewDialog from '@/components/AttachmentPreviewDialog.vue'
import { provideAttachmentPreview } from '@/composables/useAttachmentPreview'
import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { useAgentStream } from '@/composables/useAgentStream'
import { streamCapabilityConfiguration } from '@/utils/agentStreamCapabilitiesCache'
import { useTerminalStore } from '@/stores/terminalStore'
import { useAppStore } from '@/stores/appStore'
import { useFeishuBotPoolStore } from '@/stores/feishuBotPoolStore'
import GoalStatusBar from '@/components/GoalStatusBar.vue'
import type { ChatPaneProps } from '@/features/chat/controllers/types'
import { useChatGoal } from '@/features/chat/controllers/useChatGoal'
import { useChatAttachments } from '@/features/chat/controllers/useChatAttachments'
import { useChatDelivery } from '@/features/chat/controllers/useChatDelivery'
import { useChatSettings } from '@/features/chat/controllers/useChatSettings'
import { useChatComposer } from '@/features/chat/controllers/useChatComposer'
import { useChatTimeline } from '@/features/chat/controllers/useChatTimeline'
import { useChatActions } from '@/features/chat/controllers/useChatActions'
import { useChatQueue } from '@/features/chat/controllers/useChatQueue'
import { useChatImages } from '@/features/chat/controllers/useChatImages'
const props = defineProps<ChatPaneProps>()
const preview = provideAttachmentPreview()
const isManagedChat = computed(() => Boolean(props.managedChat))
const managedIdentityMissing = computed(() => isManagedChat.value && !props.managedSessionId)
const streamSource = computed(() => isManagedChat.value ? 'managed-session' as const : 'terminal-tab' as const)
const streamSourceId = computed(() => isManagedChat.value ? props.managedSessionId ?? '' : props.tabId)
const streamBasePath = computed(() => isManagedChat.value
  ? `/api/workspaces/sessions/${encodeURIComponent(streamSourceId.value)}/stream`
  : `/api/workspaces/tabs/${encodeURIComponent(streamSourceId.value)}/stream`)
const markdownImageScope = computed(() => isManagedChat.value ? streamBasePath.value : props.tabId)
const terminalStore = useTerminalStore()
const appStore = useAppStore()
const feishuBotPoolStore = useFeishuBotPoolStore()
const currentTab = computed(() =>
  terminalStore.tabs.find(t => t.id === props.tabId) ?? null,
)
const capabilityConfiguration = computed(() => streamCapabilityConfiguration(currentTab.value))
const {
  events,
  historyDetails,
  historyDetailRevision,
  connectionState,
  errorMessage,
  capabilities,
  start,
  loadHistoryDetail,
  retry: retryStream,
  refreshCapabilities,
  setMode,
  stop,
  nudge,
  reset: resetStream,
  historyReady,
} = useAgentStream(() => capabilityConfiguration.value)
const isHistoryVisible = computed(() => historyReady.value)
function startStream() {
  if (managedIdentityMissing.value) {
    errorMessage.value = 'Managed Chat identity is not available yet. Refresh Workspace sessions and try again.'
    connectionState.value = 'failed'
    return
  }
  void start(streamSourceId.value, streamSource.value)
}
const supportsEditResend = computed(() =>
  !isManagedChat.value && ['claude', 'codex', 'cursor'].includes(currentTab.value?.agent_type ?? '')
)
const isPaneActivated = ref(false)
function retry() {
  if (managedIdentityMissing.value) {
    startStream()
    return
  }
  void retryStream(streamSourceId.value, streamSource.value)
}
function suspendStreamForDeactivation() {
  stop()
}
// Lazy, typed dependencies connect owners without duplicating their refs.
// Construct every owner before subscribing so immediate watchers see the full graph.
const goalController = useChatGoal({
  get attachments() { return attachmentsController.attachments },
  get capabilities() { return capabilities },
  get connectionState() { return connectionState },
  get currentModeId() { return settings.currentModeId },
  get events() { return events },
  get isManagedChat() { return isManagedChat },
  get isUpdatingMode() { return settings.isUpdatingMode },
  get isUpdatingModel() { return settings.isUpdatingModel },
  get isUpdatingReasoningEffort() { return settings.isUpdatingReasoningEffort },
  get modeInteractionLocked() { return delivery.modeInteractionLocked },
  get props() { return props },
  get terminalStore() { return terminalStore },
  get timelineDisposed() { return timeline.timelineDisposed },
})
const attachmentsController = useChatAttachments({
  get capabilities() { return capabilities },
  get composerError() { return delivery.composerError },
  get goalComposerReason() { return goalController.goalComposerReason },
  get goalInputBlocked() { return goalController.goalInputBlocked },
  get isGoalMutating() { return goalController.isGoalMutating },
  get isSending() { return delivery.isSending },
})
const delivery = useChatDelivery({
  get attachments() { return attachmentsController.attachments },
  get authoritativeTurns() { return timeline.authoritativeTurns },
  get connectionState() { return connectionState },
  get draftQueue() { return queue.draftQueue },
  get enqueueDraft() { return queue.enqueueDraft },
  get events() { return events },
  get goal() { return goalController.goal },
  get goalComposerReason() { return goalController.goalComposerReason },
  get goalError() { return goalController.goalError },
  get goalImageRecovery() { return goalController.goalImageRecovery },
  get goalInputBlocked() { return goalController.goalInputBlocked },
  get goalSteerOnly() { return goalController.goalSteerOnly },
  get hydrateGoal() { return goalController.hydrateGoal },
  get isDraftQueuePaused() { return queue.isDraftQueuePaused },
  get isGoalHydrating() { return goalController.isGoalHydrating },
  get isGoalMutating() { return goalController.isGoalMutating },
  get isManagedChat() { return isManagedChat },
  get isPreparingAttachments() { return attachmentsController.isPreparingAttachments },
  get isUpdatingMode() { return settings.isUpdatingMode },
  get isUpdatingModel() { return settings.isUpdatingModel },
  get isUpdatingReasoningEffort() { return settings.isUpdatingReasoningEffort },
  get nudge() { return nudge },
  get requestLatestAnchor() { return timeline.requestLatestAnchor },
  get streamBasePath() { return streamBasePath },
  get syncComposerTextareaHeight() { return composer.syncComposerTextareaHeight },
  get terminalStore() { return terminalStore },
})
const settings = useChatSettings({
  get capabilities() { return capabilities },
  get currentTab() { return currentTab },
  get goalPlanReason() { return goalController.goalPlanReason },
  get modeInteractionLocked() { return delivery.modeInteractionLocked },
  get preparationEpoch() { return attachmentsController.preparationEpoch },
  get refreshCapabilities() { return refreshCapabilities },
  get setMode() { return setMode },
  get terminalStore() { return terminalStore },
})
const composer = useChatComposer({
  get attachments() { return attachmentsController.attachments },
  get closeModeMenu() { return settings.closeModeMenu },
  get closeModelMenu() { return settings.closeModelMenu },
  get composerError() { return delivery.composerError },
  get draftMessage() { return delivery.draftMessage },
  get draftQueue() { return queue.draftQueue },
  get enqueueDraft() { return queue.enqueueDraft },
  get goalComposerReason() { return goalController.goalComposerReason },
  get goalInputBlocked() { return goalController.goalInputBlocked },
  get goalSteerOnly() { return goalController.goalSteerOnly },
  get isManagedChat() { return isManagedChat },
  get isModeMenuOpen() { return settings.isModeMenuOpen },
  get isModelMenuOpen() { return settings.isModelMenuOpen },
  get isPaneActivated() { return isPaneActivated },
  get isSending() { return delivery.isSending },
  get sendQueuedDraft() { return queue.sendQueuedDraft },
  get submit() { return delivery.submit },
  get turnInFlight() { return delivery.turnInFlight },
  get composerTurnInFlight() { return delivery.composerTurnInFlight },
})
const timeline = useChatTimeline({
  get historyReady() { return historyReady },
  get connectionState() { return connectionState },
  get events() { return events },
  get historyDetails() { return historyDetails },
  get historyDetailRevision() { return historyDetailRevision },
  get loadHistoryDetail() { return loadHistoryDetail },
  get isApprovalResolved() { return composer.isApprovalResolved },
  get isEditingTurn() { return actions.isEditingTurn },
  get optimisticCancelledTurnId() { return delivery.optimisticCancelledTurnId },
  get pendingTurns() { return delivery.pendingTurns },
  get props() { return props },
})
const actions = useChatActions({
  get composerError() { return delivery.composerError },
  get currentModeId() { return settings.currentModeId },
  get feishuBotPoolStore() { return feishuBotPoolStore },
  get goalComposerLocked() { return goalController.goalComposerLocked },
  get goalComposerReason() { return goalController.goalComposerReason },
  get goalEditReason() { return goalController.goalEditReason },
  get isManagedChat() { return isManagedChat },
  get modeChangeError() { return settings.modeChangeError },
  get props() { return props },
  get resetStream() { return resetStream },
  get setMode() { return setMode },
  get start() { return start },
  get streamBasePath() { return streamBasePath },
  get streamSource() { return streamSource },
  get streamSourceId() { return streamSourceId },
  get submit() { return delivery.submit },
  get terminalStore() { return terminalStore },
  get turnInFlight() { return delivery.turnInFlight },
  get turns() { return timeline.turns },
})
const queue = useChatQueue({
  get attachments() { return attachmentsController.attachments },
  get authoritativeTurns() { return timeline.authoritativeTurns },
  get canSend() { return delivery.canSend },
  get composerError() { return delivery.composerError },
  get composerTextareaEl() { return composer.composerTextareaEl },
  get connectionState() { return connectionState },
  get draftMessage() { return delivery.draftMessage },
  get goalComposerLocked() { return goalController.goalComposerLocked },
  get goalComposerReason() { return goalController.goalComposerReason },
  get goalInputBlocked() { return goalController.goalInputBlocked },
  get goalSteerOnly() { return goalController.goalSteerOnly },
  get isPaneActivated() { return isPaneActivated },
  get isPreparingAttachments() { return attachmentsController.isPreparingAttachments },
  get isSending() { return delivery.isSending },
  get isSubmissionReady() { return delivery.isSubmissionReady },
  get props() { return props },
  get retrySendAttempt() { return delivery.retrySendAttempt },
  get submit() { return delivery.submit },
  get syncComposerTextareaHeight() { return composer.syncComposerTextareaHeight },
  get terminalStore() { return terminalStore },
  get composerTurnInFlight() { return delivery.composerTurnInFlight },
})
const images = useChatImages({
  openPreview: preview.open,
  get streamBasePath() { return streamBasePath },
})
// The pane owns KeepAlive ordering; leaf components only borrow owner refs.
onMounted(() => {
  composer.handleViewportResize()
  window.addEventListener('resize', composer.handleViewportResize)
  document.addEventListener('keydown', composer.handleDocumentKeydown)
  document.addEventListener('pointerdown', settings.handleModeOutsidePointer)
  document.addEventListener('pointerdown', settings.handleModelOutsidePointer)
})
onActivated(() => {
  isPaneActivated.value = true
  timeline.activateViewport()
  startStream()
  if (!isManagedChat.value) void goalController.hydrateGoal()
  void nextTick(() => {
    if (timeline.timelineDisposed) return
    timeline.observeTimelineGeometry()
  })
})
onDeactivated(() => {
  isPaneActivated.value = false
  preview.close(false)
  settings.isAddMenuOpen.value = false
  goalController.isGoalSetupOpen.value = false
  goalController.cancelGoalRefresh()
  timeline.deactivateViewport()
  suspendStreamForDeactivation()
  actions.clearCopyFeedback()
})
onUnmounted(() => {
  // A minimal QueuedChatObserver may still own a ready FIFO request after this
  // full pane is evicted from the bounded KeepAlive cache. Do not release that
  // store-owned request here: send completion, an explicit failure yield, or
  // archive/delete owns its retirement.
  terminalStore.pruneQueuedChatState(props.paneId, props.tabId)
  goalController.cancelGoalRefresh()
  // Bump the epoch on unmount so any in-flight preparation batch aborts
  // instead of mutating state after the component is gone.
  attachmentsController.preparationEpoch.value++
  document.removeEventListener('keydown', composer.handleDocumentKeydown)
  document.removeEventListener('pointerdown', settings.handleModeOutsidePointer)
  document.removeEventListener('pointerdown', settings.handleModelOutsidePointer)
  window.removeEventListener('resize', composer.handleViewportResize)
  preview.close(false)
  actions.clearCopyFeedback()
  stop()
})
// Preserve the original subscription order after all cyclic dependencies exist.
settings.registerAvailabilityWatcher()
delivery.registerPendingWatcher()
goalController.registerWatchers()
settings.registerMenuWatchers()
composer.registerWatchers()
queue.registerReconciliationWatcher()
delivery.registerGoalRecoveryWatcher()
watch(capabilityConfiguration, () => {
  if (isPaneActivated.value) startStream()
})
queue.registerAutoFlushWatcher()
delivery.registerTerminalWatcher()
timeline.registerWatchers()
const { goal, goalError, isGoalMutating, hydrateGoal, pauseGoal, resumeGoal, completeGoal, clearGoal, isGoalSetupOpen, isGoalEditing } = goalController
const { isFollowingLatest, timelineLoadingMessage, jumpToLatest } = timeline
const { isCancelling, stopArmed, cancelActiveTurn } = delivery
const { isDragOver, handleDragOver, handleDragLeave, handleDrop } = attachmentsController
provide(chatIdentityKey, { connectionState, capabilities, props, isManagedChat, managedIdentityMissing, markdownImageScope, supportsEditResend, isHistoryVisible, currentTab, feishuBotPoolStore, appStore, isPaneActivated })
provide(chatGoalKey, goalController)
provide(chatTimelineKey, timeline)
provide(chatActionsKey, actions)
provide(chatDeliveryKey, delivery)
provide(chatQueueKey, queue)
provide(chatAttachmentsKey, attachmentsController)
provide(chatImagesKey, images)
provide(chatSettingsKey, settings)
provide(chatComposerKey, composer)
</script>

<style scoped>
.structured-pane {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background-color: var(--ch-color-main-bg);
  color: var(--ch-color-text);
  font-size: 13px;
  line-height: 1.5;
}

.structured-banner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background-color: var(--ch-color-surface);
  border-bottom: 1px solid var(--ch-color-border-muted);
  font-size: 12px;
  color: var(--ch-color-text-muted);
  flex-shrink: 0;
}

.structured-banner[role='alert'] {
  /* Keep diagnostics below the floating session name and binding control. */
  padding-top: 38px;
  align-items: flex-start;
}

.banner-message {
  flex: 1 1 240px;
  min-width: 0;
  max-height: min(160px, 25vh);
  overflow: auto;
  overflow-wrap: anywhere;
  overscroll-behavior: contain;
  white-space: pre-wrap;
}

.banner-message:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 2px;
}

.banner-guidance {
  margin-left: auto;
  overflow-wrap: anywhere;
  color: var(--ch-color-text-subtle);
}

.banner-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--ch-color-border);
  border-top-color: var(--ch-color-accent);
  border-radius: 50%;
  animation: structured-spin 0.8s linear infinite;
}

@keyframes structured-spin {
  to {
    transform: rotate(360deg);
  }
}

.banner-icon {
  color: var(--ch-color-warning, #e0a800);
}

.banner-retry {
  flex-shrink: 0;
  margin-left: auto;
  padding: 2px 10px;
  font-size: 12px;
  color: var(--ch-color-accent);
  background: transparent;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  cursor: pointer;
}

.banner-retry:hover {
  background-color: var(--ch-color-surface-control-hover);
}
.banner-stop {
  padding: 2px 10px;
  font-size: 12px;
  font-weight: var(--ch-weight-medium, 500);
  color: var(--ch-color-danger, #d04848);
  background: transparent;
  border: 1px solid var(--ch-color-danger, #d04848);
  border-radius: var(--ch-radius-sm);
  cursor: pointer;
}

.banner-stop:hover:not(:disabled) {
  background-color: var(--ch-color-danger, #d04848);
  color: #fff;
}

.banner-stop:disabled {
  opacity: 0.6;
  cursor: default;
}

.structured-jump-latest {
  position: absolute;
  right: 28px;
  bottom: 92px;
  z-index: 3;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border: 1px solid var(--ch-color-border);
  border-radius: 999px;
  color: var(--ch-color-text);
  background: var(--ch-color-surface-elevated, var(--ch-color-surface));
  box-shadow: var(--ch-shadow-md, 0 8px 24px rgb(0 0 0 / 24%));
  cursor: pointer;
}

.structured-jump-latest:hover {
  border-color: var(--ch-color-accent);
}
.structured-pane {
  background:
    radial-gradient(circle at 50% -22%, var(--ch-color-surface-soft), transparent 44%),
    var(--ch-color-main-bg);
}

@media (prefers-reduced-motion: reduce) {

  .banner-spinner {
    animation: none;
  }
}

.structured-pane.is-dragging :deep(.structured-timeline) {
  outline: 2px dashed var(--ch-color-accent);
  outline-offset: -4px;
}

</style>
