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
      </template>
      <template v-else-if="connectionState === 'failed'">
        <span
          class="banner-icon"
          aria-hidden="true"
        >⚠</span>
        <span>{{ errorMessage || 'Structured view unavailable.' }}</span>
        <button
          type="button"
          class="banner-retry"
          @click="retry"
        >
          Retry
        </button>
        <span class="banner-guidance">Create a Terminal session for native TUI access.</span>
      </template>
    </div>

    <!-- Timeline -->
    <div
      ref="timelineEl"
      class="structured-timeline"
      :class="{ 'is-timeline-hidden': timelinePhase !== 'revealed' }"
      role="log"
      aria-live="polite"
      aria-label="Chat conversation"
      :aria-busy="Boolean(timelineLoadingMessage)"
      @scroll.passive="handleTimelineScroll"
    >
      <div
        ref="timelineContentEl"
        class="structured-timeline-content"
      >
        <button
          v-if="historyWindowStart > 0"
          type="button"
          class="structured-load-earlier"
          :disabled="isLoadingEarlier"
          @click="loadEarlierTurns"
        >
          {{ isLoadingEarlier ? 'Loading earlier messages…' : 'Load earlier messages' }}
        </button>
        <div
          v-if="turns.length === 0 && pendingDirectTurns.length === 0 && isHistoryVisible"
          class="structured-empty"
        >
          <span
            class="empty-orbit"
            aria-hidden="true"
          >✦</span>
          <strong>Ready when you are</strong>
          <p>Send a message below to start this chat.</p>
        </div>

        <div
          v-for="{ turn, ordinal } in visibleTurns"
          :key="turn.key"
          v-memo="[turn.renderRevision, ordinal, erroredAttachments.size, turnApprovalSignature(turn), turnFoldSignature(turn), forkingOrdinal === ordinal, implementingPlanKey === turn.key, isEditingTurn(turn), isEditingTurn(turn) ? editError : null, isEditingTurn(turn) ? isEditSending : false]"
          class="structured-turn"
          :data-turn-key="turn.key"
        >
          <!-- A right-aligned user bubble and a left-aligned assistant bubble make
               this the same conversation as the terminal, not terminal text
               pasted into a second surface. -->
          <div
            v-if="turn.userText || turn.attachments?.length"
            class="conversation-row conversation-row--user"
          >
            <div class="conversation-bubble conversation-bubble--user">
              <!-- Inline edit mode -->
              <div
                v-if="isEditingTurn(turn)"
                class="edit-resend-form"
              >
                <textarea
                  v-model="editDraft"
                  class="edit-resend-textarea"
                  rows="3"
                  placeholder="Edit your message..."
                  @keydown.enter.exact.prevent="submitEdit(turn)"
                  @keydown.esc.prevent="cancelEdit"
                />
                <!-- Preserved attachments: edit-resend re-sends the original
                     images, so show them as read-only thumbnails so the user
                     knows they are kept (not dropped) on resend. -->
                <div
                  v-if="turn.attachments?.length"
                  class="turn-attachments edit-resend-attachments"
                >
                  <template
                    v-for="(att, i) in turn.attachments"
                    :key="att.id ?? `edit-null-${turn.key}-${i}`"
                  >
                    <img
                      v-if="att.id !== null && !erroredAttachments.has(att.id)"
                      :src="attachmentUrl(att.id)"
                      class="turn-attachment-img"
                      alt="attached image (will be re-sent)"
                      @error="onAttachmentError($event, att)"
                    >
                    <div
                      v-else
                      class="turn-attachment-placeholder"
                    >
                      <span>{{ att.id === null ? 'Preview unavailable' : 'Preview expired' }}</span>
                    </div>
                  </template>
                </div>
                <div
                  v-if="editError"
                  class="edit-resend-error"
                >
                  {{ editError }}
                </div>
                <div class="edit-resend-actions">
                  <button
                    type="button"
                    class="edit-resend-btn edit-resend-btn--primary"
                    :disabled="isEditSending"
                    @click="submitEdit(turn)"
                  >
                    {{ isEditSending ? 'Sending...' : 'Resend' }}
                  </button>
                  <button
                    type="button"
                    class="edit-resend-btn"
                    :disabled="isEditSending"
                    @click="cancelEdit"
                  >
                    Cancel
                  </button>
                </div>
              </div>
              <!-- Normal display mode -->
              <template v-else>
                <MarkdownContent
                  v-if="turn.userText"
                  :text="turn.userText"
                  compact
                />
                <div
                  v-if="turn.attachments?.length"
                  class="turn-attachments"
                >
                  <template
                    v-for="(att, i) in turn.attachments"
                    :key="att.id ?? `null-${turn.key}-${i}`"
                  >
                    <!-- Keep conversation density high: render a bounded
                         thumbnail and open the full preview in a lightbox. -->
                    <button
                      v-if="att.id !== null && !erroredAttachments.has(att.id)"
                      type="button"
                      class="turn-attachment-button"
                      aria-label="Open attached image preview"
                      @click="openImageLightbox(attachmentUrl(att.id), 'attached image', $event)"
                    >
                      <img
                        :src="attachmentUrl(att.id)"
                        class="turn-attachment-img"
                        alt="attached image"
                        @error="onAttachmentError($event, att)"
                      >
                    </button>
                    <!-- Placeholder for no-preview (id is null) or evicted
                         preview (fetch returned 404/410). -->
                    <div
                      v-else
                      class="turn-attachment-placeholder"
                    >
                      <span>{{ att.id === null ? 'Preview unavailable' : 'Preview expired' }}</span>
                    </div>
                  </template>
                </div>
              </template>
            </div>
          </div>

          <!-- The message's own actions: a row under the bubble that the turn's
               hover reveals, with room for both the buttons and the time.
               Normal flow, not pinned over the turn — absolutely positioned it
               sat behind the bubble (which is itself positioned) and swallowed
               clicks. -->
          <div
            v-if="turn.userText && !isEditingTurn(turn)"
            class="turn-actions turn-actions--message"
          >
            <button
              v-if="turn.turnId && supportsEditResend"
              type="button"
              class="edit-resend-hover-btn"
              :disabled="turnInFlight || Boolean(goalEditReason)"
              :title="turnInFlight ? 'A turn is currently running' : (goalEditReason || 'Edit message')"
              :aria-label="turnInFlight ? 'Edit message (unavailable while a turn is running)' : (goalEditReason ? `Edit message (${goalEditReason})` : 'Edit message')"
              @click="startEdit(turn)"
            >
              ✎ 编辑
            </button>
            <time
              v-if="messageClockLabel(turn)"
              class="turn-time"
            >{{ messageClockLabel(turn) }}</time>
          </div>

          <div
            v-if="turn.awaitingAgentActivity"
            class="conversation-row conversation-row--assistant"
            role="status"
            aria-live="polite"
          >
            <span
              class="conversation-avatar conversation-avatar--waiting"
              aria-hidden="true"
            >✦</span>
            <div class="agent-waiting-card">
              <span
                class="agent-waiting-pulse"
                aria-hidden="true"
              >
                <i />
                <i />
                <i />
              </span>
              <span>Waiting for response…</span>
            </div>
          </div>

          <!-- Ordered parts: thinking, text, tool, error, and status render in
               the exact order the provider emitted them. Paseo does not defer
               protocol errors to the turn end, so neither do we. -->
          <template
            v-for="part in turnPartsFor(turn)"
            :key="part.key"
          >
            <details
              v-if="part.kind === 'thinking'"
              class="thinking-card"
            >
              <summary>
                <span
                  class="thinking-indicator"
                  aria-hidden="true"
                />
                Thinking
              </summary>
              <!-- Thinking is rendered as plain text (no marked/DOMPurify) to
                   avoid re-parsing multi-kilobyte reasoning streams on every
                   delta. Whitespace is preserved with pre-wrap. -->
              <pre class="thinking-body">{{ part.text }}</pre>
              <button
                type="button"
                class="details-collapse"
                @click="collapseDetails"
              >
                收起
              </button>
            </details>

            <div
              v-else-if="part.kind === 'text'"
              class="conversation-row conversation-row--assistant"
            >
              <span
                class="conversation-avatar"
                aria-hidden="true"
              >✦</span>
              <div class="conversation-bubble conversation-bubble--assistant">
                <MarkdownContent
                  :text="part.text"
                  compact
                  :complete="turn.completed"
                />
              </div>
            </div>

            <div
              v-else-if="part.kind === 'tool_group'"
              class="conversation-row conversation-row--assistant"
            >
              <span
                class="conversation-avatar conversation-avatar--tool"
                aria-hidden="true"
              >⌘</span>
              <details class="tool-card tool-card--group">
                <summary
                  class="tool-header"
                  :class="{ 'tool-header--single': part.tools.length === 1 }"
                >
                  <span class="tool-name">
                    {{ part.tools.length === 1 ? part.tools[0].name : `${part.tools.length} tools` }}
                  </span>
                  <template v-if="part.tools.length > 1">
                    <span class="tool-group-names">
                      {{ part.tools.map(t => t.name).join(', ') }}
                    </span>
                  </template>
                  <span
                    class="tool-status"
                    :class="toolGroupStatus(part.tools)"
                  >{{ toolGroupStatus(part.tools) }}</span>
                </summary>
                <div
                  v-for="tool in part.tools"
                  :key="tool.key"
                  class="tool-group-item"
                >
                  <div class="tool-group-item-header">
                    <span class="tool-name">{{ tool.name }}</span>
                    <span
                      class="tool-status"
                      :class="tool.status"
                    >{{ tool.status }}</span>
                  </div>
                  <div
                    v-if="tool.argsText"
                    class="tool-block"
                  >
                    <span>Input</span>
                    <pre>{{ tool.argsText }}</pre>
                  </div>
                  <div
                    v-if="tool.resultText"
                    class="tool-block"
                  >
                    <span>Result</span>
                    <pre>{{ tool.resultText }}</pre>
                  </div>
                </div>
                <button
                  type="button"
                  class="details-collapse"
                  @click="collapseDetails"
                >
                  收起
                </button>
              </details>
            </div>

            <div
              v-else-if="part.kind === 'approval'"
              class="conversation-row conversation-row--assistant"
            >
              <span
                class="conversation-avatar conversation-avatar--tool"
                aria-hidden="true"
              >?</span>
              <div
                class="approval-card"
                :class="{ 'approval-card--resolved': isApprovalResolved(part.approval) }"
              >
                <div class="approval-card-header">
                  <span class="approval-card-title">
                    {{ part.approval.title || '需要你的选择' }}
                  </span>
                  <span
                    v-if="isApprovalResolved(part.approval)"
                    class="approval-card-badge"
                  >已提交</span>
                </div>
                <div
                  v-for="question in part.approval.questions"
                  :key="question.id"
                  class="approval-question"
                >
                  <div class="approval-question-prompt">
                    {{ question.prompt }}
                  </div>
                  <div
                    class="approval-options"
                    role="group"
                    :aria-label="question.prompt"
                  >
                    <button
                      v-for="option in question.options"
                      :key="option.id"
                      type="button"
                      class="approval-option"
                      :class="{
                        'approval-option--selected': isQuestionOptionSelected(
                          part.approval.key,
                          question.id,
                          option.id,
                        ),
                      }"
                      :disabled="isApprovalResolved(part.approval) || isSending"
                      @click="toggleQuestionOption(
                        part.approval.key,
                        question.id,
                        option.id,
                        question.allowMultiple,
                      )"
                    >
                      <span>{{ option.label }}</span>
                      <small v-if="option.description">{{ option.description }}</small>
                    </button>
                  </div>
                  <!-- The listed options are the agent's guess at the answer,
                       not the whole space of them. What the user types here is
                       merged with the ticked options on submit, so it satisfies
                       the completion check, travels in the same payload, and is
                       what the agent reads. -->
                  <input
                    :type="question.isSecret ? 'password' : 'text'"
                    class="approval-custom-answer"
                    :value="customAnswer(part.approval.key, question)"
                    :disabled="isApprovalResolved(part.approval) || isSending"
                    :placeholder="question.options.length === 0 ? '输入你的回答' : question.allowMultiple ? '其他（可多选，自己输入）' : '其他（自己输入）'"
                    :aria-label="`${question.prompt} — 其他答案`"
                    @input="setCustomAnswer(
                      part.approval.key,
                      question,
                      ($event.target as HTMLInputElement).value,
                    )"
                  >
                </div>
                <button
                  v-if="!isApprovalResolved(part.approval)"
                  type="button"
                  class="approval-submit-btn"
                  :disabled="!canSubmitQuestion(part.approval) || isSending"
                  @click="submitQuestionResponse(part.approval)"
                >
                  提交选择
                </button>
              </div>
            </div>

            <div
              v-else-if="part.kind === 'error'"
              class="event-error"
              role="alert"
            >
              <span
                class="error-icon"
                aria-hidden="true"
              >⚠</span>
              <span>{{ part.message }}</span>
            </div>

            <div
              v-else-if="part.kind === 'status'"
              class="event-status"
            >
              <span>{{ part.text }}</span>
            </div>

            <!-- Header of a finished turn's working region. Synthetic: it
                 stands in for the thinking/tool parts the reducer produced, so
                 it is only ever seen for turns that are already history. It
                 keeps its place in both states — only the detail underneath
                 grows — so expanding never moves the control out from under the
                 pointer, and collapsing is one click wherever the reader has
                 scrolled to. -->
            <button
              v-else-if="part.kind === 'process'"
              type="button"
              class="process-fold"
              :class="{ 'process-fold--open': part.expanded }"
              :aria-expanded="part.expanded"
              @click="toggleTurnProcess(turn)"
            >
              <span
                class="process-fold-chevron"
                aria-hidden="true"
              >{{ part.expanded ? '▾' : '▸' }}</span>
              <span class="process-fold-meta">{{ part.meta }}</span>
            </button>
          </template>

          <!-- Turn-level actions close the turn the way the message actions
               open it, so a long turn's controls sit with its result rather
               than floating back at the top. -->
          <div class="turn-actions turn-actions--turn">
            <button
              v-if="completedPlanText(turn)"
              type="button"
              class="turn-fork-button turn-implement-button"
              :disabled="implementingPlanKey !== null || turnInFlight || goalComposerLocked"
              :title="goalComposerReason || 'Switch to Agent mode and ask the CLI to implement this plan'"
              @click="implementPlan(turn)"
            >
              {{ implementingPlanKey === turn.key ? 'Starting…' : 'Implement plan' }}
            </button>
            <button
              type="button"
              class="turn-fork-button"
              :disabled="forkingOrdinal !== null"
              title="Fork a new chat from this turn"
              @click="forkFromTurn(ordinal)"
            >
              {{ forkingOrdinal === ordinal ? 'Forking…' : 'Fork from here' }}
            </button>
            <time
              v-if="turnClockLabel(turn)"
              class="turn-time"
            >{{ turnClockLabel(turn) }}</time>
          </div>
        </div>

        <!-- Optimistic turns are reconciled by client_turn_id, never by text. -->
        <div
          v-for="turn in pendingTurns"
          :key="turn.key"
          class="structured-turn structured-turn--pending"
        >
          <div class="conversation-row conversation-row--user">
            <div class="conversation-bubble conversation-bubble--user">
              <MarkdownContent
                v-if="turn.userText"
                :text="turn.userText"
                compact
              />
              <div
                v-if="turn.attachments?.length"
                class="turn-attachments"
              >
                <button
                  v-for="(att, i) in turn.attachments"
                  :key="i"
                  type="button"
                  class="turn-attachment-button"
                  aria-label="Open attached image preview"
                  @click="openImageLightbox(att.preview_url, 'attached image', $event)"
                >
                  <img
                    :src="att.preview_url"
                    class="turn-attachment-img"
                    alt="attached image"
                  >
                </button>
              </div>
            </div>
          </div>
          <div class="event-status event-status--pending">
            <span>Waiting for model activity…</span>
          </div>
        </div>
      </div>
    </div>

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
      v-if="goal"
      :goal="goal"
      :busy="isGoalMutating"
      :error="goalError"
      @pause="pauseGoal"
      @resume="resumeGoal"
      @complete="completeGoal"
      @clear="clearGoal"
    />

    <!-- Composer -->
    <div class="structured-composer">
      <div class="composer-shell">
        <div
          v-if="modeChangeError"
          class="composer-mode-error"
          role="alert"
        >
          {{ modeChangeError }}
        </div>
        <div
          v-if="goalError && !goal && !isGoalSetupOpen"
          class="composer-mode-error"
          role="alert"
        >
          Goal: {{ goalError }}
          <button
            type="button"
            @click="hydrateGoal"
          >
            Retry
          </button>
        </div>

        <!-- Attachment previews -->
        <div
          v-if="attachments.length > 0"
          class="composer-attachments"
        >
          <div
            v-for="att in attachments"
            :key="att.id"
            class="attachment-chip"
          >
            <img
              :src="att.preview_url"
              :alt="att.filename"
              class="attachment-thumb"
            >
            <button
              type="button"
              class="attachment-remove"
              :aria-label="`Remove ${att.filename}`"
              @click="removeAttachment(att)"
            >
              ×
            </button>
          </div>
        </div>

        <!-- Validation error -->
        <div
          v-if="composerError"
          class="composer-error"
          role="alert"
        >
          {{ composerError }}
        </div>

        <div
          v-if="draftQueue.length > 0"
          class="composer-queue"
          aria-live="polite"
        >
          {{ draftQueue.length }} message{{ draftQueue.length === 1 ? '' : 's' }} queued
        </div>

        <div class="composer-row">
          <textarea
            ref="composerTextareaEl"
            v-model="draftMessage"
            class="composer-textarea"
            placeholder="Send a message…"
            rows="1"
            :disabled="isSending || connectionState !== 'live' || goalComposerLocked"
            :title="goalComposerReason || undefined"
            @compositionstart="isComposing = true"
            @compositionend="isComposing = false"
            @keydown.enter.exact="handleComposerEnter"
            @keydown.enter.meta.exact="handleComposerEnter"
            @keydown.enter.ctrl.exact="handleComposerEnter"
            @input="syncComposerTextareaHeight"
            @paste="handlePaste"
          />
          <div class="composer-tools">
            <ComposerAddMenu
              v-model:open="isAddMenuOpen"
              :show-goal="!goal && Boolean(capabilities?.supports_goals)"
              :attachment-disabled-reason="attachmentDisabledReason"
              :goal-disabled-reason="goalSetupDisabledReason"
              @attachment="triggerFilePicker"
              @goal="isGoalSetupOpen = true"
              @schedule="appStore.openScheduledTasks(props.tabId)"
            />
            <input
              ref="fileInputEl"
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp"
              multiple
              class="composer-file-input"
              @change="handleFilePick"
            >
            <div
              v-if="modeOptions.length > 0"
              ref="modePickerEl"
              class="composer-mode-picker"
            >
              <button
                ref="modeTriggerEl"
                type="button"
                class="composer-mode-trigger"
                aria-haspopup="menu"
                :aria-expanded="isModeMenuOpen"
                :aria-label="`Chat mode: ${currentModeLabel}`"
                :title="currentModeOption?.description || `${currentModeLabel} mode`"
                :disabled="modeInteractionLocked || isUpdatingMode"
                @click="toggleModeMenu"
              >
                <span class="composer-mode-trigger-label">{{ currentModeLabel }}</span>
                <span
                  class="composer-mode-chevron"
                  aria-hidden="true"
                >▴</span>
              </button>
              <div
                v-if="isModeMenuOpen"
                ref="modeMenuEl"
                class="composer-mode-menu"
                role="menu"
                aria-label="Chat mode"
              >
                <button
                  v-for="option in modeOptions"
                  :key="option.id"
                  type="button"
                  class="composer-mode-menu-item"
                  role="menuitemradio"
                  :aria-checked="currentModeId === option.id"
                  :disabled="option.id === 'plan' && Boolean(goalPlanReason)"
                  :title="option.id === 'plan' && goalPlanReason ? goalPlanReason : (option.description || `${option.label} mode`)"
                  @click="selectMode(option.id)"
                >
                  <span>{{ option.label }}</span>
                  <span
                    v-if="currentModeId === option.id"
                    class="composer-mode-check"
                    aria-hidden="true"
                  >✓</span>
                </button>
              </div>
            </div>
            <div
              v-if="isModelPickerAvailable"
              ref="modelPickerEl"
              class="composer-mode-picker"
            >
              <button
                ref="modelTriggerEl"
                type="button"
                class="composer-mode-trigger"
                aria-haspopup="dialog"
                :aria-expanded="isModelMenuOpen"
                :aria-label="`Model and thinking effort: ${combinedModelLabel}`"
                :title="combinedModelLabel"
                :disabled="modeInteractionLocked || isUpdatingModel || isUpdatingReasoningEffort"
                @click="toggleModelMenu"
              >
                <span class="composer-mode-trigger-label">{{ combinedModelLabel }}</span>
                <span
                  class="composer-mode-chevron"
                  aria-hidden="true"
                >▴</span>
              </button>
              <div
                v-if="isModelMenuOpen"
                class="composer-mode-menu composer-model-menu"
                role="dialog"
                aria-label="Model and thinking effort"
              >
                <div class="composer-mode-search">
                  <input
                    ref="modelSearchEl"
                    type="text"
                    class="composer-textarea composer-mode-search-input"
                    placeholder="Search models…"
                    aria-label="Search models"
                    :value="modelSearch"
                    @input="modelSearch = ($event.target as HTMLInputElement).value"
                  >
                </div>
                <div class="composer-model-columns">
                  <div
                    class="composer-mode-list composer-model-list"
                    role="listbox"
                    aria-label="Models"
                  >
                    <button
                      v-for="model in filteredModelOptions"
                      :key="model.id"
                      type="button"
                      class="composer-mode-menu-item"
                      role="option"
                      :aria-selected="selectedMenuModel?.id === model.id"
                      @click="chooseMenuModel(model)"
                    >
                      <span class="composer-mode-item-label">{{ model.label }}</span>
                      <span
                        v-if="model.supported_reasoning_efforts.length"
                        aria-hidden="true"
                      >›</span>
                    </button>
                    <div
                      v-if="filteredModelOptions.length === 0"
                      class="composer-mode-empty"
                    >
                      No matching models
                    </div>
                  </div>
                  <div
                    v-if="selectedMenuModel"
                    class="composer-effort-list"
                    role="listbox"
                    aria-label="Thinking effort"
                  >
                    <div class="composer-effort-heading">
                      <strong>{{ selectedMenuModel.label }}</strong>
                      <span>Thinking effort</span>
                    </div>
                    <button
                      v-if="selectedMenuModel.supported_reasoning_efforts.length === 0"
                      type="button"
                      class="composer-mode-menu-item"
                      role="option"
                      :aria-selected="isModelActive(selectedMenuModel)"
                      @click="selectModelAndEffort(selectedMenuModel, '')"
                    >
                      <span>Use model</span><span v-if="isModelActive(selectedMenuModel)">✓</span>
                    </button>
                    <button
                      v-else
                      type="button"
                      class="composer-mode-menu-item"
                      role="option"
                      :aria-selected="isModelEffortActive(selectedMenuModel, '')"
                      @click="selectModelAndEffort(selectedMenuModel, '')"
                    >
                      <span>Default ({{ defaultReasoningEffortLabel(selectedMenuModel) }})</span>
                      <span v-if="isModelEffortActive(selectedMenuModel, '')">✓</span>
                    </button>
                    <button
                      v-for="effort in selectedMenuModel.supported_reasoning_efforts"
                      :key="effort.id"
                      type="button"
                      class="composer-mode-menu-item"
                      role="option"
                      :aria-selected="isModelEffortActive(selectedMenuModel, effort.id)"
                      :title="effort.description || effort.label || effort.id"
                      @click="selectModelAndEffort(selectedMenuModel, effort.id)"
                    >
                      <span>{{ effort.label || effortLabel(effort.id) }}</span>
                      <span v-if="isModelEffortActive(selectedMenuModel, effort.id)">✓</span>
                    </button>
                  </div>
                </div>
                <div class="composer-mode-custom">
                  <input
                    ref="modelInputEl"
                    type="text"
                    class="composer-textarea"
                    placeholder="custom model id…"
                    :value="currentModel"
                    @keydown.enter="selectModel(($event.target as HTMLInputElement).value)"
                  >
                </div>
              </div>
            </div>
          </div>
          <button
            v-if="turnInFlight"
            type="button"
            class="composer-stop-btn"
            :disabled="isSending || isCancelling || connectionState !== 'live'"
            @click="cancelActiveTurn"
          >
            {{ isCancelling ? 'Stopping…' : 'Stop' }}
          </button>
          <button
            type="button"
            class="composer-send-btn"
            :disabled="!canSend || isSending"
            :title="goalComposerReason || undefined"
            @click="() => submit('normal')"
          >
            {{ isSending ? 'Sending…' : (turnInFlight ? 'Queue' : 'Send') }}
          </button>
        </div>
        <div
          v-if="!isMobileViewport"
          class="composer-hints"
        >
          <template v-if="goalComposerLocked">
            <span>{{ goalComposerReason }}</span>
          </template>
          <template v-else>
            <span>Enter {{ turnInFlight ? 'queue' : 'send' }}</span>
            <span>⌘/Ctrl+Enter steer</span>
            <span>Shift+Enter newline</span>
          </template>
        </div>
      </div>
    </div>

    <GoalSetupDialog
      :open="isGoalSetupOpen"
      :busy="isGoalMutating"
      :error="goalError"
      @close="isGoalSetupOpen = false"
      @submit="startGoal"
    />

    <Teleport to="body">
      <div
        v-if="imageLightboxUrl"
        class="structured-image-lightbox"
        role="dialog"
        aria-modal="true"
        aria-label="Image preview"
        @click.self="closeImageLightbox"
      >
        <button
          ref="lightboxCloseEl"
          type="button"
          class="structured-image-lightbox-close"
          aria-label="Close image preview"
          @click="closeImageLightbox"
        >
          ×
        </button>
        <img
          class="structured-image-lightbox-img"
          :src="imageLightboxUrl"
          :alt="imageLightboxAlt"
          @click.stop
          @error="closeImageLightbox"
        >
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, ref, toRef, watch } from 'vue'
import { useAgentStream, validateImageAttachment, fileToDataUrl, generatePreviewDataUrl } from '@/composables/useAgentStream'
import { useChatGoal } from '@/composables/useChatGoal'
import { useQuestionAnswers, approvalStateSignature } from '@/composables/useQuestionAnswers'
import { IncrementalTimelineReducer, foldTurnParts, getCompletedPlanText, messageClockLabel, splitTurnProcess, turnClockLabel, turnProcessLabel, type TimelineApproval, type TimelineAttachment, type TimelinePart, type TimelineTool, type TimelineTurn } from '@/utils/agentStreamTimeline'
import { isTimelineNearBottom } from '@/utils/timelineFollow'
import { createTimelineActivation, type TimelinePhase } from '@/utils/timelineActivation'
import { TIMELINE_PAGE_SIZE, selectTimelineWindow, timelineWindowStart } from '@/utils/timelineWindow'
import { getAvailableChatModes, getCurrentChatModeId } from '@/utils/chatModePolicy'
import { goalBlocksPlan, goalPlanLockReason, isGoalTerminal } from '@/utils/chatGoalPolicy'
import { hasChatStatusRefreshBoundary, isChatModeLocked } from '@/utils/chatTurnLifecycle'
import {
  autoresizeComposerTextarea,
  resolveComposerEnterAction,
} from '@/utils/chatComposerInteraction'
import { formatAskQuestionResponse } from '@/utils/chatQuestionResponse'
import { useTerminalStore } from '@/stores/terminalStore'
import { useAppStore } from '@/stores/appStore'
import MarkdownContent from '@/components/MarkdownContent.vue'
import GoalSetupDialog from '@/components/GoalSetupDialog.vue'
import ComposerAddMenu from '@/components/ComposerAddMenu.vue'
import GoalStatusBar from '@/components/GoalStatusBar.vue'
import type { StreamModelOption, WorkspaceAttachmentCreate } from '@/types'

