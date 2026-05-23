<template>
  <section class="workspace-view">
    <header class="workspace-header">
      <div class="workspace-title-block">
        <h1>Agent Workspace</h1>
        <p>Manual task queue with multiple resident workspace agents.</p>
        <div class="workspace-mobile-identity">
          <span>Agent Workspace</span>
          <strong>{{ activeWorkspace?.name || 'No workspace' }}</strong>
          <small>{{ mobileWorkspaceSummary }}</small>
        </div>
      </div>
      <div class="workspace-actions">
        <div
          class="workspace-select-shell"
          :data-loading="isPending('workspace:switch')"
        >
          <select
            v-model="selectedWorkspaceId"
            class="workspace-select"
            :disabled="isPending('workspace:switch')"
            @change="handleWorkspaceChange"
          >
            <option
              v-for="workspace in workspaces"
              :key="workspace.id"
              :value="workspace.id"
            >
              {{ workspace.name }}
            </option>
          </select>
          <span
            v-if="isPending('workspace:switch')"
            class="workspace-select-spinner"
            aria-hidden="true"
          />
        </div>
        <button
          type="button"
          class="tool-button workspace-desktop-action"
          @click="openWorkspaceModal"
        >
          New Workspace
        </button>
        <button
          type="button"
          class="tool-button workspace-desktop-action"
          :disabled="!activeWorkspaceId"
          @click="openEditWorkspaceModal"
        >
          Edit Workspace
        </button>
        <button
          type="button"
          class="tool-button workspace-desktop-action"
          :disabled="!activeWorkspaceId"
          @click="openAgentOptionsModal"
        >
          Manage Agents
        </button>
        <button
          type="button"
          class="primary-button"
          :disabled="!activeWorkspaceId"
          @click="openTaskModal"
        >
          Add Task
        </button>
        <details
          ref="workspaceMobileMenuRef"
          class="workspace-mobile-menu"
        >
          <summary
            class="workspace-mobile-menu-trigger"
            title="Workspace menu"
            aria-label="Workspace menu"
          >
            ⋯
          </summary>
          <div class="workspace-mobile-menu-panel">
            <button
              type="button"
              class="workspace-mobile-menu-item workspace-mobile-menu-item--mode"
              @click="goToTerminalMode"
            >
              <span>Terminal</span>
            </button>
            <button
              type="button"
              class="workspace-mobile-menu-item workspace-mobile-menu-item--mode active"
              @click="closeWorkspaceMobileMenu"
            >
              <span>Workspace</span>
              <strong>Current</strong>
            </button>
            <button
              type="button"
              class="workspace-mobile-menu-item"
              @click="openWorkspaceModalFromMenu"
            >
              New Workspace
            </button>
            <button
              type="button"
              class="workspace-mobile-menu-item"
              :disabled="!activeWorkspaceId"
              @click="openEditWorkspaceModalFromMenu"
            >
              Edit Workspace
            </button>
            <button
              type="button"
              class="workspace-mobile-menu-item"
              :disabled="!activeWorkspaceId"
              @click="openAgentOptionsModalFromMenu"
            >
              Manage Agents
            </button>
            <LoadingButton
              type="button"
              class="workspace-mobile-menu-item"
              :loading="isPending('workspace:refresh-statuses')"
              loading-label="Refreshing"
              @click="refreshAgentStatusesFromMenu"
            >
              Refresh
            </LoadingButton>
            <NetworkAccessMenu variant="menu" />
            <button
              type="button"
              class="workspace-mobile-menu-item workspace-mobile-menu-item--theme"
              @click="toggleThemeFromMenu"
            >
              <span>{{ colorScheme === 'dark' ? 'Switch to Light' : 'Switch to Dark' }}</span>
              <strong>{{ colorScheme }}</strong>
            </button>
          </div>
        </details>
      </div>
    </header>

    <div
      v-if="error"
      class="workspace-error"
    >
      {{ error }}
    </div>

    <div
      v-if="activeWorkspaceId"
      class="workspace-summary-strip"
    >
      <div class="workspace-summary-primary">
        <span>{{ workspaceAgents.length }} agents</span>
        <span>{{ reviewerAgents.length + temporaryReviewers.length }} reviewers</span>
        <strong>{{ workspaceAgents.filter(agent => agent.runtime_status === 'working').length }} working</strong>
        <span>{{ tasksByStatus('queued').length }} queued</span>
      </div>
      <div class="workspace-column-tabs">
        <span
          v-for="column in columns"
          :key="column.status"
        >
          {{ column.label }} {{ tasksByStatus(column.status).length }}
        </span>
      </div>
    </div>

    <section
      v-if="activeWorkspaceId"
      class="workspace-agent-status"
      aria-label="Current workspace agent statuses"
    >
      <div class="agent-status-header">
        <div
          class="agent-status-view-switch"
          aria-label="Agent status view"
        >
          <button
            type="button"
            :data-active="workspaceSessionView === 'agents'"
            @click="workspaceSessionView = 'agents'"
          >
            <span>Agents</span>
            <strong>{{ workspaceAgents.length }}</strong>
          </button>
          <button
            type="button"
            :data-active="workspaceSessionView === 'reviewers'"
            @click="workspaceSessionView = 'reviewers'"
          >
            <span>Reviewers</span>
            <strong>{{ reviewerSessions.length }}</strong>
          </button>
        </div>
        <div class="agent-status-toolbar">
          <LoadingButton
            type="button"
            class="agent-status-refresh"
            title="Refresh statuses"
            :loading="isPending('workspace:refresh-statuses')"
            hide-content-while-loading
            loading-label="Refreshing statuses"
            @click="refreshAgentStatuses"
          >
            ↻
          </LoadingButton>
        </div>
      </div>
      <div class="agent-status-grid">
        <article
          v-for="agent in visibleWorkspaceSessions"
          :key="agent.id"
          class="agent-status-card"
        >
          <button
            type="button"
            class="agent-status-card-main"
            :disabled="isPending(sessionActionKey('open', agent.id))"
            @click="openSession(agent)"
          >
            <span
              class="agent-status-dot"
              :data-status="agentRuntimeStatus(agent)"
            />
            <span class="agent-status-main">
              <span class="agent-status-line">
                <span class="agent-status-name">{{ agent.title }}</span>
                <span class="agent-status-kind">{{ agentRoleLabel(agent) }}</span>
              </span>
              <span class="agent-status-detail">
                {{ agentRuntimeDetail(agent) }}
              </span>
              <span class="agent-status-meta">
                <span>{{ agent.target }}</span>
                <span v-if="agent.ephemeral">temporary</span>
                <span>{{ agentTaskLabel(agent) }}</span>
                <span>queued {{ agent.queued_count }}</span>
              </span>
            </span>
            <span
              class="agent-status-pill"
              :data-status="agentRuntimeStatus(agent)"
            >
              <span
                v-if="isPending(sessionActionKey('open', agent.id))"
                class="agent-status-inline-spinner"
                aria-hidden="true"
              />
              {{ agentRuntimeText(agent) }}
            </span>
          </button>
          <div
            v-if="agent.role !== 'dispatcher'"
            class="agent-status-actions"
          >
            <LoadingButton
              type="button"
              class="agent-status-delete"
              :disabled="!canDeleteAgent(agent)"
              :title="agentDeleteTitle(agent)"
              :loading="isPending(agentActionKey('delete', agent.id))"
              loading-label="Deleting agent"
              @click="deleteAgent(agent)"
            >
              Delete
            </LoadingButton>
          </div>
        </article>
        <div
          v-if="visibleWorkspaceSessions.length === 0"
          class="agent-status-empty"
        >
          {{ visibleWorkspaceSessionsEmptyText }}
        </div>
      </div>
    </section>

    <div class="workspace-layout">
      <main class="board">
        <div
          v-if="!activeWorkspaceId"
          class="empty-board"
        >
          Create a workspace to start agents and queue tasks.
        </div>

        <template v-else>
          <section
            v-for="column in columns"
            :key="column.status"
            :class="[
              'task-column',
              {
                'task-column--empty': tasksByStatus(column.status).length === 0,
                'task-column--collapsed': isMobileColumnCollapsed(column.status),
              },
            ]"
          >
            <div class="column-header">
              <h2>{{ column.label }}</h2>
              <div class="column-meta">
                <span>{{ tasksByStatus(column.status).length }}</span>
                <button
                  type="button"
                  class="column-collapse-button"
                  :aria-expanded="!isMobileColumnCollapsed(column.status)"
                  @click="toggleMobileColumn(column.status)"
                >
                  {{ isMobileColumnCollapsed(column.status) ? 'Show' : 'Hide' }}
                </button>
              </div>
            </div>
            <div class="task-list">
              <article
                v-for="task in tasksByStatus(column.status)"
                :key="task.id"
                :class="[
                  'task-card',
                  `task-card--${task.status}`,
                  { selected: selectedTaskId === task.id },
                ]"
                @click="selectTask($event, task.id)"
              >
                <div class="task-card-header">
                  <h3>{{ task.title }}</h3>
                  <span class="task-card-badges">
                    <span
                      v-if="task.status === 'working' && activeReviewBadge(task)"
                      :class="[
                        'review-badge',
                        `review-badge--${activeReviewBadge(task)?.kind}`,
                      ]"
                      :title="activeReviewBadge(task)?.title"
                    >
                      <span class="review-badge-dot" />
                      {{ activeReviewBadge(task)?.label }}
                    </span>
                    <span
                      v-else
                      class="agent-badge"
                    >
                      <span :class="['status-dot', `status-dot--${task.status}`]" />
                      {{ task.status }}
                    </span>
                  </span>
                </div>
                <p class="task-card-description">
                  {{ task.prompt }}
                </p>
                <div
                  v-if="task.dispatch_pending"
                  class="latest-report"
                >
                  Waiting for dispatcher decision
                </div>
                <div
                  v-else-if="task.dispatch_reason"
                  class="latest-report"
                >
                  {{ task.dispatch_reason }}
                </div>
                <div
                  v-if="latestReportForTask(task)"
                  class="latest-report"
                >
                  <strong>{{ latestReportForTask(task)?.state }}</strong>
                  <span>{{ latestReportForTask(task)?.message }}</span>
                </div>
                <div class="session-meta">
                  <span>task {{ task.status }}</span>
                  <span>agent {{ agentTitle(task.session_id) }}</span>
                  <span v-if="task.review_session_id">
                    reviewer {{ reviewerTitle(task.review_session_id) }}
                  </span>
                  <span v-if="reviewStatusLabel(task)">{{ reviewStatusLabel(task) }}</span>
                  <span v-if="sessionForTask(task)">
                    runtime {{ sessionForTask(task)?.runtime_status }}
                  </span>
                </div>
                <details
                  v-if="task.status === 'todo'"
                  class="advanced-start"
                  @click.stop
                >
                  <summary>Dispatch options</summary>
                  <label>
                    Agent
                    <select v-model="startOptionsFor(task).target_session_id">
                      <option value="">Auto</option>
                      <option
                        v-for="agent in workspaceAgents"
                        :key="agent.id"
                        :value="agent.id"
                      >
                        {{ agent.title }}
                      </option>
                    </select>
                  </label>
                  <label>
                    Related task
                    <select v-model="startOptionsFor(task).related_task_id">
                      <option value="">Auto</option>
                      <option
                        v-for="candidate in tasks.filter(item => item.id !== task.id)"
                        :key="candidate.id"
                        :value="candidate.id"
                      >
                        {{ candidate.title }}
                      </option>
                    </select>
                  </label>
                  <label class="checkbox-label">
                    <input
                      v-model="startOptionsFor(task).clear_context"
                      type="checkbox"
                    >
                    Clear context
                  </label>
                </details>
                <div class="task-actions">
                  <LoadingButton
                    v-if="task.status === 'todo'"
                    type="button"
                    :loading="isPending(taskActionKey('start', task.id))"
                    loading-label="Starting task"
                    @click.stop="startTask(task)"
                  >
                    Start
                  </LoadingButton>
                  <LoadingButton
                    v-if="canAcceptTask(task)"
                    type="button"
                    :loading="isPending(taskActionKey('mark-done', task.id))"
                    loading-label="Accepting"
                    @click.stop="markTask(task.id, 'done')"
                  >
                    Accept
                  </LoadingButton>
                  <LoadingButton
                    v-if="canRequestChanges(task)"
                    type="button"
                    :loading="isPending(taskActionKey('request-changes', task.id))"
                    loading-label="Requesting changes"
                    @click.stop="requestChanges(task)"
                  >
                    Request changes
                  </LoadingButton>
                  <LoadingButton
                    v-if="task.status === 'review' && task.review_skipped_at"
                    type="button"
                    :loading="isPending(taskActionKey('request-review', task.id))"
                    loading-label="Requesting review"
                    @click.stop="requestReview(task.id)"
                  >
                    Request review
                  </LoadingButton>
                  <LoadingButton
                    v-if="sessionForTask(task)"
                    type="button"
                    :loading="isPending(sessionActionKey('open', task.session_id))"
                    loading-label="Opening tab"
                    @click.stop="openSession(sessionForTask(task)!)"
                  >
                    Open tab
                  </LoadingButton>
                  <LoadingButton
                    type="button"
                    class="danger-button"
                    :loading="isPending(taskActionKey('delete', task.id))"
                    loading-label="Deleting task"
                    @click.stop="deleteTask(task)"
                  >
                    Delete
                  </LoadingButton>
                </div>
              </article>
              <div
                v-if="tasksByStatus(column.status).length === 0"
                class="column-empty"
              >
                No tasks
              </div>
            </div>
          </section>
        </template>
      </main>
    </div>

    <Teleport to="body">
      <div
        v-if="selectedTask"
        class="task-detail-overlay"
        @click.self="closeTaskDetail"
      >
        <aside
          :class="['task-detail-panel', { 'mobile-actions-expanded': isDetailActionsExpanded }]"
          role="dialog"
          aria-modal="true"
          :aria-label="selectedTask.title"
        >
          <div class="detail-header">
            <div>
              <span class="detail-eyebrow">{{ selectedTask.status }}</span>
              <h2>{{ selectedTask.title }}</h2>
            </div>
            <button
              type="button"
              class="icon-button"
              aria-label="Close task detail"
              @click="closeTaskDetail"
            >
              x
            </button>
          </div>

          <div class="detail-body">
            <section class="detail-section">
              <div class="detail-section-title">
                Task description
              </div>
              <MarkdownContent
                class="detail-copy"
                :text="selectedTask.prompt"
              />
              <div
                v-if="selectedTask.attachments.length > 0"
                class="attachment-list attachment-list--readonly"
              >
                <div
                  v-for="attachment in selectedTask.attachments"
                  :key="attachment.id"
                  class="attachment-row"
                >
                  <div class="attachment-thumb">
                    <img
                      :src="`/api/workspaces/attachments/${attachment.id}`"
                      :alt="attachment.filename"
                    >
                  </div>
                  <div class="attachment-meta">
                    <strong>{{ attachment.filename }}</strong>
                    <span>{{ persistedAttachmentMeta(attachment) }}</span>
                    <code>{{ attachment.path }}</code>
                  </div>
                </div>
              </div>
            </section>

            <section class="detail-section">
              <div class="detail-section-title">
                Goal Packet
              </div>
              <div
                v-if="!selectedTask.goal_packet"
                class="empty-timeline"
              >
                No goal packet recorded yet.
              </div>
              <div
                v-else
                class="goal-packet"
              >
                <div class="goal-packet-objective">
                  <span>Objective</span>
                  <MarkdownContent
                    compact
                    :text="selectedTask.goal_packet.objective"
                  />
                </div>
                <div class="goal-packet-meta">
                  <span>{{ selectedTask.goal_packet.status || 'draft' }}</span>
                  <span>{{ selectedTask.goal_packet.source || 'agent_generated' }}</span>
                </div>
                <div
                  v-for="section in goalPacketSections(selectedTask.goal_packet)"
                  :key="section.key"
                  class="goal-packet-section"
                >
                  <strong>{{ section.label }}</strong>
                  <ol v-if="section.items.length > 0">
                    <li
                      v-for="item in section.items"
                      :key="item"
                    >
                      {{ item }}
                    </li>
                  </ol>
                  <span
                    v-else
                    class="goal-packet-empty"
                  >
                    none
                  </span>
                </div>
              </div>
            </section>

            <section class="detail-section">
              <div class="detail-section-title">
                Assignment
              </div>
              <div class="fact-grid">
                <div>
                  <span>Task stage</span>
                  <strong>{{ selectedTask.status }}</strong>
                </div>
                <div>
                  <span>Agent runtime</span>
                  <strong>{{ selectedSession?.runtime_status || 'none' }}</strong>
                </div>
                <div>
                  <span>Agent</span>
                  <strong>{{ selectedSession?.title || 'auto' }}</strong>
                </div>
                <div>
                  <span>Reviewer</span>
                  <strong>{{ selectedTask.review_session_id ? reviewerTitle(selectedTask.review_session_id) : 'none' }}</strong>
                </div>
                <div>
                  <span>Review state</span>
                  <strong>{{ reviewStatusLabel(selectedTask) || 'not requested' }}</strong>
                </div>
                <div v-if="selectedTask.human_acceptance_requested_at">
                  <span>Human acceptance</span>
                  <strong>{{ selectedTask.human_accepted_at ? 'accepted' : 'awaiting' }}</strong>
                </div>
                <div v-if="selectedTask.review_skip_reason">
                  <span>Review reason</span>
                  <strong>{{ selectedTask.review_skip_reason }}</strong>
                </div>
                <div>
                  <span>Queued behind</span>
                  <strong>{{ selectedSession?.queued_count || 0 }}</strong>
                </div>
                <div>
                  <span>Clear context</span>
                  <strong>{{ selectedTask.clear_context ? 'yes' : 'no' }}</strong>
                </div>
                <div>
                  <span>Snapshot</span>
                  <strong>{{ board?.snapshot_path || 'none' }}</strong>
                </div>
              </div>
            </section>

            <section class="detail-section">
              <div class="detail-section-title detail-section-title--with-controls">
                <span>Progress</span>
                <div
                  v-if="selectedReports.length > 0 && hasBilingualReport"
                  class="lang-toggle"
                  role="group"
                  aria-label="Report language"
                >
                  <button
                    type="button"
                    class="lang-toggle-btn"
                    :class="{ active: reportLang === 'en' }"
                    @click="setReportLang('en')"
                  >
                    EN
                  </button>
                  <button
                    type="button"
                    class="lang-toggle-btn"
                    :class="{ active: reportLang === 'zh' }"
                    @click="setReportLang('zh')"
                  >
                    中
                  </button>
                </div>
              </div>
              <div
                v-if="selectedReports.length === 0"
                class="empty-timeline"
              >
                No agent reports yet.
              </div>
              <ol
                v-else
                class="timeline"
              >
                <li
                  v-for="report in selectedReports"
                  :key="report.id"
                >
                  <details
                    class="report-card"
                    :open="isLatestSelectedReport(report)"
                  >
                    <summary>
                      <span class="report-state">{{ report.state }}</span>
                      <span class="report-time">{{ formatTime(report.created_at) }}</span>
                    </summary>
                    <MarkdownContent
                      class="report-message"
                      :text="reportMessageForLang(report)"
                    />
                    <div
                      v-if="report.changed_files.length > 0"
                      class="report-files"
                    >
                      <span
                        v-for="file in report.changed_files"
                        :key="file"
                      >
                        {{ file }}
                      </span>
                    </div>
                    <div
                      v-if="report.validation"
                      class="report-note"
                    >
                      <strong>Validation</strong>
                      <MarkdownContent
                        compact
                        :text="report.validation"
                      />
                    </div>
                    <div
                      v-if="acceptanceChecksFor(report).length > 0"
                      class="report-note"
                    >
                      <strong>Acceptance Check</strong>
                      <ol class="acceptance-check-list">
                        <li
                          v-for="check in acceptanceChecksFor(report)"
                          :key="`${check.criterion}-${check.status}`"
                        >
                          <span>{{ check.status }}</span>
                          {{ check.criterion }} - {{ check.evidence }}
                        </li>
                      </ol>
                    </div>
                    <div
                      v-if="report.risks"
                      class="report-note"
                    >
                      <strong>Risks</strong>
                      <MarkdownContent
                        compact
                        :text="report.risks"
                      />
                    </div>
                  </details>
                </li>
              </ol>
            </section>
          </div>

          <div class="detail-footer">
            <button
              type="button"
              class="detail-footer-toggle"
              :aria-expanded="isDetailActionsExpanded"
              @click="toggleDetailActions"
            >
              <span>{{ isDetailActionsExpanded ? 'Hide actions' : 'Actions' }}</span>
              <span class="detail-footer-chevron">
                {{ isDetailActionsExpanded ? 'v' : '^' }}
              </span>
            </button>
            <div class="detail-action-drawer">
              <div class="detail-actions">
                <LoadingButton
                  v-if="selectedTask.status === 'todo'"
                  type="button"
                  class="primary-button"
                  :loading="isPending(taskActionKey('start', selectedTask.id))"
                  loading-label="Starting task"
                  @click="startTask(selectedTask)"
                >
                  Start
                </LoadingButton>
                <LoadingButton
                  v-if="canAcceptTask(selectedTask)"
                  type="button"
                  class="tool-button"
                  :loading="isPending(taskActionKey('mark-done', selectedTask.id))"
                  loading-label="Accepting"
                  @click="markTask(selectedTask.id, 'done')"
                >
                  Accept
                </LoadingButton>
                <LoadingButton
                  v-if="canRequestChanges(selectedTask)"
                  type="button"
                  class="tool-button"
                  :loading="isPending(taskActionKey('request-changes', selectedTask.id))"
                  loading-label="Requesting changes"
                  @click="requestChanges(selectedTask)"
                >
                  Request changes
                </LoadingButton>
                <LoadingButton
                  v-if="selectedTask.status === 'review' && selectedTask.review_skipped_at"
                  type="button"
                  class="tool-button"
                  :loading="isPending(taskActionKey('request-review', selectedTask.id))"
                  loading-label="Requesting review"
                  @click="requestReview(selectedTask.id)"
                >
                  Request review
                </LoadingButton>
                <LoadingButton
                  v-if="selectedSession"
                  type="button"
                  class="tool-button"
                  :loading="isPending(sessionActionKey('open', selectedSession.id))"
                  loading-label="Opening terminal"
                  @click="openSession(selectedSession)"
                >
                  Open terminal
                </LoadingButton>
                <LoadingButton
                  type="button"
                  class="danger-button"
                  :loading="isPending(taskActionKey('delete', selectedTask.id))"
                  loading-label="Deleting task"
                  @click="deleteTask(selectedTask)"
                >
                  Delete
                </LoadingButton>
              </div>
              <form
                v-if="selectedSession"
                class="send-form"
                @submit.prevent="sendDetailMessage"
                @paste="handleAttachmentPaste($event, detailAttachments)"
              >
                <textarea
                  v-model="detailMessage"
                  placeholder="Follow-up instructions..."
                />
                <div
                  v-if="detailAttachments.length > 0"
                  class="attachment-list send-attachments"
                >
                  <div
                    v-for="attachment in detailAttachments"
                    :key="attachment.id"
                    class="attachment-row"
                  >
                    <div class="attachment-thumb">
                      <img
                        :src="attachment.preview_url"
                        :alt="attachment.filename"
                      >
                    </div>
                    <div class="attachment-meta">
                      <strong>{{ attachment.filename }}</strong>
                      <span>{{ attachment.mime_type }} · {{ formatAttachmentSize(attachment.size_bytes) }}</span>
                    </div>
                    <button
                      type="button"
                      class="icon-button"
                      aria-label="Remove attachment"
                      @click="removeDraftAttachment(detailAttachments, attachment)"
                    >
                      x
                    </button>
                  </div>
                </div>
                <LoadingButton
                  type="submit"
                  class="primary-button"
                  :disabled="!detailMessage.trim() && detailAttachments.length === 0"
                  :loading="isPending(selectedTaskSendKey)"
                  loading-label="Sending message"
                >
                  Send
                </LoadingButton>
              </form>
            </div>
          </div>
        </aside>
      </div>
    </Teleport>

    <div
      v-if="showWorkspaceModal"
      class="workspace-modal-overlay"
      @click.self="closeWorkspaceModal"
    >
      <div class="workspace-modal">
        <h3>{{ workspaceModalMode === 'edit' ? 'Edit Workspace' : 'Create Workspace' }}</h3>
        <form @submit.prevent="handleSubmitWorkspace">
          <div class="modal-field">
            <label>Name</label>
            <input
              v-model="workspaceForm.name"
              placeholder="claude_hub"
              autofocus
            >
          </div>
          <div class="modal-field">
            <label>Local workspace dir</label>
            <input
              v-model="workspaceForm.path"
              placeholder="/Users/me/workspace"
            >
            <p class="modal-hint">
              Used as the default working directory for new agents in this workspace.
            </p>
          </div>
          <div class="modal-field">
            <label>Environment</label>
            <div class="segmented-control">
              <button
                type="button"
                :class="['segment-button', { active: workspaceForm.target === 'local' }]"
                :disabled="workspaceModalMode === 'edit'"
                @click="workspaceForm.target = 'local'"
              >
                Local
              </button>
              <button
                type="button"
                :class="['segment-button', { active: workspaceForm.target === 'remote' }]"
                :disabled="workspaceModalMode === 'edit'"
                @click="workspaceForm.target = 'remote'"
              >
                Remote
              </button>
            </div>
          </div>
          <template v-if="workspaceForm.target === 'remote'">
            <div class="modal-field">
              <label>Remote profile</label>
              <select
                v-model="workspaceForm.remote_profile_id"
                :disabled="remoteProfilesLoading || workspaceModalMode === 'edit'"
              >
                <option value="">
                  Select server
                </option>
                <option
                  v-for="profile in remoteProfiles"
                  :key="profile.id"
                  :value="profile.id"
                >
                  {{ profile.name }}
                </option>
              </select>
            </div>
            <div class="modal-field">
              <label>Remote start dir</label>
              <input
                v-model="workspaceForm.remote_cwd"
                placeholder="~"
              >
              <p class="modal-hint">
                Used as the default remote working directory for new agents.
              </p>
            </div>
            <div class="modal-field">
              <label class="checkbox-label">
                <input
                  v-model="workspaceForm.remote_reconnect"
                  type="checkbox"
                >
                Reconnect
              </label>
            </div>
          </template>
          <div class="form-row">
            <div class="modal-field">
              <label>Branch hint</label>
              <input
                v-model="workspaceForm.default_branch"
                placeholder="main"
              >
            </div>
            <div class="modal-field">
              <label>Prefix</label>
              <input
                v-model="workspaceForm.session_prefix"
                placeholder="chub"
                :disabled="workspaceModalMode === 'edit'"
              >
            </div>
          </div>
          <div class="modal-actions">
            <button
              type="button"
              class="tool-button"
              @click="closeWorkspaceModal"
            >
              Cancel
            </button>
            <LoadingButton
              type="submit"
              class="primary-button"
              :disabled="isLoading || (workspaceForm.target === 'remote' && !workspaceForm.remote_profile_id)"
              :loading="isPending(workspaceModalMode === 'edit' ? 'workspace:update' : 'workspace:create')"
              :loading-label="workspaceModalMode === 'edit' ? 'Saving workspace' : 'Creating workspace'"
            >
              {{ workspaceModalMode === 'edit' ? 'Save workspace' : 'Create workspace' }}
            </LoadingButton>
          </div>
        </form>
      </div>
    </div>

    <div
      v-if="showTaskModal"
      class="workspace-modal-overlay"
      @click.self="closeTaskModal"
    >
      <div class="workspace-modal">
        <h3>Add Task</h3>
        <form
          @submit.prevent="handleCreateTask"
          @paste="handleAttachmentPaste($event, taskForm.attachments)"
        >
          <div class="modal-field">
            <label>Title</label>
            <input
              v-model="taskForm.title"
              placeholder="Implement a focused change"
              :disabled="!activeWorkspaceId"
              autofocus
            >
          </div>
          <div class="modal-field">
            <label>Task description</label>
            <textarea
              v-model="taskForm.prompt"
              placeholder="Describe what the workspace agent should implement..."
              :disabled="!activeWorkspaceId"
            />
            <div
              v-if="taskForm.attachments.length > 0"
              class="attachment-list"
            >
              <div
                v-for="attachment in taskForm.attachments"
                :key="attachment.id"
                class="attachment-row"
              >
                <div class="attachment-thumb">
                  <img
                    :src="attachment.preview_url"
                    :alt="attachment.filename"
                  >
                </div>
                <div class="attachment-meta">
                  <strong>{{ attachment.filename }}</strong>
                  <span>{{ attachment.mime_type }} · {{ formatAttachmentSize(attachment.size_bytes) }}</span>
                </div>
                <button
                  type="button"
                  class="icon-button"
                  aria-label="Remove attachment"
                  @click="removeDraftAttachment(taskForm.attachments, attachment)"
                >
                  x
                </button>
              </div>
            </div>
          </div>
          <div class="modal-field">
            <label>Related task</label>
            <select
              v-model="taskForm.related_task_id"
              :disabled="!activeWorkspaceId"
            >
              <option value="">
                None
              </option>
              <option
                v-for="task in tasks"
                :key="task.id"
                :value="task.id"
              >
                {{ task.title }}
              </option>
            </select>
          </div>
          <div class="modal-actions">
            <button
              type="button"
              class="tool-button"
              @click="closeTaskModal"
            >
              Cancel
            </button>
            <LoadingButton
              type="submit"
              class="primary-button"
              :disabled="!activeWorkspaceId || isLoading || !taskForm.title.trim() || (!taskForm.prompt.trim() && taskForm.attachments.length === 0)"
              :loading="isPending('task:create')"
              loading-label="Adding task"
            >
              Add task
            </LoadingButton>
          </div>
        </form>
      </div>
    </div>

    <div
      v-if="showAgentOptionsModal"
      class="workspace-modal-overlay"
      @click.self="closeAgentOptionsModal"
    >
      <div class="workspace-modal agent-manager-modal">
        <h3>Manage Agents</h3>
        <section class="modal-section modal-section--first">
          <div class="modal-section-header">
            <h4>Workspace Agents</h4>
            <span>{{ managedWorkspaceSessions.length }}</span>
          </div>
          <div class="agent-list">
            <article
              v-for="agent in managedWorkspaceSessions"
              :key="agent.id"
              :class="['agent-row', { 'dispatcher-row': agent.role === 'dispatcher' }]"
            >
              <div>
                <strong>{{ agent.title }}</strong>
                <span>{{ agentRoleLabel(agent) }} · {{ agent.agent_type }} · {{ agent.id }}</span>
                <span>{{ agent.target }} · {{ agent.workspace_path }}</span>
              </div>
              <div class="agent-row-meta">
                <span :class="['runtime-pill', `runtime-pill--${agent.runtime_status}`]">
                  {{ agent.runtime_status }}
                </span>
                <span v-if="agent.ephemeral">temporary</span>
                <span>current {{ taskTitle(agent.current_task_id) }}</span>
                <span>queued {{ agent.queued_count }}</span>
              </div>
              <div class="agent-row-actions">
                <LoadingButton
                  type="button"
                  :loading="isPending(sessionActionKey('open', agent.id))"
                  loading-label="Opening agent"
                  @click="openSession(agent)"
                >
                  Open
                </LoadingButton>
                <LoadingButton
                  v-if="agent.role !== 'dispatcher'"
                  type="button"
                  class="danger-button"
                  :disabled="!canDeleteAgent(agent)"
                  :title="agentDeleteTitle(agent)"
                  :loading="isPending(agentActionKey('delete', agent.id))"
                  loading-label="Deleting agent"
                  @click="deleteAgent(agent)"
                >
                  Delete
                </LoadingButton>
              </div>
            </article>
            <div
              v-if="managedWorkspaceSessions.length === 0"
              class="empty-inline"
            >
              No workspace agents.
            </div>
          </div>
        </section>

        <form
          class="modal-section agent-create-form"
          @submit.prevent="handleCreateAdvancedAgent"
        >
          <div class="modal-section-header">
            <h4>Add Agent</h4>
          </div>
          <div class="modal-field">
            <label>Title</label>
            <input
              v-model="agentOptionsForm.title"
              placeholder="Workspace Agent"
            >
          </div>

          <div class="modal-field">
            <label>Role</label>
            <select v-model="agentOptionsForm.role">
              <option value="orchestrator">
                Agent
              </option>
              <option value="reviewer">
                Reviewer
              </option>
            </select>
          </div>

          <div class="modal-field">
            <label>Agent Type</label>
            <select v-model="agentOptionsForm.agent_type">
              <option value="codex">
                Codex
              </option>
              <option value="claude">
                Claude
              </option>
              <option value="cursor">
                Cursor
              </option>
              <option value="terminal">
                Terminal
              </option>
            </select>
          </div>

          <div class="modal-field">
            <label>Run On</label>
            <div class="segmented-control">
              <button
                type="button"
                :class="['segment-button', { active: agentOptionsForm.target === 'local' }]"
                @click="handleAgentTargetChange('local')"
              >
                Local
              </button>
              <button
                type="button"
                :class="['segment-button', { active: agentOptionsForm.target === 'remote' }]"
                @click="handleAgentTargetChange('remote')"
              >
                Remote
              </button>
            </div>
          </div>

          <div
            v-if="agentOptionsForm.target === 'remote'"
            class="modal-field"
          >
            <label>Remote Server</label>
            <select
              v-model="agentOptionsForm.remote_profile_id"
              :disabled="remoteProfilesLoading"
            >
              <option value="">
                Select server
              </option>
              <option
                v-for="profile in remoteProfiles"
                :key="profile.id"
                :value="profile.id"
              >
                {{ profile.name }}
              </option>
            </select>
            <p
              v-if="remoteProfiles.length === 0"
              class="modal-hint"
            >
              Add profiles in ~/.claude_hub/remote_profiles.json or ~/.ssh/config
            </p>
          </div>

          <div class="modal-field">
            <label>Working Directory</label>
            <div class="path-input-row">
              <input
                v-model="agentOptionsForm.cwd"
                :placeholder="agentOptionsForm.target === 'remote' ? '~/workspace/project' : '/Users/me/workspace'"
              >
              <LoadingButton
                type="button"
                class="tool-button"
                :disabled="agentOptionsForm.target === 'remote' && !agentOptionsForm.remote_profile_id"
                :loading="isPending('agent-browser:open')"
                loading-label="Opening browser"
                @click="openAgentDirectoryBrowser"
              >
                Browse
              </LoadingButton>
            </div>
          </div>

          <div
            v-if="agentSupportsSoloMode"
            class="modal-field"
          >
            <label class="checkbox-label">
              <input
                v-model="agentOptionsForm.solo_mode"
                type="checkbox"
              >
              YOLO mode
            </label>
            <p class="modal-hint">
              {{ agentYoloHint }}
            </p>
          </div>

          <div
            v-if="agentOptionsForm.target === 'remote'"
            class="modal-field"
          >
            <label class="checkbox-label">
              <input
                v-model="agentOptionsForm.remote_reconnect"
                type="checkbox"
              >
              Auto reconnect
            </label>
          </div>

          <div class="modal-actions">
            <button
              type="button"
              class="tool-button"
              @click="closeAgentOptionsModal"
            >
              Cancel
            </button>
            <LoadingButton
              type="submit"
              class="primary-button"
              :disabled="isAgentOptionsCreateDisabled"
              :loading="isPending('agent:create')"
              loading-label="Creating agent"
            >
              Create agent
            </LoadingButton>
          </div>
        </form>
      </div>
    </div>

    <div
      v-if="showAgentFileBrowser"
      class="workspace-modal-overlay file-browser-overlay"
      @click.self="showAgentFileBrowser = false"
    >
      <div class="workspace-modal file-browser-modal">
        <div class="file-browser-header">
          <h3>{{ agentOptionsForm.target === 'remote' ? 'Select Remote Directory' : 'Select Working Directory' }}</h3>
          <button
            type="button"
            class="tool-button"
            @click="showAgentFileBrowser = false"
          >
            Close
          </button>
        </div>
        <div class="file-browser-path">
          <LoadingButton
            type="button"
            class="path-nav-button"
            :loading="isPending('agent-browser:home')"
            loading-label="Loading home"
            @click="navigateAgentBrowserHome"
          >
            Home
          </LoadingButton>
          <LoadingButton
            v-if="agentBrowserParentPath"
            type="button"
            class="path-nav-button"
            :loading="isPending('agent-browser:up')"
            loading-label="Loading parent"
            @click="navigateAgentBrowserParent"
          >
            Up
          </LoadingButton>
          <input
            v-model="agentBrowserPathInput"
            @keyup.enter="loadAgentDirectory(agentBrowserPathInput)"
          >
          <LoadingButton
            type="button"
            class="path-nav-button"
            :loading="isPending('agent-browser:refresh')"
            loading-label="Refreshing directory"
            @click="refreshAgentDirectory"
          >
            Refresh
          </LoadingButton>
        </div>
        <div class="file-browser-list">
          <div
            v-if="agentBrowserParentPath"
            class="file-item is-dir"
            @click="loadAgentDirectory(agentBrowserParentPath)"
          >
            <span>Dir</span>
            <strong>..</strong>
          </div>
          <div
            v-for="item in agentBrowserItems"
            :key="item.path"
            :class="['file-item', { 'is-dir': item.is_dir }]"
            @click="handleAgentFileItemClick(item)"
          >
            <span>{{ item.is_dir ? 'Dir' : 'File' }}</span>
            <strong>{{ item.name }}</strong>
          </div>
          <div
            v-if="agentBrowserLoading"
            class="file-status"
          >
            Loading...
          </div>
          <div
            v-if="agentBrowserError"
            class="file-status file-error"
          >
            {{ agentBrowserError }}
          </div>
        </div>
        <div class="modal-actions">
          <button
            type="button"
            class="tool-button"
            @click="showAgentFileBrowser = false"
          >
            Cancel
          </button>
          <button
            type="button"
            class="primary-button"
            @click="selectAgentCurrentDirectory"
          >
            Select directory
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import LoadingButton from '@/components/LoadingButton.vue'
import MarkdownContent from '@/components/MarkdownContent.vue'
import NetworkAccessMenu from '@/components/NetworkAccessMenu.vue'
import { usePendingActions } from '@/composables/usePendingActions'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import type {
  AgentReport,
  AgentRuntimeStatus,
  AgentType,
  AcceptanceCheck,
  ExecutionTarget,
  GoalPacket,
  ManagedSession,
  RemoteProfile,
  TerminalAgentStatus,
  WorkspaceAttachment,
  WorkspaceAttachmentCreate,
  WorkspaceSessionRole,
  WorkspaceTask,
  WorkspaceTaskStatus,
} from '@/types'

