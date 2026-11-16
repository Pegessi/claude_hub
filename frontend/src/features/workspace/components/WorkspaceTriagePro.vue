<template>
  <div class="tp-shell">
    <div
      v-if="!activeWorkspaceId && scopeMode !== 'all'"
      class="tp-empty-workspace"
    >
      <div class="tp-empty-card">
        <h2>No Workspace Selected</h2>
        <p>Create or select a Git repository workspace above to triage tasks and review agent runs.</p>
      </div>
    </div>

    <div
      v-else
      class="tp-linear"
    >
      <!-- ==================== LEFT TRIAGE RAIL ==================== -->
      <aside
        class="tp-rail"
        aria-label="Workspace tasks"
      >
        <div
          v-if="workspaces.length > 1"
          class="tp-scope-bar"
          role="group"
          aria-label="Workspace scope"
        >
          <button
            type="button"
            :class="['tp-scope-btn', { active: scopeMode === 'current' }]"
            :aria-pressed="scopeMode === 'current'"
            @click="setScopeMode('current')"
          >
            {{ activeWorkspace?.name || 'Current' }}
          </button>
          <button
            type="button"
            :class="['tp-scope-btn', { active: scopeMode === 'all' }]"
            :aria-pressed="scopeMode === 'all'"
            @click="setScopeMode('all')"
          >
            <span>All Workspaces</span>
            <span
              v-if="overviewAttentionTotal > 0"
              class="tp-scope-badge tp-num"
            >{{ overviewAttentionTotal }}</span>
          </button>
        </div>

        <div
          v-if="(scopeMode === 'all' ? overviewLoading : boardLoading) && triageItems.length === 0"
          class="tp-rail-loading"
          role="status"
          aria-live="polite"
        >
          Loading tasks…
        </div>

        <div
          v-else-if="triageItems.length === 0"
          class="tp-rail-empty"
        >
          <p>
            No tasks in
            {{ scopeMode === 'all' ? 'any workspace' : activeWorkspace?.name || 'this workspace' }}
            yet.
          </p>
          <button
            v-if="activeWorkspaceId"
            type="button"
            class="tp-primary-btn"
            @click="openTaskModal"
          >
            Add Task
          </button>
        </div>

        <div
          v-else
          class="tp-groups"
        >
          <section
            v-for="group in groupedTasks"
            :key="group.key"
            class="tp-group"
          >
            <header class="tp-group-header">
              <span>{{ group.label }}</span>
              <span class="tp-num">{{ group.items.length }}</span>
            </header>

            <div
              v-if="group.items.length === 0"
              class="tp-group-empty"
            >
              No {{ group.label.toLowerCase() }} tasks
            </div>

            <button
              v-for="item in group.items"
              :key="item.id"
              type="button"
              :class="['tp-row', { active: activeItem?.id === item.id }]"
              :aria-current="activeItem?.id === item.id ? 'true' : undefined"
              :data-task-id="item.id"
              @click="selectTriageItem(item)"
            >
              <div class="tp-row-top">
                <span :class="['tp-dot', `tp-dot--${item.statusTone}`]" />
                <span class="tp-row-code tp-mono">{{ item.code }}</span>
                <span
                  v-if="scopeMode === 'all'"
                  class="tp-row-ws tp-mono"
                >{{ item.workspaceName }}</span>
                <span :class="['tp-row-state', `tp-row-state--${item.statusTone}`]">
                  {{ item.statusLabel }}
                </span>
                <span class="tp-row-elapsed tp-num">{{ item.elapsedLabel }}</span>
              </div>
              <div class="tp-row-title">
                {{ item.title }}
              </div>
            </button>
          </section>

          <div
            v-if="scopeMode === 'current' && boardTasksPagination && boardOlderTasksRemaining > 0"
            class="tp-load-more-wrap"
          >
            <button
              type="button"
              class="tp-ghost-btn tp-load-more-btn"
              :disabled="boardLoadMoreLoading"
              @click="handleLoadOlderTasks"
            >
              {{
                boardLoadMoreLoading
                  ? 'Loading older…'
                  : `Load older done (${boardOlderTasksRemaining} more)`
              }}
            </button>
            <span
              v-if="boardLoadMoreError"
              class="tp-error-inline"
              role="alert"
            >
              {{ boardLoadMoreError }}
            </span>
          </div>
        </div>
      </aside>

      <!-- ==================== RIGHT UNIFIED DETAIL CANVAS ==================== -->
      <main
        class="tp-detail"
        aria-label="Selected task detail"
      >
        <div
          v-if="!activeItem"
          class="tp-detail-empty"
        >
          <p>Select a task on the left or create a new task to inspect its summary and deliverables.</p>
        </div>

        <div
          v-else
          class="tp-editorial"
        >
          <!-- Top Bar: Identity + AI Handoff Utilities -->
          <div class="tp-top-bar">
            <div class="tp-meta-line">
              <span :class="['tp-status-pill', `tp-status-pill--${activeItem.statusTone}`]">
                <i :class="['tp-dot', `tp-dot--${activeItem.statusTone}`]" />
                {{ activeItem.statusLabel }}
              </span>
              <span class="tp-mono tp-meta-code">{{ activeItem.code }}</span>
              <span class="tp-sep">·</span>
              <span class="tp-mono">{{ activeItem.workspaceName }}</span>
              <span class="tp-sep">·</span>
              <span class="tp-branch-chip tp-mono">{{ activeItem.branchLabel }}</span>
              <span
                v-if="activeItem.reviewSummaryChip"
                class="tp-review-chip tp-mono"
              >
                {{ activeItem.reviewSummaryChip }}
              </span>
            </div>

            <div class="tp-handoff-tools">
              <button
                type="button"
                class="tp-ghost-btn tp-mono"
                :disabled="briefCopying"
                @click="copyAiTaskBrief(activeItem)"
              >
                {{ copiedKey === `brief:${activeItem.id}` ? 'Brief copied' : 'Copy AI Brief' }}
              </button>
              <LoadingButton
                type="button"
                class="tp-ghost-btn"
                :loading="handoffChatLaunching"
                loading-label="Opening…"
                @click="continueInNewChat(activeItem)"
              >
                Continue in New Chat ↗
              </LoadingButton>
            </div>
          </div>

          <!-- Title + At-a-Glance Macro Stage Strip -->
          <div class="tp-heading-block">
            <h1 class="tp-title">
              {{ activeItem.title }}
            </h1>

            <div
              class="tp-stage-strip"
              aria-label="Task lifecycle stages"
            >
              <template
                v-for="(stage, idx) in activeItem.macroStages"
                :key="stage.key"
              >
                <span :class="['tp-stage-pill', `tp-stage-pill--${stage.state}`]">
                  <span v-if="stage.state === 'done'">✓</span>
                  <span v-else-if="stage.state === 'warn'">!</span>
                  <span v-else-if="stage.state === 'current'">●</span>
                  {{ stage.label }}
                </span>
                <span
                  v-if="idx < activeItem.macroStages.length - 1"
                  class="tp-stage-arrow"
                  aria-hidden="true"
                >→</span>
              </template>

              <span class="tp-stage-elapsed tp-mono tp-num">
                <template v-if="activeItem.updatedAgoLabel">{{ activeItem.updatedAgoLabel }} · </template>
                {{ activeItem.elapsedLabel }}
              </span>
            </div>
          </div>

          <!-- Action / Decision Gate (Supports BOTH Managed & Initiator Tasks!) -->
          <section
            :class="['tp-gate-card', { 'tp-gate-card--attention': activeItem.bucket === 'needs_you' }]"
            aria-label="Task actions and decision gate"
          >
            <div class="tp-spec-gate">
              <div class="tp-spec-gate-left">
                <span class="tp-gate-eyebrow">
                  {{
                    activeItem.bucket === 'needs_you'
                      ? 'Decision needed'
                      : activeItem.bucket === 'review'
                        ? 'Ready for review'
                        : activeItem.bucket === 'done'
                          ? 'Completed'
                          : 'Current status'
                  }}
                </span>
                <span class="tp-decision-copy">
                  {{ gateBannerCopy }}
                </span>
              </div>

              <div class="tp-decision-btns">
                <!-- Workspace-controlled actions -->
                <template v-if="canUseManagedTaskActions(activeItem.task)">
                  <LoadingButton
                    v-if="activeItem.task.status === 'todo'"
                    type="button"
                    class="tp-primary-btn"
                    :loading="isPending(taskActionKey('start', activeItem.id))"
                    loading-label="Starting…"
                    @click="startTask(activeItem.task)"
                  >
                    Start Task
                  </LoadingButton>

                  <button
                    v-if="selectedSessionForActive"
                    type="button"
                    class="tp-ghost-btn"
                    @click="toggleSteerComposer"
                  >
                    {{ isSteerOpen ? 'Close steer' : 'Steer…' }}
                  </button>

                  <LoadingButton
                    v-if="canRequestReviewTask(activeItem.task)"
                    type="button"
                    class="tp-ghost-btn"
                    :loading="isPending(taskActionKey('request-review', activeItem.id))"
                    loading-label="Requesting…"
                    @click="requestReview(activeItem.task)"
                  >
                    Request review
                  </LoadingButton>

                  <LoadingButton
                    v-if="canMarkDoneTask(activeItem.task)"
                    type="button"
                    class="tp-primary-btn"
                    :loading="isPending(taskActionKey('mark-done', activeItem.id))"
                    loading-label="Accepting…"
                    @click="acceptAndCleanupTask(activeItem.task)"
                  >
                    Accept
                  </LoadingButton>

                  <LoadingButton
                    v-if="activeItem.bucket === 'done' && selectedSessionForActive?.ephemeral"
                    type="button"
                    class="tp-ghost-btn"
                    :loading="isPending(taskActionKey('cleanup-session', activeItem.id))"
                    loading-label="Releasing…"
                    @click="cleanupCompletedTaskSession(activeItem.task)"
                  >
                    Release session
                  </LoadingButton>
                </template>

                <!-- Initiator-controlled actions: Inline Reply + One-click Mark Done -->
                <template v-else-if="isInitiatorTask(activeItem.task)">
                  <button
                    v-if="activeItem.task.source?.tab_id && activeItem.bucket !== 'done'"
                    type="button"
                    class="tp-ghost-btn"
                    @click="toggleInitiatorReply"
                  >
                    {{ isInitiatorReplyOpen ? 'Close reply' : 'Reply inline…' }}
                  </button>

                  <LoadingButton
                    v-if="canMarkInitiatorDone(activeItem.task)"
                    type="button"
                    :class="activeItem.statusLabel === 'Session lost' ? 'tp-primary-btn' : 'tp-ghost-btn'"
                    :loading="initiatorCloseLoading"
                    loading-label="Closing…"
                    @click="markInitiatorTaskDone(activeItem)"
                  >
                    Mark Done
                  </LoadingButton>
                </template>

                <button
                  v-if="selectedSessionForActive"
                  type="button"
                  class="tp-ghost-btn"
                  :disabled="isPending(sessionActionKey('open', selectedSessionForActive.id))"
                  @click="openSession(selectedSessionForActive)"
                >
                  Open session ↗
                </button>

                <button
                  v-else-if="activeItem.task.source?.tab_id"
                  type="button"
                  :class="
                    activeItem.bucket === 'needs_you' && activeItem.statusLabel !== 'Session lost'
                      ? 'tp-primary-btn'
                      : 'tp-ghost-btn'
                  "
                  @click="openOrRestoreTab(activeItem.task.source.tab_id)"
                >
                  Open Chat ↗
                </button>
              </div>
            </div>

            <div
              v-if="gateActionNotice"
              :class="['tp-gate-notice', `tp-gate-notice--${gateActionNotice.tone}`]"
              :role="gateActionNotice.tone === 'error' ? 'alert' : 'status'"
            >
              {{ gateActionNotice.text }}
            </div>

            <!-- Managed Session Steer Form -->
            <form
              v-if="isSteerOpen && selectedSessionForActive && canUseManagedTaskActions(activeItem.task)"
              class="tp-steer-form"
              @submit.prevent="submitSteer"
              @paste="handleSteerPaste"
            >
              <textarea
                ref="steerInputRef"
                v-model="detailMessage"
                rows="2"
                placeholder="Send steering instructions or revision feedback to the agent…"
              />
              <div
                v-if="detailAttachments.length > 0"
                class="tp-steer-attachments"
              >
                <span
                  v-for="att in detailAttachments"
                  :key="att.id"
                  class="tp-mono tp-att-chip"
                >
                  {{ att.filename }} ({{ formatAttachmentSize(att.size_bytes) }})
                  <button
                    type="button"
                    aria-label="Remove attachment"
                    @click="removeDraftAttachment(detailAttachments, att)"
                  >×</button>
                </span>
              </div>
              <div class="tp-steer-actions">
                <span class="tp-steer-hint tp-mono">
                  {{
                    activeItem.task.status === 'review' || activeItem.task.status === 'failed'
                      ? 'Continues task from review/failed'
                      : 'Sends live follow-up to active session'
                  }}
                </span>
                <LoadingButton
                  type="submit"
                  class="tp-primary-btn"
                  :disabled="!detailMessage.trim() && detailAttachments.length === 0"
                  :loading="isPending(selectedTaskSendKey)"
                  loading-label="Sending…"
                >
                  Send instructions
                </LoadingButton>
              </div>
            </form>

            <!-- Initiator Chat Inline Reply Form -->
            <form
              v-if="isInitiatorReplyOpen && activeItem.task.source?.tab_id"
              class="tp-steer-form"
              @submit.prevent="submitInitiatorReply(activeItem)"
            >
              <textarea
                ref="initiatorReplyInputRef"
                v-model="initiatorReplyText"
                rows="2"
                placeholder="Reply directly to the originating Chat without leaving the board…"
              />
              <div class="tp-steer-actions">
                <span class="tp-steer-hint tp-mono">
                  Restores archived Chat automatically if needed, then delivers your reply
                </span>
                <LoadingButton
                  type="submit"
                  class="tp-primary-btn"
                  :disabled="!initiatorReplyText.trim()"
                  :loading="initiatorReplySending"
                  loading-label="Sending…"
                >
                  Send reply
                </LoadingButton>
              </div>
            </form>
          </section>

          <!-- 1. Summary & Key Takeaways -->
          <section
            class="tp-card"
            aria-label="Summary and key takeaways"
          >
            <div class="tp-section-head">
              <h2 class="tp-micro-heading">
                Summary &amp; Key Takeaways
              </h2>
              <button
                v-if="activeItem.digest.rawText && activeItem.digest.bullets.length > 0"
                type="button"
                class="tp-inline-link tp-mono"
                @click="showRawDigest = !showRawDigest"
              >
                {{ showRawDigest ? 'Structured view' : 'Raw text' }}
              </button>
            </div>

            <pre
              v-if="showRawDigest && activeItem.digest.rawText"
              class="tp-raw-summary"
            >{{ activeItem.digest.rawText }}</pre>

            <template v-else>
              <p class="tp-headline">
                {{ activeItem.digest.headline }}
              </p>
              <ul
                v-if="activeItem.digest.bullets.length > 0"
                class="tp-bullets"
              >
                <li
                  v-for="(bullet, idx) in activeItem.digest.bullets"
                  :key="idx"
                >
                  <span
                    v-if="bullet.label"
                    class="tp-bullet-label"
                  >{{ bullet.label }}</span>
                  <span>{{ bullet.text }}</span>
                </li>
              </ul>
            </template>
          </section>

          <!-- 2. Deliverables Card (Unified with Inline Markdown Preview + Modal + Copy) -->
          <section
            v-if="hasDeliverables"
            class="tp-card"
            aria-label="Deliverables"
          >
            <div class="tp-section-head">
              <h2 class="tp-micro-heading">
                Deliverables ({{ activeItem.deliverables.length + imageAttachments.length }})
              </h2>
            </div>

            <div class="tp-deliverables">
              <template
                v-for="deliv in activeItem.deliverables"
                :key="deliv.key"
              >
                <a
                  v-if="deliv.url"
                  :href="deliv.url"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="tp-deliverable-item"
                >
                  <div class="tp-deliv-main">
                    <div class="tp-deliv-title-row">
                      <span :class="['tp-deliv-kind', `tp-deliv-kind--${deliv.kind}`]">{{ deliv.kind }}</span>
                      <span class="tp-deliv-title">{{ deliv.title }}</span>
                    </div>
                    <span class="tp-deliv-sub tp-mono">{{ deliv.subtitle }}</span>
                  </div>
                  <span class="tp-deliv-actions tp-mono">Open ↗</span>
                </a>

                <div
                  v-else
                  class="tp-deliverable-wrap"
                >
                  <div class="tp-deliverable-item">
                    <div class="tp-deliv-main">
                      <div class="tp-deliv-title-row">
                        <span :class="['tp-deliv-kind', `tp-deliv-kind--${deliv.kind}`]">
                          {{ deliv.kind === 'markdown' ? 'md' : deliv.kind }}
                        </span>
                        <span class="tp-deliv-title tp-mono">{{ deliv.title }}</span>
                      </div>
                      <span class="tp-deliv-sub tp-mono">{{ deliv.subtitle }}</span>
                    </div>

                    <div class="tp-deliv-actions">
                      <button
                        v-if="deliv.kind === 'markdown' && deliv.artifactPath"
                        type="button"
                        class="tp-mini-btn tp-mini-btn--accent"
                        @click="toggleInlineMarkdown(deliv)"
                      >
                        {{ inlineMdKey === deliv.key ? 'Hide' : 'Inline' }}
                      </button>
                      <button
                        v-if="deliv.kind === 'markdown' && deliv.artifactPath"
                        type="button"
                        class="tp-mini-btn"
                        @click="openDeliverableModal(deliv)"
                      >
                        Preview
                      </button>
                      <button
                        v-if="deliv.copyText || deliv.artifactPath"
                        type="button"
                        class="tp-mini-btn tp-mono"
                        @click="copyDeliverableText(deliv.key, deliv.copyText || deliv.artifactPath || '')"
                      >
                        {{ copiedKey === deliv.key ? 'Copied' : 'Copy' }}
                      </button>
                    </div>
                  </div>

                  <div
                    v-if="inlineMdKey === deliv.key"
                    class="tp-inline-md-box"
                  >
                    <div class="tp-inline-md-head">
                      <span class="tp-mono">{{ deliv.artifactPath }}</span>
                      <button
                        v-if="deliv.artifactPath"
                        type="button"
                        class="tp-inline-link tp-mono"
                        @click="openDeliverableModal(deliv)"
                      >
                        Full reader ↗
                      </button>
                    </div>
                    <div
                      v-if="inlineMdLoading"
                      class="tp-log-status tp-mono"
                    >
                      Loading Markdown document…
                    </div>
                    <div
                      v-else-if="inlineMdError"
                      class="tp-log-status tp-mono"
                    >
                      {{ inlineMdError }}
                    </div>
                    <MarkdownContent
                      v-else-if="inlineMdContent"
                      link-markdown-paths
                      :text="inlineMdContent"
                      @markdown-path-click="path => openMarkdownPreviewModal(path)"
                    />
                  </div>
                </div>
              </template>

              <div
                v-for="img in imageAttachments"
                :key="img.id"
                class="tp-deliverable-item"
              >
                <div class="tp-deliv-main">
                  <div class="tp-deliv-title-row">
                    <span class="tp-deliv-kind">img</span>
                    <span class="tp-deliv-title tp-mono">{{ img.filename }}</span>
                  </div>
                </div>
                <div class="tp-deliv-actions">
                  <button
                    type="button"
                    class="tp-mini-btn"
                    @click="openImageLightbox(`/api/workspaces/attachments/${img.id}`, img.filename)"
                  >
                    View ↗
                  </button>
                </div>
              </div>
            </div>
          </section>

          <!-- 3. Verification & Caveats Card -->
          <section
            v-if="hasVerification"
            class="tp-card"
            aria-label="Verification and caveats"
          >
            <div class="tp-section-head">
              <h2 class="tp-micro-heading">
                Verification &amp; Caveats
              </h2>
              <span
                :class="[
                  'tp-val-badge',
                  'tp-mono',
                  activeItem.validation.failedCount > 0
                    ? 'tp-val-badge--fail'
                    : activeItem.validation.passedCount > 0
                      ? 'tp-val-badge--pass'
                      : '',
                ]"
              >
                {{ activeItem.validation.summaryLabel }}
              </span>
            </div>

            <div
              v-if="activeItem.validation.pills.length > 0"
              class="tp-pill-wrap"
            >
              <span
                v-for="(pill, idx) in activeItem.validation.pills"
                :key="idx"
                :class="['tp-check-pill', 'tp-mono', `tp-check-pill--${pill.status}`]"
              >
                <span>{{ pill.status === 'fail' ? '✕' : '✓' }}</span>
                <span>{{ pill.label }}</span>
              </span>
            </div>

            <ul
              v-if="activeItem.validation.notes.length > 0"
              class="tp-val-notes"
            >
              <li
                v-for="(note, idx) in activeItem.validation.notes"
                :key="idx"
              >
                {{ note }}
              </li>
            </ul>

            <div
              v-if="activeItem.validation.riskBullets.length > 0"
              class="tp-risk-box"
            >
              <span class="tp-risk-title">Caveats &amp; Unverified Scope</span>
              <ul class="tp-risk-list">
                <li
                  v-for="(risk, idx) in activeItem.validation.riskBullets"
                  :key="idx"
                >
                  {{ risk }}
                </li>
              </ul>
            </div>
          </section>

          <!-- 4. Adaptive Key Milestones: Expanded for Open Tasks, Folded for Done Tasks -->
          <details
            class="tp-milestones-disclosure"
            :open="milestonesExpanded"
            aria-label="Key milestones"
            @toggle="onMilestonesToggle"
          >
            <summary class="tp-milestones-summary">
              <span class="tp-micro-heading">
                Key Milestones ({{ activeItem.milestones.length }})
              </span>
              <span class="tp-mono tp-sep">
                <template v-if="eventsLoading">Syncing checkpoints…</template>
                <template v-else>{{ milestonesExpanded ? 'Collapse' : 'Expand history' }}</template>
              </span>
            </summary>

            <ol class="tp-milestone-list">
              <li
                v-for="step in activeItem.milestones"
                :key="step.id"
                :class="['tp-ms-item', `tp-ms-item--${step.tone}`]"
              >
                <div class="tp-ms-time-col tp-num">
                  <span class="tp-ms-time">{{ step.timeLabel }}</span>
                  <span
                    v-if="step.deltaLabel"
                    class="tp-ms-delta tp-mono"
                  >{{ step.deltaLabel }}</span>
                </div>

                <span class="tp-ms-track">
                  <i class="tp-ms-dot" />
                </span>

                <div class="tp-ms-body">
                  <div class="tp-ms-head">
                    <span class="tp-ms-title">{{ step.title }}</span>
                    <span
                      v-if="step.badge"
                      :class="['tp-ms-badge', 'tp-mono', `tp-ms-badge--${step.tone}`]"
                    >
                      {{ step.badge }}
                    </span>

                    <div class="tp-ms-links">
                      <button
                        v-if="step.detailText"
                        type="button"
                        class="tp-inline-link tp-mono"
                        @click="toggleStepDetail(step.id)"
                      >
                        {{
                          expandedStepIds.has(step.id)
                            ? 'Hide details'
                            : step.collapsedCount
                              ? `Details (${step.collapsedCount})`
                              : 'Details'
                        }}
                      </button>
                      <button
                        v-if="step.sessionId || step.tabId"
                        type="button"
                        class="tp-inline-link tp-mono"
                        @click="toggleTranscript(step)"
                      >
                        {{ openTranscriptNodeId === step.id ? 'Hide transcript' : 'Transcript ↗' }}
                      </button>
                    </div>
                  </div>

                  <p :class="['tp-ms-oneliner', { 'tp-ms-oneliner--echo': step.isCurrentEcho }]">
                    {{ step.oneLiner }}
                  </p>

                  <div
                    v-if="step.detailText && expandedStepIds.has(step.id)"
                    class="tp-ms-detail-box"
                  >
                    <pre class="tp-ms-detail-pre">{{ step.detailText }}</pre>
                  </div>

                  <div
                    v-if="openTranscriptNodeId === step.id"
                    class="tp-log-excerpt"
                  >
                    <div
                      v-if="transcriptLoading"
                      class="tp-log-status tp-mono"
                    >
                      Loading session stream…
                    </div>
                    <div
                      v-else-if="transcriptError"
                      class="tp-log-status tp-mono"
                    >
                      {{ transcriptError }}
                    </div>
                    <div
                      v-else-if="transcriptLines.length === 0"
                      class="tp-log-status tp-mono"
                    >
                      No persisted structured stream turns found for this session.
                    </div>
                    <template v-else>
                      <div
                        v-for="(line, idx) in transcriptLines"
                        :key="idx"
                        class="tp-log-row"
                      >
                        <span class="tp-mono tp-num">{{ line.time }}</span>
                        <span :class="['tp-mono', 'tp-log-speaker', `tp-log-speaker--${line.role}`]">
                          {{ line.speaker }}
                        </span>
                        <span>{{ line.text }}</span>
                      </div>
                    </template>

                    <div
                      v-if="step.tabId"
                      class="tp-log-footer"
                    >
                      <button
                        type="button"
                        class="tp-inline-link tp-mono"
                        @click="openOrRestoreTab(step.tabId)"
                      >
                        Open full conversation in Chat pane ↗
                      </button>
                    </div>
                  </div>
                </div>
              </li>
            </ol>
          </details>

          <!-- 5. Collapsible Raw Prompt, Execution Control & Secondary Actions -->
          <details class="tp-meta-disclosure">
            <summary>
              <span>Prompt, Execution Control &amp; Secondary Actions</span>
              <span class="tp-mono">
                control={{ activeItem.internalMeta.control }} · mode={{ activeItem.internalMeta.mode }} · seat={{ activeItem.internalMeta.seat }}
              </span>
            </summary>

            <div class="tp-disclosure-body">
              <div
                v-if="canUseManagedTaskActions(activeItem.task)"
                class="tp-secondary-actions"
              >
                <button
                  v-if="canEditTask(activeItem.task)"
                  type="button"
                  class="tp-ghost-btn"
                  @click="openEditTaskModal(activeItem.task)"
                >
                  Edit Task
                </button>
                <LoadingButton
                  v-if="canAbortTask(activeItem.task)"
                  type="button"
                  class="tp-ghost-btn tp-danger-text"
                  :loading="isPending(taskActionKey('abort', activeItem.id))"
                  loading-label="Aborting…"
                  @click="abortTask(activeItem.task)"
                >
                  Abort Task
                </LoadingButton>
                <LoadingButton
                  type="button"
                  class="tp-ghost-btn tp-danger-text"
                  :loading="isPending(taskActionKey('delete', activeItem.id))"
                  loading-label="Deleting…"
                  @click="deleteTask(activeItem.task)"
                >
                  Delete Task
                </LoadingButton>
              </div>

              <div class="tp-raw-prompt-block">
                <div class="tp-micro-heading">
                  Task Description / Brief
                </div>
                <MarkdownContent
                  class="tp-prompt-md"
                  link-markdown-paths
                  :text="activeItem.task.prompt || '(empty description)'"
                  @markdown-path-click="path => openMarkdownPreviewModal(path)"
                />
              </div>

              <TaskExecutionPanel
                :key="`${activeItem.task.workspace_id}:${activeItem.id}`"
                :workspace-id="activeItem.task.workspace_id"
                :task="activeItem.task"
                :capabilities="taskCapabilities"
                :debug-open="false"
                :show-progress="false"
              />
            </div>
          </details>
        </div>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from 'vue'