const props = defineProps<{
  /** A top-level Chat tab owns its transcript directly. */
  tabId: string
}>()

const terminalStore = useTerminalStore()
const appStore = useAppStore()
const {
  goal,
  error: goalError,
  isHydrated: isGoalHydrated,
  isHydrating: isGoalHydrating,
  isMutating: isGoalMutating,
  hydrate: hydrateGoal,
  create: createGoal,
  pause: pauseGoal,
  resume: resumeGoal,
  complete: completeGoal,
  clear: clearGoal,
} = useChatGoal(toRef(props, 'tabId'))
const isGoalSetupOpen = ref(false)
const isAddMenuOpen = ref(false)
const goalPlanReason = computed(() => goalPlanLockReason(goal.value))
const goalEditReason = computed(() => goal.value && (!isGoalTerminal(goal.value.status) || goalBlocksPlan(goal.value))
  ? 'Finish or clear the Goal before editing history'
  : null)
const goalComposerLocked = computed(() => goalBlocksPlan(goal.value))
const goalComposerReason = computed(() => goalComposerLocked.value
  ? 'Pause or complete the active Goal before sending messages'
  : null)

async function startGoal(input: { objective: string }) {
  if (goalSetupDisabledReason.value) {
    goalError.value = goalSetupDisabledReason.value
    return
  }
  if (await createGoal(input)) isGoalSetupOpen.value = false
}