interface TaskStartOptions {
  target_session_id: string
  related_task_id: string
  clear_context: boolean
}

interface FileInfo {
  name: string
  path: string
  is_dir: boolean
}

interface DirectoryListing {
  current_path: string
  parent_path: string | null
  items: FileInfo[]
}

interface DraftAttachment extends WorkspaceAttachmentCreate {
  id: string
  preview_url: string
  size_bytes: number
}

type WorkspaceSessionView = 'agents' | 'reviewers'

const appStore = useAppStore()
const terminalStore = useTerminalStore()
const workspaceStore = useWorkspaceStore()
const { isPending, runPending } = usePendingActions()
const { colorScheme } = storeToRefs(appStore)
const {
  workspaces,
  activeWorkspaceId,
  board,
  tasks,
  workspaceAgents,
  reviewerAgents,
  temporaryReviewers,
  dispatcherAgent,
  isLoading,
  error,
} = storeToRefs(workspaceStore)

const selectedWorkspaceId = ref(activeWorkspaceId.value || '')
const selectedTaskId = ref<string | null>(null)
const detailMessage = ref('')
const detailAttachments = ref<DraftAttachment[]>([])
const isDetailActionsExpanded = ref(false)
const showWorkspaceModal = ref(false)
const workspaceModalMode = ref<'create' | 'edit'>('create')
const editingWorkspaceId = ref<string | null>(null)
const showAgentOptionsModal = ref(false)
const showAgentFileBrowser = ref(false)
const showTaskModal = ref(false)
const workspaceSessionView = ref<WorkspaceSessionView>('agents')
const workspaceMobileMenuRef = ref<HTMLDetailsElement | null>(null)
const remoteProfiles = ref<RemoteProfile[]>([])
const remoteProfilesLoading = ref(false)
const agentBrowserCurrentPath = ref('')
const agentBrowserPathInput = ref('')
const agentBrowserParentPath = ref<string | null>(null)
const agentBrowserItems = ref<FileInfo[]>([])
const agentBrowserLoading = ref(false)
const agentBrowserError = ref<string | null>(null)
const mobileCollapsedColumns = reactive<Record<WorkspaceTaskStatus, boolean>>({
  todo: false,
  queued: false,
  working: false,
  review: false,
  done: false,
})
const startOptions = reactive<Record<string, TaskStartOptions>>({})
let boardPollTimer: number | null = null