import LoadingButton from '@/components/LoadingButton.vue'
import MarkdownContent from '@/components/MarkdownContent.vue'
import TaskExecutionPanel from '@/components/TaskExecutionPanel.vue'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import type {
  AgentReport,
  AgentStreamEvent,
  ManagedSession,
  TaskLifecycle,
  Workspace,
  WorkspaceArtifactPreview,
  WorkspaceTask,
  WorkspaceTaskEvent,
} from '@/types'
import { createComposerDraftId } from '@/utils/chatComposerInteraction'
import { writeClipboard } from '@/utils/clipboard'
import {
  formatAttachmentSize,
  handleAttachmentPaste,
  removeDraftAttachment,
  resetDraftAttachments,
  type DraftAttachment,
} from '@/utils/draftAttachments'
import { parseTimestampMs } from '@/utils/duration'
import { generateTaskCallId, isWorkspaceControlledTask } from '@/utils/taskExecution'
import {
  useWorkspaceArtifactsContext,
  useWorkspaceRuntimeContext,
  useWorkspaceTaskActionsContext,
  useWorkspaceTaskPresentationContext,
} from '../context'
import {
  buildTriageTaskViewModel,
  groupTriageTasks,
  type DeliverableItem,
  type MilestoneStep,
  type TriageTaskViewModel,
} from '../presentation/triageProModel'

