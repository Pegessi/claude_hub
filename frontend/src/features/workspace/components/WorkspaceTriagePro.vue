<template>
  <div class="tp-shell">
    <div
      v-if="!activeWorkspaceId"
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
          v-if="boardLoading && triageItems.length === 0"
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
          <p>No tasks in {{ activeWorkspace?.name || 'this workspace' }} yet.</p>
          <button
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
              @click="selectTriageTask(item.id)"
            >
              <div class="tp-row-top">
                <span :class="['tp-dot', `tp-dot--${item.statusTone}`]" />
                <span class="tp-row-code tp-mono">{{ item.code }}</span>
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
            v-if="boardTasksPagination && boardOlderTasksRemaining > 0"
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

      <!-- ==================== RIGHT DETAIL CANVAS ==================== -->
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
          <!-- Top Bar: Concise Identity + Live A/B Presentation Switcher -->
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

            <div
              class="tp-mode-switch"
              role="group"
              aria-label="Detail view layout"
            >
              <button
                type="button"
                :class="['tp-mode-btn', { active: detailMode === 'digest' }]"
                :aria-pressed="detailMode === 'digest'"
                @click="setDetailMode('digest')"
              >
                A · Milestone Digest
              </button>
              <button
                type="button"
                :class="['tp-mode-btn', { active: detailMode === 'memo' }]"
                :aria-pressed="detailMode === 'memo'"
                @click="setDetailMode('memo')"
              >
                B · One-Page Brief
              </button>
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

          <!-- Action / Decision Gate -->
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
                    v-if="selectedSession"
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
                    v-if="activeItem.bucket === 'done' && selectedSession?.ephemeral"
                    type="button"
                    class="tp-ghost-btn"
                    :loading="isPending(taskActionKey('cleanup-session', activeItem.id))"
                    loading-label="Releasing…"
                    @click="cleanupCompletedTaskSession(activeItem.task)"
                  >
                    Release session
                  </LoadingButton>
                </template>

                <button
                  v-if="selectedSession"
                  type="button"
                  class="tp-ghost-btn"
                  :disabled="isPending(sessionActionKey('open', selectedSession.id))"
                  @click="openSession(selectedSession)"
                >
                  Open session ↗
                </button>

                <button
                  v-else-if="activeItem.task.source?.tab_id"
                  type="button"
                  :class="activeItem.bucket === 'needs_you' ? 'tp-primary-btn' : 'tp-ghost-btn'"
                  @click="openOrRestoreTab(activeItem.task.source.tab_id)"
                >
                  Open Chat ↗
                </button>
              </div>
            </div>

            <form
              v-if="isSteerOpen && selectedSession && canUseManagedTaskActions(activeItem.task)"
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
          </section>

          <!-- ==================== MODE A: MILESTONE DIGEST ==================== -->
          <template v-if="detailMode === 'digest'">
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

            <div
              v-if="hasDeliverables || hasVerification"
              class="tp-evidence-grid"
            >
              <!-- Deliverables Card -->
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
                      <span class="tp-mono">Open ↗</span>
                    </a>

                    <div
                      v-else
                      class="tp-deliverable-item"
                    >
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
                    <button
                      type="button"
                      class="tp-mini-btn"
                      @click="openImageLightbox(`/api/workspaces/attachments/${img.id}`, img.filename)"
                    >
                      View ↗
                    </button>
                  </div>
                </div>
              </section>

              <!-- Verification & Caveats Card -->
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
            </div>

            <!-- Aggregated Key Milestones (2~6 clean nodes, zero text duplication) -->
            <section
              class="tp-milestones-section"
              aria-label="Key milestones"
            >
              <div class="tp-section-head">
                <h2 class="tp-micro-heading">
                  Key Milestones ({{ activeItem.milestones.length }})
                </h2>
                <span
                  v-if="eventsLoading"
                  class="tp-mono tp-sep"
                >Syncing checkpoints…</span>
              </div>

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
            </section>
          </template>

          <!-- ==================== MODE B: ONE-PAGE BRIEF ==================== -->
          <template v-else>
            <section
              class="tp-card tp-card--memo"
              aria-label="One-page sign-off brief"
            >
              <div class="tp-section-head">
                <h2 class="tp-micro-heading">
                  1 · Executive Summary
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

              <template v-if="hasDeliverables">
                <div class="tp-section-head">
                  <h2 class="tp-micro-heading">
                    2 · Deliverables &amp; Reports
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
                      <span class="tp-mono">Open ↗</span>
                    </a>

                    <div v-else>
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
                            {{ inlineMdKey === deliv.key ? 'Hide preview' : 'Inline preview' }}
                          </button>
                          <button
                            v-if="deliv.kind === 'markdown' && deliv.artifactPath"
                            type="button"
                            class="tp-mini-btn"
                            @click="openDeliverableModal(deliv)"
                          >
                            Modal ↗
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
                    <button
                      type="button"
                      class="tp-mini-btn"
                      @click="openImageLightbox(`/api/workspaces/attachments/${img.id}`, img.filename)"
                    >
                      View ↗
                    </button>
                  </div>
                </div>
              </template>

              <template v-if="hasVerification">
                <div class="tp-section-head">
                  <h2 class="tp-micro-heading">
                    {{ hasDeliverables ? '3 · Verification & Caveats' : '2 · Verification & Caveats' }}
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
              </template>
            </section>

            <!-- Mode B keeps Process History quietly tucked in a collapsible drawer -->
            <details class="tp-meta-disclosure">
              <summary>
                <span>Process History ({{ activeItem.milestones.length }} milestones)</span>
                <span class="tp-mono tp-num">{{ activeItem.elapsedLabel }}</span>
              </summary>
              <div class="tp-disclosure-body">
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
                      </div>
                      <p :class="['tp-ms-oneliner', { 'tp-ms-oneliner--echo': step.isCurrentEcho }]">
                        {{ step.oneLiner }}
                      </p>
                    </div>
                  </li>
                </ol>
              </div>
            </details>
          </template>

          <!-- Collapsible Raw Prompt, Execution Control & Secondary Actions -->
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
                v-if="activeWorkspaceId"
                :key="`${activeWorkspaceId}:${activeItem.id}`"
                :workspace-id="activeWorkspaceId"
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
  AgentStreamEvent,
  ManagedSession,
  WorkspaceArtifactPreview,
  WorkspaceTaskEvent,
} from '@/types'
import { writeClipboard } from '@/utils/clipboard'
import {
  formatAttachmentSize,
  handleAttachmentPaste,
  removeDraftAttachment,
  resetDraftAttachments,
  type DraftAttachment,
} from '@/utils/draftAttachments'
import { parseTimestampMs } from '@/utils/duration'
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