const {
  events,
  connectionState,
  errorMessage,
  capabilities,
  start,
  retry: retryStream,
  setMode,
  stop,
  reset: resetStream,
} = useAgentStream()

// Approval-card selection state (AskUserQuestion / AskQuestion /
// request_user_input). Component-scoped so a stream batch rebuilding the
// timeline turn never wipes a pending selection.
const {
  questionAnswers,
  resolvedApprovalKeys,
  customAnswers,
  customAnswer,
  setCustomAnswer,
  isQuestionOptionSelected,
  toggleQuestionOption,
  isApprovalResolved,
  canSubmitQuestion,
  answersFor,
  markResolved,
} = useQuestionAnswers()

const isHistoryVisible = computed(() =>
  connectionState.value === 'live' || connectionState.value === 'reconciling'
)

function startStream() {
  void start(props.tabId, 'terminal-tab')
}

// ── Timeline grouping ───────────────────────────────────────────────────────
// The flat event stream is grouped into turns by the
// ``IncrementalTimelineReducer`` (see agentStreamTimeline.ts).
//
// ``groupEventsIntoTurns`` re-scans the entire event list on every call. For
// a session with thousands of historical events, each incoming delta re-runs
// the full O(n) reduction and dominates the long-task budget. The incremental
// reducer keeps state across calls and only processes the unseen suffix, so
// each batch costs O(new events) regardless of history length.

const timelineReducer = new IncrementalTimelineReducer()
const authoritativeTurns = computed(() => timelineReducer.reduce(events.value))

// Assistant text streams directly from the batched event stream (backend
// 60ms coalescer + frontend rAF/48ms batcher). No second-stage character
// reveal: each committed batch updates the visible text once. On turn
// completion MarkdownContent caches the final block and exposes the exact
// final text synchronously.
const turns = authoritativeTurns
// null follows a bounded tail window; detaching freezes its start so incoming
// turns cannot remove the message being read. Expanding never trims history.
const visibleHistoryStart = ref<number | null>(null)
const isLoadingEarlier = ref(false)
const historyWindowStart = computed(() => timelineWindowStart(turns.value.length, visibleHistoryStart.value))
const visibleTurns = computed(() => selectTimelineWindow(
  turns.value,
  historyWindowStart.value,
  turn => isEditingTurn(turn) || turn.approvals.some(approval => !isApprovalResolved(approval)),
).map(({ turn, ordinal }) => ({
  ordinal,
  turn: {
    ...turn,
    awaitingAgentActivity: !turn.completed &&
      turn.parts.length === 0 &&
      turn.errors.length === 0 &&
      turn.statuses.length === 0,
  },
})))

// ── Fork from turn ──────────────────────────────────────────────────────────
// Preserve the full transcript ordinal when rendering a bounded history window.
const forkingOrdinal = ref<number | null>(null)
const implementingPlanKey = ref<string | null>(null)

function completedPlanText(turn: TimelineTurn): string | null {
  if (turn.key !== turns.value[turns.value.length - 1]?.key) return null
  return getCompletedPlanText(turn)
}

async function implementPlan(turn: TimelineTurn) {
  const plan = completedPlanText(turn)
  if (!plan || implementingPlanKey.value || turnInFlight.value) return
  if (goalComposerLocked.value) {
    modeChangeError.value = goalComposerReason.value
    return
  }
  implementingPlanKey.value = turn.key
  modeChangeError.value = null
  try {
    if (currentModeId.value !== 'default') await setMode('default')
    const sent = await submit('normal', 'Implement the approved plan now.')
    if (!sent) throw new Error(composerError.value || 'Could not start implementation.')
  } catch (err) {
    modeChangeError.value = err instanceof Error ? err.message : 'Failed to start implementation.'
  } finally {
    implementingPlanKey.value = null
  }
}

async function forkFromTurn(ordinal: number) {
  if (forkingOrdinal.value !== null) return
  forkingOrdinal.value = ordinal
  try {
    await terminalStore.forkTab(props.tabId, ordinal)
  } finally {
    forkingOrdinal.value = null
  }
}

type PendingTurn = {
  key: string
  turnId: string
  userText: string
  attachments: { preview_url: string; mime_type: string }[]
}

const pendingDirectTurns = ref<PendingTurn[]>([])
const isUpdatingMode = ref(false)
const modeChangeError = ref<string | null>(null)
const isModeMenuOpen = ref(false)
const modeOptions = computed(() => getAvailableChatModes(capabilities.value))
const currentModeId = computed(() => getCurrentChatModeId(capabilities.value))
const currentModeOption = computed(() => modeOptions.value.find(option => option.id === currentModeId.value))
const currentModeLabel = computed(() => currentModeOption.value?.label ?? 'Mode')

// ── Model picker ────────────────────────────────────────────────────────────
// Each agent type reads its model from a different env var. The frontend
// model picker sets that env var via ``switch-env``; the backend forwards it
// to the provider (Claude/Codex read it from the environment; Cursor receives
// it as a ``--model`` flag because the agent CLI does not read ``CURSOR_MODEL``).

const MODEL_ENV_VAR: Record<string, string> = {
  claude: 'ANTHROPIC_MODEL',
  codex: 'CODEX_MODEL',
  // TraeX reuses the Codex app-server; the backend injects the chosen slug via
  // the collaborationMode.settings.model channel keyed off CODEX_MODEL.
  traex: 'CODEX_MODEL',
  cursor: 'CURSOR_MODEL',
}
const REASONING_EFFORT_ENV: Record<string, string> = {
  codex: 'CODEX_REASONING_EFFORT',
  traex: 'CODEX_REASONING_EFFORT',
}
const LEGACY_TRAEX_REASONING_EFFORT_ENV = 'TRAEX_REASONING_EFFORT'

// Models are discovered at runtime by the backend (cursor via
// ``agent --list-models``; claude/codex via a curated static list) and
// surfaced on the session capabilities. The user can still type a custom
// model id into the picker's text input.
const currentTab = computed(() =>
  terminalStore.tabs.find(t => t.id === props.tabId) ?? null,
)
const modelEnvVar = computed(() => {
  const at = currentTab.value?.agent_type
  return at ? MODEL_ENV_VAR[at] ?? null : null
})
// Edit-resend truncates the provider's on-disk transcript, which is only wired
// for providers whose rollout Hub can locate. TraeX transcripts live under
// ~/.trae and are not wired this wave, so the control is hidden (it would 409).
const supportsEditResend = computed(() => currentTab.value?.agent_type !== 'traex')
const currentModel = computed(() => {
  const env = currentTab.value?.env ?? {}
  const key = modelEnvVar.value
  return key ? env[key] ?? '' : ''
})
const modelOptions = computed<StreamModelOption[]>(
  () => capabilities.value?.available_models ?? [],
)
const effectiveCurrentModel = computed(() => {
  if (currentModel.value && currentModel.value !== 'auto') return currentModel.value
  return capabilities.value?.current_model ?? ''
})
const currentModelOption = computed(() =>
  modelOptions.value.find(model =>
    model.id === effectiveCurrentModel.value
    || model.provider_model_id === currentModel.value
    || model.supported_reasoning_efforts.some(effort => effort.provider_model_id === currentModel.value),
  ) ?? null,
)
const currentModelLabel = computed(() => {
  const id = currentModel.value
  if (!id) {
    // Codex-family providers report their effective thread model. Cursor
    // exposes an explicit Auto row instead.
    return currentModelOption.value?.label
      ?? (modelOptions.value.some(m => m.id === 'auto') ? 'Auto' : 'default')
  }
  return currentModelOption.value?.label ?? id
})
const reasoningEffortEnvVar = computed(() => {
  const at = currentTab.value?.agent_type
  return at ? REASONING_EFFORT_ENV[at] ?? null : null
})
const currentReasoningEffort = computed(() => {
  if (currentTab.value?.agent_type === 'cursor') {
    if (currentModel.value === currentModelOption.value?.provider_model_id) return ''
    return currentModelOption.value?.supported_reasoning_efforts.find(
      effort => effort.provider_model_id === currentModel.value,
    )?.id ?? ''
  }
  const key = reasoningEffortEnvVar.value
  if (!key) return ''
  const selected = currentTab.value?.env?.[key]
    ?? (currentTab.value?.agent_type === 'traex'
      ? currentTab.value?.env?.[LEGACY_TRAEX_REASONING_EFFORT_ENV]
      : '')
    ?? ''
  if (
    selected
    && currentModelOption.value
    && !currentModelOption.value.supported_reasoning_efforts.some(effort => effort.id === selected)
  ) return ''
  return selected
})
const currentReasoningEffortLabel = computed(() =>
  effortLabel(
    currentReasoningEffort.value
    || capabilities.value?.current_reasoning_effort
    || currentModelOption.value?.default_reasoning_effort,
  ),
)
const combinedModelLabel = computed(() => {
  if (!currentModelOption.value?.supported_reasoning_efforts.length) return currentModelLabel.value
  return `${currentModelLabel.value} · ${currentReasoningEffortLabel.value}`
})
// Whether a model row is the active selection. The empty-env state (no
// explicit model) matches the provider's ``auto`` row.
function isModelActive(model: StreamModelOption) {
  if (model.id === 'auto') {
    return currentModel.value === '' || currentModel.value === 'auto'
  }
  return currentModelOption.value?.id === model.id
}
function isModelEffortActive(model: StreamModelOption, effort: string) {
  return isModelActive(model) && currentReasoningEffort.value === effort
}
function effortLabel(effort?: string | null) {
  if (!effort) return 'Default'
  return ({ xhigh: 'Extra High' } as Record<string, string>)[effort]
    ?? effort.replace(/(^|[-_])\w/g, part => part.replace(/[-_]/, ' ').toUpperCase())
}
function defaultReasoningEffortLabel(model: StreamModelOption) {
  return effortLabel(
    capabilities.value?.current_reasoning_effort ?? model.default_reasoning_effort,
  )
}
// Search filter for the picker. Matches id or label, case-insensitive.
const modelSearch = ref('')
const filteredModelOptions = computed(() => {
  const q = modelSearch.value.trim().toLowerCase()
  if (!q) return modelOptions.value
  return modelOptions.value.filter(
    m => m.id.toLowerCase().includes(q) || m.label.toLowerCase().includes(q),
  )
})
const isModelPickerAvailable = computed(() => modelEnvVar.value !== null)
// Reset picker state if it unmounts while open (e.g. the agent type changes),
// so a remount does not reopen with a stale menu/query.
watch(isModelPickerAvailable, available => {
  if (!available) {
    isModelMenuOpen.value = false
    modelSearch.value = ''
  }
})
const isModelMenuOpen = ref(false)
const isUpdatingModel = ref(false)
const isUpdatingReasoningEffort = ref(false)
const selectedMenuModelId = ref('')
const selectedMenuModel = computed(() =>
  filteredModelOptions.value.find(model => model.id === selectedMenuModelId.value)
  ?? filteredModelOptions.value.find(model => model.id === currentModelOption.value?.id)
  ?? filteredModelOptions.value[0]
  ?? null,
)
function chooseMenuModel(model: StreamModelOption) {
  selectedMenuModelId.value = model.id
  if (model.supported_reasoning_efforts.length === 0) {
    void selectModelAndEffort(model, '')
  }
}

async function selectModelAndEffort(model: StreamModelOption, effort: string) {
  closeModelMenu(true)
  const key = modelEnvVar.value
  if (!key) return
  const tab = currentTab.value
  if (!tab) return
  const epoch = preparationEpoch.value
  isUpdatingModel.value = true
  isUpdatingReasoningEffort.value = true
  try {
    const env = { ...(tab.env ?? {}) }
    const selectedEffort = model.supported_reasoning_efforts.find(option => option.id === effort)
    const providerModelId = selectedEffort?.provider_model_id ?? model.provider_model_id ?? model.id
    if (providerModelId) env[key] = providerModelId
    else delete env[key]
    const effortKey = reasoningEffortEnvVar.value
    if (effortKey) {
      if (effort) env[effortKey] = effort
      else delete env[effortKey]
    }
    if (tab.agent_type === 'traex') delete env[LEGACY_TRAEX_REASONING_EFFORT_ENV]
    // Errors surface via the store's notifyError toast; swallow so the
    // rejection is not unhandled.
    await terminalStore.switchEnv(tab.id, { env })
  } catch {
    // Swallow: error feedback comes from terminalStore.switchEnv's toast.
  } finally {
    if (preparationEpoch.value === epoch) {
      isUpdatingModel.value = false
      isUpdatingReasoningEffort.value = false
    }
  }
}

async function selectModel(model: string) {
  const option = modelOptions.value.find(item => item.id === model)
  if (option) {
    await selectModelAndEffort(option, '')
    return
  }
  const custom: StreamModelOption = { id: model, label: model, supported_reasoning_efforts: [] }
  await selectModelAndEffort(custom, '')
}