type TriageScopeMode = 'current' | 'all'
const SCOPE_MODE_STORAGE_KEY = 'claude-hub.triage-scope-mode.v1'

interface BoardOverviewPayload {
  workspaces: Workspace[]
  tasks: WorkspaceTask[]
  reports: AgentReport[]
  sessions: ManagedSession[]
  task_lifecycles: Record<string, TaskLifecycle>
  attention_counts: Record<string, number>
}

interface TaskBriefPayload {
  workspace_id: string
  workspace_name: string
  workspace_path: string
  task_id: string
  title: string
  markdown: string
}

function readInitialScopeMode(): TriageScopeMode {
  if (typeof window !== 'undefined') {
    const param = new URLSearchParams(window.location.search).get('ws_scope')
    if (param === 'all' || param === 'current') return param
    try {
      const saved = window.localStorage.getItem(SCOPE_MODE_STORAGE_KEY)
      if (saved === 'all' || saved === 'current') return saved
    } catch {
      // Ignore storage access errors.
    }
  }
  return 'current'
}

const scopeMode = ref<TriageScopeMode>(readInitialScopeMode())

const workspaceStore = useWorkspaceStore()
const terminalStore = useTerminalStore()
const appStore = useAppStore()

const {
  activeWorkspace,
  activeWorkspaceId,
  selectedWorkspaceId,
  workspaces,
  handleWorkspaceChange,
  board,
  boardLoading,
  tasks,
  selectedTaskId,
  isPending,
  taskActionKey,
  sessionActionKey,
  openSession,
  openTaskModal,
  boardTasksPagination,
  boardOlderTasksRemaining,
  boardLoadMoreLoading,
  handleLoadOlderTasks,
  boardLoadMoreError,
  taskCapabilities,
} = useWorkspaceRuntimeContext()