const columns: { status: WorkspaceTaskStatus; label: string }[] = [
  { status: 'todo', label: 'Todo' },
  { status: 'queued', label: 'Queued' },
  { status: 'working', label: 'Working' },
  { status: 'review', label: 'Review' },
  { status: 'done', label: 'Done' },
]

const workspaceForm = reactive({
  name: 'Claude Hub',
  path: '/Users/bytedance/claude_hub',
  default_branch: 'main',
  session_prefix: 'chub',
  target: 'local' as ExecutionTarget,
  remote_profile_id: '',
  remote_cwd: '',
  remote_reconnect: true,
})

const agentOptionsForm = reactive({
  title: '',
  role: 'orchestrator' as WorkspaceSessionRole,
  agent_type: 'codex' as AgentType,
  target: 'local' as ExecutionTarget,
  cwd: '',
  solo_mode: true,
  remote_profile_id: '',
  remote_reconnect: true,
})

const taskForm = reactive({
  title: '',
  prompt: '',
  related_task_id: '',
  attachments: [] as DraftAttachment[],
})

const activeWorkspace = computed(() =>
  workspaces.value.find(workspace => workspace.id === activeWorkspaceId.value) || null
)

const mobileWorkspaceSummary = computed(() => {
  if (!activeWorkspaceId.value) return 'Create a workspace to begin'

  const agentCount = workspaceAgents.value.length
  const reviewerCount = reviewerAgents.value.length + temporaryReviewers.value.length
  const workingCount = workspaceAgents.value.filter(agent => agent.runtime_status === 'working').length
  const queuedCount = tasksByStatus('queued').length
  return `${agentCount} agents · ${reviewerCount} reviewers · ${workingCount} working · ${queuedCount} queued`
})

const selectedTask = computed(() =>
  tasks.value.find(task => task.id === selectedTaskId.value) || null
)

const selectedSession = computed(() =>
  selectedTask.value ? workspaceStore.sessionForTask(selectedTask.value) : null
)

const selectedTaskSendKey = computed(() =>
  selectedTask.value ? taskActionKey('send', selectedTask.value.id) : 'task:none:send'
)

const selectedReports = computed<AgentReport[]>(() =>
  selectedTask.value ? workspaceStore.reportsForTask(selectedTask.value) : []
)