function closeModelMenu(focusTrigger: boolean) {
  isModelMenuOpen.value = false
  modelSearch.value = ''
  if (focusTrigger) modelTriggerEl.value?.focus()
}

function toggleModelMenu() {
  if (
    modeInteractionLocked.value
    || isUpdatingModel.value
    || isUpdatingReasoningEffort.value
  ) return
  if (isModelMenuOpen.value) {
    closeModelMenu(false)
    return
  }
  isModelMenuOpen.value = true
  selectedMenuModelId.value = currentModelOption.value?.id ?? filteredModelOptions.value[0]?.id ?? ''
  void nextTick(() => {
    modelSearchEl.value?.focus()
  })
}

function handleModelOutsidePointer(e: PointerEvent) {
  if (!isModelMenuOpen.value) return
  const el = modelPickerEl.value
  if (el && !el.contains(e.target as Node)) closeModelMenu(false)
}

const modelPickerEl = ref<HTMLElement | null>(null)
const modelTriggerEl = ref<HTMLButtonElement | null>(null)
const modelInputEl = ref<HTMLInputElement | null>(null)
const modelSearchEl = ref<HTMLInputElement | null>(null)

const pendingTurns = computed(() => {
  const observedTurnIds = new Set(authoritativeTurns.value.map(turn => turn.turnId).filter(Boolean))
  return pendingDirectTurns.value.filter(turn => !observedTurnIds.has(turn.turnId))
})

// ── Composer state ──────────────────────────────────────────────────────────
// Declared before any ``immediate`` watchers so ``turnInFlight`` / ``draftQueue``
// are initialized when Vue runs the first callback (TDZ-safe).

interface DraftAttachment extends WorkspaceAttachmentCreate {
  id: string
  preview_url: string
  size_bytes: number
}

const draftMessage = ref('')
const attachments = ref<DraftAttachment[]>([])
const composerError = ref<string | null>(null)
const isSending = ref(false)
const isCancelling = ref(false)
const isComposing = ref(false)
// Mobile soft keyboards have no Shift/⌘/Ctrl keys, so on a mobile viewport the
// composer hides the desktop-only key hints and treats Enter as a newline
// (sending is only via the Send button). Reactive on resize so rotating or
// resizing a device updates it live.
const isMobileViewport = ref(false)
function handleViewportResize() {
  isMobileViewport.value = window.innerWidth <= 768
}
const composerTextareaEl = ref<HTMLTextAreaElement | null>(null)
const draftQueue = ref<Array<{ message: string; attachments: DraftAttachment[] }>>([])
const isPreparingAttachments = ref(false)
const turnInFlight = computed(() => isChatModeLocked(
  pendingDirectTurns.value.length > 0,
  authoritativeTurns.value,
))
const modeInteractionLocked = computed(() => isSending.value || turnInFlight.value)

// Reconcile optimistic (pending) turns against authoritative turns as they
// arrive. No text-reveal state is kept: assistant text is rendered directly
// from the batched event stream.
watch(
  authoritativeTurns,
  (latest) => {
    const observed = new Set(latest.map(turn => turn.turnId).filter(Boolean))
    pendingDirectTurns.value = pendingDirectTurns.value.filter(turn => !observed.has(turn.turnId))
  },
  { immediate: true },
)

const GOAL_REFRESH_DELAYS_MS = [0, 120, 300, 650, 1200] as const
let goalRefreshEpoch = 0

function goalSnapshotVersion(): string {
  const current = goal.value
  if (!current) return 'none'
  return [
    current.updated_at,
    current.status,
    current.turns_completed,
    current.checkpoint?.turn_id ?? '',
  ].join('\u0000')
}

function goalReflectsCompletedTurn(turnId: string | null, baselineVersion: string): boolean {
  const current = goal.value
  if (!current || current.status !== 'active') return true
  if (turnId && (
    current.completed_turn_ids?.includes(turnId) ||
    current.checkpoint?.turn_id === turnId
  )) return true
  return turnId ? false : goalSnapshotVersion() !== baselineVersion
}

function waitForGoalRefresh(delayMs: number): Promise<void> {
  return new Promise(resolve => window.setTimeout(resolve, delayMs))
}

/**
 * Goal observation runs asynchronously after the transcript commits a
 * turn_completed event. The first GET may therefore still return the prior
 * snapshot. Retry with a short bounded backoff until either the completed turn
 * is visible or the authoritative Goal version advances.
 */
async function refreshGoalAfterTurn(turnId: string | null): Promise<void> {
  const refreshEpoch = ++goalRefreshEpoch
  const baselineVersion = goalSnapshotVersion()
  for (const delayMs of GOAL_REFRESH_DELAYS_MS) {
    if (delayMs > 0) await waitForGoalRefresh(delayMs)
    if (refreshEpoch !== goalRefreshEpoch || timelineDisposed) return
    await hydrateGoal()
    if (refreshEpoch !== goalRefreshEpoch || timelineDisposed) return
    if (goalReflectsCompletedTurn(turnId, baselineVersion)) return
  }
}

// Chat lifecycle edges are authoritative status boundaries. Refresh the tab
// status once per committed boundary. Goal completion gets a bounded poll
// because its observer updates the separate control plane asynchronously.
watch(events, (latest, previous) => {
  if (hasChatStatusRefreshBoundary(previous, latest)) {
    void terminalStore.fetchAgentStatuses()
    const previousLength = previous.length <= latest.length ? previous.length : 0
    let completed = null as (typeof latest)[number] | null
    for (let index = latest.length - 1; index >= previousLength; index -= 1) {
      if (latest[index].type === 'turn_completed') {
        completed = latest[index]
        break
      }
    }
    if (completed) void refreshGoalAfterTurn(completed.turn_id ?? null)
    else void hydrateGoal()
  }
})

// ── Stream lifecycle ────────────────────────────────────────────────────────

onMounted(() => {
  handleViewportResize()
  window.addEventListener('resize', handleViewportResize)
  document.addEventListener('keydown', handleDocumentKeydown)
  document.addEventListener('pointerdown', handleModeOutsidePointer)
  document.addEventListener('pointerdown', handleModelOutsidePointer)
})

// KeepAlive lifecycle. The pane is keyed by tabId (see TerminalPane), so each
// chat tab owns a stable instance and the tabId prop never changes for a given
// instance — the old tabId watcher that wiped the composer on every switch is
// gone. The stream is owned by the *active* pane: on deactivate we stop it
// (caching history) so cached panes don't hold open SSE/long-poll connections;
// on activate we resume from the cached snapshot (reconciling, no full reload).
// onActivated also fires on the initial mount, so it replaces the startStream()
// call that used to live in onMounted.
onActivated(() => {
  timelineDisposed = false
  timelineVisit++
  visibleHistoryStart.value = null
  resetActivation()
  startStream()
  void hydrateGoal()
  void nextTick(() => {
    if (timelineDisposed) return
    observeTimelineGeometry()
  })
})

onDeactivated(() => {
  isAddMenuOpen.value = false
  isGoalSetupOpen.value = false
  goalRefreshEpoch++
  timelineDisposed = true
  timelineVisit++
  stop()
  timelineResizeObserver?.disconnect()
  timelineResizeObserver = null
  cancelScheduledTimelineScroll()
})

onUnmounted(() => {
  goalRefreshEpoch++
  // Bump the epoch on unmount so any in-flight preparation batch aborts
  // instead of mutating state after the component is gone.
  preparationEpoch.value++
  document.removeEventListener('keydown', handleDocumentKeydown)
  document.removeEventListener('pointerdown', handleModeOutsidePointer)
  document.removeEventListener('pointerdown', handleModelOutsidePointer)
  window.removeEventListener('resize', handleViewportResize)
  dismissImageLightbox(false)
  stop()
})

function retry() {
  void retryStream(props.tabId, 'terminal-tab')
}

/** Monotonically increasing epoch that advances on every source switch
 *  (session/tab change) and on unmount. Captured at the start of an
 *  attachment preparation batch and re-checked after every await; an exact
 *  match is required so a stale batch from a previous source visit cannot
 *  append into a later visit of the same source (the ABA problem: switch
 *  A→B→A while FileReader/canvas awaits, and the stale A batch would
 *  otherwise pass a source-string equality check). */
const preparationEpoch = ref(0)
const isDragOver = ref(false)
const fileInputEl = ref<HTMLInputElement | null>(null)
const timelineEl = ref<HTMLElement | null>(null)
const timelineContentEl = ref<HTMLElement | null>(null)
const modePickerEl = ref<HTMLElement | null>(null)
const modeTriggerEl = ref<HTMLButtonElement | null>(null)
const modeMenuEl = ref<HTMLElement | null>(null)
/** Attachment ids whose preview fetch returned 404/410 (evicted or never
 *  cached). Rendered as a visible "Preview expired" placeholder. */
const erroredAttachments = ref<Set<string>>(new Set())
const imageLightboxUrl = ref<string | null>(null)
const imageLightboxAlt = ref('')
const lightboxCloseEl = ref<HTMLButtonElement | null>(null)
let imageLightboxTrigger: HTMLElement | null = null

function openImageLightbox(url: string, alt: string, event: MouseEvent) {
  imageLightboxUrl.value = url
  imageLightboxAlt.value = alt
  imageLightboxTrigger = event.currentTarget instanceof HTMLElement
    ? event.currentTarget
    : null
  void nextTick(() => lightboxCloseEl.value?.focus())
}

function dismissImageLightbox(restoreFocus: boolean) {
  if (!imageLightboxUrl.value) return
  const trigger = imageLightboxTrigger
  imageLightboxUrl.value = null
  imageLightboxAlt.value = ''
  imageLightboxTrigger = null
  if (restoreFocus) void nextTick(() => trigger?.focus())
}

function closeImageLightbox() {
  dismissImageLightbox(true)
}

function handleDocumentKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && isModelMenuOpen.value) {
    event.preventDefault()
    closeModelMenu(true)
    return
  }
  if (event.key === 'Escape' && isModeMenuOpen.value) {
    event.preventDefault()
    closeModeMenu(true)
    return
  }
  if (event.key === 'Escape' && imageLightboxUrl.value) {
    event.preventDefault()
    closeImageLightbox()
  }
}

function handleModeOutsidePointer(event: PointerEvent) {
  if (!isModeMenuOpen.value || modePickerEl.value?.contains(event.target as Node)) return
  closeModeMenu(false)
}

// Activation gate: the timeline is not revealed until authoritative history
// has been hydrated and the tail has been synchronously pinned. This prevents
// the visible "scroll replay" where history paints at the top and then jumps
// to the bottom. See timelineActivation.ts for the state machine contract.
const activation = createTimelineActivation()
const timelinePhase = ref<TimelinePhase>(activation.phase)
const isFollowingLatest = ref(activation.followOutput)

function syncActivation() {
  timelinePhase.value = activation.phase
  isFollowingLatest.value = activation.followOutput
}

function markHistoryReady() {
  activation.markHistoryReady()
  syncActivation()
}

function confirmTailPinned() {
  activation.confirmTailPinned()
  syncActivation()
}

function detachFromTail() {
  if (visibleHistoryStart.value === null) visibleHistoryStart.value = historyWindowStart.value
  activation.detachFromTail()
  syncActivation()
}

function rearmFollow() {
  activation.rearmFollow()
  syncActivation()
}

function resetActivation() {
  activation.reset()
  syncActivation()
}

let timelineResizeObserver: ResizeObserver | null = null
let timelineVerificationFrame: number | null = null
let timelineDisposed = false
let timelineVisit = 0

const timelineLoadingMessage = computed(() => {
  if (connectionState.value === 'failed') return ''
  if (connectionState.value === 'reconciling') return 'Updating conversation…'
  if (connectionState.value === 'idle' || connectionState.value === 'hydrating') return 'Loading conversation…'
  return timelinePhase.value !== 'revealed' ? 'Opening latest messages…' : ''
})

const canSend = computed(() => connectionState.value === 'live' &&
  !goalComposerLocked.value &&
  !isPreparingAttachments.value &&
  !isUpdatingMode.value &&
  !isUpdatingModel.value &&
  !isUpdatingReasoningEffort.value &&
  (draftMessage.value.trim().length > 0 || attachments.value.length > 0))

const supportsImages = computed(() => capabilities.value?.supports_images ?? false)
const attachmentDisabledReason = computed(() => {
  if (!supportsImages.value) return 'This chat does not support image attachments'
  if (goalComposerLocked.value) return goalComposerReason.value
  if (isSending.value || isPreparingAttachments.value) return 'Wait for the current upload or send to finish'
  return null
})
const goalSetupDisabledReason = computed(() => {
  if (goal.value) return 'Clear the current Goal before starting another'
  if (!capabilities.value?.supports_goals) return 'Goals are unavailable for this chat'
  if (!isGoalHydrated.value || isGoalHydrating.value) return 'Loading Goal status…'
  if (goalError.value && !isGoalSetupOpen.value) return 'Reload Goal status before starting a Goal'
  if (isGoalMutating.value) return 'Updating Goal…'
  if (connectionState.value !== 'live') return 'Wait for Chat to reconnect'
  if (modeInteractionLocked.value || isUpdatingMode.value || isUpdatingModel.value || isUpdatingReasoningEffort.value) return 'Wait for the current turn or settings change to finish'
  if (currentModeId.value === 'plan') return 'Switch to Agent mode before setting a Goal'
  return null
})

watch(isAddMenuOpen, open => {
  if (open) { closeModeMenu(false); closeModelMenu(false) }
})
watch([isModeMenuOpen, isModelMenuOpen], ([mode, model]) => {
  if (mode || model) isAddMenuOpen.value = false
})

function closeModeMenu(restoreFocus: boolean) {
  if (!isModeMenuOpen.value) return
  isModeMenuOpen.value = false
  if (restoreFocus) void nextTick(() => modeTriggerEl.value?.focus())
}

function toggleModeMenu() {
  if (modeInteractionLocked.value || isUpdatingMode.value) return
  if (isModeMenuOpen.value) {
    closeModeMenu(false)
    return
  }
  isModeMenuOpen.value = true
  void nextTick(() => {
    const currentItem = modeMenuEl.value?.querySelector<HTMLButtonElement>('[aria-checked="true"]')
    const firstItem = modeMenuEl.value?.querySelector<HTMLButtonElement>('[role="menuitemradio"]')
    const focusTarget = currentItem ?? firstItem
    focusTarget?.focus()
  })
}