const {
  canUseManagedTaskActions,
  canEditTask,
  canMarkDoneTask,
  canRequestReviewTask,
  canAbortTask,
  selectedSession,
  selectedReports,
  selectedMarkdownDocuments,
  selectedTaskSendKey,
} = useWorkspaceTaskPresentationContext()

const {
  startTask,
  openEditTaskModal,
  acceptAndCleanupTask,
  cleanupCompletedTaskSession,
  requestReview,
  abortTask,
  deleteTask,
  sendDetailMessage,
  detailAttachments,
  detailMessage,
} = useWorkspaceTaskActionsContext()

const {
  openMarkdownPreviewModal,
  openImageLightbox,
} = useWorkspaceArtifactsContext()

const nowMs = ref(Date.now())
let clockTimer: ReturnType<typeof setInterval> | null = null
let overviewTimer: ReturnType<typeof setInterval> | null = null

const overviewData = ref<BoardOverviewPayload | null>(null)
const overviewLoading = ref(false)
let overviewEpoch = 0

async function fetchOverviewFallbackFromBoards(): Promise<BoardOverviewPayload | null> {
  const wsList = workspaces.value
  if (!wsList.length) return null
  const boards = await Promise.all(
    wsList.map(async ws => {
      try {
        const r = await fetch(
          `/api/workspaces/${encodeURIComponent(ws.id)}/board?tasks_limit=8`,
          { credentials: 'same-origin' },
        )
        return r.ok ? ((await r.json()) as Record<string, unknown>) : null
      } catch {
        return null
      }
    }),
  )
  const mergedTasks: WorkspaceTask[] = []
  const mergedReports: AgentReport[] = []
  const mergedSessions: ManagedSession[] = []
  const mergedLifecycles: Record<string, TaskLifecycle> = {}
  const attentionCounts: Record<string, number> = {}
  boards.forEach((b, idx) => {
    const ws = wsList[idx]
    if (!b || !ws) return
    const tList = Array.isArray(b.tasks) ? (b.tasks as WorkspaceTask[]) : []
    const rList = Array.isArray(b.reports) ? (b.reports as AgentReport[]) : []
    const sList = Array.isArray(b.sessions) ? (b.sessions as ManagedSession[]) : []
    const lcs = (b.task_lifecycles as Record<string, TaskLifecycle> | undefined) ?? {}
    mergedTasks.push(...tList)
    mergedReports.push(...rList)
    mergedSessions.push(...sList)
    Object.assign(mergedLifecycles, lcs)
    attentionCounts[ws.id] = Object.values(lcs).filter(lc => lc.attention).length
  })
  return {
    workspaces: wsList,
    tasks: mergedTasks,
    reports: mergedReports,
    sessions: mergedSessions,
    task_lifecycles: mergedLifecycles,
    attention_counts: attentionCounts,
  }
}