function goalPacketSections(goalPacket: GoalPacket) {
  return [
    {
      key: 'acceptance_criteria',
      label: 'Acceptance Criteria',
      items: goalPacketItems(goalPacket.acceptance_criteria),
    },
    {
      key: 'validation_plan',
      label: 'Validation Plan',
      items: goalPacketItems(goalPacket.validation_plan),
    },
    {
      key: 'assumptions',
      label: 'Assumptions',
      items: goalPacketItems(goalPacket.assumptions),
    },
    {
      key: 'out_of_scope',
      label: 'Out of Scope',
      items: goalPacketItems(goalPacket.out_of_scope),
    },
    {
      key: 'handoff_requirements',
      label: 'Handoff Requirements',
      items: goalPacketItems(goalPacket.handoff_requirements),
    },
  ]
}

function goalPacketItems(items: string[] | undefined): string[] {
  return Array.isArray(items) ? items : []
}

function acceptanceChecksFor(report: AgentReport): AcceptanceCheck[] {
  return Array.isArray(report.acceptance_check) ? report.acceptance_check : []
}

const reviewerSessions = computed<ManagedSession[]>(() => [
  ...reviewerAgents.value,
  ...temporaryReviewers.value,
])

const managedWorkspaceSessions = computed<ManagedSession[]>(() => [
  ...workspaceAgents.value,
  ...reviewerSessions.value,
  ...(dispatcherAgent.value ? [dispatcherAgent.value] : []),
])

const visibleWorkspaceSessions = computed<ManagedSession[]>(() =>
  workspaceSessionView.value === 'reviewers' ? reviewerSessions.value : workspaceAgents.value
)

const visibleWorkspaceSessionsEmptyText = computed(() =>
  workspaceSessionView.value === 'reviewers'
    ? 'No reviewers in this workspace.'
    : 'No agents in this workspace.'
)

const terminalStatusByTabId = computed<Record<string, TerminalAgentStatus>>(() => {
  const map: Record<string, TerminalAgentStatus> = {}
  for (const status of terminalStore.agentStatuses) {
    map[status.tab_id] = status
  }
  return map
})

const selectedRemoteProfile = computed(() =>
  remoteProfiles.value.find(profile => profile.id === workspaceForm.remote_profile_id) || null
)

const selectedAgentRemoteProfile = computed(() =>
  remoteProfiles.value.find(profile => profile.id === agentOptionsForm.remote_profile_id) || null
)

const agentSupportsSoloMode = computed(
  () =>
    agentOptionsForm.agent_type !== 'cursor' &&
    agentOptionsForm.agent_type !== 'terminal',
)

const agentYoloHint = computed(() => {
  if (agentOptionsForm.agent_type === 'codex') {
    return 'Runs Codex with --ask-for-approval never and --sandbox danger-full-access'
  }
  return 'Runs Claude with IS_SANDBOX=1 and --dangerously-skip-permissions'
})

const isAgentOptionsCreateDisabled = computed(
  () =>
    isLoading.value ||
    isPending('agent:create') ||
    (agentOptionsForm.target === 'remote' && !agentOptionsForm.remote_profile_id)
)

function taskActionKey(action: string, taskId: string | null | undefined) {
  return `task:${taskId || 'none'}:${action}`
}

function agentActionKey(action: string, agentId: string | null | undefined) {
  return `agent:${agentId || 'none'}:${action}`
}

function sessionActionKey(action: string, sessionId: string | null | undefined) {
  return `session:${sessionId || 'none'}:${action}`
}

function startOptionsFor(task: WorkspaceTask): TaskStartOptions {
  if (!startOptions[task.id]) {
    startOptions[task.id] = {
      target_session_id: '',
      related_task_id: '',
      clear_context: false,
    }
  }
  return startOptions[task.id]
}

function tasksByStatus(status: WorkspaceTaskStatus) {
  return tasks.value.filter(task => task.status === status)
}

function sessionForTask(task: WorkspaceTask) {
  return workspaceStore.sessionForTask(task)
}

function latestReportForTask(task: WorkspaceTask) {
  return workspaceStore.latestReportForTask(task)
}

function latestReviewReportForTask(task: WorkspaceTask) {
  const reviewReports = workspaceStore
    .reportsForTask(task)
    .filter(report => report.state.startsWith('review_'))
  return reviewReports[reviewReports.length - 1] || null
}

function reviewStatusLabel(task: WorkspaceTask) {
  if (task.human_accepted_at) return 'Human accepted'
  const latestReviewReport = latestReviewReportForTask(task)
  if (latestReviewReport?.state === 'review_passed') return 'AI review passed, awaiting human acceptance'
  if (latestReviewReport?.state === 'review_failed') return 'Changes requested'
  if (latestReviewReport?.state === 'review_needs_input') return 'Review needs input'
  if (latestReviewReport?.state === 'review_started') return 'Reviewing'
  if (task.review_skipped_at) return 'AI review skipped, awaiting human acceptance'
  if (task.review_requested_at && !task.review_completed_at) return 'Pending review'
  if (task.review_attempts > 0) return `Review attempts ${task.review_attempts}`
  return ''
}

function activeReviewBadge(
  task: WorkspaceTask,
): { kind: 'active' | 'pending' | 'attention'; label: string; title: string } | null {
  const latestReviewReport = latestReviewReportForTask(task)
  if (latestReviewReport?.state === 'review_failed') {
    return {
      kind: 'attention',
      label: 'Changes requested',
      title: 'Reviewer requested changes before this task can be accepted',
    }
  }
  if (latestReviewReport?.state === 'review_needs_input') {
    const reviewerName = task.review_session_id
      ? reviewerTitle(task.review_session_id)
      : 'AI reviewer'
    return {
      kind: 'attention',
      label: 'Review needs input',
      title: `${reviewerName} needs input to continue the review`,
    }
  }
  if (awaitingHumanAcceptance(task)) {
    return {
      kind: 'pending',
      label: 'Awaiting human acceptance',
      title: 'AI review is complete or skipped; human acceptance is required',
    }
  }
  if (task.review_skipped_at) return null
  if (task.review_completed_at) return null
  const reviewerName = task.review_session_id
    ? reviewerTitle(task.review_session_id)
    : 'AI reviewer'
  if (latestReviewReport?.state === 'review_started') {
    return {
      kind: 'active',
      label: 'AI reviewing',
      title: `${reviewerName} is reviewing this task`,
    }
  }
  if (task.review_requested_at) {
    return {
      kind: 'pending',
      label: 'Awaiting AI review',
      title: `Queued for review by ${reviewerName}`,
    }
  }
  return null
}

function awaitingHumanAcceptance(task: WorkspaceTask) {
  const latestReviewReport = latestReviewReportForTask(task)
  return task.status === 'review' && (
    Boolean(task.human_acceptance_requested_at) ||
    Boolean(task.review_skipped_at) ||
    latestReviewReport?.state === 'review_passed'
  )
}

function hasBlockingReviewResult(task: WorkspaceTask) {
  const latestReviewReport = latestReviewReportForTask(task)
  return latestReviewReport?.state === 'review_failed' ||
    latestReviewReport?.state === 'review_needs_input'
}

function canAcceptTask(task: WorkspaceTask) {
  return awaitingHumanAcceptance(task) && !hasBlockingReviewResult(task)
}

function canRequestChanges(task: WorkspaceTask) {
  if (task.status !== 'review' || !sessionForTask(task)) return false
  const latestReviewReport = latestReviewReportForTask(task)
  return awaitingHumanAcceptance(task) || latestReviewReport?.state === 'review_failed'
}

function isLatestSelectedReport(report: AgentReport) {
  return selectedReports.value[selectedReports.value.length - 1]?.id === report.id
}

function agentTitle(sessionId?: string | null) {
  if (!sessionId) return 'auto'
  return managedWorkspaceSessions.value.find(agent => agent.id === sessionId)?.title || sessionId
}

function reviewerTitle(sessionId?: string | null) {
  if (!sessionId) return 'none'
  return managedWorkspaceSessions.value.find(agent => agent.id === sessionId)?.title || sessionId
}

function agentRoleLabel(agent: ManagedSession) {
  if (agent.role === 'dispatcher') return 'Dispatcher'
  if (agent.role === 'reviewer') return agent.ephemeral ? 'Temporary Reviewer' : 'Reviewer'
  return 'Agent'
}

function taskTitle(taskId?: string | null) {
  if (!taskId) return 'none'
  return tasks.value.find(task => task.id === taskId)?.title || taskId
}

function formatAttachmentSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function persistedAttachmentMeta(attachment: WorkspaceAttachment) {
  return `${attachment.filename} (${attachment.mime_type}, ${formatAttachmentSize(attachment.size_bytes)})`
}