async function selectMode(modeId: string) {
  closeModeMenu(true)
  await changeMode(modeId)
}

async function changeMode(modeId: string) {
  if (modeInteractionLocked.value || isUpdatingMode.value || currentModeId.value === modeId) return
  if (modeId === 'plan' && goalPlanReason.value) {
    modeChangeError.value = goalPlanReason.value
    return
  }
  const epoch = preparationEpoch.value
  isUpdatingMode.value = true
  modeChangeError.value = null
  try {
    await setMode(modeId)
  } catch (err) {
    if (preparationEpoch.value !== epoch) return
    modeChangeError.value = err instanceof Error ? err.message : 'Failed to update Chat mode.'
  } finally {
    if (preparationEpoch.value === epoch) isUpdatingMode.value = false
  }
}

watch([modeInteractionLocked, isUpdatingMode], ([locked, updating]) => {
  if (locked || updating) closeModeMenu(false)
})

watch([modeInteractionLocked, isUpdatingModel], ([locked, updating]) => {
  if (locked || updating) closeModelMenu(false)
})

function triggerFilePicker() {
  if (attachmentDisabledReason.value) return
  fileInputEl.value?.click()
}

const MAX_ATTACHMENTS = 10
// Backend enforces a 40 MiB total decoded cap per send request (originals +
// previews). We enforce it conservatively client-side: sum of original file
// sizes plus 512 KiB reserved per selected preview must stay <= 40 MiB. The
// backend remains authoritative.
const MAX_TOTAL_REQUEST_BYTES = 40 * 1024 * 1024
const PREVIEW_RESERVED_BYTES = 512 * 1024