let overviewInFlight = false

async function refreshOverview() {
  if (scopeMode.value !== 'all' || overviewInFlight) return
  overviewInFlight = true
  const epoch = ++overviewEpoch
  if (!overviewData.value) overviewLoading.value = true
  try {
    const res = await fetch('/api/workspaces/board-overview?done_per_workspace=8', {
      credentials: 'same-origin',
    })
    if (epoch !== overviewEpoch) return
    if (res.ok) {
      overviewData.value = (await res.json()) as BoardOverviewPayload
      return
    }
    if (res.status === 404) {
      // Compatibility fallback when previewing against an older un-restarted backend
      const fallback = await fetchOverviewFallbackFromBoards()
      if (epoch === overviewEpoch && fallback) {
        overviewData.value = fallback
      }
    }
  } catch {
    // Ignore transient poll errors.
  } finally {
    overviewInFlight = false
    if (epoch === overviewEpoch) {
      overviewLoading.value = false
    }
  }
}

function setScopeMode(mode: TriageScopeMode) {
  if (scopeMode.value === mode) return
  scopeMode.value = mode
  try {
    window.localStorage.setItem(SCOPE_MODE_STORAGE_KEY, mode)
  } catch {
    // Ignore storage write errors.
  }
  if (mode === 'all') {
    void refreshOverview()
  }
}

const overviewAttentionTotal = computed(() => {
  if (overviewData.value?.attention_counts) {
    return Object.values(overviewData.value.attention_counts).reduce((a, b) => a + b, 0)
  }
  const lifecycles = board.value?.task_lifecycles ?? {}
  return Object.values(lifecycles).filter(lc => lc.attention).length
})

const isSteerOpen = ref(false)
const steerInputRef = ref<HTMLTextAreaElement | null>(null)
const isInitiatorReplyOpen = ref(false)
const initiatorReplyInputRef = ref<HTMLTextAreaElement | null>(null)
const initiatorReplyText = ref('')
const initiatorReplyTurnId = ref<string>('')
const initiatorReplySending = ref(false)
const initiatorCloseLoading = ref(false)
let initiatorCloseCallId: string | null = null

const briefCopying = ref(false)
const handoffChatLaunching = ref(false)
const gateActionNotice = ref<{ tone: 'success' | 'error'; text: string } | null>(null)

const showRawDigest = ref(false)
const expandedStepIds = reactive(new Set<string>())
const milestonesManualOverride = ref<boolean | null>(null)
const copiedKey = ref<string | null>(null)
let copyResetTimer: ReturnType<typeof setTimeout> | null = null

// On-demand initiator progress events cache (fetched only when progress_revision > 1)
const taskEventsByTaskId = reactive<Record<string, WorkspaceTaskEvent[]>>({})
const taskEventsFetchedRev = new Map<string, number>()
const eventsLoading = ref(false)
let eventsFetchEpoch = 0

// Inline Markdown preview state (unified into the single detail surface)
const inlineMdKey = ref<string | null>(null)
const inlineMdContent = ref<string | null>(null)
const inlineMdLoading = ref(false)
const inlineMdError = ref<string | null>(null)
let inlineMdEpoch = 0

interface TranscriptExcerptRow {
  time: string
  speaker: string
  role: 'worker' | 'reviewer' | 'gate' | 'system'
  text: string
}

const openTranscriptNodeId = ref<string | null>(null)
const transcriptLoading = ref(false)
const transcriptError = ref<string | null>(null)
const transcriptLines = ref<TranscriptExcerptRow[]>([])
let transcriptEpoch = 0

const sessionsById = computed(() => {
  const map = new Map<string, ManagedSession>()
  const list =
    scopeMode.value === 'all' && overviewData.value
      ? overviewData.value.sessions
      : board.value?.sessions ?? []
  for (const session of list) {
    map.set(session.id, session)
  }
  return map
})