type TriageDetailMode = 'digest' | 'memo'
const DETAIL_MODE_STORAGE_KEY = 'claude-hub.triage-detail-mode.v1'

function readInitialDetailMode(): TriageDetailMode {
  if (typeof window !== 'undefined') {
    const param = new URLSearchParams(window.location.search).get('triage_mode')
    if (param === 'memo' || param === 'digest') return param
    try {
      const saved = window.localStorage.getItem(DETAIL_MODE_STORAGE_KEY)
      if (saved === 'memo' || saved === 'digest') return saved
    } catch {
      // Ignore storage access errors.
    }
  }
  return 'digest'
}

const detailMode = ref<TriageDetailMode>(readInitialDetailMode())

function setDetailMode(mode: TriageDetailMode) {
  detailMode.value = mode
  try {
    window.localStorage.setItem(DETAIL_MODE_STORAGE_KEY, mode)
  } catch {
    // Ignore storage write errors.
  }
}

const workspaceStore = useWorkspaceStore()
const terminalStore = useTerminalStore()
const appStore = useAppStore()

const {
  activeWorkspace,
  activeWorkspaceId,
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

const isSteerOpen = ref(false)
const steerInputRef = ref<HTMLTextAreaElement | null>(null)
const showRawDigest = ref(false)
const expandedStepIds = reactive(new Set<string>())
const copiedKey = ref<string | null>(null)
let copyResetTimer: ReturnType<typeof setTimeout> | null = null

// On-demand initiator progress events cache (fetched only when progress_revision > 1)
const taskEventsByTaskId = reactive<Record<string, WorkspaceTaskEvent[]>>({})
const taskEventsFetchedRev = new Map<string, number>()
const eventsLoading = ref(false)
let eventsFetchEpoch = 0

// Inline Markdown preview state for Mode B (One-Page Brief)
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
  for (const session of board.value?.sessions ?? []) {
    map.set(session.id, session)
  }
  return map
})

const triageItems = computed<TriageTaskViewModel[]>(() => {
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

watch(
  [orderedVisibleItems, activeWorkspaceId],
  ([items]) => {
    if (items.length === 0) return
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
  showRawDigest.value = false
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

function selectTriageTask(id: string) {
  if (selectedTaskId.value === id) return
  selectedTaskId.value = id
}

function toggleSteerComposer() {
  isSteerOpen.value = !isSteerOpen.value
  if (isSteerOpen.value) {
    void nextTick(() => steerInputRef.value?.focus())
  }
}

async function submitSteer() {
  await sendDetailMessage()
  if (!detailMessage.value.trim() && detailAttachments.value.length === 0) {
    isSteerOpen.value = false
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
  clockTimer = setInterval(() => {
    nowMs.value = Date.now()
  }, 15_000)
})

onBeforeUnmount(() => {
  if (clockTimer !== null) clearInterval(clockTimer)
  if (copyResetTimer !== null) clearTimeout(copyResetTimer)
  transcriptEpoch += 1
  eventsFetchEpoch += 1
  inlineMdEpoch += 1
  resetTaskTransientState()
})
</script>

<style scoped src="../styles/workspace-triage-pro.css"></style>