function draftAttachmentId() {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID()
  }
  return `attachment-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

function arrayBufferToBase64(buffer: ArrayBuffer) {
  const bytes = new Uint8Array(buffer)
  const chunkSize = 0x8000
  let binary = ''
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize))
  }
  return btoa(binary)
}

async function fileToImageDataUrl(file: File, mimeType: string): Promise<string> {
  const base64 = arrayBufferToBase64(await file.arrayBuffer())
  return `data:${mimeType};base64,${base64}`
}

function imageFilename(file: File) {
  if (file.name) return file.name
  if (file.type === 'image/jpeg') return 'clipboard.jpg'
  if (file.type === 'image/gif') return 'clipboard.gif'
  if (file.type === 'image/webp') return 'clipboard.webp'
  return 'clipboard.png'
}

function imageMimeType(file: File): string | null {
  const mimeType = file.type.trim().toLowerCase()
  if (mimeType.startsWith('image/')) {
    return mimeType === 'image/jpg' ? 'image/jpeg' : mimeType
  }

  const extension = file.name.split('.').pop()?.toLowerCase()
  if (extension === 'png') return 'image/png'
  if (extension === 'jpg' || extension === 'jpeg') return 'image/jpeg'
  if (extension === 'gif') return 'image/gif'
  if (extension === 'webp') return 'image/webp'
  return null
}

function normalizeImageDataUrl(dataUrl: string, mimeType: string) {
  if (dataUrl.startsWith(`data:${mimeType};base64,`)) return dataUrl
  return dataUrl.replace(/^data:[^;,]*(;base64,)/i, `data:${mimeType}$1`)
}

function clipboardImageFiles(event: ClipboardEvent): File[] {
  const clipboard = event.clipboardData
  if (!clipboard) return []

  const files: File[] = []
  const seen = new Set<string>()

  const addFile = (file: File | null) => {
    if (!file || !imageMimeType(file)) return
    const key = `${file.name}:${file.type}:${file.size}:${file.lastModified}`
    if (seen.has(key)) return
    seen.add(key)
    files.push(file)
  }

  for (const item of Array.from(clipboard.items || [])) {
    if (item.kind === 'file') {
      addFile(item.getAsFile())
    }
  }

  for (const file of Array.from(clipboard.files || [])) {
    addFile(file)
  }

  return files
}

function dataUrlFilename(mimeType: string, index: number) {
  if (mimeType === 'image/jpeg') return `clipboard-${index}.jpg`
  if (mimeType === 'image/gif') return `clipboard-${index}.gif`
  if (mimeType === 'image/webp') return `clipboard-${index}.webp`
  return `clipboard-${index}.png`
}

function imageDataUrlsFromText(value: string): string[] {
  const matches = value.match(/data:image\/(?:png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=]+/gi)
  return matches || []
}

function imageDataUrlKey(dataUrl: string): string | null {
  const match = dataUrl.match(/^data:(image\/(?:png|jpeg|jpg|gif|webp));base64,([A-Za-z0-9+/=]+)$/i)
  if (!match) return null
  const mimeType = match[1].toLowerCase() === 'image/jpg' ? 'image/jpeg' : match[1].toLowerCase()
  return `${mimeType}:${match[2]}`
}

function clipboardImageDataUrls(event: ClipboardEvent): string[] {
  const clipboard = event.clipboardData
  if (!clipboard) return []

  const urls = new Set<string>()
  const html = clipboard.getData('text/html')
  if (html) {
    const document = new DOMParser().parseFromString(html, 'text/html')
    for (const image of Array.from(document.images)) {
      if (image.src.startsWith('data:image/')) {
        urls.add(image.src)
      }
    }
    for (const url of imageDataUrlsFromText(html)) {
      urls.add(url)
    }
  }

  for (const url of imageDataUrlsFromText(clipboard.getData('text/plain'))) {
    urls.add(url)
  }

  return Array.from(urls)
}

function addDataUrlAttachments(
  target: DraftAttachment[],
  dataUrls: string[],
  seenDataUrlKeys: Set<string>
) {
  dataUrls.forEach((dataUrl, index) => {
    const match = dataUrl.match(/^data:(image\/(?:png|jpeg|jpg|gif|webp));base64,/i)
    if (!match) return
    const mimeType = match[1].toLowerCase() === 'image/jpg' ? 'image/jpeg' : match[1].toLowerCase()
    const normalizedDataUrl = dataUrl.replace(/^data:image\/jpg;/i, 'data:image/jpeg;')
    const key = imageDataUrlKey(normalizedDataUrl)
    if (key && seenDataUrlKeys.has(key)) return
    if (key) seenDataUrlKeys.add(key)
    target.push({
      id: draftAttachmentId(),
      filename: dataUrlFilename(mimeType, index + 1),
      mime_type: mimeType,
      data_url: normalizedDataUrl,
      preview_url: dataUrl,
      size_bytes: Math.floor((dataUrl.length - dataUrl.indexOf(',') - 1) * 0.75),
    })
  })
}

async function addImageAttachments(
  target: DraftAttachment[],
  files: File[],
  seenDataUrlKeys: Set<string>
) {
  for (const file of files) {
    const mimeType = imageMimeType(file)
    if (!mimeType) continue
    const dataUrl = normalizeImageDataUrl(await fileToImageDataUrl(file, mimeType), mimeType)
    const key = imageDataUrlKey(dataUrl)
    if (key && seenDataUrlKeys.has(key)) continue
    if (key) seenDataUrlKeys.add(key)
    target.push({
      id: draftAttachmentId(),
      filename: imageFilename(file),
      mime_type: mimeType,
      data_url: dataUrl,
      preview_url: dataUrl,
      size_bytes: file.size,
    })
  }
}

async function handleAttachmentPaste(event: ClipboardEvent, target: DraftAttachment[]) {
  const files = clipboardImageFiles(event)
  const dataUrls = clipboardImageDataUrls(event)
  if (files.length === 0 && dataUrls.length === 0) return
  event.preventDefault()
  const seenDataUrlKeys = new Set<string>()
  await addImageAttachments(target, files, seenDataUrlKeys)
  addDataUrlAttachments(target, dataUrls, seenDataUrlKeys)
}

function removeDraftAttachment(target: DraftAttachment[], attachment: DraftAttachment) {
  const index = target.findIndex(item => item.id === attachment.id)
  if (index >= 0) {
    URL.revokeObjectURL(target[index].preview_url)
    target.splice(index, 1)
  }
}

function resetDraftAttachments(target: DraftAttachment[]) {
  for (const attachment of target) {
    URL.revokeObjectURL(attachment.preview_url)
  }
  target.splice(0)
}

function serializeDraftAttachments(target: DraftAttachment[]): WorkspaceAttachmentCreate[] {
  return target.map(({ filename, mime_type, data_url }) => ({ filename, mime_type, data_url }))
}

function terminalStatusForAgent(agent: ManagedSession) {
  return terminalStatusByTabId.value[agent.tab_id] || null
}

function agentRuntimeStatus(agent: ManagedSession): AgentRuntimeStatus {
  return terminalStatusForAgent(agent)?.status || agent.runtime_status
}

function agentRuntimeText(agent: ManagedSession) {
  return terminalStatusForAgent(agent)?.status_text || agent.runtime_status
}

function agentRuntimeDetail(agent: ManagedSession) {
  const status = terminalStatusForAgent(agent)
  if (status?.detail) return status.detail
  if (agent.current_task_id) return `Current task: ${taskTitle(agent.current_task_id)}`
  return agent.workspace_path
}

function agentTaskLabel(agent: ManagedSession) {
  if (!agent.current_task_id) return 'no task'
  return taskTitle(agent.current_task_id)
}

function openTasksForAgent(agent: ManagedSession) {
  return tasks.value.filter(
    task =>
      (task.session_id === agent.id || task.review_session_id === agent.id) &&
      task.status !== 'done'
  )
}

function agentDeleteDisabledReason(agent: ManagedSession) {
  const openTasks = openTasksForAgent(agent)
  if (openTasks.length === 0) return ''
  if (openTasks.length === 1) {
    return `Cannot delete while "${openTasks[0].title}" is still assigned`
  }
  return `Cannot delete while ${openTasks.length} tasks are still assigned`
}

function canDeleteAgent(agent: ManagedSession) {
  return agentDeleteDisabledReason(agent) === ''
}

function agentDeleteTitle(agent: ManagedSession) {
  return agentDeleteDisabledReason(agent) || `Delete ${agent.title}`
}

function isInteractiveElement(target: EventTarget | null) {
  return target instanceof HTMLElement && Boolean(target.closest(
    'button, a, input, select, textarea, summary, details, label'
  ))
}

function selectTask(event: MouseEvent, taskId: string) {
  if (isInteractiveElement(event.target)) return
  if (selectedTaskId.value !== taskId) {
    detailMessage.value = ''
    resetDraftAttachments(detailAttachments.value)
    isDetailActionsExpanded.value = false
  }
  selectedTaskId.value = taskId
}

function isMobileColumnCollapsed(status: WorkspaceTaskStatus) {
  return mobileCollapsedColumns[status]
}

function toggleMobileColumn(status: WorkspaceTaskStatus) {
  mobileCollapsedColumns[status] = !mobileCollapsedColumns[status]
}

function toggleDetailActions() {
  isDetailActionsExpanded.value = !isDetailActionsExpanded.value
}

function closeTaskDetail() {
  selectedTaskId.value = null
  detailMessage.value = ''
  resetDraftAttachments(detailAttachments.value)
  isDetailActionsExpanded.value = false
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

const REPORT_LANG_STORAGE_KEY = 'claude-hub:report-lang'
type ReportLang = 'en' | 'zh'

const reportLang = ref<ReportLang>(
  (typeof localStorage !== 'undefined' && (localStorage.getItem(REPORT_LANG_STORAGE_KEY) as ReportLang | null)) || 'en'
)

function setReportLang(lang: ReportLang) {
  reportLang.value = lang
  try {
    localStorage.setItem(REPORT_LANG_STORAGE_KEY, lang)
  } catch {
    /* ignore quota / disabled storage */
  }
}

function reportMessageForLang(report: AgentReport): string {
  const preferred = reportLang.value === 'zh' ? report.message_zh : report.message_en
  const fallback = reportLang.value === 'zh' ? report.message_en : report.message_zh
  return preferred || fallback || report.message
}

const hasBilingualReport = computed(() =>
  selectedReports.value.some((r) => (r.message_en && r.message_en.trim()) || (r.message_zh && r.message_zh.trim()))
)

async function fetchRemoteProfiles() {
  remoteProfilesLoading.value = true
  try {
    const response = await fetch('/api/remote/profiles')
    if (!response.ok) throw new Error('Failed to load remote profiles')
    remoteProfiles.value = await response.json()
    if (!workspaceForm.remote_profile_id && remoteProfiles.value.length > 0) {
      workspaceForm.remote_profile_id = remoteProfiles.value[0].id
    }
  } catch (e) {
    workspaceStore.error = e instanceof Error ? e.message : 'Failed to load remote profiles'
  } finally {
    remoteProfilesLoading.value = false
  }
}

async function handleSubmitWorkspace() {
  if (workspaceModalMode.value === 'edit') {
    return handleSaveWorkspace()
  }
  return handleCreateWorkspace()
}

async function handleCreateWorkspace() {
  const workspace = await runPending('workspace:create', () =>
    workspaceStore.createWorkspace({
      name: workspaceForm.name.trim(),
      path: workspaceForm.path.trim(),
      default_branch: workspaceForm.default_branch.trim() || 'main',
      session_prefix: workspaceForm.session_prefix.trim() || undefined,
      target: workspaceForm.target,
      remote_profile_id:
        workspaceForm.target === 'remote' ? workspaceForm.remote_profile_id || null : null,
      remote_cwd:
        workspaceForm.target === 'remote' ? workspaceForm.remote_cwd.trim() || null : null,
      remote_reconnect: workspaceForm.remote_reconnect,
    })
  )
  if (workspace) {
    selectedWorkspaceId.value = workspace.id
    showWorkspaceModal.value = false
  }
}

async function handleSaveWorkspace() {
  const workspaceId = editingWorkspaceId.value
  if (!workspaceId) return
  const workspace = await runPending('workspace:update', () =>
    workspaceStore.updateWorkspace(workspaceId, {
      name: workspaceForm.name.trim() || undefined,
      path: workspaceForm.path.trim() || undefined,
      default_branch: workspaceForm.default_branch.trim() || undefined,
      remote_cwd:
        workspaceForm.target === 'remote' ? workspaceForm.remote_cwd.trim() || null : undefined,
      remote_reconnect:
        workspaceForm.target === 'remote' ? workspaceForm.remote_reconnect : undefined,
    })
  )
  if (workspace) {
    showWorkspaceModal.value = false
  }
}

function resetWorkspaceForm() {
  workspaceForm.name = 'Claude Hub'
  workspaceForm.path = '/Users/bytedance/claude_hub'
  workspaceForm.default_branch = 'main'
  workspaceForm.session_prefix = 'chub'
  workspaceForm.target = 'local'
  workspaceForm.remote_profile_id = remoteProfiles.value[0]?.id || ''
  workspaceForm.remote_cwd = ''
  workspaceForm.remote_reconnect = true
}

function openWorkspaceModal() {
  resetWorkspaceForm()
  workspaceModalMode.value = 'create'
  editingWorkspaceId.value = null
  showWorkspaceModal.value = true
}

function openEditWorkspaceModal() {
  const workspace = activeWorkspace.value
  if (!workspace) return
  workspaceForm.name = workspace.name
  workspaceForm.path = workspace.path
  workspaceForm.default_branch = workspace.default_branch
  workspaceForm.session_prefix = workspace.session_prefix
  workspaceForm.target = workspace.target
  workspaceForm.remote_profile_id = workspace.remote_profile_id || ''
  workspaceForm.remote_cwd = workspace.remote_cwd || ''
  workspaceForm.remote_reconnect = workspace.remote_reconnect
  workspaceModalMode.value = 'edit'
  editingWorkspaceId.value = workspace.id
  showWorkspaceModal.value = true
  if (workspace.target === 'remote') {
    fetchRemoteProfiles()
  }
}

function closeWorkspaceModal() {
  showWorkspaceModal.value = false
  workspaceModalMode.value = 'create'
  editingWorkspaceId.value = null
}

function closeWorkspaceMobileMenu() {
  if (workspaceMobileMenuRef.value) {
    workspaceMobileMenuRef.value.open = false
  }
}

function handleWorkspaceDocumentPointerDown(event: PointerEvent) {
  const target = event.target
  if (
    target instanceof Node &&
    workspaceMobileMenuRef.value?.open &&
    !workspaceMobileMenuRef.value.contains(target)
  ) {
    closeWorkspaceMobileMenu()
  }
}

function goToTerminalMode() {
  appStore.setMode('terminal')
  closeWorkspaceMobileMenu()
}

function openWorkspaceModalFromMenu() {
  openWorkspaceModal()
  closeWorkspaceMobileMenu()
}

function openEditWorkspaceModalFromMenu() {
  openEditWorkspaceModal()
  closeWorkspaceMobileMenu()
}

function openAgentOptionsModalFromMenu() {
  openAgentOptionsModal()
  closeWorkspaceMobileMenu()
}

function toggleThemeFromMenu() {
  appStore.toggleColorScheme()
  closeWorkspaceMobileMenu()
}

function workspaceDefaultCwd(target: ExecutionTarget): string {
  const workspace = activeWorkspace.value
  if (!workspace) return ''
  if (target === 'remote') {
    return workspace.remote_cwd || selectedAgentRemoteProfile.value?.default_cwd || ''
  }
  return workspace.path || ''
}

function resetAgentOptionsForm() {
  const workspace = activeWorkspace.value
  agentOptionsForm.title = ''
  agentOptionsForm.role = 'orchestrator'
  agentOptionsForm.agent_type = 'codex'
  agentOptionsForm.target = 'local'
  agentOptionsForm.solo_mode = true
  agentOptionsForm.remote_reconnect = workspace?.remote_reconnect ?? true
  agentOptionsForm.remote_profile_id =
    workspace?.remote_profile_id || remoteProfiles.value[0]?.id || ''
  agentOptionsForm.cwd = workspaceDefaultCwd(agentOptionsForm.target)
}

function handleAgentTargetChange(target: ExecutionTarget) {
  if (agentOptionsForm.target === target) return
  const previousDefault = workspaceDefaultCwd(agentOptionsForm.target)
  agentOptionsForm.target = target
  if (!agentOptionsForm.cwd.trim() || agentOptionsForm.cwd === previousDefault) {
    agentOptionsForm.cwd = workspaceDefaultCwd(target)
  }
  if (target === 'remote') {
    fetchRemoteProfiles()
  }
}

function openAgentOptionsModal() {
  resetAgentOptionsForm()
  if (agentOptionsForm.target === 'remote') {
    fetchRemoteProfiles()
  }
  showAgentOptionsModal.value = true
}

function closeAgentOptionsModal() {
  showAgentOptionsModal.value = false
  showAgentFileBrowser.value = false
}

async function handleCreateAdvancedAgent() {
  const cwd = agentOptionsForm.cwd.trim()
  await runPending('agent:create', async () => {
    await workspaceStore.ensureWorkspaceAgent({
      agent_type: agentOptionsForm.agent_type,
      title: agentOptionsForm.title.trim() || null,
      role: agentOptionsForm.role,
      reuse_existing: false,
      target: agentOptionsForm.target,
      cwd: agentOptionsForm.target === 'local' ? cwd || null : null,
      remote_profile_id:
        agentOptionsForm.target === 'remote' ? agentOptionsForm.remote_profile_id || null : null,
      remote_cwd: agentOptionsForm.target === 'remote' ? cwd || null : null,
      remote_reconnect:
        agentOptionsForm.target === 'remote' ? agentOptionsForm.remote_reconnect : null,
      solo_mode:
        agentOptionsForm.agent_type === 'cursor' ||
        agentOptionsForm.agent_type === 'terminal'
          ? false
          : agentOptionsForm.solo_mode,
    })
    showAgentFileBrowser.value = false
    agentOptionsForm.title = ''
    await terminalStore.fetchTabs()
  })
}

async function listAgentDirectory(path?: string): Promise<DirectoryListing> {
  const params = new URLSearchParams()
  if (path) {
    params.append('path', path)
  }
  if (agentOptionsForm.target === 'remote') {
    if (!agentOptionsForm.remote_profile_id) {
      throw new Error('Select a remote server first')
    }
    params.append('profile_id', agentOptionsForm.remote_profile_id)
  }
  const endpoint =
    agentOptionsForm.target === 'remote' ? '/api/remote/filesystem/list' : '/api/filesystem/list'
  const queryString = params.toString()
  const response = await fetch(`${endpoint}${queryString ? `?${queryString}` : ''}`)
  if (!response.ok) {
    const error = await response.text()
    throw new Error(error || 'Failed to list directory')
  }
  return await response.json()
}

async function loadAgentDirectory(path?: string, pendingKey = 'agent-browser:load') {
  await runPending(pendingKey, async () => {
    agentBrowserLoading.value = true
    agentBrowserError.value = null
    try {
      const listing = await listAgentDirectory(path)
      agentBrowserCurrentPath.value = listing.current_path
      agentBrowserPathInput.value = listing.current_path
      agentBrowserParentPath.value = listing.parent_path
      agentBrowserItems.value = listing.items
    } catch (e) {
      agentBrowserError.value = e instanceof Error ? e.message : 'Failed to load directory'
    } finally {
      agentBrowserLoading.value = false
    }
  })
}

async function openAgentDirectoryBrowser() {
  showAgentFileBrowser.value = true
  if (agentOptionsForm.cwd) {
    await loadAgentDirectory(agentOptionsForm.cwd, 'agent-browser:open')
  } else if (agentOptionsForm.target === 'remote') {
    await loadAgentDirectory(selectedAgentRemoteProfile.value?.default_cwd || '~', 'agent-browser:open')
  } else {
    await loadAgentDirectory('~', 'agent-browser:open')
  }
}

function navigateAgentBrowserHome() {
  if (agentOptionsForm.target === 'remote') {
    loadAgentDirectory(selectedAgentRemoteProfile.value?.default_cwd || '~', 'agent-browser:home')
  } else {
    loadAgentDirectory('~', 'agent-browser:home')
  }
}

function navigateAgentBrowserParent() {
  if (!agentBrowserParentPath.value) return
  loadAgentDirectory(agentBrowserParentPath.value, 'agent-browser:up')
}

function refreshAgentDirectory() {
  loadAgentDirectory(
    agentBrowserCurrentPath.value || agentBrowserPathInput.value || '~',
    'agent-browser:refresh'
  )
}

function handleAgentFileItemClick(item: FileInfo) {
  if (item.is_dir) {
    loadAgentDirectory(item.path)
  }
}

function selectAgentCurrentDirectory() {
  agentOptionsForm.cwd = agentBrowserCurrentPath.value
  showAgentFileBrowser.value = false
}

function resetTaskForm() {
  taskForm.title = ''
  taskForm.prompt = ''
  taskForm.related_task_id = ''
  resetDraftAttachments(taskForm.attachments)
}

function openTaskModal() {
  resetTaskForm()
  showTaskModal.value = true
}

function closeTaskModal() {
  showTaskModal.value = false
  resetDraftAttachments(taskForm.attachments)
}

async function handleCreateTask() {
  if (!taskForm.title.trim() || (!taskForm.prompt.trim() && taskForm.attachments.length === 0)) {
    return
  }
  await runPending('task:create', async () => {
    await workspaceStore.createTask({
      title: taskForm.title.trim(),
      prompt: taskForm.prompt.trim(),
      related_task_id: taskForm.related_task_id || null,
      attachments: serializeDraftAttachments(taskForm.attachments),
    })
    taskForm.title = ''
    taskForm.prompt = ''
    taskForm.related_task_id = ''
    resetDraftAttachments(taskForm.attachments)
    showTaskModal.value = false
  })
}

async function handleWorkspaceChange() {
  if (!selectedWorkspaceId.value) return
  const workspaceId = selectedWorkspaceId.value
  await runPending('workspace:switch', async () => {
    workspaceStore.setActiveWorkspace(workspaceId)
    await workspaceStore.fetchBoard(workspaceId)
  })
}

async function refreshBoard() {
  try {
    await workspaceStore.fetchBoard()
  } catch {
    // Error state is owned by the workspace store.
  }
}

async function refreshAgentStatuses() {
  await runPending('workspace:refresh-statuses', () =>
    Promise.all([
      workspaceStore.fetchBoard(),
      terminalStore.fetchAgentStatuses(),
    ])
  )
}

async function refreshAgentStatusesFromMenu() {
  await refreshAgentStatuses()
  closeWorkspaceMobileMenu()
}

async function startTask(task: WorkspaceTask) {
  await runPending(taskActionKey('start', task.id), async () => {
    const options = startOptionsFor(task)
    await workspaceStore.startTask(task.id, {
      target_session_id: options.target_session_id || null,
      related_task_id: options.related_task_id || null,
      clear_context: options.clear_context ? true : null,
    })
    await terminalStore.fetchTabs()
  })
}

async function openSession(session: ManagedSession) {
  await runPending(sessionActionKey('open', session.id), async () => {
    await terminalStore.fetchTabs()
    appStore.setMode('terminal')
    terminalStore.setActiveTab(session.tab_id)
  })
}

async function sendDetailMessage() {
  if (
    !selectedTask.value ||
    !selectedSession.value ||
    (!detailMessage.value.trim() && detailAttachments.value.length === 0)
  ) {
    return
  }
  const task = selectedTask.value
  const session = selectedSession.value
  const message = detailMessage.value.trim()
  const attachments = serializeDraftAttachments(detailAttachments.value)
  await runPending(taskActionKey('send', task.id), async () => {
    if (task.status === 'review') {
      await workspaceStore.continueTask(task.id, { message, attachments })
    } else {
      await workspaceStore.sendMessage(session.id, message, attachments)
    }
    detailMessage.value = ''
    resetDraftAttachments(detailAttachments.value)
  })
}

async function markTask(taskId: string, status: WorkspaceTaskStatus) {
  await runPending(taskActionKey(`mark-${status}`, taskId), () =>
    workspaceStore.updateTaskStatus(taskId, status)
  )
}

async function requestChanges(task: WorkspaceTask) {
  const message = window.prompt(
    'Describe the changes needed before accepting this task:',
    detailMessage.value.trim(),
  )
  if (!message || !message.trim()) return
  detailMessage.value = ''
  await runPending(taskActionKey('request-changes', task.id), () =>
    workspaceStore.continueTask(task.id, { message: message.trim() })
  )
}

async function requestReview(taskId: string) {
  await runPending(taskActionKey('request-review', taskId), () =>
    workspaceStore.requestTaskReview(taskId)
  )
}

async function deleteTask(task: WorkspaceTask) {
  const confirmed = window.confirm(`Delete task "${task.title}"?`)
  if (!confirmed) return
  await runPending(taskActionKey('delete', task.id), async () => {
    await workspaceStore.deleteTask(task.id)
    if (selectedTaskId.value === task.id) {
      closeTaskDetail()
    }
  })
}

async function deleteAgent(agent: ManagedSession) {
  const confirmed = window.confirm(`Delete agent "${agent.title}"?`)
  if (!confirmed) return
  await runPending(agentActionKey('delete', agent.id), async () => {
    await workspaceStore.deleteSession(agent.id)
    await terminalStore.fetchTabs()
  })
}

watch(tasks, value => {
  if (selectedTaskId.value && !value.some(task => task.id === selectedTaskId.value)) {
    closeTaskDetail()
  }
})

watch(activeWorkspaceId, value => {
  selectedWorkspaceId.value = value || ''
  workspaceSessionView.value = 'agents'
  closeTaskDetail()
})

watch(
  () => workspaceForm.target,
  target => {
    if (target === 'remote') {
      fetchRemoteProfiles()
      if (!workspaceForm.remote_cwd && selectedRemoteProfile.value?.default_cwd) {
        workspaceForm.remote_cwd = selectedRemoteProfile.value.default_cwd
      }
    }
  }
)

watch(
  () => workspaceForm.remote_profile_id,
  () => {
    if (workspaceForm.target === 'remote' && !workspaceForm.remote_cwd) {
      workspaceForm.remote_cwd = selectedRemoteProfile.value?.default_cwd || ''
    }
  }
)

watch(
  () => agentOptionsForm.agent_type,
  agentType => {
    if (agentType === 'cursor' || agentType === 'terminal') {
      agentOptionsForm.solo_mode = false
    } else if (!showAgentOptionsModal.value) {
      agentOptionsForm.solo_mode = true
    }
  }
)

watch(
  () => agentOptionsForm.target,
  target => {
    if (target === 'remote') {
      fetchRemoteProfiles()
      if (!agentOptionsForm.remote_profile_id) {
        agentOptionsForm.remote_profile_id =
          activeWorkspace.value?.remote_profile_id || remoteProfiles.value[0]?.id || ''
      }
      agentOptionsForm.cwd =
        activeWorkspace.value?.remote_cwd || selectedAgentRemoteProfile.value?.default_cwd || '~'
    } else {
      agentOptionsForm.cwd = activeWorkspace.value?.path || ''
    }
  }
)

watch(
  () => agentOptionsForm.remote_profile_id,
  () => {
    if (
      agentOptionsForm.target === 'remote' &&
      (!agentOptionsForm.cwd || agentOptionsForm.cwd === '~')
    ) {
      agentOptionsForm.cwd = selectedAgentRemoteProfile.value?.default_cwd || '~'
    }
  }
)

onMounted(async () => {
  document.addEventListener('pointerdown', handleWorkspaceDocumentPointerDown)
  await fetchRemoteProfiles()
  await workspaceStore.fetchWorkspaces()
  terminalStore.startAgentStatusPolling()
  boardPollTimer = window.setInterval(refreshBoard, 2500)
})

onUnmounted(() => {
  document.removeEventListener('pointerdown', handleWorkspaceDocumentPointerDown)
  if (boardPollTimer !== null) {
    window.clearInterval(boardPollTimer)
    boardPollTimer = null
  }
  resetDraftAttachments(taskForm.attachments)
  resetDraftAttachments(detailAttachments.value)
  terminalStore.stopAgentStatusPolling()
})
</script>

<style scoped>
.workspace-view {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--ch-color-app-bg);
  color: var(--ch-color-text);
}

.workspace-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 18px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface-raised);
}

.workspace-header h1 {
  font-size: 18px;
  line-height: 1.2;
  margin: 0 0 4px;
  color: var(--ch-color-text-strong);
}

.workspace-header p {
  margin: 0;
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.workspace-title-block {
  min-width: 0;
}

.workspace-mobile-identity,
.workspace-mobile-menu {
  display: none;
}

.workspace-actions,
.form-row,
.task-actions,
.session-meta,
.agent-status-toolbar,
.agent-row-actions,
.agent-row-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.workspace-actions {
  justify-content: flex-end;
  flex-wrap: wrap;
}

.workspace-mobile-menu {
  position: relative;
}

.workspace-mobile-menu summary {
  list-style: none;
}

.workspace-mobile-menu summary::-webkit-details-marker {
  display: none;
}

.workspace-mobile-menu-trigger {
  width: 34px;
  height: 34px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  font-size: 20px;
  line-height: 1;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast);
}

.workspace-mobile-menu-trigger:hover,
.workspace-mobile-menu[open] .workspace-mobile-menu-trigger {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
}

.workspace-mobile-menu-panel {
  position: absolute;
  top: calc(100% + 7px);
  right: 0;
  z-index: 80;
  width: 188px;
  max-height: min(560px, calc(100dvh - 96px));
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 6px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-glass);
  box-shadow: var(--ch-shadow-soft);
  scrollbar-width: thin;
  touch-action: pan-y;
  -webkit-overflow-scrolling: touch;
}

.workspace-mobile-menu-item {
  width: 100%;
  min-height: 34px;
  display: flex;
  align-items: center;
  padding: 5px 8px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text);
  font-size: 12px;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
}

.workspace-mobile-menu-item:hover {
  background: var(--ch-color-surface-control-hover);
}

.workspace-mobile-menu-item--mode {
  justify-content: space-between;
  border-color: var(--ch-color-border-muted);
  background: var(--ch-color-surface-soft);
}

.workspace-mobile-menu-item--mode + .workspace-mobile-menu-item:not(.workspace-mobile-menu-item--mode) {
  margin-top: 4px;
}

.workspace-mobile-menu-item--mode.active {
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-accent-soft);
}

.workspace-mobile-menu-item--mode strong {
  border-radius: 999px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: 10px;
  line-height: 1;
  padding: 4px 7px;
  text-transform: uppercase;
}

.workspace-mobile-menu-item:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.workspace-mobile-menu-item--theme {
  justify-content: space-between;
  margin-top: 4px;
  border-color: var(--ch-color-accent-ring-strong);
  background: var(--ch-color-accent-soft);
}

.workspace-mobile-menu-item--theme strong {
  border-radius: 999px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: 10px;
  line-height: 1;
  padding: 4px 7px;
  text-transform: uppercase;
}

.workspace-summary-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 18px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-canvas);
}

.workspace-summary-primary,
.workspace-column-tabs {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.workspace-summary-primary {
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.workspace-summary-primary strong {
  color: var(--ch-color-text);
}

.workspace-column-tabs {
  overflow-x: auto;
}

.workspace-column-tabs span {
  flex: 0 0 auto;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  background: var(--ch-color-chip-bg-muted);
  color: var(--ch-color-text);
  font-size: 11px;
  padding: 4px 9px;
}

.workspace-agent-status {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 10px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-canvas);
  padding: 12px 18px;
}

.agent-status-header {
  min-width: 210px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.agent-status-view-switch {
  min-width: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.agent-status-view-switch button {
  height: 30px;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  cursor: pointer;
  padding: 0 9px;
  text-align: left;
}

.agent-status-view-switch button[data-active='true'] {
  border-color: var(--ch-color-border-strong);
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.agent-status-view-switch span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 700;
}

.agent-status-view-switch strong {
  min-width: 20px;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: currentColor;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
}

.agent-status-eyebrow {
  color: var(--ch-color-text-muted);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}

.agent-status-toolbar {
  justify-content: flex-end;
}

.agent-status-refresh {
  height: 28px;
  width: 28px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
}

.agent-status-grid {
  min-width: 0;
  display: flex;
  flex-wrap: nowrap;
  gap: 8px;
  overflow-x: auto;
  overflow-y: hidden;
  padding-bottom: 2px;
  scroll-snap-type: x proximity;
  -webkit-overflow-scrolling: touch;
}

.agent-status-card {
  min-width: 320px;
  flex: 0 0 clamp(320px, 34vw, 520px);
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface);
  color: inherit;
  padding: 10px 11px;
  scroll-snap-align: start;
  box-shadow: 0 1px 0 var(--ch-shadow-color-soft);
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), transform var(--ch-motion-fast), box-shadow var(--ch-motion-fast);
}

.agent-status-card:hover {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-raised);
  box-shadow: 0 8px 24px var(--ch-shadow-color-soft);
  transform: translateY(-1px);
}

.agent-status-card-main {
  min-width: 0;
  display: grid;
  grid-template-columns: 12px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  padding: 0;
  text-align: left;
}

.agent-status-card-main:focus-visible,
.agent-status-delete:focus-visible,
.agent-status-refresh:focus-visible,
.agent-status-view-switch button:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring-strong);
  outline-offset: 2px;
}

.agent-status-card-main:active,
.agent-status-delete:active,
.agent-status-refresh:active,
.agent-status-view-switch button:active {
  transform: translateY(1px);
}

.agent-status-card-main:disabled {
  cursor: wait;
  opacity: 0.8;
}

.agent-status-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: currentColor;
}

.agent-status-main {
  min-width: 0;
}

.agent-status-line,
.agent-status-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.agent-status-name {
  min-width: 0;
  color: var(--ch-color-text);
  font-size: 13px;
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-status-kind {
  flex: 0 0 auto;
  color: var(--ch-color-text-muted);
  font-size: 10px;
  text-transform: uppercase;
}

.agent-status-detail {
  display: block;
  margin-top: 3px;
  color: var(--ch-color-text-muted);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-status-meta {
  margin-top: 6px;
  flex-wrap: wrap;
}

.agent-status-meta span {
  max-width: 100%;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
  font-size: 10px;
  padding: 3px 7px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-status-pill {
  min-width: 74px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: currentColor;
  font-size: 11px;
  font-weight: 700;
  padding: 5px 8px;
}

.agent-status-inline-spinner {
  width: 0.9em;
  height: 0.9em;
  flex: 0 0 auto;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 999px;
  animation: agent-status-spin 700ms linear infinite;
}

@keyframes agent-status-spin {
  to {
    transform: rotate(360deg);
  }
}

.agent-status-dot[data-status='idle'],
.agent-status-pill[data-status='idle'] {
  color: var(--ch-color-success);
}

.agent-status-dot[data-status='working'],
.agent-status-pill[data-status='working'] {
  color: var(--ch-color-warning);
}

.agent-status-dot[data-status='attention'],
.agent-status-pill[data-status='attention'] {
  color: var(--ch-color-attention);
}

.agent-status-dot[data-status='offline'],
.agent-status-pill[data-status='offline'] {
  color: var(--ch-color-text-muted);
}

.agent-status-pill[data-status='idle'] {
  background: var(--ch-color-success-bg);
}

.agent-status-pill[data-status='working'] {
  background: var(--ch-color-warning-bg);
}

.agent-status-pill[data-status='attention'] {
  background: var(--ch-color-attention-bg);
}

.agent-status-pill[data-status='offline'] {
  background: var(--ch-color-chip-bg);
}

.agent-status-actions {
  justify-content: flex-end;
  padding-left: 22px;
}

.agent-status-delete {
  height: 26px;
  border: 1px solid var(--ch-color-danger-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-danger-bg);
  color: var(--ch-color-danger-text);
  cursor: pointer;
  font-size: 12px;
  padding: 0 9px;
}

.agent-status-delete:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.agent-status-empty {
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  padding: 8px 0;
}

.workspace-select-shell {
  position: relative;
  min-width: 220px;
}

.workspace-select,
.tool-button,
.primary-button,
.danger-button,
.advanced-start select {
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
}

.workspace-select,
.tool-button,
.primary-button,
.danger-button {
  height: 30px;
  padding: 0 10px;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.tool-button:hover,
.primary-button:hover,
.danger-button:hover {
  border-color: var(--ch-color-border-hover);
}

.workspace-select {
  width: 100%;
  min-width: 220px;
  padding-right: 32px;
}

.workspace-select-shell[data-loading='true'] .workspace-select {
  cursor: wait;
  opacity: 0.75;
}

.workspace-select-spinner {
  position: absolute;
  right: 10px;
  top: 50%;
  width: 14px;
  height: 14px;
  margin-top: -7px;
  pointer-events: none;
  border: 2px solid var(--ch-color-text);
  border-right-color: transparent;
  border-radius: 999px;
  animation: workspace-select-spin 700ms linear infinite;
}

@keyframes workspace-select-spin {
  to {
    transform: rotate(360deg);
  }
}

.tool-button,
.primary-button,
.danger-button,
.task-actions button,
.agent-row button {
  cursor: pointer;
}

.tool-button:disabled,
.primary-button:disabled,
.danger-button:disabled,
.task-actions button:disabled,
.agent-row button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.primary-button {
  background: var(--ch-color-accent-strong);
  border-color: var(--ch-color-accent-strong);
  color: var(--ch-color-text-inverse);
  font-weight: 700;
}

.primary-button:hover {
  background: var(--ch-color-accent-hover);
  border-color: var(--ch-color-accent-hover);
}

.danger-button {
  background: var(--ch-color-danger-bg);
  border-color: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
}

.workspace-error {
  padding: 8px 16px;
  background: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
  font-size: 13px;
}

.workspace-layout {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
}

.column-header h2,
.task-card h3 {
  margin: 0;
}

.advanced-start label {
  display: flex;
  flex-direction: column;
  gap: 5px;
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.advanced-start select {
  height: 32px;
  padding: 0 9px;
}

.form-row > label,
.form-row > .modal-field {
  flex: 1;
}

.agent-submit-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.compact-button {
  flex: 0 0 auto;
}

.agent-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.agent-row {
  border: 1px solid var(--ch-color-surface-control-active);
  border-radius: 6px;
  background: var(--ch-color-surface-soft);
  padding: 9px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.agent-row strong {
  display: block;
  color: var(--ch-color-text-strong);
  font-size: 12px;
}

.agent-row span {
  display: block;
  color: var(--ch-color-text-muted);
  font-size: 11px;
}

.dispatcher-row {
  border-color: var(--ch-color-discovery-border);
  background: var(--ch-color-discovery-bg);
}

.runtime-pill {
  border-radius: 999px;
  padding: 3px 7px;
  background: var(--ch-color-border-strong);
  color: var(--ch-color-text);
}

.runtime-pill--idle {
  background: var(--ch-color-success-bg);
  color: var(--ch-color-success);
}

.runtime-pill--working {
  background: var(--ch-color-warning-bg);
  color: var(--ch-color-warning);
}

.runtime-pill--attention {
  background: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
}

.runtime-pill--offline {
  background: var(--ch-color-surface-muted);
  color: var(--ch-color-text-muted);
}

.empty-inline {
  color: var(--ch-color-text-subtle);
  font-size: 12px;
}

.board {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-columns: repeat(5, minmax(220px, 1fr));
  gap: 12px;
  overflow: auto;
  padding: 14px;
}

.empty-board {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ch-color-text-muted);
}

.task-column {
  min-width: 220px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-raised);
  overflow: hidden;
}

.column-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px 12px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface);
}

.column-header h2 {
  color: var(--ch-color-text-strong);
  font-size: 13px;
}

.column-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.column-header span {
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.column-collapse-button {
  display: none;
}

.task-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
  padding: 10px;
}

.task-card {
  position: relative;
  flex: 0 0 auto;
  overflow: hidden;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
  padding: 10px 10px 10px 12px;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), box-shadow var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.task-card::before {
  content: '';
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  background: var(--ch-color-text-subtle);
}

.task-card:hover {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-raised);
  box-shadow: 0 8px 20px var(--ch-shadow-color-soft);
  transform: translateY(-1px);
}

.task-card.selected {
  border-color: var(--ch-color-accent);
  background: var(--ch-color-surface-selected);
  box-shadow: 0 0 0 1px var(--ch-color-accent-ring), 0 8px 20px var(--ch-shadow-color-soft);
}

.task-card--todo::before {
  background: var(--ch-color-text-subtle);
}

.task-card--queued::before {
  background: var(--ch-color-info);
}

.task-card--working::before {
  background: var(--ch-color-warning-strong);
}

.task-card--review::before {
  background: var(--ch-color-attention-strong);
}

.task-card--done::before {
  background: var(--ch-color-success-strong);
}

.task-card--done {
  min-height: 46px;
  padding-top: 8px;
  padding-bottom: 8px;
}

.task-card--done .task-card-header {
  align-items: center;
  min-height: 28px;
}

.task-card--done h3 {
  display: flex;
  align-items: center;
  min-height: 24px;
  line-height: 1.2;
}

.task-card--done .agent-badge {
  align-self: center;
}

.task-card--done .task-card-description,
.task-card--done .latest-report,
.task-card--done .session-meta,
.task-card--done .advanced-start,
.task-card--done .task-actions {
  display: none;
}

.task-card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.task-card h3 {
  color: var(--ch-color-text-strong);
  font-size: 13px;
  line-height: 1.35;
}

.task-card-description {
  max-width: 100%;
  margin: 6px 0 8px;
  color: var(--ch-color-text-muted);
  font-size: 11px;
  line-height: 1.35;
  white-space: normal;
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow-wrap: anywhere;
  word-break: break-all;
}

.agent-badge,
.session-meta span {
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
  font-size: 10px;
  padding: 3px 7px;
  white-space: nowrap;
}

.agent-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.task-card-badges {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.review-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border-radius: 999px;
  padding: 3px 8px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.02em;
  white-space: nowrap;
  border: 1px solid transparent;
}

.review-badge-dot {
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: currentColor;
}

.review-badge--active {
  background: color-mix(in srgb, var(--ch-color-info) 18%, transparent);
  border-color: color-mix(in srgb, var(--ch-color-info) 45%, transparent);
  color: var(--ch-color-info);
}

.review-badge--active .review-badge-dot {
  animation: review-badge-pulse 1.4s ease-in-out infinite;
}

.review-badge--pending {
  background: color-mix(in srgb, var(--ch-color-text-subtle) 18%, transparent);
  border-color: color-mix(in srgb, var(--ch-color-text-subtle) 45%, transparent);
  color: var(--ch-color-text-muted);
}

.review-badge--attention {
  background: color-mix(in srgb, var(--ch-color-attention-strong) 18%, transparent);
  border-color: color-mix(in srgb, var(--ch-color-attention-strong) 45%, transparent);
  color: var(--ch-color-attention-strong);
}

@keyframes review-badge-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.45; transform: scale(0.7); }
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 999px;
  background: var(--ch-color-text-subtle);
}

.status-dot--todo {
  background: var(--ch-color-text-subtle);
}

.status-dot--queued {
  background: var(--ch-color-info);
}

.status-dot--working {
  background: var(--ch-color-warning-strong);
}

.status-dot--review {
  background: var(--ch-color-attention-strong);
}

.status-dot--done {
  background: var(--ch-color-success-strong);
}

.latest-report {
  margin: 0 0 8px;
  border-left: 2px solid var(--ch-color-border-hover);
  border-radius: 0 var(--ch-radius-sm) var(--ch-radius-sm) 0;
  background: var(--ch-color-chip-bg-muted);
  padding: 6px 8px;
  color: var(--ch-color-text-muted);
  font-size: 11px;
  line-height: 1.35;
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow-wrap: anywhere;
  word-break: break-all;
}

.latest-report strong {
  color: var(--ch-color-text);
  font-weight: 700;
}

.latest-report span {
  margin-left: 4px;
}

.session-meta {
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.advanced-start {
  margin: 0 0 8px;
  border-top: 1px solid var(--ch-color-border-strong);
  padding-top: 8px;
}

.advanced-start summary {
  margin-bottom: 8px;
  color: var(--ch-color-accent);
  font-size: 11px;
  cursor: pointer;
}

.advanced-start label {
  margin-bottom: 8px;
}

.advanced-start .checkbox-label {
  flex-direction: row;
  align-items: center;
}

.advanced-start input[type='checkbox'] {
  width: 14px;
  height: 14px;
}

.task-actions {
  flex-wrap: wrap;
}

.task-actions button,
.agent-row button {
  height: 26px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 4px;
  background: var(--ch-color-surface-control-active);
  color: var(--ch-color-text);
  padding: 0 8px;
  transition: background 0.12s ease, border-color 0.12s ease, transform 0.08s ease;
  -webkit-tap-highlight-color: var(--ch-color-accent-ring);
}

.task-actions button:active,
.agent-row button:active,
.tool-button:active,
.primary-button:active,
.danger-button:active {
  transform: translateY(1px);
  background: var(--ch-color-surface-pressed);
}

.task-actions .danger-button,
.agent-row .danger-button {
  background: var(--ch-color-danger-bg);
  border-color: var(--ch-color-danger-border);
}

.column-empty {
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  text-align: center;
  padding: 18px 0;
}

.task-detail-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow-y: auto;
  overscroll-behavior: contain;
  background: var(--ch-color-overlay);
  padding: 24px;
}

.task-detail-panel {
  width: min(1040px, calc(100vw - 48px));
  max-height: min(860px, calc(100dvh - 48px));
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-raised);
  box-shadow: var(--ch-shadow-dialog);
}

.detail-header {
  position: sticky;
  top: 0;
  z-index: 2;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface-raised);
}

.detail-eyebrow,
.detail-section-title {
  display: block;
  color: var(--ch-color-accent);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}

.detail-section-title--with-controls {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.lang-toggle {
  display: inline-flex;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  overflow: hidden;
  text-transform: none;
}

.lang-toggle-btn {
  padding: 2px 8px;
  border: 0;
  background: transparent;
  color: var(--ch-color-text-subtle);
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  letter-spacing: 0.02em;
}

.lang-toggle-btn:hover {
  color: var(--ch-color-text);
}

.lang-toggle-btn.active {
  background: var(--ch-color-accent);
  color: var(--ch-color-on-accent, #fff);
}

.detail-header h2 {
  margin: 4px 0 0;
  color: var(--ch-color-text-strong);
  font-size: 18px;
}

.icon-button {
  width: 28px;
  height: 28px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.icon-button:hover {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
}

.detail-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow-y: auto;
  padding: 16px 18px;
}

.detail-section {
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface);
  padding: 14px;
}

.detail-copy {
  margin: 8px 0 0;
}

.fact-grid {
  margin-top: 10px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.fact-grid div {
  min-width: 0;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 8px;
}

.fact-grid span {
  display: block;
  color: var(--ch-color-text-muted);
  font-size: 11px;
}

.fact-grid strong {
  display: block;
  margin-top: 4px;
  color: var(--ch-color-text);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.detail-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.detail-footer-toggle {
  display: none;
}

.detail-action-drawer {
  display: block;
}

.send-form {
  margin-top: 10px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: 8px;
}

.send-form textarea {
  min-height: 100px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  padding: 9px;
  resize: vertical;
}

.send-form .primary-button {
  height: 34px;
}

.send-attachments {
  grid-column: 1 / -1;
}

.attachment-list {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.attachment-row {
  min-width: 0;
  display: grid;
  grid-template-columns: 54px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 6px;
  background: var(--ch-color-surface-soft);
  padding: 8px;
}

.attachment-list--readonly .attachment-row {
  grid-template-columns: 54px minmax(0, 1fr);
}

.attachment-thumb {
  width: 54px;
  height: 42px;
  overflow: hidden;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 4px;
  background: var(--ch-color-surface-sunken);
}

.attachment-thumb img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}

.attachment-meta {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.attachment-meta strong,
.attachment-meta span,
.attachment-meta code {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-meta strong {
  color: var(--ch-color-text);
  font-size: 12px;
}

.attachment-meta span,
.attachment-meta code {
  color: var(--ch-color-text-muted);
  font-size: 11px;
}

.detail-footer {
  position: sticky;
  bottom: 0;
  z-index: 2;
  border-top: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface-raised);
  padding: 12px 16px;
}

.empty-timeline {
  margin-top: 8px;
  color: var(--ch-color-text-subtle);
  font-size: 12px;
}

.goal-packet {
  margin-top: 10px;
  display: grid;
  gap: 10px;
  min-width: 0;
}

.goal-packet-objective,
.goal-packet-section {
  min-width: 0;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 6px;
  background: var(--ch-color-surface-soft);
  padding: 10px;
}

.goal-packet-objective span,
.goal-packet-section strong {
  display: block;
  margin-bottom: 6px;
  color: var(--ch-color-text);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}

.goal-packet-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.goal-packet-meta span {
  border-radius: 999px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  font-size: 10px;
  padding: 3px 7px;
}

.goal-packet-section ol {
  margin: 0;
  padding-left: 18px;
  color: var(--ch-color-text-muted);
  font-size: 12px;
  line-height: 1.45;
}

.goal-packet-section li {
  overflow-wrap: anywhere;
}

.goal-packet-empty {
  color: var(--ch-color-text-subtle);
  font-size: 12px;
}

.timeline {
  list-style: none;
  margin: 12px 0 0;
  padding: 0 0 0 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-left: 1px solid var(--ch-color-border-muted);
}

.timeline li {
  position: relative;
  min-width: 0;
}

.timeline li::before {
  content: '';
  position: absolute;
  top: 16px;
  left: -16px;
  width: 7px;
  height: 7px;
  border: 2px solid var(--ch-color-surface);
  border-radius: 999px;
  background: var(--ch-color-accent);
}

.report-card {
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 0;
}

.report-card summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  cursor: pointer;
  padding: 10px;
  color: var(--ch-color-text);
  font-size: 12px;
}

.report-card summary::-webkit-details-marker {
  display: none;
}

.report-card[open] summary {
  border-bottom: 1px solid var(--ch-color-border-muted);
}

.report-state {
  font-weight: 700;
  color: var(--ch-color-text);
}

.report-time {
  flex: 0 0 auto;
  color: var(--ch-color-text-subtle);
}

.report-message {
  padding: 10px;
}

.report-files {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  padding: 0 10px 10px;
}

.report-files span {
  border-radius: 999px;
  background: var(--ch-color-surface-control-active);
  color: var(--ch-color-text);
  font-size: 10px;
  padding: 3px 7px;
}

.report-note {
  margin: 0 10px 10px;
  border-left: 2px solid var(--ch-color-border-hover);
  padding-left: 8px;
  color: var(--ch-color-text-muted);
}

.report-note strong {
  display: block;
  margin-bottom: 5px;
  color: var(--ch-color-text);
  font-size: 11px;
  text-transform: uppercase;
}

.acceptance-check-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.45;
}

.acceptance-check-list li {
  overflow-wrap: anywhere;
}

.acceptance-check-list span {
  margin-right: 5px;
  color: var(--ch-color-text);
  font-weight: 700;
}

.workspace-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow-y: auto;
  background: var(--ch-color-overlay);
  padding: 16px;
}

.file-browser-overlay {
  z-index: 1100;
}

.workspace-modal {
  width: min(520px, 100%);
  max-height: calc(100dvh - 32px);
  overflow-y: auto;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface);
  box-shadow: var(--ch-shadow-dialog);
  padding: 20px;
}

.agent-manager-modal {
  width: min(720px, 100%);
}

.workspace-modal h3 {
  margin: 0 0 16px;
  color: var(--ch-color-text-strong);
  font-size: 18px;
}

.modal-field {
  margin-bottom: 14px;
}

.modal-field label {
  display: block;
  margin-bottom: 6px;
  color: var(--ch-color-text-muted);
  font-size: 13px;
}

.modal-field input,
.modal-field textarea,
.modal-field select,
.file-browser-path input {
  width: 100%;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  font-size: 14px;
  box-sizing: border-box;
}

.modal-field input,
.modal-field select {
  height: 34px;
  padding: 0 10px;
}

.modal-field textarea {
  min-height: 120px;
  resize: vertical;
  padding: 10px;
}

.modal-field input:focus,
.modal-field textarea:focus,
.modal-field select:focus,
.file-browser-path input:focus {
  outline: none;
  border-color: var(--ch-color-accent);
  box-shadow: 0 0 0 2px var(--ch-color-accent-ring);
}

.modal-field .checkbox-label {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
  margin-bottom: 0;
  color: var(--ch-color-text);
}

.modal-field .checkbox-label input {
  width: 16px;
  height: 16px;
}

.modal-hint {
  margin: 6px 0 0;
  color: var(--ch-color-text-soft);
  font-size: 12px;
  line-height: 1.35;
}

.segmented-control {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
  padding: 4px;
}

.segment-button {
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text-muted);
  cursor: pointer;
  font-size: 14px;
  padding: 8px 10px;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast);
}

.segment-button.active {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-strong);
}

.segment-button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.path-input-row {
  display: flex;
  gap: 8px;
}

.path-input-row input {
  min-width: 0;
  flex: 1;
}

.path-input-row .tool-button {
  height: 34px;
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 18px;
}

.modal-section {
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--ch-color-border);
}

.modal-section--first {
  margin-top: 0;
  padding-top: 0;
  border-top: 0;
}

.modal-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.modal-section-header h4 {
  margin: 0;
  color: var(--ch-color-text-strong);
  font-size: 13px;
}

.modal-section-header span {
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.file-browser-modal {
  width: min(720px, 100%);
  height: min(70dvh, 620px);
  display: flex;
  flex-direction: column;
}

.file-browser-header,
.file-browser-path {
  display: flex;
  align-items: center;
  gap: 8px;
}

.file-browser-header {
  justify-content: space-between;
  margin-bottom: 12px;
}

.file-browser-header h3 {
  margin: 0;
}

.file-browser-path {
  border-radius: 4px;
  background: var(--ch-color-surface-soft);
  padding: 8px;
}

.file-browser-path input {
  min-width: 0;
  flex: 1;
  height: 30px;
  padding: 0 8px;
  font-family: monospace;
  font-size: 12px;
}

.path-nav-button {
  height: 30px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: 4px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  padding: 0 8px;
}

.file-browser-list {
  flex: 1;
  min-height: 160px;
  overflow-y: auto;
  border: 1px solid var(--ch-color-border);
  border-radius: 4px;
  background: var(--ch-color-app-bg);
  margin-top: 12px;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  color: var(--ch-color-text-muted);
  cursor: default;
}

.file-item.is-dir {
  color: var(--ch-color-accent);
  cursor: pointer;
}

.file-item:hover {
  background: var(--ch-color-surface-soft);
}

.file-item span {
  width: 28px;
  color: var(--ch-color-text-subtle);
  font-size: 11px;
}

.file-item strong {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 500;
}

.file-status {
  padding: 16px;
  text-align: center;
  color: var(--ch-color-text-soft);
  font-size: 13px;
}

.file-error {
  color: var(--ch-color-danger);
}

@media (max-width: 760px) {
  .workspace-view {
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
  }

  .workspace-modal-overlay {
    align-items: flex-start;
    padding: 10px;
  }

  .workspace-modal {
    width: 100%;
    max-height: calc(100dvh - 20px);
    padding: 14px;
    border-radius: var(--ch-radius-md);
  }

  .file-browser-modal {
    height: calc(100dvh - 20px);
  }

  .path-input-row,
  .file-browser-path {
    flex-wrap: wrap;
  }

  .path-input-row .tool-button,
  .file-browser-path input {
    flex: 1 1 100%;
  }

  .modal-actions {
    position: sticky;
    bottom: -1px;
    background: var(--ch-color-surface);
    padding-top: 10px;
  }

  .modal-actions .tool-button,
  .modal-actions .primary-button {
    flex: 1;
  }

  .workspace-header {
    position: sticky;
    top: 0;
    z-index: 20;
    flex-direction: row;
    align-items: stretch;
    gap: 0;
    padding: 6px 8px;
    background: var(--ch-color-surface);
  }

  .workspace-title-block {
    display: none;
  }

  .workspace-header .workspace-title-block > h1,
  .workspace-header .workspace-title-block > p {
    display: none;
  }

  .workspace-mobile-identity {
    min-width: 0;
    display: grid;
    gap: 2px;
  }

  .workspace-mobile-identity span {
    color: var(--ch-color-text-muted);
    font-size: 10px;
    font-weight: 700;
    line-height: 1;
    text-transform: uppercase;
  }

  .workspace-mobile-identity strong {
    min-width: 0;
    color: var(--ch-color-text-strong);
    font-size: 15px;
    line-height: 1.2;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .workspace-mobile-identity small {
    color: var(--ch-color-text-muted);
    font-size: 11px;
    line-height: 1.2;
  }

  .workspace-header h1 {
    font-size: 16px;
    margin: 0;
  }

  .workspace-header p {
    display: none;
  }

  .workspace-actions {
    width: 100%;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 6px;
  }

  .workspace-select-shell {
    grid-column: auto;
    width: 100%;
    min-width: 0;
  }

  .workspace-select {
    min-width: 0;
  }

  .workspace-select,
  .workspace-actions .tool-button,
  .workspace-actions .primary-button {
    height: 32px;
  }

  .workspace-actions .tool-button,
  .workspace-actions .primary-button {
    width: 100%;
    padding: 0 8px;
  }

  .workspace-actions .workspace-desktop-action {
    display: none;
  }

  .workspace-actions .primary-button {
    grid-column: auto;
    width: auto;
    min-width: 74px;
    white-space: nowrap;
  }

  .workspace-mobile-menu {
    display: block;
  }

  .workspace-mobile-menu-trigger {
    width: 32px;
    height: 32px;
  }

  .workspace-summary-strip {
    display: none;
  }

  .workspace-agent-status {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 7px 10px;
    background: var(--ch-color-canvas);
  }

  .agent-status-header {
    min-width: 0;
    display: flex;
    gap: 8px;
  }

  .agent-status-view-switch {
    flex-direction: row;
    gap: 6px;
  }

  .agent-status-view-switch button {
    height: 28px;
    padding: 0 8px;
  }

  .agent-status-view-switch span {
    font-size: 11px;
  }

  .agent-status-view-switch strong {
    min-width: 18px;
    font-size: 10px;
    line-height: 17px;
  }

  .agent-status-toolbar {
    flex: 0 0 auto;
  }

  .agent-status-grid {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    padding-bottom: 2px;
    scroll-snap-type: x proximity;
  }

  .agent-status-card {
    min-width: 0;
    flex: 0 0 min(46vw, 184px);
    gap: 0;
    padding: 7px 8px;
    border-radius: var(--ch-radius-md);
    scroll-snap-align: start;
    box-shadow: none;
  }

  .agent-status-card:hover {
    transform: none;
    box-shadow: none;
  }

  .agent-status-card-main {
    grid-template-columns: 9px minmax(0, 1fr);
    gap: 7px;
    align-items: start;
  }

  .agent-status-line {
    gap: 4px;
  }

  .agent-status-kind,
  .agent-status-detail,
  .agent-status-meta,
  .agent-status-actions {
    display: none;
  }

  .agent-status-name {
    font-size: 12px;
  }

  .agent-status-pill {
    grid-column: 2;
    width: fit-content;
    min-width: 0;
    margin-top: 3px;
    padding: 3px 6px;
    font-size: 10px;
  }

  .workspace-layout {
    display: block;
    min-height: auto;
  }

  .agent-row-meta,
  .agent-row-actions {
    flex-wrap: wrap;
  }

  .board {
    min-height: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    overflow: visible;
    padding: 8px;
  }

  .task-column {
    min-width: 0;
    border-radius: var(--ch-radius-md);
  }

  .task-column--collapsed .task-list {
    display: none;
  }

  .task-column--collapsed .column-header {
    border-bottom: 0;
  }

  .task-column--empty .task-list {
    display: none;
  }

  .task-column--empty .column-header {
    border-bottom: 0;
  }

  .column-header {
    padding: 8px 10px;
  }

  .column-meta {
    gap: 6px;
  }

  .column-collapse-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: 28px;
    min-width: 54px;
    border: 1px solid var(--ch-color-border-strong);
    border-radius: 4px;
    background: var(--ch-color-surface-control-active);
    color: var(--ch-color-text);
    font-size: 12px;
    padding: 0 8px;
    cursor: pointer;
    -webkit-tap-highlight-color: var(--ch-color-accent-ring);
  }

  .column-collapse-button:active {
    background: var(--ch-color-surface-pressed);
    transform: translateY(1px);
  }

  .task-list {
    overflow: visible;
    padding: 8px;
  }

  .task-card {
    padding: 10px 10px 10px 12px;
  }

  .task-card.selected {
    border-color: var(--ch-color-accent);
    background: var(--ch-color-surface-selected);
  }

  .task-card-header {
    align-items: center;
  }

  .task-card-description {
    display: -webkit-box;
    overflow: hidden;
    -webkit-line-clamp: 4;
    -webkit-box-orient: vertical;
  }

  .agent-badge,
  .session-meta span {
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .task-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px;
  }

  .task-actions button,
  .agent-row button {
    width: 100%;
    height: 34px;
  }

  .advanced-start select {
    width: 100%;
    font-size: 16px;
  }

  .task-detail-overlay {
    align-items: stretch;
    overflow: hidden;
    padding: 0;
  }

  .task-detail-panel {
    width: 100%;
    height: 100dvh;
    max-height: 100dvh;
    border-radius: 0;
    border-left: 0;
    border-right: 0;
    -webkit-overflow-scrolling: touch;
  }

  .detail-header {
    position: sticky;
    top: 0;
    z-index: 2;
    align-items: center;
    background: var(--ch-color-surface-raised);
    padding: 12px;
  }

  .detail-header h2 {
    font-size: 17px;
  }

  .icon-button {
    width: 34px;
    height: 34px;
  }

  .detail-body {
    flex: 1;
    min-height: 0;
    max-height: none;
    padding: 12px;
  }

  .task-detail-panel.mobile-actions-expanded .detail-body {
    max-height: none;
  }

  .fact-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .detail-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .detail-actions .primary-button {
    grid-column: 1 / -1;
  }

  .detail-footer {
    position: sticky;
    bottom: 0;
    padding: 8px 12px max(10px, env(safe-area-inset-bottom));
    box-shadow: 0 -12px 24px var(--ch-shadow-color-soft);
  }

  .detail-footer-toggle {
    width: 100%;
    height: 36px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    border: 1px solid var(--ch-color-border-strong);
    border-radius: 6px;
    background: var(--ch-color-surface-control);
    color: var(--ch-color-text);
    font-size: 14px;
    font-weight: 700;
  }

  .detail-footer-chevron {
    color: var(--ch-color-accent);
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  }

  .detail-action-drawer {
    display: none;
    margin-top: 8px;
  }

  .task-detail-panel.mobile-actions-expanded .detail-action-drawer {
    display: block;
  }

  .send-form {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 88px;
    align-items: end;
  }

  .detail-actions button,
  .send-form button {
    width: auto;
    min-width: 0;
    height: 36px;
  }

  .send-form textarea {
    min-height: 44px;
    max-height: 92px;
    font-size: 16px;
  }
}

@media (max-width: 480px) {
  .workspace-actions {
    grid-template-columns: minmax(0, 1fr) auto auto;
  }

  .workspace-select-shell {
    grid-column: auto;
  }

  .form-row {
    flex-direction: column;
    align-items: stretch;
  }

  .task-actions {
    grid-template-columns: 1fr;
  }

  .agent-status-card-main {
    grid-template-columns: 10px minmax(0, 1fr);
  }

  .agent-status-pill {
    grid-column: 2;
    width: fit-content;
    margin-top: 2px;
  }
}
</style>