const triageItems = computed<TriageTaskViewModel[]>(() => {
  if (scopeMode.value === 'all' && overviewData.value) {
    const wsById = new Map(overviewData.value.workspaces.map(w => [w.id, w]))
    const reportsByTask = new Map<string, AgentReport[]>()
    for (const r of overviewData.value.reports) {
      if (!r.task_id) continue
      const arr = reportsByTask.get(r.task_id) ?? []
      arr.push(r)
      reportsByTask.set(r.task_id, arr)
    }
    const lifecycles = overviewData.value.task_lifecycles ?? {}
    return overviewData.value.tasks.map(task => {
      const ws = wsById.get(task.workspace_id)
      const isSelected = selectedTaskId.value === task.id
      const fullReports =
        isSelected && activeWorkspaceId.value === task.workspace_id ? selectedReports.value : []
      const reports = fullReports.length > 0 ? fullReports : reportsByTask.get(task.id) ?? []
      const docs =
        isSelected && activeWorkspaceId.value === task.workspace_id
          ? selectedMarkdownDocuments.value
          : []
      const events = taskEventsByTaskId[task.id] || []
      return buildTriageTaskViewModel({
        task,
        workspaceName: ws?.name || 'workspace',
        defaultBranch: ws?.default_branch || 'main',
        reports,
        events,
        markdownDocs: docs,
        sessionsById: sessionsById.value,
        lifecycle: lifecycles[task.id],
        nowMs: nowMs.value,
      })
    })
  }

  const wsName = board.value?.workspace?.name || activeWorkspace.value?.name || 'workspace'
  const defBranch = board.value?.workspace?.default_branch || activeWorkspace.value?.default_branch || 'main'
  const lifecycles = board.value?.task_lifecycles ?? {}

  return tasks.value.map(task => {
    const isSelected = selectedTaskId.value === task.id
    const fullReports = isSelected ? selectedReports.value : []
    const reports = fullReports.length > 0 ? fullReports : workspaceStore.reportsForTask(task)
    const docs = isSelected ? selectedMarkdownDocuments.value : []
    const events = taskEventsByTaskId[task.id] || []
    return buildTriageTaskViewModel({
      task,
      workspaceName: wsName,
      defaultBranch: defBranch,
      reports,
      events,
      markdownDocs: docs,
      sessionsById: sessionsById.value,
      lifecycle: lifecycles[task.id],
      nowMs: nowMs.value,
    })
  })
})

const groupedTasks = computed(() => groupTriageTasks(triageItems.value))
const orderedVisibleItems = computed(() => groupedTasks.value.flatMap(g => g.items))

let pendingCrossWorkspaceSelectionId: string | null = null

watch(
  [orderedVisibleItems, activeWorkspaceId, scopeMode],
  ([items]) => {
    if (items.length === 0) return
    if (
      pendingCrossWorkspaceSelectionId &&
      items.some(i => i.id === pendingCrossWorkspaceSelectionId)
    ) {
      selectedTaskId.value = pendingCrossWorkspaceSelectionId
      return
    }
    if (!selectedTaskId.value || !items.some(i => i.id === selectedTaskId.value)) {
      selectedTaskId.value = items[0].id
    }
  },
  { immediate: true },
)

const activeItem = computed<TriageTaskViewModel | null>(() => {
  if (!orderedVisibleItems.value.length) return null
  return (
    orderedVisibleItems.value.find(item => item.id === selectedTaskId.value) ??
    orderedVisibleItems.value[0]
  )
})

const selectedSessionForActive = computed<ManagedSession | null>(() => {
  const item = activeItem.value
  if (!item) return null
  if (item.task.session_id) {
    return sessionsById.value.get(item.task.session_id) ?? selectedSession.value ?? null
  }
  return selectedSession.value
})

// Adaptive Milestones: automatically expanded for active/needs-you tasks, folded for done tasks
const milestonesExpanded = computed(() => {
  if (milestonesManualOverride.value !== null) return milestonesManualOverride.value
  return activeItem.value?.bucket !== 'done'
})

function onMilestonesToggle(event: Event) {
  const el = event.target as HTMLDetailsElement | null
  if (!el) return
  milestonesManualOverride.value = el.open
}

const imageAttachments = computed(() =>
  (activeItem.value?.task.attachments || []).filter(att =>
    (att.mime_type || '').startsWith('image/'),
  ),
)

const hasDeliverables = computed(() =>
  Boolean(
    activeItem.value &&
      (activeItem.value.deliverables.length > 0 || imageAttachments.value.length > 0),
  ),
)

const hasVerification = computed(() => {
  const v = activeItem.value?.validation
  if (!v) return false
  return v.pills.length > 0 || v.notes.length > 0 || v.riskBullets.length > 0
})

const gateBannerCopy = computed(() => {
  const item = activeItem.value
  if (!item) return ''
  if (item.digest.actionPrompt) return item.digest.actionPrompt
  if (item.decisionLine) return item.decisionLine
  if (item.task.status === 'todo') return 'Queued in backlog — click Start Task to dispatch.'
  if (item.bucket === 'working') {
    return `Active run · ${item.milestones.length} milestone${item.milestones.length === 1 ? '' : 's'} recorded`
  }
  return item.task.human_accepted_at ? 'Accepted and finalized.' : 'Completed.'
})

function isInitiatorTask(task: WorkspaceTask): boolean {
  return !isWorkspaceControlledTask(task)
}

function canMarkInitiatorDone(task: WorkspaceTask): boolean {
  return (
    isInitiatorTask(task) &&
    !task.execution_released &&
    task.status !== 'done' &&
    task.status !== 'failed' &&
    Number.isInteger(task.execution_epoch) &&
    (task.execution_epoch ?? 0) >= 1 &&
    Number.isInteger(task.progress_revision) &&
    (task.progress_revision ?? -1) >= 0
  )
}

// Lazily hydrate initiator checkpoint history when a multi-checkpoint task is selected
watch(
  () => {
    const t = activeItem.value?.task
    if (!t) return null
    return {
      wsId: t.workspace_id,
      taskId: t.id,
      control: t.execution_control,
      rev: t.progress_revision ?? 0,
    }
  },
  async target => {
    if (!target || target.control !== 'initiator' || target.rev <= 1) return
    if (taskEventsFetchedRev.get(target.taskId) === target.rev) return
    taskEventsFetchedRev.set(target.taskId, target.rev)
    const epoch = ++eventsFetchEpoch
    eventsLoading.value = true
    try {
      const res = await fetch(
        `/api/workspaces/${encodeURIComponent(target.wsId)}/tasks/${encodeURIComponent(target.taskId)}/events?since_sequence=0`,
        { credentials: 'same-origin' },
      )
      if (epoch !== eventsFetchEpoch) return
      if (res.ok) {
        const data = (await res.json()) as WorkspaceTaskEvent[]
        if (Array.isArray(data)) {
          taskEventsByTaskId[target.taskId] = data
        }
      }
    } catch {
      // Quietly fall back to latest_progress snapshot on offline/mocked surfaces.
    } finally {
      if (epoch === eventsFetchEpoch) {
        eventsLoading.value = false
      }
    }
  },
  { immediate: true },
)

let steerSelectionEpoch = 0

function resetTaskTransientState() {
  steerSelectionEpoch += 1
  inlineMdEpoch += 1
  detailMessage.value = ''
  resetDraftAttachments(detailAttachments.value)
  detailAttachments.value = []
  isSteerOpen.value = false
  isInitiatorReplyOpen.value = false
  initiatorReplyText.value = ''
  initiatorReplyTurnId.value = ''
  initiatorCloseCallId = null
  gateActionNotice.value = null
  showRawDigest.value = false
  milestonesManualOverride.value = null
  expandedStepIds.clear()
  inlineMdKey.value = null
  inlineMdContent.value = null
  inlineMdError.value = null
  inlineMdLoading.value = false
  openTranscriptNodeId.value = null
  transcriptLines.value = []
  transcriptError.value = null
}

async function handleSteerPaste(event: ClipboardEvent) {
  const epoch = steerSelectionEpoch
  const staged: DraftAttachment[] = []
  await handleAttachmentPaste(event, staged)
  if (epoch !== steerSelectionEpoch) {
    resetDraftAttachments(staged)
    return
  }
  detailAttachments.value.push(...staged)
}