async function addFiles(files: FileList | File[]) {
  // Capture the epoch BEFORE any state mutation. If the source switches
  // (or the component unmounts) during this batch, the epoch advances and
  // every post-await check fails, so this stale batch never touches the
  // new source's composer state.
  const epoch = preparationEpoch.value

  composerError.value = null
  if (!supportsImages.value) {
    composerError.value = 'This chat does not support image attachments.'
    return
  }
  // Serialize preparation batches: reject new input while a previous batch is
  // still reading/generating previews. Without this, two concurrent batches
  // both set isPreparingAttachments=true; the first to finish clears it and
  // re-enables Send while the second is still mid-await.
  if (isPreparingAttachments.value) return
  // Block new attachments while a send is in flight: an async continuation
  // must never append into a composer that has already been cleared and sent.
  if (isSending.value) return

  const list = Array.from(files)
  // Enforce the max-attachment count client-side.
  const remaining = MAX_ATTACHMENTS - attachments.value.length
  if (remaining <= 0) {
    composerError.value = `You can attach up to ${MAX_ATTACHMENTS} images.`
    return
  }
  const accepted = list.slice(0, remaining)
  if (accepted.length < list.length) {
    composerError.value = `You can attach up to ${MAX_ATTACHMENTS} images.`
  }

  // Enforce the total request byte cap before reading: sum of existing draft
  // originals + new originals + 512 KiB per preview must stay <= 40 MiB.
  const existingBytes = attachments.value.reduce((sum, a) => sum + (a.size_bytes || 0), 0)
  const newBytes = accepted.reduce((sum, f) => sum + f.size, 0)
  const totalPreviews = attachments.value.length + accepted.length
  const projected = existingBytes + newBytes + totalPreviews * PREVIEW_RESERVED_BYTES
  if (projected > MAX_TOTAL_REQUEST_BYTES) {
    const mb = (MAX_TOTAL_REQUEST_BYTES / 1024 / 1024).toFixed(0)
    composerError.value = `Total attachment size exceeds the ${mb} MiB request limit.`
    return
  }

  // Validate all accepted files before starting any async work.
  for (const file of accepted) {
    const err = validateImageAttachment(file)
    if (err) {
      composerError.value = err
      return
    }
  }

  // Mark the entire preparation batch active BEFORE the first await so Send
  // stays disabled for the whole read+preview-generation window.
  isPreparingAttachments.value = true

  try {
    for (const file of accepted) {
      // Read the original once. The provisional thumbnail uses the original
      // data URL so it appears instantly; the bounded preview is generated
      // from the same data URL (no second read).
      let dataUrl: string
      try {
        dataUrl = await fileToDataUrl(file)
      } catch (e) {
        // Only surface the error if this batch still owns the composer.
        if (preparationEpoch.value === epoch) {
          composerError.value = e instanceof Error
            ? `Failed to read ${file.name}: ${e.message}`
            : `Failed to read ${file.name}.`
        }
        continue
      }
      // Stale batch (source switched or send started): bail out WITHOUT
      // clearing isPreparingAttachments — the new source's batch may own it.
      if (preparationEpoch.value !== epoch || isSending.value) return

      const provisionalId = `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
      const provisional: DraftAttachment = {
        id: provisionalId,
        filename: file.name,
        mime_type: file.type,
        data_url: dataUrl,
        // Provisional thumbnail uses the original data URL.
        preview_url: dataUrl,
        size_bytes: file.size,
      }
      attachments.value.push(provisional)

      try {
        // Pass the already-read data URL so we do not read the file twice.
        const previewDataUrl = await generatePreviewDataUrl(file, dataUrl)
        // Stale batch: bail out without clearing the flag.
        if (preparationEpoch.value !== epoch || isSending.value) return
        const idx = attachments.value.findIndex((a) => a.id === provisionalId)
        if (idx < 0) continue // removed by the user while preparing
        attachments.value[idx] = {
          ...provisional,
          preview_data_url: previewDataUrl,
          // Replace the provisional thumbnail with the bounded preview.
          preview_url: previewDataUrl,
        }
      } catch (e) {
        // Preview generation failed. Only mutate state if this batch still
        // owns the composer; otherwise leave the new source's state alone.
        if (preparationEpoch.value !== epoch) return
        attachments.value = attachments.value.filter((a) => a.id !== provisionalId)
        composerError.value = e instanceof Error
          ? `Failed to prepare ${file.name}: ${e.message}`
          : `Failed to prepare ${file.name}.`
      }
    }
  } finally {
    // Only the owning batch may clear the preparing flag. A stale batch
    // (epoch advanced) must not unlock a newer batch that set the flag.
    if (preparationEpoch.value === epoch) {
      isPreparingAttachments.value = false
    }
  }
}

function handleFilePick(event: Event) {
  const input = event.target as HTMLInputElement
  if (input.files && input.files.length > 0) {
    void addFiles(input.files)
  }
  input.value = ''
}

function handlePaste(event: ClipboardEvent) {
  if (isSending.value) return
  const items = event.clipboardData?.items
  if (!items) return
  const files: File[] = []
  for (const item of items) {
    if (item.kind === 'file') {
      const f = item.getAsFile()
      if (f) files.push(f)
    }
  }
  if (files.length > 0) {
    event.preventDefault()
    void addFiles(files)
  }
}

function handleDragOver(event: DragEvent) {
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
  isDragOver.value = true
}

function handleDragLeave() {
  isDragOver.value = false
}

function handleDrop(event: DragEvent) {
  event.preventDefault()
  isDragOver.value = false
  if (isSending.value) return
  const files = event.dataTransfer?.files
  if (files && files.length > 0) {
    void addFiles(files)
  }
}

function removeAttachment(att: DraftAttachment) {
  if (isSending.value) return
  const idx = attachments.value.findIndex((a) => a.id === att.id)
  if (idx >= 0) attachments.value.splice(idx, 1)
}

/**
 * Build the scoped preview URL for an authoritative attachment id.
 *
 * The endpoint is session/tab-scoped so a leaked id from another session
 * cannot be used to fetch previews. The backend validates the id against the
 * session/tab manifest before serving.
 */
function attachmentUrl(attachmentId: string): string {
  const encId = encodeURIComponent(attachmentId)
  return `/api/workspaces/tabs/${encodeURIComponent(props.tabId)}/stream/attachments/${encId}`
}

/**
 * Record an attachment whose preview fetch failed (404/410 or network error).
 *
 * The template renders a visible "Preview expired" placeholder for ids in
 * ``erroredAttachments`` instead of a broken image icon. The set is keyed by
 * attachment id; a re-render of the turn (e.g. on reconnect) does not clear
 * it, so the placeholder stays stable.
 */
function onAttachmentError(_event: Event, att: TimelineAttachment): void {
  if (att.id === null) return
  erroredAttachments.value = new Set(erroredAttachments.value).add(att.id)
}

watch(draftMessage, () => {
  void nextTick(() => syncComposerTextareaHeight())
})

function syncComposerTextareaHeight() {
  autoresizeComposerTextarea(composerTextareaEl.value)
}

function handleComposerEnter(event: KeyboardEvent) {
  if (goalComposerLocked.value) {
    composerError.value = goalComposerReason.value
    return
  }
  const action = resolveComposerEnterAction({
    isComposing: isComposing.value,
    shiftKey: event.shiftKey,
    metaKey: event.metaKey,
    ctrlKey: event.ctrlKey,
    altKey: event.altKey,
    turnInFlight: turnInFlight.value,
    hasDraft: draftMessage.value.trim().length > 0 || attachments.value.length > 0,
    isMobile: isMobileViewport.value,
  })
  if (action === 'ignore' || action === 'newline') return
  event.preventDefault()
  if (action === 'queue') {
    enqueueDraft()
    return
  }
  void submit(action === 'steer' ? 'steer' : 'normal')
}

function enqueueDraft() {
  if (goalComposerLocked.value) {
    composerError.value = goalComposerReason.value
    return
  }
  if (!canSend.value) return
  draftQueue.value.push({
    message: draftMessage.value,
    attachments: [...attachments.value],
  })
  draftMessage.value = ''
  attachments.value = []
  composerError.value = null
  void nextTick(() => syncComposerTextareaHeight())
}

async function flushDraftQueue() {
  while (draftQueue.value.length > 0 && !turnInFlight.value && !isSending.value && !goalComposerLocked.value) {
    const next = draftQueue.value.shift()
    if (!next) break
    draftMessage.value = next.message
    attachments.value = next.attachments
    await submit('normal')
    if (composerError.value) break
  }
}

watch(
  [turnInFlight, () => draftQueue.value.length, isSending, goalComposerLocked],
  () => {
    if (!turnInFlight.value && draftQueue.value.length > 0 && !isSending.value && !goalComposerLocked.value) {
      void flushDraftQueue()
    }
  },
)

/**
 * Per-turn memo signature for approval-card interaction state.
 *
 * Each turn is memoized with ``v-memo="[renderRevision, erroredAttachments,
 * signature]"``. The approval card's selected / resolved / send-disabled state
 * lives in component refs (``questionAnswers``, ``resolvedApprovalKeys``,
 * ``isSending``) that are NOT part of the timeline turn, so without this
 * signature a click toggles the reactive state but ``v-memo`` skips
 * re-rendering the turn — the chip never shows selected and the submit button
 * stays disabled. Folding this signature into the deps re-renders only the
 * turn owning the changed approval; turns without approvals return ``''`` and
 * stay memoized.
 */
function turnApprovalSignature(turn: TimelineTurn): string {
  // Turns without approvals never depend on this state; keep them memoized.
  if (turn.approvals.length === 0) return ''
  let signature = ''
  for (const approval of turn.approvals) {
    signature += `${approval.key}=${approvalStateSignature(
      approval,
      questionAnswers.value,
      resolvedApprovalKeys.value,
      customAnswers.value,
    )};`
  }
  // isSending gates the option/submit disabled state inside the card, so a
  // send start/end must also invalidate the memo for approval-bearing turns.
  return isSending.value ? `${signature}|sending` : signature
}

/**
 * Which turns the viewer has explicitly opened or closed.
 *
 * Fold state is per-viewer UI state, not part of the durable timeline, so it
 * lives here rather than on the turn the shared reducer produces. Same
 * ``v-memo`` constraint as ``turnApprovalSignature``: a turn whose deps do not
 * change will not re-render, so the fold decision has to reach the deps array
 * via ``turnFoldSignature`` below or a click would do nothing.
 */
const processExpandedOverrides = ref(new Map<string, boolean>())

/**
 * Key of the newest turn, or ``null`` while none has started.
 *
 * That turn stays open: it is the one being read, and folding it the instant
 * it finished would collapse the process out from under someone mid-glance.
 * Older turns are history and fold themselves. The newest turn is the one
 * that stays open even while it is still running — so a finished turn folds
 * as soon as a newer turn starts, rather than staying expanded for the whole
 * time the newer turn is in flight.
 */
const latestTurnKey = computed(() => {
  const list = turns.value
  return list.length > 0 ? list[list.length - 1].key : null
})

function isProcessExpanded(turn: TimelineTurn): boolean {
  const override = processExpandedOverrides.value.get(turn.key)
  if (override !== undefined) return override
  return turn.key === latestTurnKey.value
}

/** True when this turn has a working region worth folding.
 *
 * The newest turn is excluded so it keeps rendering exactly as it did before
 * folding existed, and ``splitTurnProcess`` rejects the rest of the unsafe
 * cases (still running, no delivered answer, work continuing past the last
 * text). An approval card is not one of them — it folds with the record, and
 * the header counts it so the card is not hidden without a trace. */
function isTurnFoldable(turn: TimelineTurn): boolean {
  return turn.key !== latestTurnKey.value && splitTurnProcess(turn) !== null
}

function toggleTurnProcess(turn: TimelineTurn): void {
  const next = new Map(processExpandedOverrides.value)
  next.set(turn.key, !isProcessExpanded(turn))
  processExpandedOverrides.value = next
}

/** Parts to render for a turn, with its process folded away once it is history. */
function turnPartsFor(turn: TimelineTurn): TimelinePart[] {
  if (!isTurnFoldable(turn)) return turn.parts
  return foldTurnParts(turn, isProcessExpanded(turn))
}

function turnFoldSignature(turn: TimelineTurn): string {
  // Turns with nothing foldable — including the active one — never depend on
  // this state; keep them on the cheap memoized path.
  if (!isTurnFoldable(turn)) return ''
  const split = splitTurnProcess(turn)
  const label = split ? turnProcessLabel(turn, split.before) : ''
  return `${isProcessExpanded(turn) ? 'open' : 'folded'}|${label}`
}

/**
 * Collapse the native ``<details>`` containing the clicked button.
 *
 * The thinking and tool cards stay uncontrolled: the browser owns their open
 * state, which is exactly what lets ``v-memo`` skip historical turns without
 * losing an expanded card. A footer "收起" button therefore reaches for the
 * enclosing element rather than introducing per-card Vue state that every
 * memo dependency would then have to track.
 */
function collapseDetails(event: MouseEvent): void {
  const target = event.currentTarget
  if (target instanceof HTMLElement) {
    target.closest('details')?.removeAttribute('open')
  }
}


/** Aggregate status for a tool group: 'running' if any tool is still running,
 *  'failed' if any tool failed (and none running), 'cancelled' if every tool
 *  was cancelled (e.g. a dismissed AskUserQuestion), else 'completed'. */
function toolGroupStatus(
  tools: TimelineTool[],
): 'running' | 'completed' | 'failed' | 'cancelled' {
  if (tools.some(t => t.status === 'running')) return 'running'
  if (tools.some(t => t.status === 'failed')) return 'failed'
  if (tools.length > 0 && tools.every(t => t.status === 'cancelled')) return 'cancelled'
  return 'completed'
}

async function submitQuestionResponse(approval: TimelineApproval) {
  if (!canSubmitQuestion(approval)) {
    composerError.value = '请选择所有问题的选项后再提交。'
    return
  }
  const sent = await submit(turnInFlight.value ? 'steer' : 'normal', formatAskQuestionResponse(answersFor(approval.key)))
  if (sent) markResolved(approval.key)
}

async function cancelActiveTurn() {
  if (isCancelling.value || isSending.value || !turnInFlight.value) return
  isCancelling.value = true
  composerError.value = null
  try {
    const res = await fetch(`/api/workspaces/tabs/${props.tabId}/stream/cancel`, {
      method: 'POST',
      credentials: 'same-origin',
    })
    if (!res.ok) {
      let detail = `HTTP ${res.status}`
      try {
        const body = await res.json()
        if (body?.detail) detail = body.detail
      } catch {
        // ignore non-JSON error body
      }
      throw new Error(detail)
    }
    void terminalStore.fetchAgentStatuses()
  } catch (err) {
    composerError.value = err instanceof Error ? err.message : 'Failed to stop the current turn.'
  } finally {
    isCancelling.value = false
  }
}

/**
 * Deliver composer input to the native provider transport via ``/stream/send``.
 *
 * StructuredPane is mounted only for top-level Chat tabs. Text and images are
 * staged and submitted together through the native transport's atomic
 * send_message boundary.
 */
async function sendToStream(
  message: string,
  atts: WorkspaceAttachmentCreate[],
  clientTurnId: string,
  delivery: 'normal' | 'steer' = 'normal',
) {
  const base = `/api/workspaces/tabs/${props.tabId}/stream/send`
  const res = await fetch(base, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
      client_turn_id: clientTurnId,
      text: message,
      delivery,
      attachments: atts.map(({ filename, mime_type, data_url, preview_data_url }) => ({
        filename,
        mime_type,
        data_url,
        preview_data_url,
      })),
    }),
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      // ignore non-JSON error body
    }
    throw new Error(detail)
  }
}

// ── Edit-resend ──────────────────────────────────────────────────────────────
// A user can edit one of their sent messages; after resending, the
// conversation reruns from that point (truncating subsequent turns).

const editingTurnKey = ref<string | null>(null)
const editDraft = ref('')
const isEditSending = ref(false)
const editError = ref<string | null>(null)

function isEditingTurn(turn: TimelineTurn): boolean {
  return editingTurnKey.value === turn.key
}

function startEdit(turn: TimelineTurn) {
  if (isEditSending.value) return
  // Refuse to edit while a turn is running.  The backend guard is
  // authoritative (409); this just avoids a round-trip and keeps the button
  // and the action in sync.
  if (turnInFlight.value) return
  if (goalEditReason.value) {
    editError.value = goalEditReason.value
    return
  }
  editingTurnKey.value = turn.key
  editDraft.value = turn.userText
  editError.value = null
}

function cancelEdit() {
  if (isEditSending.value) return
  editingTurnKey.value = null
  editDraft.value = ''
  editError.value = null
}

async function submitEdit(turn: TimelineTurn) {
  if (isEditSending.value) return
  const text = editDraft.value
  if (!text.trim()) {
    editError.value = 'Message cannot be empty'
    return
  }
  if (!turn.turnId) {
    editError.value = 'Cannot edit this turn (missing turn id)'
    return
  }
  isEditSending.value = true
  editError.value = null
  const clientTurnId = typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `turn-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`
  try {
    const res = await fetch(`/api/workspaces/tabs/${props.tabId}/stream/edit-resend`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        text,
        client_turn_id: clientTurnId,
        turn_id: turn.turnId,
      }),
    })
    if (!res.ok) {
      let detail = `HTTP ${res.status}`
      try {
        const body = await res.json()
        if (body?.detail) detail = body.detail
      } catch {
        // ignore non-JSON error body
      }
      throw new Error(detail)
    }
    // Success: clear editing state and rehydrate the stream from the
    // truncated event store.
    editingTurnKey.value = null
    editDraft.value = ''
    resetStream()
    void start(props.tabId, 'terminal-tab')
  } catch (err) {
    editError.value = err instanceof Error ? err.message : 'Failed to resend message'
  } finally {
    isEditSending.value = false
  }
}

async function submit(
  delivery: 'normal' | 'steer' = 'normal',
  messageOverride?: string,
): Promise<boolean> {
  if (isSending.value) return false
  if (!messageOverride && goalComposerLocked.value) {
    composerError.value = goalComposerReason.value
    return false
  }
  const message = messageOverride ?? draftMessage.value
  const hasContent = message.trim().length > 0 || attachments.value.length > 0
  if (!hasContent) return false
  if (delivery === 'normal' && turnInFlight.value && !messageOverride) {
    enqueueDraft()
    return false
  }
  isSending.value = true
  composerError.value = null
  // Snapshot the full draft attachments (including id, preview_url,
  // preview_data_url, size_bytes) so we can restore the composer exactly on
  // send failure — no reconstructed ids, no zeroed sizes.
  const draftAtts: DraftAttachment[] = messageOverride ? [] : [...attachments.value]
  // Wire payload carries the original data_url (for the provider) and the
  // bounded preview_data_url (for the cache) separately.
  const atts: WorkspaceAttachmentCreate[] = draftAtts.map(
    ({ filename, mime_type, data_url, preview_data_url }) => ({
      filename,
      mime_type,
      data_url,
      preview_data_url,
    }),
  )
  // Optimistic bubble thumbnails come from the bounded preview.
  const pendingAtts = draftAtts.map(({ preview_url, mime_type }) => ({
    preview_url,
    mime_type,
  }))
  const clientTurnId = typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `turn-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`
  try {
    if (!messageOverride) {
      draftMessage.value = ''
      attachments.value = []
    }
    // Show the user's turn immediately; the stream will replace it with the
    // authoritative transcript line once the provider echoes it back.
    //
    // A provider-native question answer (messageOverride) is NOT a new turn:
    // the transport routes it as the blocking question's JSON-RPC response and
    // publishes no turn_started for it, so an optimistic bubble would never
    // reconcile against an authoritative turnId — leaving it pinned forever and
    // holding turnInFlight (composer lock) until remount. The approval card is
    // already marked resolved by submitQuestionResponse as the visual ack.
    if (!messageOverride) {
      pendingDirectTurns.value = [
        ...pendingDirectTurns.value,
        {
          key: `pending-${clientTurnId}`,
          turnId: clientTurnId,
          userText: message,
          attachments: pendingAtts,
        },
      ]
    }
    requestLatestAnchor(true)
    await sendToStream(message, atts, clientTurnId, delivery)
    if (messageOverride) await hydrateGoal()
    // The POST acknowledgement means provider dispatch has begun. Refresh the
    // backend-native tab status now rather than waiting for the 5s poll phase;
    // turn_started/completed/error boundaries above provide subsequent edges.
    void terminalStore.fetchAgentStatuses()
    // Success: composer already cleared; nothing more to do.
    void nextTick(() => syncComposerTextareaHeight())
    return true
  } catch (err) {
    pendingDirectTurns.value = pendingDirectTurns.value.filter(turn => turn.turnId !== clientTurnId)
    if (!messageOverride) {
      draftMessage.value = message
      attachments.value = draftAtts
    }
    composerError.value = err instanceof Error ? err.message : 'Failed to send message.'
    return false
  } finally {
    isSending.value = false
  }
}

function cancelScheduledTimelineScroll() {
  if (timelineVerificationFrame !== null) {
    cancelAnimationFrame(timelineVerificationFrame)
    timelineVerificationFrame = null
  }
}

/**
 * Pin the timeline to the latest turn.
 *
 * The initial reveal is handled by the activation gate (``markHistoryReady``
 * → synchronous ``scrollTop = scrollHeight`` in ``nextTick`` →
 * ``confirmTailPinned``). This function only drives live updates once the
 * timeline is revealed and the user has not detached.
 *
 * The scroll is applied synchronously inside ``nextTick`` (after Vue's DOM
 * commit, before the browser paints) so a growing assistant message never
 * paints above the fold and then jumps down. A single verification rAF
 * re-sticks if Markdown / image / font layout changed the scroll height.
 */
function requestLatestAnchor(force = false) {
  if (force) rearmFollow()
  if (!isFollowingLatest.value || timelineDisposed) return
  // While hidden or pinning, the activation gate owns the scroll position.
  if (timelinePhase.value !== 'revealed') return
  cancelScheduledTimelineScroll()
  const visit = timelineVisit
  void nextTick(() => {
    if (timelineDisposed || visit !== timelineVisit) return
    const el = timelineEl.value
    if (!el || !isFollowingLatest.value || timelineDisposed) return
    el.scrollTop = el.scrollHeight
    timelineVerificationFrame = requestAnimationFrame(() => {
      timelineVerificationFrame = null
      const current = timelineEl.value
      if (!current || !isFollowingLatest.value || timelineDisposed || visit !== timelineVisit) return
      if (!isTimelineNearBottom(current)) current.scrollTop = current.scrollHeight
    })
  })
}

function handleTimelineScroll() {
  const el = timelineEl.value
  if (!el) return
  // Scroll events during hydration are not user-driven and must not detach.
  if (timelineDisposed || timelinePhase.value !== 'revealed' || isLoadingEarlier.value) return
  if (isTimelineNearBottom(el)) {
    if (!isFollowingLatest.value) rearmFollow()
  } else {
    if (isFollowingLatest.value) detachFromTail()
  }
}

function jumpToLatest() {
  visibleHistoryStart.value = null
  rearmFollow()
  requestLatestAnchor(true)
}

async function loadEarlierTurns() {
  const viewport = timelineEl.value
  if (!viewport || isLoadingEarlier.value || historyWindowStart.value === 0) return
  detachFromTail()
  cancelScheduledTimelineScroll()
  const visit = timelineVisit
  const top = viewport.getBoundingClientRect().top
  // Keep a real visible row as anchor: older pending approvals may already be
  // rendered above the window, so scrollHeight differences alone are unsafe.
  const anchor = Array.from(viewport.querySelectorAll<HTMLElement>('.structured-turn'))
    .find(row => row.getBoundingClientRect().bottom > top)
  const offset = anchor?.getBoundingClientRect().top
  isLoadingEarlier.value = true
  try {
    // Let the disabled loading affordance paint before mounting the next page.
    await new Promise<void>(resolve => requestAnimationFrame(() => setTimeout(resolve, 0)))
    if (timelineDisposed || visit !== timelineVisit) return
    visibleHistoryStart.value = Math.max(0, historyWindowStart.value - TIMELINE_PAGE_SIZE)
    await nextTick()
    if (timelineDisposed || visit !== timelineVisit) return
    if (anchor?.isConnected && offset !== undefined) {
      viewport.scrollTop += anchor.getBoundingClientRect().top - offset
    }
  } finally {
    isLoadingEarlier.value = false
  }
}

function observeTimelineGeometry() {
  timelineResizeObserver?.disconnect()
  if (typeof ResizeObserver === 'undefined') return
  const viewport = timelineEl.value
  const content = timelineContentEl.value
  if (!viewport || !content) return
  timelineResizeObserver = new ResizeObserver(() => {
    // Resize only re-sticks while the user is following the tail. A detached
    // viewport must never be hijacked by a layout change.
    if (activation.shouldHandleResize()) requestLatestAnchor()
  })
  timelineResizeObserver.observe(viewport)
  timelineResizeObserver.observe(content)
}

watch(
  () => [events.value.length, pendingTurns.value.length],
  () => requestLatestAnchor(),
)

// Activation gate: when cached or authoritative history becomes renderable,
// pin the tail synchronously (after Vue's DOM commit, before paint) and only
// then reveal the timeline. This is the Paseo
// ``isAuthoritativeHistoryReady`` pattern — the first painted frame is already
// at the tail.
//
// ``immediate`` covers a quick tab switch-back: cached history enters
// ``reconciling`` immediately, without waiting for network hydration.
watch(connectionState, (state) => {
  if (state === 'live' || state === 'reconciling') {
    // Reconciliation finishing within this visit must not interrupt reading.
    // Each activation resets the gate before starting reconciliation.
    if (timelinePhase.value === 'revealed') return
    markHistoryReady()
    const visit = timelineVisit
    void nextTick(() => {
      if (timelineDisposed || visit !== timelineVisit) return
      const el = timelineEl.value
      if (el) el.scrollTop = el.scrollHeight
      confirmTailPinned()
      // Removing the loading banner changes viewport height. Re-pin after
      // the reveal DOM commit as well, then verify delayed Markdown layout.
      requestLatestAnchor()
    })
  } else if (state === 'hydrating') {
    resetActivation()
  }
  // 'failed' and 'idle' leave the timeline hidden; failed shows the Retry
  // banner. Retry re-enters 'hydrating' and the gate runs again.
}, { immediate: true })

onMounted(() => {
  timelineDisposed = false
  void nextTick(() => {
    if (timelineDisposed) return
    observeTimelineGeometry()
  })
})

onUnmounted(() => {
  timelineDisposed = true
  timelineResizeObserver?.disconnect()
  timelineResizeObserver = null
  cancelScheduledTimelineScroll()
})
</script>

<style scoped>
.structured-pane {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background-color: var(--ch-color-app-bg);
  color: var(--ch-color-text);
  font-size: 13px;
  line-height: 1.5;
}

.structured-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background-color: var(--ch-color-surface);
  border-bottom: 1px solid var(--ch-color-border-muted);
  font-size: 12px;
  color: var(--ch-color-text-muted);
  flex-shrink: 0;
}

.banner-guidance {
  margin-left: auto;
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

.structured-timeline {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  min-height: 0;
}

/* Keep the real scroll geometry while hidden: changing overflow for the gate
   can clamp scrollTop and detach follow mode on reveal. */
.structured-timeline.is-timeline-hidden {
  visibility: hidden;
  pointer-events: none;
}

.structured-load-earlier {
  align-self: center;
  padding: 7px 14px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  color: var(--ch-color-text-muted);
  background: var(--ch-color-surface);
  cursor: pointer;
}

.structured-load-earlier:disabled {
  cursor: wait;
  opacity: 0.7;
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

.structured-empty {
  text-align: center;
  color: var(--ch-color-text-subtle);
  padding: 24px 12px;
}

.empty-hint {
  font-size: 12px;
  opacity: 0.7;
}

.structured-turn {
  margin-bottom: 16px;
  position: relative;
}

/* Per-message actions: a row under the message, revealed by the turn's hover,
   holding the actions and the time. Codex's shape — and the reason it is a row
   in normal flow rather than a cluster pinned over the turn is that pinning it
   put the buttons behind the bubble (which is positioned) where they could not
   be clicked at all. The row keeps its height when hidden so revealing it does
   not shift the conversation, and reserves room for the buttons and the time
   instead of crowding them. */
.turn-actions {
  display: flex;
  align-items: center;
  gap: 14px;
  /* Reserved whether or not the row is showing: revealing it must not shift
     the conversation. Kept as tight as the pills allow — two rows per turn at
     24px each is a lot of empty space in a long thread. */
  min-height: 20px;
  margin: 0 4px 6px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease;
}

.turn-actions--message,
.turn-actions--turn {
  justify-content: flex-end;
}

.structured-turn:hover .turn-actions,
.turn-actions:focus-within {
  opacity: 1;
  pointer-events: auto;
}

/* The time reads as a quiet trailing label, not a fourth button. */
.turn-time {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  /* ``muted``, not ``subtle`` + opacity: measured 2.47:1 in dark and 2.05:1 in
     light, both under the 3:1 floor for non-text content. */
  color: var(--ch-color-text-muted, var(--ch-color-text-subtle, currentColor));
  white-space: nowrap;
}

.turn-fork-button {
  font-size: 11px;
  font-weight: 600;
  padding: 3px 9px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, currentColor 22%, transparent);
  background: var(--ch-color-bg-elevated, canvas);
  color: var(--ch-color-text-subtle, currentColor);
  cursor: pointer;
}

.turn-fork-button:hover:not(:disabled) {
  border-color: var(--ch-color-accent);
  color: var(--ch-color-accent);
}

.turn-fork-button:disabled {
  opacity: 0.6;
  cursor: default;
}

.structured-turn--pending {
  opacity: 0.82;
}

.pending-attachment {
  display: block;
  margin-top: 7px;
  padding-top: 7px;
  border-top: 1px solid color-mix(in srgb, currentColor 24%, transparent);
  font-size: 11px;
  font-weight: 600;
}

.event {
  margin-bottom: 8px;
}

.event-role {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--ch-color-text-subtle);
  margin-bottom: 2px;
}

.event-body {
  white-space: pre-wrap;
  word-break: break-word;
}

.event-user .event-body {
  background-color: var(--ch-color-accent-soft);
  border-radius: var(--ch-radius-md);
  padding: 8px 10px;
}

.event-assistant .event-body {
  background-color: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  padding: 8px 10px;
}

.event-thinking details {
  background-color: var(--ch-color-surface);
  border: 1px dashed var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  padding: 6px 10px;
}

.event-thinking summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--ch-color-text-subtle);
  outline: none;
}

.event-thinking summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
  border-radius: var(--ch-radius-sm);
}

.thinking-body {
  margin-top: 6px;
  font-size: 12px;
  color: var(--ch-color-text-muted);
  white-space: pre-wrap;
}

.event-tool {
  background-color: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  padding: 6px 10px;
  font-size: 12px;
}

.tool-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tool-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 600;
}

.tool-status {
  font-size: 10px;
  text-transform: uppercase;
  padding: 1px 6px;
  border-radius: 999px;
  background-color: var(--ch-color-border);
  color: var(--ch-color-text-muted);
}

.tool-status.completed {
  background-color: var(--ch-color-success-bg, rgba(46, 160, 67, 0.12));
  color: var(--ch-color-success-strong, #2ea043);
}

.tool-status.failed {
  background-color: var(--ch-color-danger-bg, rgba(248, 81, 73, 0.12));
  color: var(--ch-color-danger-strong, #f85149);
}

.tool-status.running {
  background-color: var(--ch-color-warning-bg, rgba(224, 168, 0, 0.12));
  color: var(--ch-color-warning, #e0a800);
}

.tool-status.cancelled {
  background-color: var(--ch-color-surface-muted, rgba(139, 148, 158, 0.12));
  color: var(--ch-color-text-muted, #8b949e);
}

.tool-args,
.tool-result {
  margin-top: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: var(--ch-color-text-muted);
  white-space: pre-wrap;
  word-break: break-all;
}

.event-error {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  background-color: var(--ch-color-danger-bg, rgba(248, 81, 73, 0.1));
  border: 1px solid var(--ch-color-danger-border, rgba(248, 81, 73, 0.3));
  border-radius: var(--ch-radius-md);
  padding: 6px 10px;
  color: var(--ch-color-danger-strong, #f85149);
  font-size: 12px;
}

.error-icon {
  flex-shrink: 0;
}

.event-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--ch-color-text-subtle);
}

.event-status--pending {
  color: var(--ch-color-text-muted);
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: var(--ch-color-text-subtle);
  flex-shrink: 0;
}

/* Composer */
.structured-composer {
  border-top: 1px solid var(--ch-color-border-muted);
  padding: 8px 12px;
  background-color: var(--ch-color-surface);
  flex-shrink: 0;
}

.composer-tools {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  flex: 0 0 auto;
  margin-right: auto;
}

.composer-mode-picker {
  position: relative;
  flex: 0 0 auto;
}

.composer-mode-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  width: auto;
  max-width: 210px;
  height: 32px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text-muted);
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}

.composer-mode-trigger:hover:not(:disabled),
.composer-mode-trigger[aria-expanded='true'] {
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control-hover);
}

.composer-mode-trigger-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-mode-chevron {
  flex: 0 0 auto;
  color: var(--ch-color-text-subtle);
  font-size: 9px;
  line-height: 1;
}

.composer-mode-trigger:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.composer-mode-trigger:focus-visible,
.composer-mode-menu-item:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

.composer-mode-menu {
  position: absolute;
  left: 0;
  bottom: calc(100% + 7px);
  z-index: 8;
  width: max-content;
  min-width: max(180px, 100%);
  max-width: min(280px, calc(100vw - 32px));
  max-height: min(420px, 65vh);
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-elevated, var(--ch-color-surface));
  box-shadow: var(--ch-shadow-md, 0 10px 30px rgb(0 0 0 / 26%));
}

.composer-mode-search {
  flex: 0 0 auto;
  padding: 6px 8px;
  border-bottom: 1px solid var(--ch-color-border-strong);
}

.composer-mode-search-input {
  width: 100%;
  min-height: 28px;
  padding: 4px 8px;
  font-size: 12px;
}

.composer-mode-list {
  flex: 1 1 auto;
  min-height: 0;
  padding: 4px;
  overflow-y: auto;
}

.composer-model-menu {
  width: min(560px, calc(100vw - 32px));
  max-width: min(560px, calc(100vw - 32px));
}

.composer-model-columns {
  display: grid;
  grid-template-columns: minmax(190px, 1.25fr) minmax(160px, 1fr);
  min-height: 0;
  overflow: hidden;
}

.composer-model-list {
  border-right: 1px solid var(--ch-color-border-strong);
}

.composer-effort-list {
  min-height: 0;
  padding: 4px;
  overflow-y: auto;
}

.composer-effort-heading {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 7px 9px 8px;
  color: var(--ch-color-text);
  font-size: 12px;
}

.composer-effort-heading span {
  color: var(--ch-color-text-subtle);
  font-size: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.composer-mode-empty {
  padding: 10px 8px;
  color: var(--ch-color-text-muted);
  font-size: 12px;
  text-align: center;
}

.composer-mode-custom {
  flex: 0 0 auto;
  display: flex;
  padding: 6px 8px;
  border-top: 1px solid var(--ch-color-border-strong);
}

.composer-mode-custom .composer-textarea {
  flex: 1 1 auto;
  min-height: 28px;
  padding: 4px 8px;
  font-size: 12px;
}

.composer-mode-item-label {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-mode-menu-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  min-height: 34px;
  padding: 6px 9px;
  border: 0;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text-muted);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.composer-mode-menu-item:hover,
.composer-mode-menu-item:focus-visible {
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control-hover);
}

.composer-mode-menu-item[aria-checked='true'] {
  color: var(--ch-color-accent);
}

.composer-mode-check {
  flex: 0 0 auto;
  font-size: 11px;
}

.composer-mode-error {
  margin: 0 0 7px;
  color: var(--ch-color-danger-strong, #f85149);
  font-size: 11px;
  line-height: 1.35;
}

.composer-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}

.attachment-chip {
  position: relative;
  width: 48px;
  height: 48px;
  border-radius: var(--ch-radius-sm);
  overflow: hidden;
  border: 1px solid var(--ch-color-border);
}

.attachment-thumb {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.attachment-remove {
  position: absolute;
  top: -2px;
  right: -2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background-color: var(--ch-color-danger-strong, #f85149);
  color: #fff;
  border: none;
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.composer-error {
  font-size: 12px;
  color: var(--ch-color-danger-strong, #f85149);
  margin-bottom: 6px;
}

.composer-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.composer-file-input {
  display: none;
}

.composer-textarea {
  flex: 1 1 100%;
  min-width: 0;
  min-height: 32px;
  max-height: 240px;
  resize: none;
  overflow-y: hidden;
  padding: 7px 10px;
  font-size: 13px;
  line-height: 1.4;
  color: var(--ch-color-text);
  background-color: var(--ch-color-app-bg);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  outline: none;
  font-family: inherit;
}

.composer-textarea:focus-visible {
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 2px var(--ch-color-accent-ring);
}

.composer-textarea:disabled {
  opacity: 0.6;
}

.composer-send-btn {
  height: 32px;
  padding: 0 14px;
  font-size: 13px;
  font-weight: 500;
  color: #fff;
  background-color: var(--ch-color-accent);
  border: none;
  border-radius: var(--ch-radius-md);
  cursor: pointer;
  flex-shrink: 0;
}

.composer-send-btn:hover:not(:disabled) {
  background-color: var(--ch-color-accent-hover);
}

.composer-send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.composer-send-btn:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 1px;
}

.composer-stop-btn {
  height: 32px;
  padding: 0 12px;
  font-size: 13px;
  font-weight: 500;
  color: var(--ch-color-text);
  background-color: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  cursor: pointer;
  flex-shrink: 0;
}

.composer-stop-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.composer-queue {
  padding: 4px 10px 0;
  font-size: 12px;
  color: var(--ch-color-text-muted);
}

.composer-hints {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  padding: 6px 10px 0;
  font-size: 11px;
  color: var(--ch-color-text-subtle);
}

.approval-card {
  flex: 1;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
}

.approval-card--resolved {
  opacity: 0.75;
}

.approval-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.approval-card-title {
  font-weight: 600;
}

.approval-card-badge {
  font-size: 11px;
  color: var(--ch-color-text-muted);
}

.approval-question + .approval-question {
  margin-top: 10px;
}

.approval-question-prompt {
  margin-bottom: 6px;
  font-weight: 500;
}

.approval-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

/* Free-text answer: the listed options are the agent's guess at the answer,
   not the whole space of them. Dashed rather than solid so it reads as one
   more choice in the same set rather than a separate form. */
.approval-custom-answer {
  width: 100%;
  margin-top: 6px;
  padding: 6px 10px;
  border: 1px dashed var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 13px;
}

.approval-custom-answer:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-style: solid;
}

.approval-custom-answer:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.approval-option {
  display: inline-flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--ch-color-text);
  background: var(--ch-color-app-bg);
  border: 1px solid var(--ch-color-border);
  border-radius: 999px;
  cursor: pointer;
}

.approval-option small {
  color: var(--ch-color-text-muted);
  text-align: left;
}

.approval-option--selected {
  border-color: var(--ch-color-accent);
  background: color-mix(in srgb, var(--ch-color-accent) 12%, transparent);
}

.approval-option:disabled {
  cursor: default;
}

.approval-submit-btn {
  margin-top: 10px;
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 500;
  color: #fff;
  background: var(--ch-color-accent);
  border: none;
  border-radius: var(--ch-radius-md);
  cursor: pointer;
}

.approval-submit-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Paseo conversation presentation ------------------------------------------------
   The initial implementation was intentionally semantic but visually read like
   terminal lines.  Keep the same stream model while giving its two peers a
   deliberate, readable conversation surface. */
.structured-pane {
  background:
    radial-gradient(circle at 50% -22%, var(--ch-color-surface-soft), transparent 44%),
    var(--ch-color-app-bg);
}

.structured-timeline {
  padding: 34px 28px 26px;
}

.structured-timeline-content {
  width: 100%;
  max-width: 860px;
  min-height: 100%;
  margin: 0 auto;
}

.structured-empty {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 24px;
}

.structured-empty strong {
  color: var(--ch-color-text-muted);
  font-weight: 500;
}

.structured-empty p {
  max-width: 340px;
  margin: 0;
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  line-height: 1.5;
}

.empty-orbit {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 4px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  color: var(--ch-color-accent);
  box-shadow: 0 8px 20px var(--ch-shadow-color-soft);
}

.structured-turn {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 22px;
}

.conversation-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  min-width: 0;
}

.conversation-row--user {
  justify-content: flex-end;
}

.conversation-bubble {
  max-width: min(85%, 680px);
  min-width: 0;
  padding: 9px 12px;
  border-radius: var(--ch-radius-md);
  overflow-wrap: anywhere;
}

.conversation-bubble--user {
  --paseo-user-bubble: #3268a8;

  position: relative;
  background: var(--paseo-user-bubble);
  color: #fff;
  border-bottom-right-radius: var(--ch-radius-sm);
}

.conversation-bubble--user :deep(.markdown-content),
.conversation-bubble--user :deep(.markdown-content :where(h1, h2, h3, h4, a, blockquote)) {
  color: inherit;
}

.turn-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

.turn-attachment-button {
  width: clamp(72px, 8vw, 88px);
  aspect-ratio: 4 / 3;
  display: block;
  padding: 0;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, #fff 34%, transparent);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  box-shadow: 0 2px 8px rgb(0 0 0 / 18%);
  cursor: zoom-in;
}

.turn-attachment-button:hover {
  border-color: color-mix(in srgb, #fff 70%, transparent);
}

.turn-attachment-button:focus-visible {
  outline: 2px solid #fff;
  outline-offset: 2px;
}

.turn-attachment-img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
  transition: transform 140ms ease;
}

.turn-attachment-button:hover .turn-attachment-img {
  transform: scale(1.025);
}

.turn-attachment-placeholder {
  width: clamp(72px, 8vw, 88px);
  aspect-ratio: 4 / 3;
  display: grid;
  place-items: center;
  padding: 10px;
  border: 1px dashed color-mix(in srgb, #fff 36%, transparent);
  border-radius: var(--ch-radius-sm);
  color: color-mix(in srgb, #fff 78%, transparent);
  font-size: 11px;
  text-align: center;
}

/* ---- Edit-resend UI ---- */

/* Lives in the message's action row, so showing and hiding come from
   ``.turn-actions`` and the button never needs positioning of its own. */
.edit-resend-hover-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border: 1px solid color-mix(in srgb, currentColor 22%, transparent);
  border-radius: 999px;
  background: var(--ch-color-bg-elevated, canvas);
  color: var(--ch-color-text-subtle, currentColor);
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.6;
  cursor: pointer;
}

.edit-resend-hover-btn:hover:not(:disabled) {
  border-color: var(--ch-color-accent);
  color: var(--ch-color-accent);
}

/* A turn is running, so the click does nothing. Say so: without this the
   button looked identical to its enabled self and simply ignored clicks. */
.edit-resend-hover-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Inline editing: the message itself becomes the input. The form used to force
   ``min-width: 320px`` and draw a bordered dark box inside the blue bubble, so
   a two-character message opened a wide bubble holding an empty-looking form.
   Now the bubble sizes to its content and the textarea reads as the message. */
.edit-resend-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: min(240px, 55vw);
}

.edit-resend-textarea {
  width: 100%;
  padding: 0;
  border: none;
  border-radius: 0;
  background: none;
  color: inherit;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.5;
  resize: vertical;
  outline: none;
}

.edit-resend-textarea:focus-visible {
  /* 2px at 65%: the earlier 1px/45% ring measured 2.43:1 against the bubble
     blue, under the 3:1 the focus indicator needs — weaker than the border it
     replaced. */
  outline: 2px solid color-mix(in srgb, #fff 65%, transparent);
  outline-offset: 3px;
  border-radius: 2px;
}

.edit-resend-textarea::placeholder {
  color: color-mix(in srgb, #fff 50%, transparent);
}

.edit-resend-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

/* Read-only preserved-attachment thumbnails inside the edit form.  The base
   .turn-attachment-img fills its container; in the edit form there is no
   fixed-size button wrapper, so bound it to the same thumbnail size. */
.edit-resend-attachments .turn-attachment-img {
  width: clamp(72px, 8vw, 88px);
  aspect-ratio: 4 / 3;
  border: 1px solid color-mix(in srgb, #fff 34%, transparent);
  border-radius: var(--ch-radius-sm);
}

.edit-resend-btn {
  padding: 5px 14px;
  border: 1px solid color-mix(in srgb, #fff 30%, transparent);
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: #fff;
  font-size: 12px;
  cursor: pointer;
  transition: background 120ms ease;
}

.edit-resend-btn:hover:not(:disabled) {
  background: rgb(255 255 255 / 12%);
}

.edit-resend-btn--primary {
  background: rgb(255 255 255 / 18%);
  border-color: color-mix(in srgb, #fff 45%, transparent);
}

.edit-resend-btn--primary:hover:not(:disabled) {
  background: rgb(255 255 255 / 28%);
}

.edit-resend-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.edit-resend-error {
  color: #ffb4b4;
  font-size: 12px;
}

.structured-image-lightbox {
  position: fixed;
  inset: 0;
  z-index: 1600;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgb(0 0 0 / 78%);
  backdrop-filter: blur(4px);
}

.structured-image-lightbox-img {
  max-width: min(1200px, calc(100vw - 48px));
  max-height: calc(100dvh - 48px);
  width: auto;
  height: auto;
  object-fit: contain;
  border-radius: var(--ch-radius-sm);
  box-shadow: 0 18px 64px rgb(0 0 0 / 52%);
  cursor: zoom-out;
}

.structured-image-lightbox-close {
  position: fixed;
  top: 16px;
  right: 16px;
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 1px solid rgb(255 255 255 / 28%);
  border-radius: 50%;
  background: rgb(24 24 24 / 88%);
  color: #fff;
  font-size: 24px;
  line-height: 1;
  cursor: pointer;
}

.structured-image-lightbox-close:hover,
.structured-image-lightbox-close:focus-visible {
  border-color: rgb(255 255 255 / 72%);
  background: rgb(42 42 42 / 96%);
  outline: none;
}

.conversation-bubble--assistant {
  background: var(--ch-color-surface);
  border: 1px solid var(--ch-color-border-muted);
  border-bottom-left-radius: var(--ch-radius-sm);
  box-shadow: 0 6px 18px var(--ch-shadow-color-soft);
}

.conversation-avatar {
  width: 23px;
  height: 23px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 11px;
  font-weight: 600;
}

.conversation-avatar--tool {
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  font-size: 10px;
}

.conversation-avatar--waiting {
  color: var(--ch-color-text-subtle);
  background: var(--ch-color-surface-control);
}

.agent-waiting-card {
  min-height: 34px;
  display: inline-flex;
  align-items: center;
  gap: 9px;
  padding: 7px 11px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  border-bottom-left-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-subtle);
  font-size: 12px;
}

.agent-waiting-pulse {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.agent-waiting-pulse i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentcolor;
  animation: agent-waiting-dot 1.2s ease-in-out infinite;
}

.agent-waiting-pulse i:nth-child(2) {
  animation-delay: 0.15s;
}

.agent-waiting-pulse i:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes agent-waiting-dot {
  0%,
  60%,
  100% {
    opacity: 0.35;
    transform: translateY(0);
  }

  30% {
    opacity: 1;
    transform: translateY(-2px);
  }
}

.thinking-card {
  align-self: flex-start;
  width: min(85%, 680px);
  border: 1px dashed var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-muted);
  padding: 7px 10px;
}

.thinking-card summary,
.tool-card summary {
  cursor: pointer;
  list-style: none;
}

.thinking-card summary::-webkit-details-marker,
.tool-card summary::-webkit-details-marker {
  display: none;
}

/* A long thinking or tool card scrolls its own header out of view, leaving no
   way to collapse it without scrolling back to the top. Pinning the summary to
   the top of the timeline keeps the toggle reachable at any depth. The
   background must be opaque and match its card, or the body shows through as it
   passes underneath; the thinking card's summary is pulled out to the card's
   edges for the same reason. */
.thinking-card summary,
.tool-card summary {
  position: sticky;
  top: 0;
  z-index: 1;
}

.thinking-card summary {
  margin: -7px -10px 0;
  padding: 7px 10px;
  background: var(--ch-color-surface-soft);
}

.tool-card summary {
  background: var(--ch-color-surface);
}

/* Footer escape hatch for a card whose body is taller than the viewport, so it
   can be re-folded without scrolling back up to its summary. */
.details-collapse {
  display: block;
  width: 100%;
  margin-top: 8px;
  padding: 5px 0;
  border: none;
  border-top: 1px solid var(--ch-color-border-muted);
  background: none;
  color: var(--ch-color-text-subtle);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.details-collapse:hover {
  color: var(--ch-color-text-muted);
}

.details-collapse:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

/* The working process of a finished turn: a single line standing in for the
   thinking, tool calls, and narration that produced the answer. It is both the
   collapsed summary and the toggle for the detail beneath it, so it never
   moves when the detail opens. */
.process-fold {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  align-self: flex-start;
  max-width: 100%;
  margin: 2px 0;
  padding: 5px 10px;
  border: 1px dashed var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  color: var(--ch-color-text-subtle);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}

.process-fold:hover {
  color: var(--ch-color-text-muted);
}

.process-fold:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
}

/* Open, the header pins to the top of the timeline for the same reason the
   thinking and tool summaries do: a working region can be taller than the
   viewport, and the toggle must stay reachable at any depth. */
.process-fold--open {
  position: sticky;
  top: 0;
  z-index: 1;
  border-style: solid;
}

.process-fold-chevron {
  flex-shrink: 0;
  font-size: 10px;
}

.process-fold-meta {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.thinking-card summary {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  font-weight: 600;
}

.thinking-card summary:focus-visible,
.tool-card summary:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring);
  outline-offset: 2px;
  border-radius: var(--ch-radius-sm);
}

.thinking-indicator {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ch-color-text-subtle);
}

.thinking-body {
  margin: 8px 0 0;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.5;
  color: var(--ch-color-text-muted);
  white-space: pre-wrap;
  word-break: break-word;
}

.tool-card {
  width: min(85%, 680px);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  /* ``clip`` rather than ``hidden``: both keep child backgrounds inside the
     rounded corners, but ``hidden`` would make this card a scroll container and
     silently stop its summary from sticking to the timeline's scrollport. */
  overflow: clip;
}

.tool-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 10px;
}

.tool-name {
  flex-shrink: 0;
  min-width: 0;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 600;
}

/* Single-tool group header: let the name shrink and ellipsize so an
   ultra-long tool name never clips the status badge. Multi-tool headers
   keep flex-shrink: 0 on the "N tools" label and ellipsize the names string
   instead. */
.tool-header--single .tool-name {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tool-status {
  flex: 0 0 auto;
  padding: 2px 8px;
}

.tool-block {
  padding: 0 10px 10px;
}

.tool-block > span {
  display: block;
  color: var(--ch-color-text-subtle);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.tool-block pre {
  max-height: 220px;
  margin: 5px 0 0;
  padding: 8px;
  overflow: auto;
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-canvas);
  color: var(--ch-color-text-code);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

.tool-group-names {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text-subtle);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-group-item {
  padding: 8px 10px;
  border-top: 1px solid var(--ch-color-border-muted);
}

.tool-group-item:first-child {
  border-top: 0;
}

.tool-group-item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 6px;
}

.tool-group-item-header .tool-name {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 12px;
}

.tool-group-item .tool-block {
  padding: 0;
}

.event-error {
  align-self: flex-start;
  width: min(85%, 680px);
  margin: 0;
}

.event-status {
  justify-content: center;
  font-style: italic;
  text-align: center;
}

.structured-composer {
  padding: 12px 28px;
  background: color-mix(in srgb, var(--ch-color-surface) 94%, transparent);
}

.composer-shell {
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
}

.composer-row {
  padding: 5px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: calc(var(--ch-radius-md) + 2px);
  background: var(--ch-color-app-bg);
  box-shadow: 0 8px 24px var(--ch-shadow-color-soft);
}

.composer-row:focus-within {
  border-color: var(--ch-color-accent);
}

.composer-textarea {
  min-height: 34px;
  padding: 7px 6px;
  border: 0;
  background: transparent;
}

.composer-textarea:focus-visible {
  border-color: transparent;
  box-shadow: none;
}

.composer-send-btn {
  height: 34px;
  border-radius: var(--ch-radius-sm);
}

/* Drag-over highlight */
.structured-pane.is-dragging .structured-timeline {
  outline: 2px dashed var(--ch-color-accent);
  outline-offset: -4px;
}

/* Narrow viewport: tighten composer and timeline padding */
@media (max-width: 640px) {
  .structured-timeline {
    padding: 18px 12px;
  }

  .structured-composer {
    padding: 8px 12px;
  }

  .composer-row {
    gap: 4px;
  }

  .composer-tools {
    gap: 2px;
  }

  .composer-mode-trigger {
    min-height: 44px;
    height: 44px;
  }

  .composer-mode-trigger {
    max-width: 170px;
    padding: 0 7px;
  }

  .composer-mode-menu {
    max-width: min(220px, calc(100vw - 24px));
  }

  .composer-model-menu {
    width: min(320px, calc(100vw - 24px));
    max-width: min(320px, calc(100vw - 24px));
  }

  .composer-model-columns {
    grid-template-columns: 1fr;
    overflow-y: auto;
  }

  .composer-model-list {
    max-height: 210px;
    border-right: 0;
    border-bottom: 1px solid var(--ch-color-border-strong);
  }

  .composer-mode-menu-item {
    min-height: 44px;
  }

  .composer-send-btn {
    padding: 0 10px;
  }

  .attachment-chip {
    width: 40px;
    height: 40px;
  }

  .conversation-bubble,
  .thinking-card,
  .tool-card,
  .event-error {
    width: min(92%, 680px);
    max-width: 92%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .agent-waiting-pulse i,
  .banner-spinner,
  .thinking-indicator {
    animation: none;
  }

  .turn-attachment-img {
    transition: none;
  }
}
</style>