watch(selectedTaskId, (nextId, prevId) => {
  if (prevId !== undefined && nextId !== prevId) {
    resetTaskTransientState()
  }
})

async function selectTriageItem(item: TriageTaskViewModel) {
  const targetId = item.id
  pendingCrossWorkspaceSelectionId = targetId
  try {
    if (scopeMode.value === 'all' && item.task.workspace_id !== activeWorkspaceId.value) {
      selectedWorkspaceId.value = item.task.workspace_id
      await handleWorkspaceChange()
      await nextTick()
    }
    selectedTaskId.value = targetId
  } finally {
    pendingCrossWorkspaceSelectionId = null
  }
}

function toggleSteerComposer() {
  isSteerOpen.value = !isSteerOpen.value
  if (isSteerOpen.value) {
    isInitiatorReplyOpen.value = false
    void nextTick(() => steerInputRef.value?.focus())
  }
}

function toggleInitiatorReply() {
  isInitiatorReplyOpen.value = !isInitiatorReplyOpen.value
  if (isInitiatorReplyOpen.value) {
    isSteerOpen.value = false
    if (!initiatorReplyTurnId.value) {
      initiatorReplyTurnId.value = createComposerDraftId()
    }
    void nextTick(() => initiatorReplyInputRef.value?.focus())
  }
}

watch(initiatorReplyText, () => {
  // Fresh text edits mint a new idempotency turn ID; retrying after a network error without editing keeps the same ID.
  initiatorReplyTurnId.value = createComposerDraftId()
})

async function submitSteer() {
  await sendDetailMessage()
  if (!detailMessage.value.trim() && detailAttachments.value.length === 0) {
    isSteerOpen.value = false
  }
}

async function submitInitiatorReply(item: TriageTaskViewModel) {
  const tabId = item.task.source?.tab_id
  const message = initiatorReplyText.value.trim()
  if (!tabId || !message || initiatorReplySending.value) return

  initiatorReplySending.value = true
  gateActionNotice.value = null
  const turnId = initiatorReplyTurnId.value || createComposerDraftId()
  initiatorReplyTurnId.value = turnId

  try {
    await Promise.all([
      terminalStore.fetchTabs(),
      terminalStore.fetchArchivedTabs(),
    ])
    if (terminalStore.archivedTabs.some(t => t.id === tabId)) {
      const restored = await terminalStore.unarchiveTab(tabId)
      if (!restored) {
        throw new Error('Could not restore the archived Chat tab.')
      }
    }

    const res = await fetch(`/api/workspaces/tabs/${encodeURIComponent(tabId)}/stream/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        client_turn_id: turnId,
        text: message,
        delivery: 'normal',
        attachments: [],
      }),
    })
    if (!res.ok) {
      let detail = `Reply failed (HTTP ${res.status})`
      try {
        const errBody = await res.json()
        if (typeof errBody?.detail === 'string' && errBody.detail.trim()) {
          detail = errBody.detail.trim()
        }
      } catch {
        // Keep status fallback.
      }
      throw new Error(detail)
    }

    initiatorReplyText.value = ''
    initiatorReplyTurnId.value = ''
    isInitiatorReplyOpen.value = false
    gateActionNotice.value = {
      tone: 'success',
      text: 'Reply delivered to the originating Chat.',
    }
  } catch (err) {
    gateActionNotice.value = {
      tone: 'error',
      text: err instanceof Error ? err.message : 'Failed to deliver reply.',
    }
  } finally {
    initiatorReplySending.value = false
  }
}

async function markInitiatorTaskDone(item: TriageTaskViewModel) {
  if (!canMarkInitiatorDone(item.task) || initiatorCloseLoading.value) return
  initiatorCloseLoading.value = true
  gateActionNotice.value = null
  const callId = initiatorCloseCallId || generateTaskCallId()
  initiatorCloseCallId = callId
  const prog = item.task.latest_progress

  try {
    await workspaceStore.reportTaskProgressManually(item.task.workspace_id, item.task.id, {
      call_id: callId,
      expected_execution_epoch: item.task.execution_epoch!,
      expected_progress_revision: item.task.progress_revision!,
      state: 'completed',
      summary: prog?.summary || `Marked completed from Agent Workspace: ${item.task.title}`,
      validation: prog?.validation ?? null,
      risks: prog?.risks ?? null,
      artifact_refs: prog?.artifact_refs ?? [],
    })
    initiatorCloseCallId = null
    await Promise.all([
      workspaceStore.fetchBoard(item.task.workspace_id),
      scopeMode.value === 'all' ? refreshOverview() : Promise.resolve(),
    ])
    gateActionNotice.value = {
      tone: 'success',
      text: 'Task marked Done.',
    }
  } catch (err) {
    gateActionNotice.value = {
      tone: 'error',
      text: err instanceof Error ? err.message : 'Could not mark task Done.',
    }
  } finally {
    initiatorCloseLoading.value = false
  }
}

async function fetchOrBuildTaskBrief(item: TriageTaskViewModel): Promise<{
  markdown: string
  workspacePath: string
}> {
  const wsId = item.task.workspace_id
  try {
    const res = await fetch(
      `/api/workspaces/${encodeURIComponent(wsId)}/tasks/${encodeURIComponent(item.task.id)}/brief`,
      { credentials: 'same-origin' },
    )
    if (res.ok) {
      const payload = (await res.json()) as TaskBriefPayload
      if (payload.markdown) {
        return {
          markdown: payload.markdown,
          workspacePath: payload.workspace_path || activeWorkspace.value?.path || '',
        }
      }
    }
  } catch {
    // Fall back to client-side brief synthesis below.
  }

  const wsObj = workspaces.value.find(w => w.id === wsId) || activeWorkspace.value
  const lines = [
    `# Task Handoff Brief — ${item.title}`,
    `- Task: \`${item.id}\` | Workspace: \`${item.workspaceName}\` (\`${wsId}\`)`,
    `- State: status=\`${item.task.status}\`, label=\`${item.statusLabel}\`, control=\`${item.internalMeta.control}\``,
    `- Summary: ${item.digest.headline}`,
  ]
  if (item.digest.actionPrompt) lines.push(`- Decision / Blocker: ${item.digest.actionPrompt}`)
  if (item.deliverables.length > 0) {
    lines.push(
      `- Deliverables: ${item.deliverables.map(d => d.artifactPath || d.url || d.title).join(', ')}`,
    )
  }
  lines.push(`- Inspect full record: \`claude-hub task status ${item.id} --workspace-id ${wsId}\``)
  return {
    markdown: lines.join('\n'),
    workspacePath: wsObj?.path || '',
  }
}

async function copyAiTaskBrief(item: TriageTaskViewModel) {
  if (briefCopying.value) return
  briefCopying.value = true
  try {
    const { markdown } = await fetchOrBuildTaskBrief(item)
    await writeClipboard(markdown)
    const key = `brief:${item.id}`
    copiedKey.value = key
    if (copyResetTimer !== null) clearTimeout(copyResetTimer)
    copyResetTimer = setTimeout(() => {
      copiedKey.value = null
    }, 1800)
  } catch {
    gateActionNotice.value = {
      tone: 'error',
      text: 'Could not copy AI brief to clipboard.',
    }
  } finally {
    briefCopying.value = false
  }
}

async function continueInNewChat(item: TriageTaskViewModel) {
  if (handoffChatLaunching.value) return
  handoffChatLaunching.value = true
  gateActionNotice.value = null
  try {
    const { markdown, workspacePath } = await fetchOrBuildTaskBrief(item)
    const supportedChatTypes = new Set(['claude', 'codex', 'cursor', 'maso', 'antigravity'])
    const chosenAgent = supportedChatTypes.has(item.task.agent_type)
      ? item.task.agent_type
      : 'claude'
    const soloMode = ['claude', 'codex'].includes(chosenAgent)

    const newTab = await terminalStore.createTab({
      name: `Handoff ${item.code}`,
      cwd: workspacePath || undefined,
      agent_type: chosenAgent,
      session_kind: 'chat',
      solo_mode: soloMode,
      target: 'local',
    })
    if (!newTab?.id) {
      throw new Error('Could not create handoff Chat session.')
    }

    const promptText = `${markdown}\n\n请基于以上 Task 恢复包接力该任务：先核对当前工作区与分支状态，确认剩余事项后继续推进或给出明确结论。`
    await fetch(`/api/workspaces/tabs/${encodeURIComponent(newTab.id)}/stream/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        client_turn_id: createComposerDraftId(),
        text: promptText,
        delivery: 'normal',
        attachments: [],
      }),
    })

    appStore.setMode('terminal')
    terminalStore.setActiveTab(newTab.id)
  } catch (err) {
    gateActionNotice.value = {
      tone: 'error',
      text: err instanceof Error ? err.message : 'Failed to start handoff Chat.',
    }
  } finally {
    handoffChatLaunching.value = false
  }
}

function toggleStepDetail(stepId: string) {
  if (expandedStepIds.has(stepId)) {
    expandedStepIds.delete(stepId)
  } else {
    expandedStepIds.add(stepId)
  }
}

function openDeliverableModal(deliv: DeliverableItem) {
  if (!deliv.artifactPath) return
  const matchingReport = deliv.reportId
    ? selectedReports.value.find(r => r.id === deliv.reportId)
    : undefined
  void openMarkdownPreviewModal(deliv.artifactPath, matchingReport)
}

async function toggleInlineMarkdown(deliv: DeliverableItem) {
  if (!deliv.artifactPath || !activeItem.value) return
  if (inlineMdKey.value === deliv.key) {
    inlineMdKey.value = null
    return
  }
  inlineMdKey.value = deliv.key
  inlineMdContent.value = null
  inlineMdError.value = null
  inlineMdLoading.value = true
  const epoch = ++inlineMdEpoch
  const wsId = activeItem.value.task.workspace_id
  const sourceTabId = activeItem.value.task.source?.tab_id

  try {
    const preview = await workspaceStore.fetchArtifactPreview(
      wsId,
      deliv.artifactPath,
      deliv.reportId,
    )
    if (epoch !== inlineMdEpoch) return
    inlineMdContent.value = preview.content
  } catch (primaryErr) {
    if (epoch !== inlineMdEpoch) return
    if (sourceTabId) {
      try {
        const res = await fetch(
          `/api/workspaces/tabs/${encodeURIComponent(sourceTabId)}/stream/markdown?path=${encodeURIComponent(deliv.artifactPath)}`,
          { credentials: 'same-origin' },
        )
        if (epoch !== inlineMdEpoch) return
        if (res.ok) {
          const fallback = (await res.json()) as WorkspaceArtifactPreview
          inlineMdContent.value = fallback.content
          return
        }
      } catch {
        // Retain primary error message below.
      }
    }
    inlineMdError.value =
      primaryErr instanceof Error ? primaryErr.message : 'Failed to load Markdown preview.'
  } finally {
    if (epoch === inlineMdEpoch) {
      inlineMdLoading.value = false
    }
  }
}

async function copyDeliverableText(key: string, value: string) {
  if (!value) return
  try {
    await writeClipboard(value)
    copiedKey.value = key
    if (copyResetTimer !== null) clearTimeout(copyResetTimer)
    copyResetTimer = setTimeout(() => {
      copiedKey.value = null
    }, 1600)
  } catch {
    // Ignore clipboard rejection.
  }
}

function formatClock(iso: string | undefined): string {
  const ms = parseTimestampMs(iso)
  if (ms === null) return '--:--'
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function toggleTranscript(step: MilestoneStep) {
  if (openTranscriptNodeId.value === step.id) {
    openTranscriptNodeId.value = null
    return
  }
  openTranscriptNodeId.value = step.id
  transcriptLoading.value = true
  transcriptError.value = null
  transcriptLines.value = []
  const reqEpoch = ++transcriptEpoch

  try {
    const endpoint = step.sessionId
      ? `/api/workspaces/sessions/${encodeURIComponent(step.sessionId)}/stream/events?since_sequence=-1&limit=120&compact=true`
      : step.tabId
        ? `/api/workspaces/tabs/${encodeURIComponent(step.tabId)}/stream/events?since_sequence=-1&limit=120&compact=true`
        : null
    if (!endpoint) {
      transcriptError.value = 'No session reference recorded on this checkpoint.'
      return
    }
    const response = await fetch(endpoint, { credentials: 'same-origin' })
    if (reqEpoch !== transcriptEpoch) return
    if (!response.ok) {
      transcriptError.value = step.tabId
        ? 'Live stream closed; click below to restore the archived conversation.'
        : `Stream unavailable (HTTP ${response.status}).`
      return
    }
    const data = (await response.json()) as { events?: AgentStreamEvent[] }
    if (reqEpoch !== transcriptEpoch) return
    const rows: TranscriptExcerptRow[] = []
    for (const ev of data.events ?? []) {
      if (ev.type === 'turn_started') {
        const summary = String(ev.payload?.summary || '').replace(/\s+/g, ' ').trim()
        if (summary) {
          rows.push({
            time: formatClock(ev.created_at),
            speaker: 'Prompt',
            role: 'gate',
            text: summary.slice(0, 220),
          })
        }
      } else if (ev.type === 'turn_completed') {
        const text = String(
          ev.payload?.final_text || ev.payload?.assistant_text || ev.payload?.summary || '',
        )
          .replace(/\s+/g, ' ')
          .trim()
        if (text) {
          rows.push({
            time: formatClock(ev.created_at),
            speaker: step.phase === 'review' ? 'Reviewer' : 'Agent',
            role: step.phase === 'review' ? 'reviewer' : 'worker',
            text: text.slice(0, 260),
          })
        }
      }
    }
    transcriptLines.value = rows.slice(-6)
  } catch {
    if (reqEpoch === transcriptEpoch) {
      transcriptError.value = 'Could not load stream preview.'
    }
  } finally {
    if (reqEpoch === transcriptEpoch) {
      transcriptLoading.value = false
    }
  }
}

async function openOrRestoreTab(tabId: string) {
  await Promise.all([
    terminalStore.fetchTabs(),
    terminalStore.fetchArchivedTabs(),
  ])
  if (terminalStore.archivedTabs.some(t => t.id === tabId)) {
    const restored = await terminalStore.unarchiveTab(tabId)
    if (!restored) return
  }
  appStore.setMode('terminal')
  terminalStore.setActiveTab(tabId)
}

onMounted(() => {
  if (scopeMode.value === 'all') {
    void refreshOverview()
  }
  clockTimer = setInterval(() => {
    nowMs.value = Date.now()
  }, 15_000)
  overviewTimer = setInterval(() => {
    if (scopeMode.value === 'all') void refreshOverview()
  }, 5_000)
})

onBeforeUnmount(() => {
  if (clockTimer !== null) clearInterval(clockTimer)
  if (overviewTimer !== null) clearInterval(overviewTimer)
  if (copyResetTimer !== null) clearTimeout(copyResetTimer)
  transcriptEpoch += 1
  eventsFetchEpoch += 1
  inlineMdEpoch += 1
  overviewEpoch += 1
  resetTaskTransientState()
})
</script>

<style scoped src="../styles/workspace-triage-pro.css"></style>
