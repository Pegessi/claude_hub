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
          <span class="btn-icon">+</span> New
        </button>
        <button
          type="button"
          class="tool-button workspace-desktop-action"
          :disabled="!activeWorkspaceId"
          @click="openEditWorkspaceModal"
        >
          <span class="btn-icon">✎</span> Edit
        </button>
        <button
          type="button"
          class="tool-button workspace-desktop-action"
          :disabled="!activeWorkspaceId"
          @click="openLessonsModal"
        >
          <span class="btn-icon">💡</span> Lessons
        </button>
        <button
          type="button"
          class="tool-button workspace-desktop-action"
          :disabled="!activeWorkspaceId"
          @click="openAgentOptionsModal"
        >
          <span class="btn-icon">⚙</span> Agents
        </button>
        <button
          type="button"
          class="primary-button"
          :disabled="!activeWorkspaceId"
          @click="openTaskModal"
        >
          <span class="btn-icon">+</span> Add Task
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
              @click="openLessonsModalFromMenu"
            >
              Lessons
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
      <span>{{ error }}</span>
      <button
        type="button"
        class="workspace-error__close"
        aria-label="关闭错误提示"
        @click="dismissWorkspaceErrors"
      >
        ×
      </button>
    </div>

    <div
      v-if="activeWorkspaceId"
      class="workspace-summary-strip"
    >
      <div class="workspace-summary-primary">
        <span class="summary-chip"><strong>{{ workspaceAgents.length }}</strong> agents</span>
        <span class="summary-chip"><strong>{{ reviewerAgents.length + temporaryReviewers.length }}</strong> reviewers</span>
        <span class="summary-chip summary-chip--accent"><strong>{{ workspaceAgents.filter(agent => agent.runtime_status === 'working').length }}</strong> working</span>
        <span class="summary-chip"><strong>{{ taskCountForStatus('queued') }}</strong> queued</span>
        <button
          type="button"
          class="summary-chip summary-chip-button"
          @click="openLessonsModal"
        >
          <strong>{{ activeFeedbackLessons.length }}</strong> lessons
        </button>
      </div>
      <div class="workspace-column-tabs">
        <span
          v-for="column in columns"
          :key="column.status"
          class="column-tab-chip"
        >
          {{ column.label }} <strong>{{ taskCountForStatus(column.status) }}</strong>
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
            <span class="agent-status-avatar-wrap">
              <AgentAvatar
                :agent-type="agent.agent_type"
                size="md"
              />
              <span
                class="agent-status-dot"
                :data-status="agentRuntimeStatus(agent)"
              />
            </span>
            <span class="agent-status-main">
              <span class="agent-status-line">
                <span class="agent-status-name">{{ agent.title }}</span>
                <span class="agent-status-kind">{{ agentRoleLabel(agent) }}</span>
                <span
                  v-if="isResidentAgent(agent) && isResidentPaused"
                  class="agent-status-kind agent-status-paused-badge"
                >Paused</span>
                <span
                  v-if="isResidentAgent(agent) && isResidentMaster"
                  class="agent-status-kind agent-status-master-badge"
                  title="Autopilot: this resident drives the workspace on its own — creating, dispatching, and accepting tasks"
                >Autopilot</span>
                <span
                  class="agent-status-cli"
                  :data-kind="agent.agent_type || 'terminal'"
                >{{ agent.agent_type || 'terminal' }}</span>
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
              <span
                v-if="isResidentAgent(agent)"
                class="agent-status-meta agent-status-meta--resident"
              >
                <span class="agent-status-timing">
                  <span class="agent-status-timing-chip">last run {{ residentLastRunLabel }}</span>
                  <span
                    v-if="residentNextRunLabel"
                    class="agent-status-timing-chip"
                    :data-run-state="
                      residentNextRunLabel === 'queued' || residentNextRunLabel === 'due now'
                        ? 'live'
                        : residentNextRunLabel === 'paused'
                          ? 'muted'
                          : 'default'
                    "
                  >next run {{ residentNextRunLabel }}</span>
                </span>
                <span
                  v-if="latestResidentReport"
                  class="agent-status-resident-message"
                  :title="latestResidentReport.message"
                >{{ latestResidentReport.message }}</span>
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
              v-if="canSwitchAgentEnv(agent)"
              type="button"
              class="agent-status-switch-env"
              title="Switch Env / Model"
              :loading="isPending(sessionActionKey('switch-env', agent.id))"
              loading-label="Switching env"
              @click.stop="openSwitchEnvModal(agent)"
            >
              <span class="btn-icon">⚙</span> Env
            </LoadingButton>
            <LoadingButton
              v-if="isResidentAgent(agent)"
              type="button"
              class="agent-status-pause"
              :loading="isPending('workspace:resident-pause')"
              :loading-label="isResidentPaused ? 'Resuming agent' : 'Pausing agent'"
              @click="toggleResidentPaused"
            >
              <span class="btn-icon">{{ isResidentPaused ? '▶' : '⏸' }}</span>
              {{ isResidentPaused ? 'Resume' : 'Pause' }}
            </LoadingButton>
            <LoadingButton
              v-if="isResidentAgent(agent)"
              type="button"
              class="agent-status-run-now"
              :disabled="residentRunPending"
              :title="residentRunPending
                ? 'A run is already queued for the next monitor tick'
                : 'Run the resident now using its saved directive and periodic tasks'"
              :loading="isPending('resident:run')"
              loading-label="Queuing run"
              @click="handleRunResidentNow"
            >
              <span class="btn-icon">▶</span>
              {{ residentRunPending ? 'Run queued' : 'Run now' }}
            </LoadingButton>
            <span
              class="agent-status-actions-sep"
              aria-hidden="true"
            />
            <LoadingButton
              type="button"
              class="agent-status-delete"
              :disabled="!canDeleteAgent(agent)"
              :title="agentDeleteTitle(agent)"
              :loading="isPending(agentActionKey('delete', agent.id))"
              loading-label="Deleting agent"
              @click="deleteAgent(agent)"
            >
              <span class="btn-icon">×</span> Delete
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
          <transition name="board-skeleton-fade">
            <div
              v-if="boardLoading"
              class="board-skeleton"
              role="status"
              aria-live="polite"
              aria-label="Loading workspace"
            >
              <div
                v-for="column in columns"
                :key="`skeleton-${column.status}`"
                class="board-skeleton-column"
              >
                <div class="board-skeleton-header">
                  <span class="board-skeleton-line board-skeleton-line--title" />
                </div>
                <div class="board-skeleton-list">
                  <div
                    v-for="card in 3"
                    :key="card"
                    class="board-skeleton-card"
                  >
                    <span class="board-skeleton-line board-skeleton-line--lg" />
                    <span class="board-skeleton-line board-skeleton-line--sm" />
                    <span class="board-skeleton-line board-skeleton-line--md" />
                  </div>
                </div>
              </div>
            </div>
          </transition>

          <section
            v-for="column in columns"
            :key="column.status"
            :class="[
              'task-column',
              {
                'task-column--empty': taskCountForStatus(column.status) === 0,
                'task-column--collapsed': isMobileColumnCollapsed(column.status),
                'task-column--live': (column.status === 'working' || column.status === 'review')
                  && taskCountForStatus(column.status) > 0,
                [`task-column--live-${column.status}`]: (column.status === 'working' || column.status === 'review')
                  && taskCountForStatus(column.status) > 0,
              },
            ]"
          >
            <div class="column-header">
              <h2>{{ column.label }}</h2>
              <div class="column-meta">
                <span
                  v-if="taskCountForStatus(column.status) > 0"
                  class="column-count"
                >
                  {{ taskCountForStatus(column.status) }}
                </span>
                <span
                  v-else
                  class="column-count column-count--empty"
                  aria-hidden="true"
                >—</span>
                <button
                  v-if="column.status === 'done' && doneTasksTotal > DONE_TASK_COLLAPSE_LIMIT"
                  type="button"
                  class="column-collapse-button column-done-toggle"
                  :aria-expanded="showAllDoneTasks"
                  @click="showAllDoneTasks = !showAllDoneTasks"
                >
                  {{ showAllDoneTasks ? 'Show recent' : `Show ${doneTasksCollapsedCount} older` }}
                </button>
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
                v-for="task in tasksForColumn(column.status)"
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
                      v-if="task.origin === 'resident'"
                      class="origin-badge"
                      title="Created by the resident agent"
                    >
                      Agent
                    </span>
                    <span
                      v-if="task.task_mode === 'autonomous'"
                      class="autonomy-badge"
                      :title="task.autonomous_run?.next_action || 'Autonomous run'"
                    >
                      Auto {{ task.autonomous_run?.iteration || 1 }}/{{ task.autonomous_run?.max_iterations || task.autonomy_policy?.max_iterations || 3 }}
                    </span>
                    <span
                      v-if="activeReviewBadge(task)"
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
                    <span
                      v-if="task.status === 'done'"
                      class="task-card-age"
                      :title="formatTime(task.completed_at || task.updated_at || task.created_at)"
                    >
                      {{ taskAgeLabel(task) }}
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
                  :data-report-tone="reportStateLabel(latestReportForTask(task)?.state).tone"
                >
                  <strong>{{ reportStateLabel(latestReportForTask(task)?.state).label }}</strong>
                  <span>{{ reportMessageForLang(latestReportForTask(task)!) }}</span>
                </div>
                <div class="session-meta">
                  <span class="meta-agent">{{ agentTitle(task.session_id) }}</span>
                  <span
                    v-if="task.review_session_id"
                    class="meta-reviewer"
                  >
                    {{ reviewerTitle(task.review_session_id) }}
                  </span>
                  <span
                    v-if="reviewStatusLabel(task)"
                    class="meta-review-state"
                  >{{ reviewStatusLabel(task) }}</span>
                  <span
                    v-if="injectedFeedbackLessonIds(task).length > 0"
                    class="feedback-meta-chip"
                  >
                    feedback {{ injectedFeedbackLessonIds(task).length }}
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
                    class="primary-button task-action--primary task-action--mobile-wide"
                    :loading="isPending(taskActionKey('start', task.id))"
                    loading-label="Starting task"
                    @click.stop="startTask(task)"
                  >
                    <span class="btn-icon">▶</span> Start
                  </LoadingButton>
                  <button
                    v-if="canEditTask(task)"
                    type="button"
                    class="tool-button task-action--hide-mobile"
                    @click.stop="openEditTaskModal(task)"
                  >
                    <span class="btn-icon">✎</span> Edit
                  </button>
                  <LoadingButton
                    v-if="canMarkDoneTask(task)"
                    type="button"
                    class="tool-button"
                    :loading="isPending(taskActionKey('mark-done', task.id))"
                    loading-label="Marking done"
                    @click.stop="markTask(task.id, 'done')"
                  >
                    <span class="btn-icon">✓</span> Done
                  </LoadingButton>
                  <LoadingButton
                    v-if="canRequestReviewTask(task)"
                    type="button"
                    class="tool-button"
                    :loading="isPending(taskActionKey('request-review', task.id))"
                    loading-label="Requesting review"
                    @click.stop="requestReview(task)"
                  >
                    <span class="btn-icon">◎</span> Request review
                  </LoadingButton>
                  <LoadingButton
                    v-if="canAbortTask(task)"
                    type="button"
                    class="abort-button"
                    :loading="isPending(taskActionKey('abort', task.id))"
                    loading-label="Aborting task"
                    @click.stop="abortTask(task)"
                  >
                    <span class="btn-icon">■</span> Abort
                  </LoadingButton>
                  <LoadingButton
                    v-if="sessionForTask(task)"
                    type="button"
                    class="tool-button"
                    :loading="isPending(sessionActionKey('open', task.session_id))"
                    loading-label="Opening tab"
                    @click.stop="openSession(sessionForTask(task)!)"
                  >
                    <span class="btn-icon">⧉</span> Open tab
                  </LoadingButton>
                  <LoadingButton
                    type="button"
                    class="danger-button task-action--hide-mobile"
                    :loading="isPending(taskActionKey('delete', task.id))"
                    loading-label="Deleting task"
                    @click.stop="deleteTask(task)"
                  >
                    <span class="btn-icon">×</span> Delete
                  </LoadingButton>
                  <details
                    class="task-card-more-menu"
                    name="task-card-more"
                  >
                    <summary
                      class="task-card-more-trigger"
                      title="More actions"
                      aria-label="More actions"
                    >
                      ⋯
                    </summary>
                    <div
                      class="task-card-more-panel"
                      role="menu"
                    >
                      <button
                        v-if="canEditTask(task)"
                        type="button"
                        class="task-card-more-item"
                        @click.stop="openEditTaskModal(task); closeTaskCardMoreMenus()"
                      >
                        <span
                          class="btn-icon"
                          aria-hidden="true"
                        >✎</span> Edit
                      </button>
                      <LoadingButton
                        type="button"
                        class="task-card-more-item task-card-more-item--danger"
                        :loading="isPending(taskActionKey('delete', task.id))"
                        loading-label="Deleting task"
                        @click.stop="deleteTask(task); closeTaskCardMoreMenus()"
                      >
                        <span
                          class="btn-icon"
                          aria-hidden="true"
                        >×</span> Delete
                      </LoadingButton>
                    </div>
                  </details>
                </div>
              </article>
              <div
                v-if="taskCountForStatus(column.status) === 0"
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
              ×
            </button>
          </div>

          <div class="detail-body">
            <section class="detail-section">
              <div class="detail-section-title">
                Task description
              </div>
              <MarkdownContent
                class="detail-copy"
                link-markdown-paths
                :text="selectedTask.prompt"
                @markdown-path-click="path => openMarkdownPreviewModal(path)"
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
                  <button
                    type="button"
                    class="attachment-thumb attachment-thumb--clickable"
                    :aria-label="`Preview ${attachment.filename}`"
                    @click="openImageLightbox(`/api/workspaces/attachments/${attachment.id}`, attachment.filename)"
                  >
                    <img
                      :src="`/api/workspaces/attachments/${attachment.id}`"
                      :alt="attachment.filename"
                    >
                  </button>
                  <div class="attachment-meta">
                    <strong>{{ attachment.filename }}</strong>
                    <span>{{ persistedAttachmentMeta(attachment) }}</span>
                    <code>{{ attachment.path }}</code>
                  </div>
                </div>
              </div>
            </section>

            <details class="detail-section detail-section--collapsible">
              <summary class="detail-section-title">
                Goal Packet
              </summary>
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
                  <span>{{ goalPacketStatusLabel(selectedTask.goal_packet.status) }}</span>
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
            </details>

            <details class="detail-section detail-section--collapsible">
              <summary class="detail-section-title">
                Assignment
              </summary>
              <div class="fact-grid">
                <div>
                  <span>Mode</span>
                  <strong>{{ taskModeLabel(selectedTask.task_mode) }}</strong>
                </div>
                <div>
                  <span>Execution</span>
                  <strong>{{ executionComplexityLabel(selectedTask.execution_complexity) }}</strong>
                </div>
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
                <div>
                  <span>Goal Packet gate</span>
                  <strong>{{ goalPacketGateLabel(selectedTask) }}</strong>
                </div>
                <div>
                  <span>Review profiles</span>
                  <strong>{{ taskReviewProfiles(selectedTask) }}</strong>
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
            </details>

            <details
              v-if="selectedTask.task_mode === 'autonomous'"
              class="detail-section detail-section--collapsible autonomous-run-panel"
            >
              <summary class="detail-section-title">
                Autonomous Run
              </summary>
              <div
                v-if="!selectedTask.autonomous_run"
                class="empty-timeline"
              >
                No autonomous run recorded yet.
              </div>
              <template v-else>
                <div class="fact-grid">
                  <div>
                    <span>Phase</span>
                    <strong>{{ autonomousRunPhaseLabel(selectedTask.autonomous_run.phase) }}</strong>
                  </div>
                  <div>
                    <span>Iteration</span>
                    <strong>{{ selectedTask.autonomous_run.iteration }} / {{ selectedTask.autonomous_run.max_iterations }}</strong>
                  </div>
                  <div>
                    <span>Score</span>
                    <strong>{{ formatAutonomousScore(selectedTask.autonomous_run.current_score) }}</strong>
                  </div>
                  <div>
                    <span>Threshold</span>
                    <strong>{{ formatAutonomousScore(selectedTask.autonomous_run.pass_threshold) }}</strong>
                  </div>
                  <div>
                    <span>Strictness</span>
                    <strong>{{ selectedTask.autonomy_policy?.evaluation_strictness || 'balanced' }}</strong>
                  </div>
                  <div>
                    <span>Artifacts</span>
                    <strong>{{ selectedTask.autonomy_policy?.require_artifact_review ? 'required' : 'optional' }}</strong>
                  </div>
                  <div>
                    <span>Total elapsed</span>
                    <strong>{{ taskTotalElapsedLabel(selectedTask) }}</strong>
                  </div>
                  <div>
                    <span>Working elapsed</span>
                    <strong>{{ taskWorkingElapsedLabel(selectedTask) }}</strong>
                  </div>
                  <div>
                    <span>Latest report age</span>
                    <strong>{{ latestSelectedReportAgeLabel }}</strong>
                  </div>
                </div>
                <div class="autonomous-next-action">
                  <span>Next action</span>
                  <strong>{{ selectedTask.autonomous_run.next_action }}</strong>
                </div>
                <div
                  v-if="(selectedTask.autonomous_run.evaluation_reports || []).length > 0"
                  class="autonomous-evaluations"
                >
                  <strong>Evaluations</strong>
                  <ol>
                    <li
                      v-for="evaluation in selectedTask.autonomous_run.evaluation_reports"
                      :key="evaluation.id"
                    >
                      <span>{{ evaluation.decision }}</span>
                      <span>round {{ evaluation.iteration }}</span>
                      <span>{{ formatAutonomousScore(evaluation.overall_score) }}</span>
                      <span
                        v-if="evaluation.profile_results?.length"
                        class="profile-summary"
                      >
                        {{ profileResultSummary(evaluation.profile_results) }}
                      </span>
                    </li>
                  </ol>
                </div>
              </template>
            </details>

            <details
              class="detail-section detail-section--collapsible"
              open
            >
              <summary class="detail-section-title">
                Progress
              </summary>
              <div
                v-if="selectedReports.length > 0 && hasBilingualReport"
                class="detail-section-controls"
              >
                <div
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
                v-if="selectedProgressTimeline.length > 0"
                class="progress-overview"
              >
                <ol class="progress-overview-timeline">
                  <li
                    v-for="item in selectedProgressTimeline"
                    :key="item.id"
                    :class="['progress-overview-item', `progress-overview-item--${item.tone}`]"
                  >
                    <span class="progress-overview-dot" />
                    <span class="progress-overview-main">{{ item.label }}</span>
                    <span class="progress-overview-time">{{ item.elapsedLabel }}</span>
                    <span
                      v-if="item.deltaLabel"
                      class="progress-overview-delta"
                    >
                      +{{ item.deltaLabel }}
                    </span>
                  </li>
                </ol>
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
                  v-for="(report, reportIndex) in selectedReports"
                  :key="report.id"
                >
                  <details
                    class="report-card"
                    :open="isLatestSelectedReport(report)"
                  >
                    <summary>
                      <span class="report-state">{{ report.state }}</span>
                      <span
                        v-if="reportIsAwaitingAcceptance(report)"
                        class="report-summary-label report-awaiting-acceptance"
                      >
                        awaiting acceptance
                      </span>
                      <span
                        v-if="reportSummaryLabel(report)"
                        class="report-summary-label"
                      >
                        {{ reportSummaryLabel(report) }}
                      </span>
                      <span class="report-summary-message">
                        {{ reportMessagePreview(report) }}
                      </span>
                      <span class="report-summary-meta">
                        <span class="report-time">{{ formatTime(report.created_at) }}</span>
                        <span
                          class="report-delta"
                          :title="reportElapsedTitle(report, reportIndex)"
                        >
                          {{ reportElapsedLabel(report, reportIndex) }}
                        </span>
                      </span>
                    </summary>
                    <MarkdownContent
                      class="report-message"
                      link-markdown-paths
                      :text="reportMessageForLang(report)"
                      @markdown-path-click="path => openMarkdownPreviewModal(path, report)"
                    />
                    <div
                      v-if="report.changed_files.length > 0"
                      class="report-files"
                    >
                      <button
                        v-for="file in report.changed_files"
                        :key="file"
                        type="button"
                        :class="['report-file-chip', { 'report-file-chip--clickable': isMarkdownArtifact(file) }]"
                        :disabled="!isMarkdownArtifact(file)"
                        @click="openMarkdownPreviewModal(file, report)"
                      >
                        {{ file }}
                      </button>
                    </div>
                    <details
                      v-if="report.validation"
                      class="report-subsection report-note"
                      :open="isReportSubsectionOpen(report, 'validation')"
                      @toggle="event => toggleReportSubsection(report, 'validation', event)"
                    >
                      <summary>
                        <strong>Validation</strong>
                        <span
                          v-if="validationPreview(report)"
                          class="report-subsection-preview"
                        >{{ validationPreview(report) }}</span>
                      </summary>
                      <MarkdownContent
                        v-if="isReportSubsectionOpen(report, 'validation')"
                        compact
                        link-markdown-paths
                        :text="report.validation"
                        @markdown-path-click="path => openMarkdownPreviewModal(path, report)"
                      />
                    </details>
                    <details
                      v-if="acceptanceChecksFor(report).length > 0"
                      class="report-subsection report-note"
                      :open="isReportSubsectionOpen(report, 'acceptance')"
                      @toggle="event => toggleReportSubsection(report, 'acceptance', event)"
                    >
                      <summary>
                        <strong>Acceptance Check</strong>
                        <span
                          v-if="acceptanceSummary(report)"
                          class="report-summary-label"
                        >{{ acceptanceSummary(report) }}</span>
                        <span class="report-subsection-count">({{ acceptanceChecksFor(report).length }} checks)</span>
                      </summary>
                      <ol
                        v-if="isReportSubsectionOpen(report, 'acceptance')"
                        class="acceptance-check-list"
                      >
                        <li
                          v-for="check in acceptanceChecksFor(report)"
                          :key="`${check.criterion}-${check.status}`"
                        >
                          <span>{{ check.status }}</span>
                          {{ check.criterion }} - {{ check.evidence }}
                        </li>
                      </ol>
                    </details>
                    <details
                      v-if="profileResultsFor(report).length > 0"
                      class="report-subsection report-note"
                      :open="isReportSubsectionOpen(report, 'profiles')"
                      @toggle="event => toggleReportSubsection(report, 'profiles', event)"
                    >
                      <summary>
                        <strong>Review Profiles</strong>
                        <span
                          v-if="profileResultSummary(profileResultsFor(report))"
                          class="report-summary-label"
                        >{{ profileResultSummary(profileResultsFor(report)) }}</span>
                        <span class="report-subsection-count">({{ profileResultsFor(report).length }} profiles)</span>
                      </summary>
                      <ol
                        v-if="isReportSubsectionOpen(report, 'profiles')"
                        class="profile-result-list"
                      >
                        <li
                          v-for="result in profileResultsFor(report)"
                          :key="`${result.profile}-${result.status}`"
                        >
                          <span>{{ result.status }}</span>
                          {{ reviewProfileLabel(result.profile) }} - {{ result.evidence || 'No evidence recorded.' }}
                          <template v-if="result.blocking_findings?.length">
                            Blocking: {{ result.blocking_findings.join('; ') }}
                          </template>
                        </li>
                      </ol>
                    </details>
                    <details
                      v-if="artifactCount(report) > 0"
                      class="report-subsection report-note"
                      :open="isReportSubsectionOpen(report, 'artifacts')"
                      @toggle="event => toggleReportSubsection(report, 'artifacts', event)"
                    >
                      <summary>
                        <strong>Artifacts</strong>
                        <span class="report-subsection-count">({{ artifactCount(report) }})</span>
                      </summary>
                      <template v-if="isReportSubsectionOpen(report, 'artifacts')">
                        <div class="report-artifacts">
                          <div
                            v-for="artifact in report.artifact_refs"
                            :key="artifact"
                            class="report-artifact"
                          >
                            <span>{{ artifact }}</span>
                            <button
                              v-if="isMarkdownArtifact(artifact)"
                              type="button"
                              class="artifact-preview-button"
                              :disabled="isArtifactPreviewLoading(report, artifact)"
                              @click="toggleArtifactPreview(report, artifact)"
                            >
                              {{ artifactPreviewButtonLabel(report, artifact) }}
                            </button>
                          </div>
                        </div>
                        <div
                          v-for="artifact in markdownArtifactRefs(report)"
                          :key="`${artifact}-preview`"
                          class="artifact-preview"
                        >
                          <template v-if="expandedArtifactKey === artifactPreviewKey(report, artifact)">
                            <div
                              v-if="artifactPreviewErrors[artifactPreviewKey(report, artifact)]"
                              class="artifact-preview-status artifact-preview-error"
                            >
                              {{ artifactPreviewErrors[artifactPreviewKey(report, artifact)] }}
                            </div>
                            <div
                              v-else-if="!artifactPreviews[artifactPreviewKey(report, artifact)]"
                              class="artifact-preview-status"
                            >
                              Loading Markdown preview...
                            </div>
                            <template v-else>
                              <div class="artifact-preview-header">
                                <span>{{ artifactPreviews[artifactPreviewKey(report, artifact)].filename }}</span>
                                <span>{{ formatAttachmentSize(artifactPreviews[artifactPreviewKey(report, artifact)].size_bytes) }}</span>
                              </div>
                              <MarkdownContent
                                class="artifact-preview-content"
                                :text="artifactPreviews[artifactPreviewKey(report, artifact)].content"
                              />
                              <div
                                v-if="artifactPreviews[artifactPreviewKey(report, artifact)].truncated"
                                class="artifact-preview-status"
                              >
                                Preview truncated to the first 512 KB.
                              </div>
                            </template>
                          </template>
                        </div>
                      </template>
                    </details>
                    <div
                      v-if="report.confidence !== null && report.confidence !== undefined"
                      class="report-note report-note--inline"
                    >
                      <strong>Confidence</strong>
                      <span>{{ formatAutonomousScore(report.confidence) }}</span>
                      <span v-if="report.requires_human_judgment">Human judgment required</span>
                    </div>
                    <details
                      v-if="report.risks"
                      class="report-subsection report-note"
                      :open="isReportSubsectionOpen(report, 'risks')"
                      @toggle="event => toggleReportSubsection(report, 'risks', event)"
                    >
                      <summary>
                        <strong>Risks</strong>
                        <span
                          v-if="risksPreview(report)"
                          class="report-subsection-preview"
                        >{{ risksPreview(report) }}</span>
                      </summary>
                      <MarkdownContent
                        v-if="isReportSubsectionOpen(report, 'risks')"
                        compact
                        link-markdown-paths
                        :text="report.risks"
                        @markdown-path-click="path => openMarkdownPreviewModal(path, report)"
                      />
                    </details>
                  </details>
                </li>
              </ol>
            </details>

            <details class="detail-section detail-section--collapsible markdown-output-section">
              <summary class="detail-section-title detail-section-title--with-count">
                <span>Markdown Outputs</span>
                <span>{{ selectedMarkdownDocuments.length }}</span>
              </summary>
              <div
                v-if="selectedMarkdownDocuments.length === 0"
                class="empty-timeline"
              >
                No Markdown outputs discovered yet. Agents can report artifact_refs, or Markdown changed_files will appear here automatically.
              </div>
              <div
                v-else
                class="markdown-output-list"
              >
                <article
                  v-for="document in selectedMarkdownDocuments"
                  :key="document.id"
                  class="markdown-output-card"
                >
                  <div class="markdown-output-row">
                    <div>
                      <strong>{{ document.label }}</strong>
                      <span>{{ markdownDocumentSourceLabel(document.source) }}</span>
                      <code>{{ document.path }}</code>
                    </div>
                    <div class="markdown-output-actions">
                      <span v-if="document.size_bytes">{{ formatAttachmentSize(document.size_bytes) }}</span>
                      <button
                        type="button"
                        class="artifact-preview-button"
                        :disabled="isMarkdownDocumentPreviewLoading(document)"
                        @click="toggleMarkdownDocumentPreview(document)"
                      >
                        {{ markdownDocumentPreviewButtonLabel(document) }}
                      </button>
                    </div>
                  </div>
                  <div
                    v-if="expandedArtifactKey === markdownDocumentPreviewKey(document)"
                    class="artifact-preview markdown-output-preview"
                  >
                    <div
                      v-if="artifactPreviewErrors[markdownDocumentPreviewKey(document)]"
                      class="artifact-preview-status artifact-preview-error"
                    >
                      {{ artifactPreviewErrors[markdownDocumentPreviewKey(document)] }}
                    </div>
                    <div
                      v-else-if="!artifactPreviews[markdownDocumentPreviewKey(document)]"
                      class="artifact-preview-status"
                    >
                      Loading Markdown preview...
                    </div>
                    <template v-else>
                      <div class="artifact-preview-header">
                        <span>{{ artifactPreviews[markdownDocumentPreviewKey(document)].filename }}</span>
                        <span>{{ formatAttachmentSize(artifactPreviews[markdownDocumentPreviewKey(document)].size_bytes) }}</span>
                      </div>
                      <MarkdownContent
                        class="artifact-preview-content"
                        :text="artifactPreviews[markdownDocumentPreviewKey(document)].content"
                      />
                      <div
                        v-if="artifactPreviews[markdownDocumentPreviewKey(document)].truncated"
                        class="artifact-preview-status"
                      >
                        Preview truncated to the first 512 KB.
                      </div>
                    </template>
                  </div>
                </article>
              </div>
            </details>
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
                  <span class="btn-icon">▶</span> Start
                </LoadingButton>
                <button
                  v-if="canEditTask(selectedTask)"
                  type="button"
                  class="tool-button"
                  @click="openEditTaskModal(selectedTask)"
                >
                  <span class="btn-icon">✎</span> Edit
                </button>
                <LoadingButton
                  v-if="canMarkDoneTask(selectedTask)"
                  type="button"
                  class="tool-button"
                  :loading="isPending(taskActionKey('mark-done', selectedTask.id))"
                  loading-label="Marking done"
                  @click="markTask(selectedTask.id, 'done')"
                >
                  <span class="btn-icon">✓</span> Done
                </LoadingButton>
                <LoadingButton
                  v-if="canRequestReviewTask(selectedTask)"
                  type="button"
                  class="tool-button"
                  :loading="isPending(taskActionKey('request-review', selectedTask.id))"
                  loading-label="Requesting review"
                  @click="requestReview(selectedTask)"
                >
                  <span class="btn-icon">◎</span> Request review
                </LoadingButton>
                <LoadingButton
                  v-if="canAbortTask(selectedTask)"
                  type="button"
                  class="abort-button"
                  :loading="isPending(taskActionKey('abort', selectedTask.id))"
                  loading-label="Aborting task"
                  @click="abortTask(selectedTask)"
                >
                  <span class="btn-icon">■</span> Abort
                </LoadingButton>
                <LoadingButton
                  v-if="selectedSession"
                  type="button"
                  class="tool-button"
                  :loading="isPending(sessionActionKey('open', selectedSession.id))"
                  loading-label="Opening terminal"
                  @click="openSession(selectedSession)"
                >
                  <span class="btn-icon">⧉</span> Open terminal
                </LoadingButton>
                <span
                  class="detail-actions-sep"
                  aria-hidden="true"
                />
                <LoadingButton
                  type="button"
                  class="danger-button"
                  :loading="isPending(taskActionKey('delete', selectedTask.id))"
                  loading-label="Deleting task"
                  @click="deleteTask(selectedTask)"
                >
                  <span class="btn-icon">×</span> Delete
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
                      ×
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

    <Teleport to="body">
      <div
        v-if="markdownPreviewModalPath"
        class="workspace-modal-overlay markdown-preview-modal-overlay"
        @click.self="closeMarkdownPreviewModal"
      >
        <div
          class="workspace-modal markdown-preview-modal"
          role="dialog"
          aria-modal="true"
          :aria-label="`Markdown preview: ${markdownPreviewModalPath}`"
        >
          <div class="markdown-preview-modal-header">
            <div>
              <span>Markdown Preview</span>
              <strong>{{ markdownPreviewModalPath }}</strong>
            </div>
            <button
              type="button"
              class="icon-button"
              aria-label="Close Markdown preview"
              @click="closeMarkdownPreviewModal"
            >
              ×
            </button>
          </div>
          <div
            v-if="markdownPreviewModalError"
            class="artifact-preview-status artifact-preview-error"
          >
            {{ markdownPreviewModalError }}
          </div>
          <div
            v-else-if="markdownPreviewModalLoading || !markdownPreviewModalContent"
            class="artifact-preview-status"
          >
            Loading Markdown preview...
          </div>
          <template v-else>
            <div class="artifact-preview-header">
              <span>{{ markdownPreviewModalContent.filename }}</span>
              <span>{{ formatAttachmentSize(markdownPreviewModalContent.size_bytes) }}</span>
            </div>
            <MarkdownContent
              class="artifact-preview-content markdown-preview-modal-content"
              :text="markdownPreviewModalContent.content"
            />
            <div
              v-if="markdownPreviewModalContent.truncated"
              class="artifact-preview-status"
            >
              Preview truncated to the first 512 KB.
            </div>
          </template>
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <div
        v-if="imageLightboxUrl"
        class="workspace-modal-overlay image-lightbox-overlay"
        @click.self="closeImageLightbox"
      >
        <button
          type="button"
          class="icon-button image-lightbox-close"
          aria-label="Close image preview"
          @click="closeImageLightbox"
        >
          ×
        </button>
        <img
          class="image-lightbox-img"
          :src="imageLightboxUrl"
          :alt="imageLightboxAlt"
          @click.stop
        >
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
          <div class="modal-field resident-agent-section">
            <div class="resident-summary-row">
              <div class="resident-summary-text">
                <span class="resident-summary-title">Resident self-driven agent</span>
                <span
                  :class="['resident-summary-status', workspaceForm.resident_agent_enabled ? 'is-on' : 'is-off']"
                >
                  {{ residentSummaryLabel }}
                </span>
              </div>
              <button
                type="button"
                class="tool-button"
                @click="openResidentAgentModal"
              >
                Configure…
              </button>
            </div>
            <p class="modal-hint">
              An optional background agent that runs on a schedule to maintain
              lessons and propose follow-up tasks for this workspace.
            </p>
          </div>
          <div class="modal-actions">
            <button
              v-if="workspaceModalMode === 'edit'"
              type="button"
              class="danger-button workspace-delete-button"
              :disabled="isLoading || isPending('workspace:delete')"
              @click="handleDeleteWorkspace"
            >
              Delete Workspace
            </button>
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

    <!-- Resident self-driven agent config (nested popup over the workspace modal) -->
    <div
      v-if="showResidentAgentModal"
      class="workspace-modal-overlay resident-agent-modal-overlay"
      @click.self="closeResidentAgentModal"
    >
      <div class="workspace-modal resident-agent-modal">
        <h3>Resident Agent</h3>
        <div class="modal-field resident-agent-section--first">
          <label class="checkbox-label">
            <input
              v-model="workspaceForm.resident_agent_enabled"
              type="checkbox"
            >
            Enable resident self-driven agent
          </label>
          <p class="modal-hint">
            Wakes every interval on its own — even when idle, with no task
            running — to maintain lessons and propose follow-up tasks. It
            never picks up normal workspace tasks.
          </p>
        </div>

        <!--
          Everything below stays visible at all times; it is disabled/grayed
          while Enable is unchecked (per the requested UX) instead of hidden.
          The same elements and components as the Add-Agent form are reused so
          the two interfaces match, but the role is fixed to resident.
        -->
        <fieldset
          class="resident-config-body"
          :disabled="!workspaceForm.resident_agent_enabled"
          :class="{ 'is-disabled': !workspaceForm.resident_agent_enabled }"
        >
          <div class="modal-field">
            <label class="checkbox-label">
              <input
                v-model="workspaceForm.resident_agent_paused"
                type="checkbox"
              >
              Pause auto-scheduling (keep the session for manual chat)
            </label>
            <p class="modal-hint">
              The resident session stays available for manual chat but won't
              auto-run on its schedule.
            </p>
          </div>
          <div class="modal-field">
            <label class="checkbox-label">
              <input
                v-model="workspaceForm.resident_agent_master_mode"
                type="checkbox"
              >
              Autopilot mode
            </label>
            <p class="modal-hint">
              Changes what each cycle does, not whether it runs. On: the
              resident drives the workspace on its own — it reviews the board,
              creates and dispatches tasks to your existing worker agents, lets
              them go through review, and accepts the finished work itself. It
              never writes code and never adds or removes worker agents. Off:
              read-only maintenance, no reports.
            </p>
          </div>
          <div class="modal-field">
            <label>Run interval (minutes)</label>
            <input
              v-model.number="workspaceForm.resident_agent_interval_minutes"
              type="number"
              min="1"
              placeholder="60"
            >
          </div>
          <div class="modal-field">
            <label>Resident agent directive (optional)</label>
            <textarea
              v-model="workspaceForm.resident_agent_directive"
              rows="3"
              placeholder="A one-shot guiding instruction for the resident (e.g. 'focus on tightening test coverage this week')."
            />
            <p class="modal-hint">
              Saving applies on the next scheduled cycle. To apply a changed
              directive immediately, use "Save &amp; run now" below.
            </p>
          </div>
          <div class="modal-field">
            <label>
              Recurring tasks
              <span
                v-if="enabledPeriodicTaskCount > 0"
                class="modal-label-badge"
              >{{ enabledPeriodicTaskCount }} active</span>
            </label>
            <p class="modal-hint">
              The resident runs every enabled task on each wake-up (in addition
              to its built-in maintenance). Disable a row to keep it without
              running it; the directive above is a separate one-shot focus.
            </p>
            <ul
              v-if="workspaceForm.resident_agent_periodic_tasks.length > 0"
              class="periodic-task-list"
            >
              <li
                v-for="task in workspaceForm.resident_agent_periodic_tasks"
                :key="task.id"
                class="periodic-task-row"
              >
                <input
                  v-model="task.enabled"
                  type="checkbox"
                  class="periodic-task-enable"
                  :title="task.enabled ? 'Enabled — runs every cycle' : 'Disabled — kept but not run'"
                >
                <input
                  v-model="task.text"
                  class="periodic-task-text"
                  :class="{ 'periodic-task-text--disabled': !task.enabled }"
                  placeholder="e.g. run the linter and open a task for any new warnings"
                >
                <button
                  type="button"
                  class="periodic-task-remove"
                  title="Remove this recurring task"
                  @click="removePeriodicTask(task.id)"
                >
                  ✕
                </button>
              </li>
            </ul>
            <p
              v-else
              class="modal-hint periodic-task-empty"
            >
              No recurring tasks yet.
            </p>
            <button
              type="button"
              class="tool-button periodic-task-add"
              @click="addPeriodicTask"
            >
              + Add recurring task
            </button>
          </div>
          <div class="modal-field">
            <label>Title</label>
            <input
              v-model="workspaceForm.resident_agent_title"
              placeholder="Workspace Resident"
            >
          </div>
          <AgentConfigFields
            v-model:agent-type="workspaceForm.resident_agent_type"
            v-model:solo-mode="workspaceForm.resident_agent_solo_mode"
            v-model:env-preset="workspaceForm.resident_env_preset"
            v-model:env-text="workspaceForm.resident_env_text"
            variant="modal"
            :disabled="!workspaceForm.resident_agent_enabled"
          />
          <div class="modal-field">
            <label>Run On</label>
            <div class="segmented-control">
              <button
                type="button"
                :class="['segment-button', { active: workspaceForm.resident_agent_target === 'local' }]"
                :disabled="!workspaceForm.resident_agent_enabled"
                @click="handleResidentTargetChange('local')"
              >
                Local
              </button>
              <button
                type="button"
                :class="['segment-button', { active: workspaceForm.resident_agent_target === 'remote' }]"
                :disabled="!workspaceForm.resident_agent_enabled"
                @click="handleResidentTargetChange('remote')"
              >
                Remote
              </button>
            </div>
          </div>
          <div
            v-if="workspaceForm.resident_agent_target === 'remote'"
            class="modal-field"
          >
            <label>Remote Server</label>
            <select
              v-model="workspaceForm.resident_agent_remote_profile_id"
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
          <div
            v-if="workspaceForm.resident_agent_target === 'remote'"
            class="modal-field"
          >
            <label class="checkbox-label">
              <input
                v-model="workspaceForm.resident_agent_remote_reconnect"
                type="checkbox"
              >
              Auto reconnect
            </label>
          </div>
        </fieldset>

        <!--
          Resident lifecycle buttons. In EDIT mode these act immediately via
          PATCH (Create=enabled:true, Pause/Resume=paused toggle,
          Delete=enabled:false resident-only teardown — never the workspace).
          In CREATE mode there is no workspace id yet, so the three lifecycle
          buttons are disabled and a hint explains the resident is created with
          the workspace via the parent "Create workspace" button. Done dismisses
          the sub-modal and returns to the parent workspace form.
        -->
        <p
          v-if="isResidentCreateMode"
          class="modal-hint"
        >
          The resident is configured here and created together with the
          workspace when you press "Create workspace".
        </p>
        <div class="modal-actions">
          <button
            type="button"
            class="tool-button"
            @click="closeResidentAgentModal"
          >
            Done
          </button>
          <LoadingButton
            type="button"
            class="danger-button workspace-delete-button"
            :disabled="isResidentCreateMode || !residentExists"
            :loading="isPending('resident:delete')"
            loading-label="Deleting"
            @click="handleDeleteResident"
          >
            Delete
          </LoadingButton>
          <LoadingButton
            type="button"
            class="tool-button"
            :disabled="isResidentCreateMode || !residentExists"
            :loading="isPending('resident:pause')"
            :loading-label="(activeWorkspace?.resident_agent_paused ?? false) ? 'Resuming resident' : 'Pausing resident'"
            @click="handleToggleResidentPause"
          >
            {{ (activeWorkspace?.resident_agent_paused ?? false) ? 'Resume' : 'Pause' }}
          </LoadingButton>
          <LoadingButton
            type="button"
            class="primary-button"
            :disabled="isResidentCreateMode || residentExists"
            :loading="isPending('resident:create')"
            loading-label="Creating"
            @click="handleCreateResident"
          >
            Create
          </LoadingButton>
          <LoadingButton
            type="button"
            class="primary-button"
            :disabled="isResidentCreateMode || !residentExists"
            :title="isResidentCreateMode || !residentExists
              ? 'Create the resident first'
              : 'Save the directive and recurring tasks, then run this cycle immediately'"
            :loading="isPending('resident:save-run')"
            loading-label="Saving & running"
            @click="handleSaveResidentAndRunNow"
          >
            Save &amp; run now
          </LoadingButton>
        </div>
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
                  ×
                </button>
              </div>
            </div>
          </div>
          <div class="modal-field">
            <label>Mode</label>
            <div class="segmented-control segmented-control--three">
              <button
                type="button"
                :class="['segment-button', { active: taskForm.task_mode === 'direct' }]"
                @click="taskForm.task_mode = 'direct'"
              >
                Direct
              </button>
              <button
                type="button"
                :class="['segment-button', { active: taskForm.task_mode === 'reviewed' }]"
                @click="taskForm.task_mode = 'reviewed'"
              >
                Reviewed
              </button>
              <button
                type="button"
                :class="['segment-button', { active: taskForm.task_mode === 'autonomous' }]"
                @click="taskForm.task_mode = 'autonomous'"
              >
                Autonomous
              </button>
            </div>
          </div>
          <div class="modal-field">
            <label>Execution</label>
            <div class="segmented-control segmented-control--three">
              <button
                type="button"
                :class="['segment-button', { active: taskForm.execution_complexity === 'auto' }]"
                title="Agent self-judges. If it picks orchestrator mode, expect roughly 10–15× the token cost of a single-agent run (Anthropic multi-agent research system; Cognition “Don't Build Multi-Agents”)."
                @click="taskForm.execution_complexity = 'auto'"
              >
                Auto
              </button>
              <button
                type="button"
                :class="['segment-button', { active: taskForm.execution_complexity === 'simple' }]"
                title="Single linear agent; no sub-agent fan-out, no extra cost beyond a normal task."
                @click="taskForm.execution_complexity = 'simple'"
              >
                Simple
              </button>
              <button
                type="button"
                :class="['segment-button', { active: taskForm.execution_complexity === 'complex' }]"
                title="Forces orchestrator mode with sub-agent delegation. Expect roughly 10–15× the token cost of a single-agent run; pick this only when the task is breadth-parallel, exceeds one context window, or splits into cleanly isolated subtasks."
                @click="taskForm.execution_complexity = 'complex'"
              >
                Complex
              </button>
            </div>
          </div>
          <div
            v-if="taskForm.task_mode === 'autonomous'"
            class="autonomy-form"
          >
            <div class="form-row">
              <div class="modal-field">
                <label>Max iterations</label>
                <input
                  v-model.number="taskForm.max_iterations"
                  type="number"
                  min="1"
                  max="10"
                >
              </div>
              <div class="modal-field">
                <label>Strictness</label>
                <select v-model="taskForm.evaluation_strictness">
                  <option value="lenient">
                    Lenient
                  </option>
                  <option value="balanced">
                    Balanced
                  </option>
                  <option value="strict">
                    Strict
                  </option>
                </select>
              </div>
            </div>
            <div class="form-row">
              <div class="modal-field">
                <label class="checkbox-label">
                  <input
                    v-model="taskForm.allow_web_research"
                    type="checkbox"
                  >
                  Web research
                </label>
              </div>
              <div class="modal-field">
                <label class="checkbox-label">
                  <input
                    v-model="taskForm.require_artifact_review"
                    type="checkbox"
                  >
                  Artifact review
                </label>
              </div>
            </div>
          </div>
          <div class="modal-field">
            <label>Dispatch agent</label>
            <select
              v-model="taskForm.session_id"
              :disabled="!activeWorkspaceId"
            >
              <option value="">
                Auto
              </option>
              <option
                v-for="agent in workspaceAgents"
                :key="agent.id"
                :value="agent.id"
              >
                {{ agent.title }}
              </option>
            </select>
          </div>
          <div class="modal-field">
            <label class="checkbox-label">
              <input
                v-model="taskForm.clear_context"
                type="checkbox"
              >
              Clear context
            </label>
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
      v-if="showEditTaskModal"
      class="workspace-modal-overlay"
      @click.self="closeEditTaskModal"
    >
      <div class="workspace-modal">
        <h3>Edit Task</h3>
        <form
          @submit.prevent="handleUpdateTask"
          @paste.prevent
        >
          <div class="modal-field">
            <label>Title</label>
            <input
              v-model="editTaskForm.title"
              autofocus
            >
          </div>
          <div class="modal-field">
            <label>Task description</label>
            <textarea v-model="editTaskForm.prompt" />
          </div>
          <div class="modal-field">
            <label>Dispatch agent</label>
            <select v-model="editTaskForm.session_id">
              <option value="">
                Auto
              </option>
              <option
                v-for="agent in workspaceAgents"
                :key="agent.id"
                :value="agent.id"
              >
                {{ agent.title }}
              </option>
            </select>
          </div>
          <div class="modal-field">
            <label>Related task</label>
            <select v-model="editTaskForm.related_task_id">
              <option value="">
                None
              </option>
              <option
                v-for="task in tasks.filter(item => item.id !== editingTaskId)"
                :key="task.id"
                :value="task.id"
              >
                {{ task.title }}
              </option>
            </select>
          </div>
          <div class="modal-field">
            <label class="checkbox-label">
              <input
                v-model="editTaskForm.clear_context"
                type="checkbox"
              >
              Clear context
            </label>
          </div>
          <div class="modal-actions">
            <button
              type="button"
              class="tool-button"
              @click="closeEditTaskModal"
            >
              Cancel
            </button>
            <LoadingButton
              type="submit"
              class="primary-button"
              :disabled="!editingTaskId || isLoading || !editTaskForm.title.trim() || !editTaskForm.prompt.trim()"
              :loading="isPending(taskActionKey('edit', editingTaskId))"
              loading-label="Saving task"
            >
              Save task
            </LoadingButton>
          </div>
        </form>
      </div>
    </div>

    <div
      v-if="showLessonsModal"
      class="workspace-modal-overlay"
      @click.self="closeLessonsModal"
    >
      <div class="workspace-modal lessons-manager-modal">
        <div class="modal-heading-row">
          <div>
            <h3>Workspace Lessons</h3>
            <p>{{ activeFeedbackLessons.length }} active rules for this workspace</p>
          </div>
          <button
            type="button"
            class="icon-button"
            aria-label="Close lessons"
            @click="closeLessonsModal"
          >
            ×
          </button>
        </div>

        <div class="lessons-toolbar">
          <LoadingButton
            type="button"
            class="tool-button"
            :disabled="!activeWorkspaceId"
            :loading="isPending('feedback:summarize')"
            loading-label="Checking"
            @click="handleSummarizeLessons(false)"
          >
            AI summarize
          </LoadingButton>
          <LoadingButton
            type="button"
            class="tool-button"
            :disabled="!activeWorkspaceId"
            :loading="isPending('feedback:summarize:force')"
            loading-label="Queueing"
            @click="handleSummarizeLessons(true)"
          >
            Force AI run
          </LoadingButton>
          <LoadingButton
            type="button"
            class="tool-button"
            :loading="isPending('feedback:refresh')"
            loading-label="Refreshing"
            @click="refreshFeedbackLessons"
          >
            Refresh
          </LoadingButton>
        </div>

        <div
          v-if="lastFeedbackSummaryRun"
          :class="['summary-run-status', `summary-run-status--${feedbackSummaryTone(lastFeedbackSummaryRun)}`]"
        >
          <strong>{{ feedbackSummaryTitle(lastFeedbackSummaryRun) }}</strong>
          <p>{{ feedbackSummaryDescription(lastFeedbackSummaryRun) }}</p>
          <div class="summary-run-meta">
            <span>{{ lastFeedbackSummaryRun.mode }}</span>
            <span>{{ lastFeedbackSummaryRun.cache_hit ? 'cache hit' : 'AI queued' }}</span>
            <span v-if="lastFeedbackSummaryRun.input_record_ids.length">
              {{ lastFeedbackSummaryRun.input_record_ids.length }} records
            </span>
            <span v-if="lastFeedbackSummaryRun.task_id">
              task {{ shortId(lastFeedbackSummaryRun.task_id) }}
            </span>
          </div>
        </div>

        <section class="modal-section modal-section--first">
          <div class="modal-section-header">
            <h4>Active Lessons</h4>
            <span>{{ feedbackLessonMatchSummary }}</span>
          </div>
          <div class="lessons-list">
            <article
              v-for="lesson in activeFeedbackLessons"
              :key="lesson.id"
              class="lesson-row"
            >
              <div class="lesson-row-main">
                <strong>{{ lessonTitle(lesson) }}</strong>
                <p>{{ lessonDescription(lesson) }}</p>
                <div class="lesson-tags">
                  <span
                    v-for="tag in lessonTags(lesson)"
                    :key="`${lesson.id}-${tag}`"
                  >
                    {{ tag }}
                  </span>
                </div>
              </div>
              <div class="lesson-row-actions">
                <span>{{ lesson.scope }}</span>
                <LoadingButton
                  type="button"
                  class="danger-button"
                  :loading="isPending(lessonActionKey('delete', lesson.id))"
                  loading-label="Deleting"
                  @click="deleteLesson(lesson)"
                >
                  Delete
                </LoadingButton>
              </div>
            </article>
            <div
              v-if="activeFeedbackLessons.length === 0"
              class="empty-inline"
            >
              No lessons yet.
            </div>
          </div>
        </section>

        <form
          class="modal-section lesson-create-form"
          @submit.prevent="handleCreateLesson"
        >
          <div class="modal-section-header">
            <h4>Add Lesson</h4>
          </div>
          <div class="form-row">
            <div class="modal-field">
              <label>Title</label>
              <input
                v-model="lessonForm.title"
                placeholder="Check workflow docs first"
              >
            </div>
            <div class="modal-field">
              <label>Tags</label>
              <input
                v-model="lessonForm.tags"
                placeholder="workflow, review"
              >
            </div>
          </div>
          <div class="modal-field">
            <label>Description</label>
            <textarea
              v-model="lessonForm.description"
              placeholder="One sentence rule that future agents should reuse."
            />
          </div>
          <div class="modal-actions">
            <button
              type="button"
              class="tool-button"
              @click="resetLessonForm"
            >
              Clear
            </button>
            <LoadingButton
              type="submit"
              class="primary-button"
              :disabled="!lessonForm.title.trim() || !lessonForm.description.trim()"
              :loading="isPending('feedback:create')"
              loading-label="Adding lesson"
            >
              Add lesson
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
            <div
              class="agent-manager-view-switch"
              aria-label="Workspace agents view"
            >
              <button
                type="button"
                :data-active="agentManagerView === 'agents'"
                @click="agentManagerView = 'agents'"
              >
                <span>Agents</span>
                <strong>{{ workspaceAgents.length + (dispatcherAgent ? 1 : 0) + (residentAgent ? 1 : 0) }}</strong>
              </button>
              <button
                type="button"
                :data-active="agentManagerView === 'reviewers'"
                @click="agentManagerView = 'reviewers'"
              >
                <span>Reviewers</span>
                <strong>{{ reviewerSessions.length }}</strong>
              </button>
            </div>
          </div>
          <div class="agent-list">
            <article
              v-for="agent in agentManagerSessions"
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
                <span
                  v-if="isResidentAgent(agent) && isResidentPaused"
                  class="runtime-pill runtime-pill--paused"
                >paused</span>
                <span
                  v-if="isResidentAgent(agent) && isResidentMaster"
                  class="runtime-pill runtime-pill--paused"
                >autopilot</span>
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
                  v-if="canSwitchAgentEnv(agent)"
                  type="button"
                  title="Switch Env / Model"
                  :loading="isPending(sessionActionKey('switch-env', agent.id))"
                  loading-label="Switching env"
                  @click="openSwitchEnvModal(agent)"
                >
                  ⚙ Env
                </LoadingButton>
                <LoadingButton
                  v-if="isResidentAgent(agent)"
                  type="button"
                  :loading="isPending('workspace:resident-pause')"
                  :loading-label="isResidentPaused ? 'Resuming agent' : 'Pausing agent'"
                  @click="toggleResidentPaused"
                >
                  {{ isResidentPaused ? 'Resume' : 'Pause' }}
                </LoadingButton>
                <LoadingButton
                  v-if="isResidentAgent(agent)"
                  type="button"
                  :disabled="residentRunPending"
                  :title="residentRunPending
                    ? 'A run is already queued for the next monitor tick'
                    : 'Run the resident now using its saved directive and periodic tasks'"
                  :loading="isPending('resident:run')"
                  loading-label="Queuing run"
                  @click="handleRunResidentNow"
                >
                  {{ residentRunPending ? 'Run queued' : 'Run now' }}
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
              v-if="agentManagerSessions.length === 0"
              class="empty-inline"
            >
              {{ agentManagerEmptyText }}
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

          <div class="modal-field-row">
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
          </div>

          <AgentConfigFields
            v-model:agent-type="agentOptionsForm.agent_type"
            v-model:solo-mode="agentOptionsForm.solo_mode"
            v-model:env-preset="agentOptionsForm.env_preset"
            v-model:env-text="agentOptionsForm.env_text"
            variant="modal"
          />

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
          <h3>{{ browserPlacement.target === 'remote' ? 'Select Remote Directory' : 'Select Working Directory' }}</h3>
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

  <!-- Switch Env Preset Manager (for the Switch Env modal) -->
  <EnvPresetManager
    v-model:model-value="switchEnvForm.env_preset"
    :visible="showSwitchEnvManager"
    @close="closeSwitchEnvPresetManager"
  />

  <!-- Switch Env Modal (for hot-swapping env/solo on running Claude agents) -->
  <div
    v-if="showSwitchEnvModal"
    class="workspace-modal-overlay"
    @click.self="closeSwitchEnvModal"
  >
    <div class="workspace-modal switch-env-modal">
      <div class="switch-env-header">
        <div
          class="switch-env-icon"
          aria-hidden="true"
        >
          ⚙
        </div>
        <div class="switch-env-title-block">
          <h3>Switch Environment</h3>
          <p class="switch-env-subtitle">
            {{ switchEnvAgent?.title }}
          </p>
        </div>
      </div>
      <p class="switch-env-callout">
        <span
          class="switch-env-callout-icon"
          aria-hidden="true"
        >↻</span>
        <span>
          The agent will restart and automatically resume its conversation.
          In-flight generation will be interrupted.
        </span>
      </p>
      <form @submit.prevent="handleSwitchEnv">
        <div class="modal-field env-editor">
          <label>Environment Preset</label>
          <div class="env-preset-row">
            <select
              v-model="switchEnvForm.env_preset"
              @change="applySwitchEnvPreset(switchEnvForm.env_preset)"
            >
              <option
                v-for="preset in envPresets"
                :key="preset.id"
                :value="preset.id"
              >
                {{ preset.name }}
              </option>
              <option value="custom">
                Custom (current values)
              </option>
            </select>
            <button
              type="button"
              class="tool-button env-manage-button"
              @click="openSwitchEnvPresetManager"
            >
              Manage
            </button>
          </div>
        </div>
        <div class="modal-field">
          <label for="wsSwitchEnvText">Environment Variables <span class="field-hint-inline">(KEY=VALUE, one per line)</span></label>
          <textarea
            id="wsSwitchEnvText"
            v-model="switchEnvForm.env_text"
            class="env-textarea"
            rows="6"
            placeholder="ANTHROPIC_MODEL=claude-sonnet-4-5&#10;ANTHROPIC_BASE_URL=https://..."
          />
          <p class="modal-hint">
            These fully replace the agent's current environment. Include
            <code>ANTHROPIC_MODEL</code> to switch models.
          </p>
        </div>
        <div class="modal-field">
          <label class="checkbox-label">
            <input
              v-model="switchEnvForm.solo_mode"
              type="checkbox"
            >
            <span>Solo Mode</span>
          </label>
          <p class="modal-hint">
            Relaunch with <code>IS_SANDBOX=1</code> and
            <code>--dangerously-skip-permissions</code>.
          </p>
        </div>
        <div class="modal-actions">
          <button
            type="button"
            class="tool-button"
            @click="closeSwitchEnvModal"
          >
            Cancel
          </button>
          <LoadingButton
            type="submit"
            class="primary-button switch-env-submit"
            :loading="switchEnvAgent ? isPending(sessionActionKey('switch-env', switchEnvAgent.id)) : false"
            loading-label="Restarting…"
          >
            Restart Agent
          </LoadingButton>
        </div>
      </form>
    </div>
  </div>

  <!-- Toast / notification stack for workspace mode -->
  <div
    v-if="workspaceNotifications.length"
    class="toast-stack"
    role="region"
    aria-label="Notifications"
  >
    <div
      v-for="n in workspaceNotifications"
      :key="n.id"
      :class="['toast', `toast--${n.type}`]"
      role="status"
    >
      <span
        class="toast__icon"
        aria-hidden="true"
      />
      <span class="toast__message">{{ n.message }}</span>
      <button
        type="button"
        class="toast__close"
        :aria-label="'Dismiss ' + n.type + ' notification'"
        @click="dismissToast(n)"
      >
        ×
      </button>
      <div
        v-if="n.autoDismissMs"
        class="toast__timer"
        :style="{ animationDuration: `${n.autoDismissMs}ms` }"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import AgentAvatar from '@/components/AgentAvatar.vue'
import AgentConfigFields from '@/components/AgentConfigFields.vue'
import EnvPresetManager from '@/components/EnvPresetManager.vue'
import LoadingButton from '@/components/LoadingButton.vue'
import MarkdownContent from '@/components/MarkdownContent.vue'
import NetworkAccessMenu from '@/components/NetworkAccessMenu.vue'
import {
  defaultLaunchEnvPresetForAgent,
  parseLaunchEnv,
  serializeLaunchEnv,
  useLaunchEnvPresets,
} from '@/composables/useLaunchEnvPresets'
import { usePendingActions } from '@/composables/usePendingActions'
import { useAppStore } from '@/stores/appStore'
import { useTerminalStore } from '@/stores/terminalStore'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import { DEFAULT_ABORT_REASON, resolveAbortReason } from '@/utils/taskAbort'
import {
  awaitingHumanAcceptance as taskAcceptanceAwaiting,
  canMarkDoneTask as taskAcceptanceCanMarkDone,
} from '@/utils/taskAcceptance'
import type {
  AgentReport,
  AgentRuntimeStatus,
  AgentType,
  WorkspaceArtifactPreview,
  WorkspaceMarkdownDocument,
  WorkspaceMarkdownDocumentSource,
  AcceptanceCheck,
  AcceptanceCheckStatus,
  AgentReportState,
  AutonomyPolicy,
  ExecutionTarget,
  FeedbackLesson,
  FeedbackSummaryRun,
  GoalPacket,
  ManagedSession,
  RemoteProfile,
  ResidentPeriodicTask,
  ReviewProfile,
  ReviewProfileResult,
  TerminalAgentStatus,
  WorkspaceAttachment,
  WorkspaceAttachmentCreate,
  WorkspaceSessionRole,
  WorkspaceTask,
  WorkspaceTaskExecutionComplexity,
  WorkspaceTaskMode,
  WorkspaceTaskStatus,
  WorkspaceTaskUpdate,
  WorkspaceUpdate,
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

// Dismiss all error-type notifications from workspace store.
function dismissWorkspaceErrors() {
  const ids = workspaceStore.notifications
    .filter(n => n.type === 'error')
    .map(n => n.id)
  for (const id of ids) workspaceStore.dismissNotification(id)
}

const { envPresets, getPresetText, defaultPresetTextForAgent } = useLaunchEnvPresets()
const { isPending, runPending } = usePendingActions()
const { colorScheme } = storeToRefs(appStore)
const {
  workspaces,
  activeWorkspaceId,
  board,
  tasks,
  tasksByStatusMap,
  activeFeedbackLessons,
  workspaceAgents,
  reviewerAgents,
  temporaryReviewers,
  dispatcherAgent,
  residentAgent,
  isLoading,
  error,
  notifications: wsNotifications,
} = storeToRefs(workspaceStore)

// Combine workspace and terminal store notifications so toasts fire regardless
// of which store pushed them (terminalStore.switchEnv pushes to itself; most
// workspace actions push to workspaceStore).
const terminalNotificationsRefs = storeToRefs(terminalStore)
const workspaceNotifications = computed(() => [
  ...wsNotifications.value,
  ...terminalNotificationsRefs.notifications.value,
])

function dismissToast(n: { id: string }) {
  // Route dismiss to whichever store owns the notification (ws-* prefix is
  // assigned by workspaceStore; everything else goes to terminalStore).
  if (n.id.startsWith('ws-')) {
    workspaceStore.dismissNotification(n.id)
  } else {
    terminalStore.dismissNotification(n.id)
  }
}

const selectedWorkspaceId = ref(activeWorkspaceId.value || '')
const selectedTaskId = ref<string | null>(null)
const detailMessage = ref('')
const detailAttachments = ref<DraftAttachment[]>([])
const isDetailActionsExpanded = ref(false)
const showWorkspaceModal = ref(false)
const showResidentAgentModal = ref(false)
const workspaceModalMode = ref<'create' | 'edit'>('create')
const editingWorkspaceId = ref<string | null>(null)
const showAgentOptionsModal = ref(false)
const showLessonsModal = ref(false)
const lastFeedbackSummaryRun = ref<FeedbackSummaryRun | null>(null)
const showAgentFileBrowser = ref(false)
// Switch Env modal state (for hot-swapping env/solo on running Claude agents)
const showSwitchEnvModal = ref(false)
const showSwitchEnvManager = ref(false)
const switchEnvAgent = ref<ManagedSession | null>(null)
const switchEnvForm = reactive({
  env_preset: 'custom',
  env_text: '',
  solo_mode: false,
})
const showTaskModal = ref(false)
const showEditTaskModal = ref(false)
const editingTaskId = ref<string | null>(null)
const workspaceSessionView = ref<WorkspaceSessionView>('agents')
const workspaceMobileMenuRef = ref<HTMLDetailsElement | null>(null)
const elapsedClockMs = ref(Date.now())
const remoteProfiles = ref<RemoteProfile[]>([])
const remoteProfilesLoading = ref(false)
const agentBrowserCurrentPath = ref('')
const agentBrowserPathInput = ref('')
const agentBrowserParentPath = ref<string | null>(null)
const agentBrowserItems = ref<FileInfo[]>([])
const agentBrowserLoading = ref(false)
const agentBrowserError = ref<string | null>(null)
const expandedArtifactKey = ref<string | null>(null)
const artifactPreviews = reactive<Record<string, WorkspaceArtifactPreview>>({})
const artifactPreviewErrors = reactive<Record<string, string>>({})
const artifactPreviewLoading = reactive<Record<string, boolean>>({})
const openReportSubsections = reactive<Record<string, boolean>>({})
const markdownPreviewModalPath = ref<string | null>(null)
const markdownPreviewModalReportId = ref<string | null>(null)
const markdownPreviewModalContent = ref<WorkspaceArtifactPreview | null>(null)
const markdownPreviewModalError = ref<string | null>(null)
const markdownPreviewModalLoading = ref(false)
const imageLightboxUrl = ref<string | null>(null)
const imageLightboxAlt = ref('')
const mobileCollapsedColumns = reactive<Record<WorkspaceTaskStatus, boolean>>({
  todo: false,
  queued: false,
  working: false,
  review: false,
  done: false,
})
const startOptions = reactive<Record<string, TaskStartOptions>>({})
let boardPollTimer: number | null = null
let elapsedClockTimer: number | null = null

const columns: { status: WorkspaceTaskStatus; label: string }[] = [
  { status: 'todo', label: 'Todo' },
  { status: 'queued', label: 'Queued' },
  { status: 'working', label: 'Working' },
  { status: 'review', label: 'Review' },
  { status: 'done', label: 'Done' },
]

// Done-task collapse: with 100+ completed tasks, rendering all done cards on
// every 2.5s poll dominates DOM and re-render cost. Show the N most recent
// done tasks by default with a toggle to reveal all. The Done column scrolls
// internally at desktop so the toggle is the "see older history" affordance.
const DONE_TASK_COLLAPSE_LIMIT = 10
const showAllDoneTasks = ref(false)
const doneTasksTotal = computed(() => tasksByStatusMap.value.done.length)
const doneTasksCollapsedCount = computed(
  () => Math.max(0, doneTasksTotal.value - DONE_TASK_COLLAPSE_LIMIT),
)
const visibleDoneTasks = computed(() => {
  const all = tasksByStatusMap.value.done
  if (showAllDoneTasks.value || all.length <= DONE_TASK_COLLAPSE_LIMIT) return all
  // Tasks are in insertion order (oldest first); show the most recent N.
  return all.slice(all.length - DONE_TASK_COLLAPSE_LIMIT)
})

// Stable per-column task lists. Non-done columns get the full list from the
// store's memoized map; the done column gets the (possibly collapsed) list.
function tasksForColumn(status: WorkspaceTaskStatus): WorkspaceTask[] {
  if (status === 'done') return visibleDoneTasks.value
  return tasksByStatusMap.value[status] || []
}

// Backwards-compat: total count for a status (always the true total, even
// when done tasks are collapsed).
function taskCountForStatus(status: WorkspaceTaskStatus): number {
  return (tasksByStatusMap.value[status] || []).length
}

// Show a graceful skeleton over the board while a workspace switch is in
// flight, or on the very first board load (board still null) for the active
// workspace. Background polling refreshes do not trigger this — once a board
// has been rendered the columns stay visible and update in place.
const boardLoading = computed(
  () =>
    !!activeWorkspaceId.value &&
    (isPending('workspace:switch') ||
      (board.value === null && isLoading.value)),
)

const workspaceForm = reactive({
  name: 'Claude Hub',
  path: '',
  default_branch: 'main',
  session_prefix: 'chub',
  target: 'local' as ExecutionTarget,
  remote_profile_id: '',
  remote_cwd: '',
  remote_reconnect: true,
  resident_agent_enabled: false,
  resident_agent_paused: false,
  resident_agent_master_mode: false,
  resident_agent_interval_minutes: 60,
  resident_agent_directive: '',
  resident_agent_periodic_tasks: [] as ResidentPeriodicTask[],
  // Resident agent CLI/env config (UI-side; resolved to resident_agent_env at submit).
  resident_agent_type: 'claude' as AgentType,
  resident_agent_solo_mode: true,
  resident_env_preset: defaultLaunchEnvPresetForAgent('claude'),
  resident_env_text: defaultPresetTextForAgent('claude'),
  // Resident agent launch placement (mirrors the Add-Agent form).
  resident_agent_title: '',
  resident_agent_target: 'local' as ExecutionTarget,
  resident_agent_remote_profile_id: '',
  resident_agent_cwd: '',
  resident_agent_remote_reconnect: true,
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
  env_preset: defaultLaunchEnvPresetForAgent('codex'),
  env_text: defaultPresetTextForAgent('codex'),
})

const taskForm = reactive({
  title: '',
  prompt: '',
  task_mode: 'reviewed' as WorkspaceTaskMode,
  execution_complexity: 'auto' as WorkspaceTaskExecutionComplexity,
  max_iterations: 3,
  evaluation_strictness: 'balanced' as AutonomyPolicy['evaluation_strictness'],
  allow_web_research: false,
  require_artifact_review: false,
  session_id: '',
  clear_context: false,
  related_task_id: '',
  attachments: [] as DraftAttachment[],
})

const editTaskForm = reactive({
  title: '',
  prompt: '',
  session_id: '',
  related_task_id: '',
  clear_context: false,
})

const lessonForm = reactive({
  title: '',
  description: '',
  tags: '',
})

const activeWorkspace = computed(() =>
  workspaces.value.find(workspace => workspace.id === activeWorkspaceId.value) || null
)

const mobileWorkspaceSummary = computed(() => {
  if (!activeWorkspaceId.value) return 'Create a workspace to begin'

  const agentCount = workspaceAgents.value.length
  const reviewerCount = reviewerAgents.value.length + temporaryReviewers.value.length
  const workingCount = workspaceAgents.value.filter(agent => agent.runtime_status === 'working').length
  const queuedCount = taskCountForStatus('queued')
  return `${agentCount} agents · ${reviewerCount} reviewers · ${workingCount} working · ${queuedCount} queued`
})

const selectedTask = computed(() =>
  tasks.value.find(task => task.id === selectedTaskId.value) || null
)

function feedbackTokens(value: string): Set<string> {
  const text = value.toLowerCase()
  const tokens = new Set(text.match(/[a-z0-9_.-]{2,}/g) || [])
  const cjkChunks = text.match(/\p{Script=Han}+/gu) || []
  cjkChunks.forEach((chunk) => {
    for (const size of [2, 3]) {
      if (chunk.length < size) continue
      for (let index = 0; index <= chunk.length - size; index += 1) {
        tokens.add(chunk.slice(index, index + size))
      }
    }
  })
  return tokens
}

function feedbackLessonText(lesson: FeedbackLesson): string {
  return [
    lesson.id,
    lesson.summary,
    lesson.do || '',
    lesson.avoid || '',
    ...(lesson.applies_when || []),
    ...(lesson.tags || []),
  ].join(' ')
}

function feedbackLessonScore(lesson: FeedbackLesson, task: WorkspaceTask): number {
  const queryTokens = feedbackTokens(`${task.title} ${task.prompt}`)
  if (queryTokens.size === 0) return 0
  const lessonTokens = feedbackTokens(feedbackLessonText(lesson))
  let score = 0
  queryTokens.forEach((token) => {
    if (lessonTokens.has(token)) score += 1
  })
  return score
}

function matchingFeedbackLessons(task: WorkspaceTask | null): FeedbackLesson[] {
  if (!task) return []
  return activeFeedbackLessons.value
    .map(lesson => ({ lesson, score: feedbackLessonScore(lesson, task) }))
    .filter(item => item.score > 0)
    .sort((a, b) => b.score - a.score)
    .map(item => item.lesson)
    .slice(0, 6)
}

function injectedFeedbackLessonIds(task: WorkspaceTask | null): string[] {
  return Array.isArray(task?.feedback_lesson_ids) ? task.feedback_lesson_ids : []
}

function lessonTitle(lesson: FeedbackLesson): string {
  return lesson.title?.trim() || lesson.summary.split(/[。.!?]/)[0]?.trim() || lesson.id
}

function lessonDescription(lesson: FeedbackLesson): string {
  return lesson.summary || lesson.do || lesson.avoid || lesson.id
}

function lessonTags(lesson: FeedbackLesson): string[] {
  return (lesson.tags || []).slice(0, 4)
}

function shortId(value: string): string {
  return value.slice(0, 8)
}

function feedbackSummaryTone(run: FeedbackSummaryRun): 'queued' | 'skipped' | 'done' {
  if (run.task_id) return 'queued'
  if (run.cache_hit || run.skipped_reason) return 'skipped'
  return 'done'
}

function feedbackSummaryTitle(run: FeedbackSummaryRun): string {
  if (run.task_id) return 'Internal AI summary queued'
  if (run.skipped_reason === 'no_new_task_records') return 'No new task records'
  if (run.skipped_reason) return 'Summary skipped'
  return 'Summary recorded'
}

function feedbackSummaryDescription(run: FeedbackSummaryRun): string {
  if (run.task_id) {
    return 'A hidden Feedback Reaper task was started. It will update lessons and write audit evidence when it finishes.'
  }
  if (run.skipped_reason === 'no_new_task_records') {
    return 'No completed task records are available or changed, so no internal AI task was started. Force AI run can reprocess cached records once this workspace has records.'
  }
  if (run.skipped_reason) {
    return `No internal AI task was started: ${run.skipped_reason}.`
  }
  return 'The feedback summary run was recorded.'
}

const feedbackLessonMatchSummary = computed(() => {
  const matchedTaskCount = tasks.value.filter(task => matchingFeedbackLessons(task).length > 0).length
  if (activeFeedbackLessons.value.length === 0) return 'No active lessons indexed'
  if (matchedTaskCount === 0) return 'No current task matches'
  return `${matchedTaskCount} current tasks match active lessons`
})

const selectedSession = computed(() =>
  selectedTask.value ? workspaceStore.sessionForTask(selectedTask.value) : null
)

const selectedTaskSendKey = computed(() =>
  selectedTask.value ? taskActionKey('send', selectedTask.value.id) : 'task:none:send'
)

const selectedReports = computed<AgentReport[]>(() =>
  selectedTask.value ? workspaceStore.reportsForTaskId(selectedTask.value.id) : []
)

const selectedMarkdownDocuments = computed<WorkspaceMarkdownDocument[]>(() => {
  const task = selectedTask.value
  if (!task) return []
  const documents = board.value?.markdown_documents || []
  const selectedReportIds = new Set(selectedReports.value.map(report => report.id))
  return documents.filter(document =>
    !isWorkspaceMaintenanceMarkdown(document.path) && (
      document.task_id === task.id ||
      (document.report_id ? selectedReportIds.has(document.report_id) : false)
    )
  )
})

interface ProgressTimelineItem {
  id: string
  label: string
  timestampMs: number
  elapsedLabel: string
  deltaLabel: string
  tone: 'task' | 'report' | 'terminal' | 'live'
}

const latestSelectedReportAgeLabel = computed(() => {
  const latestReport = selectedReports.value[selectedReports.value.length - 1]
  if (!latestReport) return 'none'
  const latestMs = parseTimestampMs(latestReport.created_at)
  if (latestMs === null) return 'unknown'
  return `${formatElapsedDuration(elapsedClockMs.value - latestMs)} ago`
})

const selectedProgressTimeline = computed<ProgressTimelineItem[]>(() => {
  const task = selectedTask.value
  if (!task) return []

  const rawItems: Array<Omit<ProgressTimelineItem, 'elapsedLabel' | 'deltaLabel'>> = []
  addTimelineItem(rawItems, 'task-created', 'Created', task.created_at, 'task')
  addTimelineItem(rawItems, 'task-queued', 'Queued', task.queued_at, 'task')
  addTimelineItem(rawItems, 'task-started', 'Started', task.started_at, 'task')
  selectedReports.value.forEach((report) => {
    addTimelineItem(
      rawItems,
      `report-${report.id}`,
      report.state.replace(/_/g, ' '),
      report.created_at,
      'report',
    )
  })
  addTimelineItem(rawItems, 'task-reviewed', 'Review', task.reviewed_at, 'task')
  addTimelineItem(rawItems, 'task-done', 'Done', task.completed_at, 'terminal')
  addTimelineItem(rawItems, 'task-aborted', 'Aborted', task.manual_aborted_at, 'terminal')

  if (task.status === 'queued' || task.status === 'working' || task.status === 'review') {
    rawItems.push({
      id: 'task-now',
      label: 'Now',
      timestampMs: elapsedClockMs.value,
      tone: 'live',
    })
  }

  return rawItems
    .sort((a, b) => a.timestampMs - b.timestampMs)
    .map((item, index, items) => {
      const previous = items[index - 1]
      return {
        ...item,
        elapsedLabel: formatElapsedDuration(item.timestampMs - items[0].timestampMs),
        deltaLabel: previous ? formatElapsedDuration(item.timestampMs - previous.timestampMs) : '',
      }
    })
})

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

const FINAL_REPORT_STATES = new Set<AgentReportState>([
  'completed',
  'ready_for_review',
  'review_passed',
  'review_failed',
])

function isFinalReport(report: AgentReport): boolean {
  return FINAL_REPORT_STATES.has(report.state)
}

function acceptanceSummary(report: AgentReport): string | null {
  const checks = acceptanceChecksFor(report)
  if (checks.length === 0) return null
  const counts: Record<string, number> = {}
  for (const check of checks) {
    counts[check.status] = (counts[check.status] || 0) + 1
  }
  const order: AcceptanceCheckStatus[] = ['passed', 'partial', 'failed', 'not_checked']
  const parts: string[] = []
  for (const status of order) {
    if (counts[status]) parts.push(`${counts[status]} ${status}`)
  }
  return parts.join(' · ')
}

function isSubstantiveReport(report: AgentReport): boolean {
  return Boolean(
    (report.changed_files && report.changed_files.length > 0) ||
    report.validation ||
    (report.acceptance_check && report.acceptance_check.length > 0) ||
    (report.profile_results && report.profile_results.length > 0) ||
    (report.artifact_refs && report.artifact_refs.length > 0) ||
    report.risks ||
    report.evaluation_report
  )
}

function reportSummaryLabel(report: AgentReport): string {
  const parts: string[] = []
  const acceptance = acceptanceSummary(report)
  if (acceptance) parts.push(acceptance)
  if (report.changed_files?.length) {
    parts.push(`${report.changed_files.length} file${report.changed_files.length === 1 ? '' : 's'}`)
  }
  if (report.validation) parts.push('validated')
  if (report.risks) parts.push('risks noted')
  if (report.profile_results?.length) parts.push(`${report.profile_results.length} review profile${report.profile_results.length === 1 ? '' : 's'}`)
  if (report.artifact_refs?.length) parts.push(`${report.artifact_refs.length} artifact${report.artifact_refs.length === 1 ? '' : 's'}`)
  return parts.join(' · ')
}

function reportMessagePreview(report: AgentReport): string {
  const text = reportMessageForLang(report).trim()
  if (text.length <= 120) return text
  return text.slice(0, 117) + '…'
}

function profileResultsFor(report: AgentReport): ReviewProfileResult[] {
  return Array.isArray(report.profile_results) ? report.profile_results : []
}

function reviewProfileLabel(profile: ReviewProfile): string {
  const labels: Record<ReviewProfile, string> = {
    general: 'General',
    code: 'Code',
    ui: 'UI',
    artifact: 'Artifact',
    delivery: 'Delivery',
    boundary: 'Boundary',
  }
  return labels[profile] || profile
}

function taskReviewProfiles(task: WorkspaceTask): string {
  const profiles = Array.isArray(task.review_profiles) ? task.review_profiles : []
  if (profiles.length > 0) return profiles.map(reviewProfileLabel).join(', ')
  const policyProfiles = task.autonomy_policy?.review_profiles || []
  if (policyProfiles.length > 0) return policyProfiles.map(reviewProfileLabel).join(', ')
  return task.autonomy_policy?.require_artifact_review ? 'Inferred + Artifact' : 'Inferred'
}

function profileResultSummary(results: ReviewProfileResult[]): string {
  return results
    .map(result => `${reviewProfileLabel(result.profile)} ${result.status}`)
    .join(' · ')
}

function _markdownPreview(text: string | null | undefined, max = 80): string {
  if (!text) return ''
  const stripped = text
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    .replace(/[*_~>]/g, '')
    .replace(/\[(.*?)\]\(.*?\)/g, '$1')
    .replace(/\s+/g, ' ')
    .trim()
  if (stripped.length <= max) return stripped
  return stripped.slice(0, Math.max(0, max - 1)) + '…'
}

function validationPreview(report: AgentReport, max = 80): string {
  return _markdownPreview(report.validation, max)
}

function risksPreview(report: AgentReport, max = 80): string {
  return _markdownPreview(report.risks, max)
}

function artifactCount(report: AgentReport): number {
  return report.artifact_refs?.length ?? 0
}

function reportIsAwaitingAcceptance(report: AgentReport): boolean {
  const task = selectedTask.value
  if (!task) return false
  if (!task.human_acceptance_requested_at || task.human_accepted_at) return false
  return isFinalReport(report)
}

function isMarkdownArtifact(artifact: string): boolean {
  const value = artifact.trim().split(/[?#]/)[0] || ''
  return /\.(md|markdown|mdown|mkd)(?::\d+)?$/i.test(value)
}

function isWorkspaceMaintenanceMarkdown(path: string): boolean {
  return path.trim().split(/[?#]/)[0].split('/').pop()?.toLowerCase() === 'changelog.md'
}

function markdownArtifactRefs(report: AgentReport): string[] {
  return (report.artifact_refs || []).filter(isMarkdownArtifact)
}

function artifactPreviewKey(report: AgentReport, artifact: string): string {
  return `${report.id}:${artifact}`
}

function reportSubsectionKey(report: AgentReport, subsection: string): string {
  return `${report.id}:${subsection}`
}

function isReportSubsectionOpen(report: AgentReport, subsection: string): boolean {
  return openReportSubsections[reportSubsectionKey(report, subsection)] === true
}

function toggleReportSubsection(
  report: AgentReport,
  subsection: string,
  event: Event,
): void {
  const el = event.currentTarget as HTMLDetailsElement | null
  const key = reportSubsectionKey(report, subsection)
  openReportSubsections[key] = el?.open === true
}

function markdownDocumentPreviewKey(document: WorkspaceMarkdownDocument): string {
  return `doc:${document.id}`
}

function markdownDocumentSourceLabel(source: WorkspaceMarkdownDocumentSource): string {
  const labels: Record<WorkspaceMarkdownDocumentSource, string> = {
    artifact: 'Official artifact',
    changed_file: 'Changed Markdown',
    snapshot: 'Workspace snapshot',
    discovered: 'Discovered',
  }
  return labels[source]
}

function isMarkdownDocumentPreviewLoading(document: WorkspaceMarkdownDocument): boolean {
  return Boolean(artifactPreviewLoading[markdownDocumentPreviewKey(document)])
}

function markdownDocumentPreviewButtonLabel(document: WorkspaceMarkdownDocument): string {
  const key = markdownDocumentPreviewKey(document)
  if (artifactPreviewLoading[key]) return 'Loading...'
  return expandedArtifactKey.value === key ? 'Hide' : 'Open'
}

async function toggleMarkdownDocumentPreview(document: WorkspaceMarkdownDocument) {
  const key = markdownDocumentPreviewKey(document)
  if (expandedArtifactKey.value === key) {
    expandedArtifactKey.value = null
    return
  }
  expandedArtifactKey.value = key
  if (artifactPreviews[key] || artifactPreviewLoading[key]) return

  const workspaceId = selectedTask.value?.workspace_id || activeWorkspaceId.value
  if (!workspaceId) return
  artifactPreviewLoading[key] = true
  delete artifactPreviewErrors[key]
  try {
    artifactPreviews[key] = await workspaceStore.fetchArtifactPreview(
      workspaceId,
      document.path,
      document.report_id || undefined,
    )
  } catch (e) {
    artifactPreviewErrors[key] = e instanceof Error ? e.message : 'Failed to load Markdown preview'
  } finally {
    artifactPreviewLoading[key] = false
  }
}

function isArtifactPreviewLoading(report: AgentReport, artifact: string): boolean {
  return Boolean(artifactPreviewLoading[artifactPreviewKey(report, artifact)])
}

function artifactPreviewButtonLabel(report: AgentReport, artifact: string): string {
  const key = artifactPreviewKey(report, artifact)
  if (artifactPreviewLoading[key]) return 'Loading...'
  return expandedArtifactKey.value === key ? 'Hide preview' : 'Preview Markdown'
}

async function toggleArtifactPreview(report: AgentReport, artifact: string) {
  const key = artifactPreviewKey(report, artifact)
  if (expandedArtifactKey.value === key) {
    expandedArtifactKey.value = null
    return
  }
  expandedArtifactKey.value = key
  if (artifactPreviews[key] || artifactPreviewLoading[key]) return

  artifactPreviewLoading[key] = true
  delete artifactPreviewErrors[key]
  try {
    artifactPreviews[key] = await workspaceStore.fetchArtifactPreview(
      report.workspace_id,
      artifact,
      report.id,
    )
  } catch (e) {
    artifactPreviewErrors[key] = e instanceof Error ? e.message : 'Failed to load Markdown preview'
  } finally {
    artifactPreviewLoading[key] = false
  }
}

async function openMarkdownPreviewModal(path: string, report?: AgentReport) {
  const workspaceId = report?.workspace_id || selectedTask.value?.workspace_id || activeWorkspaceId.value
  const trimmedPath = path.trim()
  if (!workspaceId || !trimmedPath) return

  markdownPreviewModalPath.value = trimmedPath
  markdownPreviewModalReportId.value = report?.id || null
  markdownPreviewModalContent.value = null
  markdownPreviewModalError.value = null
  markdownPreviewModalLoading.value = true
  try {
    markdownPreviewModalContent.value = await workspaceStore.fetchArtifactPreview(
      workspaceId,
      trimmedPath,
      report?.id,
    )
  } catch (e) {
    markdownPreviewModalError.value = e instanceof Error ? e.message : 'Failed to load Markdown preview'
  } finally {
    markdownPreviewModalLoading.value = false
  }
}

function closeMarkdownPreviewModal() {
  markdownPreviewModalPath.value = null
  markdownPreviewModalReportId.value = null
  markdownPreviewModalContent.value = null
  markdownPreviewModalError.value = null
  markdownPreviewModalLoading.value = false
}

function openImageLightbox(url: string, alt: string) {
  imageLightboxUrl.value = url
  imageLightboxAlt.value = alt
}

function closeImageLightbox() {
  imageLightboxUrl.value = null
  imageLightboxAlt.value = ''
}

function handleLightboxKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && imageLightboxUrl.value) {
    closeImageLightbox()
    return
  }
  if (event.key === 'Escape') {
    closeTaskCardMoreMenus()
  }
}

const reviewerSessions = computed<ManagedSession[]>(() => [
  ...reviewerAgents.value,
  ...temporaryReviewers.value,
])

const managedWorkspaceSessions = computed<ManagedSession[]>(() => [
  ...workspaceAgents.value,
  ...reviewerSessions.value,
  ...(dispatcherAgent.value ? [dispatcherAgent.value] : []),
  ...(residentAgent.value ? [residentAgent.value] : []),
])

const agentManagerView = ref<'agents' | 'reviewers'>('agents')

const agentManagerSessions = computed<ManagedSession[]>(() =>
  agentManagerView.value === 'reviewers'
    ? reviewerSessions.value
    : [
        ...workspaceAgents.value,
        ...(dispatcherAgent.value ? [dispatcherAgent.value] : []),
        ...(residentAgent.value ? [residentAgent.value] : []),
      ],
)

const agentManagerEmptyText = computed(() =>
  agentManagerView.value === 'reviewers'
    ? 'No reviewers in this workspace.'
    : 'No agents in this workspace.',
)

const visibleWorkspaceSessions = computed<ManagedSession[]>(() =>
  workspaceSessionView.value === 'reviewers'
    ? reviewerSessions.value
    : [
        ...workspaceAgents.value,
        ...(residentAgent.value ? [residentAgent.value] : []),
      ]
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

// The directory browser is used by the Add-Agent form to place the new agent's
// working directory. The resident agent has no cwd field — it always runs in
// the workspace's own directory — so the browser only serves the Add-Agent form.
interface BrowserPlacement {
  target: ExecutionTarget
  remoteProfileId: string
  cwd: string
  setCwd: (value: string) => void
}

const browserPlacement = computed<BrowserPlacement>(() => {
  return {
    target: agentOptionsForm.target,
    remoteProfileId: agentOptionsForm.remote_profile_id,
    cwd: agentOptionsForm.cwd,
    setCwd: (value: string) => {
      agentOptionsForm.cwd = value
    },
  }
})

const browserRemoteProfile = computed(() =>
  remoteProfiles.value.find(profile => profile.id === browserPlacement.value.remoteProfileId) || null
)

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
      target_session_id: task.session_id || '',
      related_task_id: task.related_task_id || '',
      clear_context: Boolean(task.clear_context),
    }
  }
  return startOptions[task.id]
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

function taskModeLabel(mode: WorkspaceTaskMode) {
  if (mode === 'autonomous') return 'Autonomous'
  if (mode === 'direct') return 'Direct'
  return 'Reviewed'
}

function executionComplexityLabel(complexity: WorkspaceTaskExecutionComplexity) {
  if (complexity === 'complex') return 'Complex'
  if (complexity === 'simple') return 'Simple'
  return 'Auto'
}

function goalPacketStatusLabel(status: GoalPacket['status']) {
  if (status === 'pending_review') return 'Pending review'
  if (status === 'approved') return 'Approved'
  if (status === 'rejected') return 'Rejected'
  if (status === 'frozen') return 'Frozen'
  if (status === 'superseded') return 'Superseded'
  return 'Draft'
}

function goalPacketGateLabel(task: WorkspaceTask) {
  const status = task.goal_packet?.status
  if (!status) return 'Not recorded'
  if (status === 'pending_review') {
    if (task.review_requested_at && !task.review_completed_at) {
      return 'Awaiting Goal Packet approval'
    }
    if (task.review_completed_at) return 'Review needs input'
    return 'Pending approval'
  }
  if (status === 'approved') return 'Approved for implementation'
  if (status === 'rejected') return 'Changes requested before implementation'
  return goalPacketStatusLabel(status)
}

function autonomousRunPhaseLabel(phase: string) {
  return phase
    .split('_')
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

function formatAutonomousScore(score: number | null | undefined) {
  if (score === null || score === undefined) return 'none'
  return `${Math.round(score * 100)}%`
}

function reviewStatusLabel(task: WorkspaceTask) {
  if (task.human_accepted_at) return 'Human accepted'
  if (task.goal_packet?.status === 'pending_review') {
    if (task.review_requested_at && !task.review_completed_at) return 'Pending Goal Packet approval'
    if (task.review_completed_at) return 'Goal Packet review needs input'
  }
  if (task.goal_packet?.status === 'rejected' && task.status === 'working') {
    return 'Goal Packet changes requested'
  }
  if (task.goal_packet?.status === 'approved' && task.status === 'working') {
    return 'Goal Packet approved; implementation active'
  }
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

// Human-readable label + tone for a report state. Maps snake_case backend
// states to title-case English and surfaces a tone hint used by the card's
// .latest-report block for subtle status color coding. Unknown states fall
// back to underscore-to-space replacement.
function reportStateLabel(state: string | undefined | null): {
  label: string
  tone: 'neutral' | 'active' | 'success' | 'attention' | 'muted'
} {
  if (!state) return { label: '', tone: 'neutral' }
  switch (state) {
    case 'working':
      return { label: 'In progress', tone: 'active' }
    case 'review_started':
      return { label: 'Reviewing', tone: 'active' }
    case 'review_passed':
      return { label: 'Review passed', tone: 'success' }
    case 'review_failed':
    case 'review_needs_input':
      return { label: 'Changes requested', tone: 'attention' }
    case 'goal_packet_proposed':
    case 'goal_packet':
      return { label: 'Goal packet', tone: 'muted' }
    case 'completed':
      return { label: 'Completed', tone: 'success' }
    case 'idle':
      return { label: 'Idle', tone: 'muted' }
    case 'blocked':
      return { label: 'Blocked', tone: 'attention' }
    case 'needs_input':
      return { label: 'Needs input', tone: 'attention' }
    case 'ready_for_review':
      return { label: 'Ready for review', tone: 'active' }
    default:
      return {
        label: state.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
        tone: 'neutral',
      }
  }
}

function activeReviewBadge(
  task: WorkspaceTask,
): { kind: 'active' | 'pending' | 'attention'; label: string; title: string } | null {
  const latestReviewReport = latestReviewReportForTask(task)
  if (
    task.goal_packet?.status === 'pending_review' &&
    task.review_requested_at &&
    !task.review_completed_at
  ) {
    const reviewerName = task.review_session_id
      ? reviewerTitle(task.review_session_id)
      : 'AI reviewer'
    return {
      kind: latestReviewReport?.state === 'review_started' ? 'active' : 'pending',
      label: latestReviewReport?.state === 'review_started'
        ? 'Packet reviewing'
        : 'Packet approval',
      title: `${reviewerName} is reviewing the Goal Packet before implementation starts`,
    }
  }
  if (task.goal_packet?.status === 'rejected' && task.status === 'working') {
    return {
      kind: 'attention',
      label: 'Packet changes',
      title: 'Goal Packet review requested changes before implementation can start',
    }
  }
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

// Thin store-backed wrappers over the pure gate logic in
// utils/taskAcceptance.ts. They resolve the latest report / latest review
// report for the task and delegate the decision so it stays unit-testable.
function awaitingHumanAcceptance(task: WorkspaceTask) {
  return taskAcceptanceAwaiting(
    task,
    workspaceStore.latestReportForTask(task),
    latestReviewReportForTask(task),
  )
}

function canMarkDoneTask(task: WorkspaceTask) {
  return taskAcceptanceCanMarkDone(
    task,
    workspaceStore.latestReportForTask(task),
    latestReviewReportForTask(task),
  )
}

function canRequestReviewTask(task: WorkspaceTask) {
  if (task.status !== 'review') return false
  const latestReviewReport = latestReviewReportForTask(task)
  return awaitingHumanAcceptance(task) ||
    task.review_skipped_at ||
    latestReviewReport?.state === 'review_failed' ||
    latestReviewReport?.state === 'review_needs_input'
}

function canAbortTask(task: WorkspaceTask) {
  return task.status === 'queued' || task.status === 'working' || task.status === 'review'
}

function canEditTask(task: WorkspaceTask) {
  return task.status === 'todo'
}

function primaryExpandedReportId(): string | null {
  const reports = selectedReports.value
  if (reports.length === 0) return null
  const finalSubstantive = [...reports].reverse().find(
    (r) => isFinalReport(r) && isSubstantiveReport(r)
  )
  if (finalSubstantive) return finalSubstantive.id
  const substantive = [...reports].reverse().find(isSubstantiveReport)
  if (substantive) return substantive.id
  return reports[reports.length - 1].id
}

function isLatestSelectedReport(report: AgentReport) {
  return primaryExpandedReportId() === report.id
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
  if (agent.role === 'resident') return 'Resident'
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

function canSwitchAgentEnv(agent: ManagedSession) {
  // Switch-env is Claude-only, requires a live local tmux session (not
  // offline/stopped), and is only available on local targets (remote sessions
  // aren't supported by the backend respawn flow).
  return agent.agent_type === 'claude'
    && agent.target === 'local'
    && agent.runtime_status !== 'offline'
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
    expandedArtifactKey.value = null
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
  expandedArtifactKey.value = null
  closeMarkdownPreviewModal()
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

// Compact relative age label for done-card scannability (e.g. '2h', '3d', '1mo').
function formatRelativeTime(value: string | null | undefined): string {
  if (!value) return ''
  const t = new Date(value).getTime()
  if (!Number.isFinite(t)) return ''
  const diffMs = Date.now() - t
  const min = 60_000
  const hour = 60 * min
  const day = 24 * hour
  const abs = Math.abs(diffMs)
  if (abs < min) return diffMs < 0 ? 'soon' : 'just now'
  if (abs < hour) {
    const n = Math.floor(abs / min)
    return `${n}m`
  }
  if (abs < day) {
    const n = Math.floor(abs / hour)
    return `${n}h`
  }
  if (abs < 30 * day) {
    const n = Math.floor(abs / day)
    return `${n}d`
  }
  if (abs < 365 * day) {
    const n = Math.floor(abs / (30 * day))
    return `${n}mo`
  }
  const n = Math.floor(abs / (365 * day))
  return `${n}y`
}

function taskAgeLabel(task: WorkspaceTask): string {
  return formatRelativeTime(task.completed_at || task.updated_at || task.created_at)
}

function parseTimestampMs(value?: string | null): number | null {
  if (!value) return null
  const timestamp = new Date(value).getTime()
  return Number.isFinite(timestamp) ? timestamp : null
}

function addTimelineItem(
  items: Array<Omit<ProgressTimelineItem, 'elapsedLabel' | 'deltaLabel'>>,
  id: string,
  label: string,
  value: string | null | undefined,
  tone: ProgressTimelineItem['tone'],
) {
  const timestampMs = parseTimestampMs(value)
  if (timestampMs === null) return
  items.push({ id, label, timestampMs, tone })
}

function formatElapsedDuration(valueMs: number) {
  const totalSeconds = Math.max(0, Math.floor(valueMs / 1000))
  if (totalSeconds < 60) return `${totalSeconds}s`

  const totalMinutes = Math.floor(totalSeconds / 60)
  if (totalMinutes < 60) return `${totalMinutes}m`

  const totalHours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  if (totalHours < 24) {
    return minutes > 0 && totalHours < 12 ? `${totalHours}h ${minutes}m` : `${totalHours}h`
  }

  const days = Math.floor(totalHours / 24)
  const hours = totalHours % 24
  return hours > 0 && days < 14 ? `${days}d ${hours}h` : `${days}d`
}

function taskTimingEndMs(task: WorkspaceTask): number {
  return (
    parseTimestampMs(task.completed_at) ??
    parseTimestampMs(task.manual_aborted_at) ??
    (
      task.status === 'queued' || task.status === 'working' || task.status === 'review'
        ? elapsedClockMs.value
        : parseTimestampMs(task.updated_at) ?? elapsedClockMs.value
    )
  )
}

function taskTotalElapsedLabel(task: WorkspaceTask) {
  const startedMs = parseTimestampMs(task.created_at)
  if (startedMs === null) return 'unknown'
  return formatElapsedDuration(taskTimingEndMs(task) - startedMs)
}

function taskWorkingStartedMs(task: WorkspaceTask): number | null {
  return (
    parseTimestampMs(task.started_at) ??
    parseTimestampMs(task.autonomous_run?.iterations?.[0]?.started_at) ??
    parseTimestampMs(selectedReports.value[0]?.created_at)
  )
}

function taskWorkingElapsedLabel(task: WorkspaceTask) {
  const startedMs = taskWorkingStartedMs(task)
  if (startedMs === null) return 'not started'
  return formatElapsedDuration(taskTimingEndMs(task) - startedMs)
}

function reportReferenceForIndex(index: number): { label: string; timestampMs: number } | null {
  const previousReportMs = parseTimestampMs(selectedReports.value[index - 1]?.created_at)
  if (previousReportMs !== null) return { label: 'previous report', timestampMs: previousReportMs }

  const task = selectedTask.value
  const reportMs = parseTimestampMs(selectedReports.value[index]?.created_at)
  if (!task || reportMs === null) return null

  const startedMs = parseTimestampMs(task.started_at)
  if (startedMs !== null && startedMs <= reportMs) return { label: 'start', timestampMs: startedMs }

  const queuedMs = parseTimestampMs(task.queued_at)
  if (queuedMs !== null && queuedMs <= reportMs) return { label: 'queue', timestampMs: queuedMs }

  const createdMs = parseTimestampMs(task.created_at)
  if (createdMs !== null) return { label: 'creation', timestampMs: createdMs }

  return null
}

function reportElapsedLabel(report: AgentReport, index: number) {
  const reportMs = parseTimestampMs(report.created_at)
  const reference = reportReferenceForIndex(index)
  if (reportMs === null || !reference) return ''

  const elapsed = formatElapsedDuration(reportMs - reference.timestampMs)
  return index === 0 ? `${reference.label} +${elapsed}` : `+${elapsed}`
}

function reportElapsedTitle(report: AgentReport, index: number) {
  const reportMs = parseTimestampMs(report.created_at)
  const reference = reportReferenceForIndex(index)
  if (reportMs === null || !reference) return 'Elapsed time unavailable'

  return `Elapsed since ${reference.label}: ${formatElapsedDuration(reportMs - reference.timestampMs)}`
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
    // (F5) error is now a computed; push to the notification queue instead.
    workspaceStore.pushNotification({
      type: 'error',
      message: e instanceof Error ? e.message : 'Failed to load remote profiles',
      autoDismissMs: 8000,
    })
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
      // NOTE: create-mode inlines the resident_agent_* fields rather than
      // spreading buildResidentPayload() because that helper returns a
      // WorkspaceUpdate, whose resident_agent_title / resident_agent_cwd are
      // `string | null`. WorkspaceCreate types those as `string | undefined`
      // (null not allowed), so the spread fails vue-tsc. For create, `undefined`
      // is the correct "omit" value — the backend applies its defaults — so the
      // `|| undefined` here is intentional and not a divergence bug.
      resident_agent_enabled: workspaceForm.resident_agent_enabled,
      resident_agent_paused: workspaceForm.resident_agent_paused,
      resident_agent_master_mode: workspaceForm.resident_agent_master_mode,
      resident_agent_interval_minutes: workspaceForm.resident_agent_interval_minutes,
      resident_agent_directive: workspaceForm.resident_agent_directive.trim() || undefined,
      resident_agent_periodic_tasks: sanitizePeriodicTasks(
        workspaceForm.resident_agent_periodic_tasks,
      ),
      resident_agent_type: workspaceForm.resident_agent_type,
      resident_agent_env: parseLaunchEnv(workspaceForm.resident_env_text) ?? {},
      resident_agent_solo_mode: workspaceForm.resident_agent_solo_mode,
      resident_agent_title: workspaceForm.resident_agent_title.trim() || undefined,
      resident_agent_target: workspaceForm.resident_agent_target,
      resident_agent_remote_profile_id:
        workspaceForm.resident_agent_target === 'remote'
          ? workspaceForm.resident_agent_remote_profile_id || null
          : null,
      resident_agent_cwd: workspaceForm.resident_agent_cwd.trim() || undefined,
      resident_agent_remote_reconnect: workspaceForm.resident_agent_remote_reconnect,
    })
  )
  if (workspace) {
    selectedWorkspaceId.value = workspace.id
    showWorkspaceModal.value = false
  }
}

// Build the resident_agent_* slice of a WorkspaceUpdate from the current form,
// merged with any overrides. Shared by handleSaveWorkspace and the resident
// lifecycle buttons (handleCreateResident) so the payload shape stays in one
// place. Field shape matches the values WorkspaceUpdate accepts.
function buildResidentPayload(overrides: Partial<WorkspaceUpdate> = {}): WorkspaceUpdate {
  return {
    resident_agent_enabled: workspaceForm.resident_agent_enabled,
    resident_agent_paused: workspaceForm.resident_agent_paused,
    resident_agent_master_mode: workspaceForm.resident_agent_master_mode,
    resident_agent_interval_minutes: workspaceForm.resident_agent_interval_minutes,
    resident_agent_directive: workspaceForm.resident_agent_directive.trim() || undefined,
    resident_agent_periodic_tasks: sanitizePeriodicTasks(
      workspaceForm.resident_agent_periodic_tasks,
    ),
    resident_agent_type: workspaceForm.resident_agent_type,
    resident_agent_env: parseLaunchEnv(workspaceForm.resident_env_text) ?? {},
    resident_agent_solo_mode: workspaceForm.resident_agent_solo_mode,
    resident_agent_title: workspaceForm.resident_agent_title.trim() || null,
    resident_agent_target: workspaceForm.resident_agent_target,
    resident_agent_remote_profile_id:
      workspaceForm.resident_agent_target === 'remote'
        ? workspaceForm.resident_agent_remote_profile_id || null
        : null,
    resident_agent_cwd: workspaceForm.resident_agent_cwd.trim() || null,
    resident_agent_remote_reconnect: workspaceForm.resident_agent_remote_reconnect,
    ...overrides,
  }
}

// --- Resident periodic-task editor helpers -----------------------------------
//
// Periodic tasks are the recurring checklist the resident runs EVERY wake-up
// (distinct from the one-shot guiding directive). The modal edits a draft array
// on workspaceForm; sanitize trims + drops empties before submit, mirroring the
// backend's _normalize_periodic_tasks so what the user sees rendered matches
// what the resident prompt embeds.

function newPeriodicTaskId() {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID()
  }
  return `ptask-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

// Drop blank rows and trim text; keep ids/order so a save round-trips stable.
function sanitizePeriodicTasks(tasks: ResidentPeriodicTask[]): ResidentPeriodicTask[] {
  return tasks
    .map((task) => ({ ...task, text: (task.text ?? '').trim() }))
    .filter((task) => task.text.length > 0)
}

function addPeriodicTask() {
  workspaceForm.resident_agent_periodic_tasks.push({
    id: newPeriodicTaskId(),
    text: '',
    enabled: true,
  })
}

function removePeriodicTask(taskId: string) {
  const tasks = workspaceForm.resident_agent_periodic_tasks
  const index = tasks.findIndex((task) => task.id === taskId)
  if (index !== -1) tasks.splice(index, 1)
}

// Count of non-blank tasks currently enabled — drives the modal summary hint so
// the user can see how many recurring items the resident will actually run.
const enabledPeriodicTaskCount = computed(
  () =>
    workspaceForm.resident_agent_periodic_tasks.filter(
      (task) => task.enabled && (task.text ?? '').trim().length > 0,
    ).length,
)

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
      ...buildResidentPayload(),
    })
  )
  if (workspace) {
    showWorkspaceModal.value = false
  }
}

function resetWorkspaceForm() {
  workspaceForm.name = 'Claude Hub'
  workspaceForm.path = ''
  workspaceForm.default_branch = 'main'
  workspaceForm.session_prefix = 'chub'
  workspaceForm.target = 'local'
  workspaceForm.remote_profile_id = remoteProfiles.value[0]?.id || ''
  workspaceForm.remote_cwd = ''
  workspaceForm.remote_reconnect = true
  workspaceForm.resident_agent_enabled = false
  workspaceForm.resident_agent_paused = false
  workspaceForm.resident_agent_master_mode = false
  workspaceForm.resident_agent_interval_minutes = 60
  workspaceForm.resident_agent_directive = ''
  workspaceForm.resident_agent_periodic_tasks = []
  workspaceForm.resident_agent_type = 'claude'
  workspaceForm.resident_agent_solo_mode = true
  workspaceForm.resident_env_preset = defaultLaunchEnvPresetForAgent('claude')
  workspaceForm.resident_env_text = defaultPresetTextForAgent('claude')
  workspaceForm.resident_agent_title = ''
  workspaceForm.resident_agent_target = 'local'
  workspaceForm.resident_agent_remote_profile_id = remoteProfiles.value[0]?.id || ''
  workspaceForm.resident_agent_cwd = ''
  workspaceForm.resident_agent_remote_reconnect = true
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
  workspaceForm.resident_agent_enabled = workspace.resident_agent_enabled ?? false
  workspaceForm.resident_agent_paused = workspace.resident_agent_paused ?? false
  workspaceForm.resident_agent_master_mode = workspace.resident_agent_master_mode ?? false
  workspaceForm.resident_agent_interval_minutes = workspace.resident_agent_interval_minutes ?? 60
  workspaceForm.resident_agent_directive = workspace.resident_agent_directive || ''
  // Clone so the modal editor mutates a draft, not the store's workspace object.
  workspaceForm.resident_agent_periodic_tasks = (
    workspace.resident_agent_periodic_tasks ?? []
  ).map((task) => ({ ...task }))
  workspaceForm.resident_agent_type = workspace.resident_agent_type ?? 'claude'
  workspaceForm.resident_agent_solo_mode = workspace.resident_agent_solo_mode ?? true
  // Preset ids are localStorage-only; default the select to the agent-type
  // default and treat the serialized env text as the source of truth.
  workspaceForm.resident_env_preset = defaultLaunchEnvPresetForAgent(
    workspaceForm.resident_agent_type
  )
  workspaceForm.resident_env_text = serializeLaunchEnv(workspace.resident_agent_env)
  workspaceForm.resident_agent_title = workspace.resident_agent_title || ''
  workspaceForm.resident_agent_target = workspace.resident_agent_target ?? 'local'
  workspaceForm.resident_agent_remote_profile_id =
    workspace.resident_agent_remote_profile_id || ''
  workspaceForm.resident_agent_cwd = workspace.resident_agent_cwd || ''
  workspaceForm.resident_agent_remote_reconnect = workspace.resident_agent_remote_reconnect ?? true
  workspaceModalMode.value = 'edit'
  editingWorkspaceId.value = workspace.id
  showWorkspaceModal.value = true
  if (workspace.target === 'remote' || workspace.resident_agent_target === 'remote') {
    fetchRemoteProfiles()
  }
}

function closeWorkspaceModal() {
  showWorkspaceModal.value = false
  showResidentAgentModal.value = false
  workspaceModalMode.value = 'create'
  editingWorkspaceId.value = null
}

function openResidentAgentModal() {
  showResidentAgentModal.value = true
  // The Remote Server dropdown needs the profile list; load it lazily the same
  // way the Add-Agent modal does.
  if (remoteProfiles.value.length === 0) {
    fetchRemoteProfiles()
  }
}

function closeResidentAgentModal() {
  showResidentAgentModal.value = false
}

// --- Resident lifecycle buttons (Create / Pause / Delete) ---------------
//
// These act immediately via PATCH /api/workspaces/{id} in EDIT mode, where the
// workspace already exists. In CREATE mode there is no id to PATCH yet, so the
// three lifecycle buttons are disabled and the resident config is persisted by
// the parent "Create workspace" button (handleCreateWorkspace already sends the
// resident_agent_* fields). "Exists" is read from the SAVED workspace
// (activeWorkspace), not the editable form flag.
const isResidentCreateMode = computed(() => workspaceModalMode.value !== 'edit')
const residentExists = computed(
  () => !isResidentCreateMode.value && (activeWorkspace.value?.resident_agent_enabled ?? false)
)

// Create / enable the resident: PATCH the full resident payload with
// enabled:true. The next monitor tick spawns the resident session. Keep the
// sub-modal open so the user sees the state flip (Create disables, Pause/Delete
// enable). The override forces enabled:true regardless of the form value, so we
// mirror the flag into the form ONLY after the PATCH succeeds (success-gated,
// like handleToggleResidentPause / handleDeleteResident). This avoids leaving
// the form at enabled:true after a failed PATCH, which a later parent "Save
// workspace" would otherwise re-send and unintentionally create the resident.
async function handleCreateResident() {
  if (workspaceModalMode.value !== 'edit' || !editingWorkspaceId.value) return
  if (residentExists.value) return
  const workspaceId = editingWorkspaceId.value
  const workspace = await runPending('resident:create', () =>
    workspaceStore.updateWorkspace(
      workspaceId,
      buildResidentPayload({ resident_agent_enabled: true })
    )
  )
  if (workspace) {
    workspaceForm.resident_agent_enabled = true
  }
}

// Toggle Pause/Resume: PATCH only resident_agent_paused. Backend keeps the
// session + tab alive on pause (only stops auto-scheduling). Mirror the new
// value into the form so the sub-modal checkbox/label stay in sync.
async function handleToggleResidentPause() {
  if (workspaceModalMode.value !== 'edit' || !editingWorkspaceId.value) return
  if (!residentExists.value) return
  const workspaceId = editingWorkspaceId.value
  const next = !(activeWorkspace.value?.resident_agent_paused ?? false)
  const workspace = await runPending('resident:pause', () =>
    workspaceStore.updateWorkspace(workspaceId, { resident_agent_paused: next })
  )
  if (workspace) {
    workspaceForm.resident_agent_paused = next
  }
}

// Delete the resident agent ONLY (not the workspace): PATCH enabled:false. The
// backend's disabling_resident path clears the session pointer, drops the
// ManagedSession (the orphan-tab pruner removes the tab), and resets
// last_run_at. The workspace itself is untouched.
async function handleDeleteResident() {
  if (workspaceModalMode.value !== 'edit' || !editingWorkspaceId.value) return
  if (!residentExists.value) return
  const confirmed = window.confirm(
    'Remove the resident agent for this workspace? Its session will be stopped. '
      + 'The workspace itself is kept.'
  )
  if (!confirmed) return
  const workspaceId = editingWorkspaceId.value
  const workspace = await runPending('resident:delete', () =>
    workspaceStore.updateWorkspace(workspaceId, { resident_agent_enabled: false })
  )
  if (workspace) {
    workspaceForm.resident_agent_enabled = false
  }
}

// "Run now": force the resident to fire on the next monitor tick using the
// SAVED directive + periodic tasks (no form save). Deliberate one-off — bypasses
// pause but respects enabled, per the backend request_resident_run guard.
async function handleRunResidentNow() {
  const workspaceId = editingWorkspaceId.value ?? activeWorkspaceId.value
  if (!workspaceId || !residentExists.value) return
  await runPending('resident:run', () => workspaceStore.runResidentNow(workspaceId))
}

// "Save & run now": persist the current form (directive + periodic tasks + all
// resident config) THEN request an immediate run, so the freshly-saved directive
// takes effect this cycle instead of waiting for the next interval. Plain "Save"
// only applies on the next natural wake-up — this button makes the timing
// explicit for the user, which was the core complaint.
async function handleSaveResidentAndRunNow() {
  if (workspaceModalMode.value !== 'edit' || !editingWorkspaceId.value) return
  if (!residentExists.value) return
  const workspaceId = editingWorkspaceId.value
  const workspace = await runPending('resident:save-run', () =>
    workspaceStore.updateWorkspace(workspaceId, buildResidentPayload())
  )
  if (!workspace) return
  await workspaceStore.runResidentNow(workspaceId)
}
const residentSummaryLabel = computed(() => {
  if (!workspaceForm.resident_agent_enabled) return 'Off'
  const every = `every ${workspaceForm.resident_agent_interval_minutes || 60}m`
  const base = workspaceForm.resident_agent_paused ? `Paused · ${every}` : `On · ${every}`
  return workspaceForm.resident_agent_master_mode ? `${base} · Autopilot` : base
})

async function handleDeleteWorkspace() {
  const workspaceId = editingWorkspaceId.value
  if (!workspaceId) return
  const name = workspaceForm.name.trim() || 'this workspace'
  const confirmed = window.confirm(
    `Delete workspace "${name}"? This cannot be undone.`,
  )
  if (!confirmed) return
  try {
    await runPending('workspace:delete', () => workspaceStore.deleteWorkspace(workspaceId))
    closeWorkspaceModal()
  } catch {
    // Error already surfaced via store notification; keep the modal open.
  }
}

function closeWorkspaceMobileMenu() {
  if (workspaceMobileMenuRef.value) {
    workspaceMobileMenuRef.value.open = false
  }
}

function closeTaskCardMoreMenus() {
  document
    .querySelectorAll<HTMLDetailsElement>('.task-card-more-menu[open]')
    .forEach((el) => {
      el.open = false
    })
}

function handleTaskCardMorePointerDown(event: PointerEvent) {
  const target = event.target
  if (!(target instanceof Node)) return
  // Close any open .task-card-more-menu that does not contain the click target.
  document
    .querySelectorAll<HTMLDetailsElement>('.task-card-more-menu[open]')
    .forEach((menu) => {
      if (!menu.contains(target)) menu.open = false
    })
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

function openLessonsModalFromMenu() {
  openLessonsModal()
  closeWorkspaceMobileMenu()
}

function toggleThemeFromMenu() {
  appStore.toggleColorScheme()
  closeWorkspaceMobileMenu()
}

function workspaceDefaultCwd(target: ExecutionTarget, remoteProfileId?: string): string {
  const workspace = activeWorkspace.value
  if (!workspace) return ''
  if (target === 'remote') {
    // When a specific profile id is supplied (e.g. the resident form's own
    // selection) resolve its default_cwd; otherwise fall back to the Add-Agent
    // form's selected profile.
    const profile = remoteProfileId
      ? remoteProfiles.value.find(p => p.id === remoteProfileId) || null
      : selectedAgentRemoteProfile.value
    return workspace.remote_cwd || profile?.default_cwd || ''
  }
  return workspace.path || ''
}

// ---- Switch Env (hot-swap on running Claude agents) ----

function serializeAgentEnv(env: Record<string, string> | undefined | null): string {
  if (!env) return ''
  return Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .join('\n')
}

function applySwitchEnvPreset(presetId: string) {
  const text = getPresetText(presetId)
  if (text === null) return
  switchEnvForm.env_text = text
}

function openSwitchEnvModal(agent: ManagedSession) {
  switchEnvAgent.value = agent
  switchEnvForm.env_preset = 'custom'
  switchEnvForm.env_text = serializeAgentEnv(agent.env)
  switchEnvForm.solo_mode = agent.solo_mode ?? false
  showSwitchEnvModal.value = true
}

function closeSwitchEnvModal() {
  showSwitchEnvModal.value = false
  showSwitchEnvManager.value = false
  switchEnvAgent.value = null
}

function openSwitchEnvPresetManager() {
  showSwitchEnvManager.value = true
}

function closeSwitchEnvPresetManager() {
  showSwitchEnvManager.value = false
  applySwitchEnvPreset(switchEnvForm.env_preset)
}

async function handleSwitchEnv() {
  const agent = switchEnvAgent.value
  if (!agent) return
  const env = parseLaunchEnv(switchEnvForm.env_text)
  if (!env) {
    workspaceStore.pushNotification({
      type: 'error',
      message: 'Please provide at least one KEY=VALUE environment variable, or pick a preset.',
      autoDismissMs: 6000,
    })
    return
  }
  try {
    await runPending(sessionActionKey('switch-env', agent.id), async () => {
      await terminalStore.switchEnv(agent.tab_id, {
        env,
        solo_mode: switchEnvForm.solo_mode,
      })
    })
    workspaceStore.pushNotification({
      type: 'success',
      message: `Environment switched for "${agent.title}". Agent is resuming conversation.`,
      autoDismissMs: 4000,
    })
    closeSwitchEnvModal()
  } catch (e) {
    // terminalStore.switchEnv already pushes an error notification; log too.
    console.error('switch env failed', e)
  }
}

function resetAgentEnvForType(agentType: AgentType) {
  agentOptionsForm.env_preset = defaultLaunchEnvPresetForAgent(agentType)
  agentOptionsForm.env_text = defaultPresetTextForAgent(agentType)
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
  resetAgentEnvForType(agentOptionsForm.agent_type)
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

function handleResidentTargetChange(target: ExecutionTarget) {
  if (!workspaceForm.resident_agent_enabled) return
  if (workspaceForm.resident_agent_target === target) return
  workspaceForm.resident_agent_target = target
  if (target === 'remote') {
    if (!workspaceForm.resident_agent_remote_profile_id && remoteProfiles.value.length > 0) {
      workspaceForm.resident_agent_remote_profile_id = remoteProfiles.value[0].id
    }
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
  const env = parseLaunchEnv(agentOptionsForm.env_text)
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
      env,
    })
    showAgentFileBrowser.value = false
    agentOptionsForm.title = ''
    resetAgentEnvForType(agentOptionsForm.agent_type)
    await terminalStore.fetchTabs()
  })
}

async function listAgentDirectory(path?: string): Promise<DirectoryListing> {
  const placement = browserPlacement.value
  const params = new URLSearchParams()
  if (path) {
    params.append('path', path)
  }
  if (placement.target === 'remote') {
    if (!placement.remoteProfileId) {
      throw new Error('Select a remote server first')
    }
    params.append('profile_id', placement.remoteProfileId)
  }
  const endpoint =
    placement.target === 'remote' ? '/api/remote/filesystem/list' : '/api/filesystem/list'
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
  await openDirectoryBrowserForPlacement()
}

async function openDirectoryBrowserForPlacement() {
  const placement = browserPlacement.value
  if (placement.cwd) {
    await loadAgentDirectory(placement.cwd, 'agent-browser:open')
  } else if (placement.target === 'remote') {
    await loadAgentDirectory(browserRemoteProfile.value?.default_cwd || '~', 'agent-browser:open')
  } else {
    await loadAgentDirectory('~', 'agent-browser:open')
  }
}

function navigateAgentBrowserHome() {
  if (browserPlacement.value.target === 'remote') {
    loadAgentDirectory(browserRemoteProfile.value?.default_cwd || '~', 'agent-browser:home')
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
  browserPlacement.value.setCwd(agentBrowserCurrentPath.value)
  showAgentFileBrowser.value = false
}

function resetTaskForm() {
  taskForm.title = ''
  taskForm.prompt = ''
  taskForm.task_mode = 'reviewed'
  taskForm.execution_complexity = 'auto'
  taskForm.max_iterations = 3
  taskForm.evaluation_strictness = 'balanced'
  taskForm.allow_web_research = false
  taskForm.require_artifact_review = false
  taskForm.session_id = ''
  taskForm.clear_context = false
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

function openEditTaskModal(task: WorkspaceTask) {
  if (!canEditTask(task)) return
  editingTaskId.value = task.id
  editTaskForm.title = task.title
  editTaskForm.prompt = task.prompt
  editTaskForm.session_id = task.session_id || ''
  editTaskForm.related_task_id = task.related_task_id || ''
  editTaskForm.clear_context = Boolean(task.clear_context)
  showEditTaskModal.value = true
}

function closeEditTaskModal() {
  showEditTaskModal.value = false
  editingTaskId.value = null
  editTaskForm.title = ''
  editTaskForm.prompt = ''
  editTaskForm.session_id = ''
  editTaskForm.related_task_id = ''
  editTaskForm.clear_context = false
}

function openLessonsModal() {
  showLessonsModal.value = true
  workspaceStore.fetchFeedbackLessons().catch(() => {
    // Error state is owned by the workspace store.
  })
}

function closeLessonsModal() {
  showLessonsModal.value = false
}

function resetLessonForm() {
  lessonForm.title = ''
  lessonForm.description = ''
  lessonForm.tags = ''
}

function parseLessonTags(value: string): string[] {
  return value
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
}

function lessonActionKey(action: string, lessonId: string) {
  return `lesson:${action}:${lessonId}`
}

async function handleCreateLesson() {
  const title = lessonForm.title.trim()
  const description = lessonForm.description.trim()
  if (!title || !description) return
  await runPending('feedback:create', async () => {
    await workspaceStore.createFeedbackLesson({
      title,
      summary: description,
      applies_when: parseLessonTags(lessonForm.tags),
      tags: parseLessonTags(lessonForm.tags),
      scope: 'workspace',
      confidence: 0.8,
    })
    resetLessonForm()
  })
}

async function deleteLesson(lesson: FeedbackLesson) {
  const confirmed = window.confirm(`Delete lesson "${lessonTitle(lesson)}"?`)
  if (!confirmed) return
  await runPending(lessonActionKey('delete', lesson.id), async () => {
    await workspaceStore.deleteFeedbackLesson(lesson.id)
  })
}

async function handleSummarizeLessons(force: boolean) {
  if (!activeWorkspaceId.value) return
  const actionKey = force ? 'feedback:summarize:force' : 'feedback:summarize'
  await runPending(actionKey, async () => {
    const run = await workspaceStore.summarizeFeedbackLessons({
      force,
      limit: 5,
      clear_context: true,
    })
    lastFeedbackSummaryRun.value = run || null
  })
}

async function handleCreateTask() {
  if (!taskForm.title.trim() || (!taskForm.prompt.trim() && taskForm.attachments.length === 0)) {
    return
  }
  await runPending('task:create', async () => {
    const autonomyPolicy: AutonomyPolicy | null = taskForm.task_mode === 'autonomous'
      ? {
          max_iterations: Math.max(1, Number(taskForm.max_iterations) || 3),
          evaluation_strictness: taskForm.evaluation_strictness,
          allow_web_research: taskForm.allow_web_research,
          require_artifact_review: taskForm.require_artifact_review,
          human_checkpoint_policy: 'final_only',
          allowed_agent_types: [],
          stop_on_repeated_failure: true,
        }
      : null
    await workspaceStore.createTask({
      title: taskForm.title.trim(),
      prompt: taskForm.prompt.trim(),
      task_mode: taskForm.task_mode,
      execution_complexity: taskForm.execution_complexity,
      autonomy_policy: autonomyPolicy,
      session_id: taskForm.session_id || null,
      clear_context: taskForm.clear_context || null,
      related_task_id: taskForm.related_task_id || null,
      attachments: serializeDraftAttachments(taskForm.attachments),
    })
    resetTaskForm()
    showTaskModal.value = false
  })
}

async function handleUpdateTask() {
  const taskId = editingTaskId.value
  if (
    !taskId ||
    !editTaskForm.title.trim() ||
    !editTaskForm.prompt.trim()
  ) {
    return
  }
  await runPending(taskActionKey('edit', taskId), async () => {
    const payload: WorkspaceTaskUpdate = {
      title: editTaskForm.title.trim(),
      prompt: editTaskForm.prompt.trim(),
      session_id: editTaskForm.session_id || null,
      related_task_id: editTaskForm.related_task_id || null,
      clear_context: editTaskForm.clear_context ? true : null,
    }
    await workspaceStore.updateTask(taskId, payload)
    // Clear cached start options so they re-read from the updated task
    delete startOptions[taskId]
    closeEditTaskModal()
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

function isTextEntryFocused(): boolean {
  // While the user is actively typing in a text field (most importantly the
  // task-detail compose textarea), a background board poll would replace the
  // entire `board` object and force Vue to re-render the large detail subtree
  // that hosts the focused input. On mobile that periodic re-render competes
  // with keystroke handling and is felt as input lag, so we skip the poll tick
  // until the field is blurred — the next tick (or any explicit action) then
  // refreshes the board normally.
  const active = document.activeElement as HTMLElement | null
  if (!active) return false
  const tag = active.tagName
  if (tag === 'TEXTAREA') return true
  if (tag === 'INPUT') {
    const type = (active as HTMLInputElement).type
    return type !== 'checkbox' && type !== 'radio' && type !== 'range'
  }
  return active.isContentEditable
}

async function refreshBoard() {
  // Defer background refreshes while the user is typing to keep input smooth.
  if (isTextEntryFocused()) return
  try {
    await workspaceStore.fetchBoard()
    // Keep the open task's full timeline live without polling its whole history:
    // the trimmed board carries the latest report per task, so refetch the
    // on-demand history only when that latest report id has changed.
    const task = selectedTask.value
    const workspaceId = activeWorkspaceId.value
    if (task && workspaceId) {
      const boardLatestId = workspaceStore.latestReportForTask(task)?.id ?? null
      const cached = workspaceStore.reportsForTaskId(task.id)
      const cachedLatestId = cached.length ? cached[cached.length - 1].id : null
      if (boardLatestId !== cachedLatestId) {
        await workspaceStore.fetchTaskReports(workspaceId, task.id)
      }
    }
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

async function refreshFeedbackLessons() {
  await runPending('feedback:refresh', async () => {
    await workspaceStore.fetchFeedbackLessons()
  })
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

async function requestReview(task: WorkspaceTask) {
  const message = window.prompt(
    'Tell the reviewer what to check:',
    detailMessage.value.trim(),
  )
  if (!message || !message.trim()) return
  detailMessage.value = ''
  await runPending(taskActionKey('request-review', task.id), () =>
    workspaceStore.requestTaskReview(task.id, { message: message.trim() })
  )
}

async function abortTask(task: WorkspaceTask) {
  const reason = window.prompt(
    `Abort task "${task.title}" and return it to todo? Enter a reason:`,
    DEFAULT_ABORT_REASON,
  )
  const auditReason = resolveAbortReason(reason)
  if (auditReason === null) return
  await runPending(taskActionKey('abort', task.id), async () => {
    await workspaceStore.abortTask(task.id, { reason: auditReason })
    if (selectedTaskId.value === task.id) {
      closeTaskDetail()
    }
  })
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

// Three-state resident lifecycle: Enable (exists + auto-works), Pause (session
// stays for manual chat but won't auto-run), Delete (teardown + clear pointers).
const isResidentPaused = computed(() => activeWorkspace.value?.resident_agent_paused ?? false)
const isResidentMaster = computed(() => activeWorkspace.value?.resident_agent_master_mode ?? false)

// Relative "last run" label for the resident card, mirroring the timeline's
// "{duration} ago" style (see latestSelectedReportAgeLabel).
const residentLastRunLabel = computed(() => {
  const lastRunMs = parseTimestampMs(activeWorkspace.value?.resident_agent_last_run_at)
  if (lastRunMs === null) return 'never'
  return `${formatElapsedDuration(elapsedClockMs.value - lastRunMs)} ago`
})

// True while a run-now request is stamped but not yet consumed by a wake-up.
// Drives the card's "queued" hint and disables the Run-now button so a user
// can't stack duplicate requests before the monitor tick fires.
const residentRunPending = computed(
  () => Boolean(activeWorkspace.value?.resident_agent_run_requested_at),
)

// Human "next run" hint for the resident card. Priority:
//   - run-now requested      -> "next run queued"
//   - paused                 -> "next run paused"
//   - disabled / no schedule -> "" (caller hides the line)
//   - future timestamp       -> "in {duration}" countdown
//   - past-due timestamp     -> "due now" (overdue backstop will pick it up)
const residentNextRunLabel = computed(() => {
  const workspace = activeWorkspace.value
  if (!workspace || !workspace.resident_agent_enabled) return ''
  if (residentRunPending.value) return 'queued'
  if (workspace.resident_agent_paused) return 'paused'
  const nextRunMs = parseTimestampMs(workspace.resident_agent_next_run_at)
  if (nextRunMs === null) return ''
  const remaining = nextRunMs - elapsedClockMs.value
  if (remaining <= 0) return 'due now'
  return `in ${formatElapsedDuration(remaining)}`
})

// Newest report posted by the resident session, used to surface its latest
// heartbeat on the resident card. Returns null when none exist.
const latestResidentReport = computed<AgentReport | null>(() => {
  const sessionId = activeWorkspace.value?.resident_agent_session_id
  if (!sessionId) return null
  const residentReports = workspaceStore.reports.filter(
    report => report.session_id === sessionId
  )
  if (residentReports.length === 0) return null
  return [...residentReports].sort(
    (a, b) => (parseTimestampMs(b.created_at) ?? 0) - (parseTimestampMs(a.created_at) ?? 0)
  )[0]
})

function isResidentAgent(agent: ManagedSession) {
  return agent.role === 'resident'
}

async function toggleResidentPaused() {
  const workspaceId = activeWorkspaceId.value
  if (!workspaceId) return
  const next = !isResidentPaused.value
  await runPending('workspace:resident-pause', () =>
    workspaceStore.updateWorkspace(workspaceId, { resident_agent_paused: next })
  )
}

watch(tasks, value => {
  if (selectedTaskId.value && !value.some(task => task.id === selectedTaskId.value)) {
    closeTaskDetail()
  }
})

// Hydrate the detail panel's full report history on demand. The board payload
// only carries the latest report per task, so opening a task fetches its
// complete history; switching away drops the prior task's cache.
watch(selectedTaskId, (taskId, prevTaskId) => {
  if (prevTaskId && prevTaskId !== taskId) {
    workspaceStore.clearTaskReports(prevTaskId)
  }
  if (taskId && activeWorkspaceId.value) {
    workspaceStore.fetchTaskReports(activeWorkspaceId.value, taskId).catch(() => {
      // Error state is owned by the workspace store.
    })
  }
})

watch(activeWorkspaceId, value => {
  selectedWorkspaceId.value = value || ''
  workspaceSessionView.value = 'agents'
  workspaceStore.clearTaskReports()
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
  document.addEventListener('pointerdown', handleTaskCardMorePointerDown)
  document.addEventListener('keydown', handleLightboxKeydown)
  await fetchRemoteProfiles()
  await workspaceStore.fetchWorkspaces()
  terminalStore.startAgentStatusPolling()
  boardPollTimer = window.setInterval(refreshBoard, 2500)
  elapsedClockTimer = window.setInterval(() => {
    elapsedClockMs.value = Date.now()
  }, 30000)
})

onUnmounted(() => {
  document.removeEventListener('pointerdown', handleWorkspaceDocumentPointerDown)
  document.removeEventListener('pointerdown', handleTaskCardMorePointerDown)
  document.removeEventListener('keydown', handleLightboxKeydown)
  if (boardPollTimer !== null) {
    window.clearInterval(boardPollTimer)
    boardPollTimer = null
  }
  if (elapsedClockTimer !== null) {
    window.clearInterval(elapsedClockTimer)
    elapsedClockTimer = null
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
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--ch-color-app-bg);
  color: var(--ch-color-text);
  overflow-x: hidden;
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
  flex-shrink: 0;
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

.workspace-summary-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 18px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-canvas);
}

.workspace-column-tabs {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex-wrap: wrap;
}

.workspace-summary-primary {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-size: 12px;
}

.summary-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 8px;
  border-radius: 999px;
  background: var(--ch-color-chip-bg-muted);
  color: var(--ch-color-text-muted);
  font-size: 11px;
  white-space: nowrap;
}

.summary-chip strong {
  color: var(--ch-color-text);
  font-weight: 700;
}

.summary-chip--accent {
  background: color-mix(in srgb, var(--ch-color-accent) 12%, transparent);
  color: var(--ch-color-accent);
}

.summary-chip--accent strong {
  color: var(--ch-color-accent-strong);
}

.summary-chip-button {
  height: 22px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  font-size: 11px;
  font-weight: 400;
  padding: 0 8px;
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  transition: border-color var(--ch-motion-fast), background var(--ch-motion-fast);
}

.summary-chip-button strong {
  font-weight: 700;
  color: var(--ch-color-text);
}

.summary-chip-button:hover {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
}

.column-tab-chip {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  background: var(--ch-color-chip-bg-muted);
  color: var(--ch-color-text-muted);
  font-size: 11px;
  padding: 3px 8px;
}

.column-tab-chip strong {
  color: var(--ch-color-text);
  font-weight: 700;
}

/* Defensive: keep the agent-status panel from ever introducing horizontal
   page overflow. The grid itself scrolls internally via overflow-x:auto. */
.workspace-agent-status {
  flex-shrink: 0;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 10px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-canvas);
  padding: 12px 18px;
  min-width: 0;
  overflow-x: hidden;
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
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  padding: 0;
  text-align: left;
}.agent-status-card-main:focus-visible,
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

.agent-status-avatar-wrap {
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
  min-width: 0;
}

.agent-status-avatar-wrap .agent-status-dot {
  position: absolute;
  right: -2px;
  bottom: -2px;
  width: 10px;
  height: 10px;
  border-radius: 999px;
  background: currentColor;
  box-shadow: 0 0 0 2px var(--ch-color-surface);
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

.agent-status-cli {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 2px 7px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text);
}

.agent-status-cli[data-kind='claude'] {
  background: rgba(217, 119, 87, 0.18);
  color: #d97757;
}

.agent-status-cli[data-kind='codex'] {
  background: rgba(16, 163, 127, 0.18);
  color: #10a37f;
}

.agent-status-cli[data-kind='cursor'] {
  background: rgba(120, 120, 120, 0.22);
  color: var(--ch-color-text);
}

.agent-status-cli[data-kind='terminal'] {
  background: rgba(126, 231, 135, 0.16);
  color: #7ee787;
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

/* Resident meta splits into a chip row for timing + a separate muted line
   for the free-form latest-report message, so 'last run' / 'next run' stay
   compact and visible instead of being pushed off by prose.

   Specificity note: .agent-status-meta span (above) is 0-1-1 and applies a
   default chip look to every span. Under .agent-status-meta--resident we
   need to (a) reset the wrapper/container spans that are NOT chips, and
   (b) give .agent-status-timing-chip / .agent-status-resident-message
   selectors that BEAT 0-1-1 (two classes = 0-2-0) so their backgrounds/
   padding/colors aren't overridden by the generic span rule. */
.agent-status-meta--resident {
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
}

/* Reset the top-level spans inside resident meta so they don't render as
   chips themselves (.agent-status-timing is a flex wrapper, not a pill). */
.agent-status-meta--resident > span {
  background: transparent;
  padding: 0;
  border-radius: 0;
  max-width: 100%;
}

.agent-status-timing {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.agent-status-meta--resident .agent-status-timing-chip {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text-muted);
  font-size: 10px;
  padding: 2px 7px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-status-meta--resident .agent-status-timing-chip[data-run-state='live'] {
  background: color-mix(in srgb, var(--ch-color-accent) 22%, var(--ch-color-chip-bg));
  color: var(--ch-color-accent);
  font-weight: 700;
}

.agent-status-meta--resident .agent-status-timing-chip[data-run-state='muted'] {
  background: var(--ch-color-chip-bg-muted);
  color: var(--ch-color-text-muted);
}

.agent-status-meta--resident .agent-status-resident-message {
  display: block;
  width: 100%;
  font-size: 10px;
  line-height: 1.3;
  color: var(--ch-color-text-muted);
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
  display: flex;
  gap: 6px;
  justify-content: flex-end;
  padding-left: 22px;
  align-items: center;
}

.agent-status-actions-sep,
.detail-actions-sep {
  width: 1px;
  height: 16px;
  background: var(--ch-color-border);
  margin: 0 2px;
  flex-shrink: 0;
}

.agent-status-pause,
.agent-status-run-now,
.agent-status-switch-env,
.agent-status-delete {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.agent-status-pause {
  height: 26px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  font-size: 12px;
  padding: 0 9px;
  transition: border-color 0.12s, background 0.12s, transform 0.08s;
}

.agent-status-pause:hover {
  border-color: var(--ch-color-border-hover);
}

.agent-status-pause:active {
  transform: translateY(1px);
}

.agent-status-run-now {
  height: 26px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  font-size: 12px;
  padding: 0 9px;
  transition: border-color 0.12s, color 0.12s, background 0.12s, transform 0.08s;
}

.agent-status-run-now:hover:not(:disabled) {
  border-color: var(--ch-color-accent);
  color: var(--ch-color-accent);
}

.agent-status-run-now:active {
  transform: translateY(1px);
}

.agent-status-run-now:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.agent-status-paused-badge {
  flex: 0 0 auto;
  border-radius: 999px;
  padding: 1px 7px;
  background: var(--ch-color-surface-sunken);
  color: var(--ch-color-text-muted);
  font-weight: 700;
}

.agent-status-master-badge {
  flex: 0 0 auto;
  border-radius: 999px;
  padding: 1px 8px;
  border: 1px solid rgba(167, 139, 250, 0.45);
  background: rgba(139, 92, 246, 0.16);
  color: #c4b5fd;
  font-weight: 700;
  letter-spacing: 0.02em;
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
  transition: border-color 0.12s, background 0.12s, color 0.12s, transform 0.08s;
}

.agent-status-delete:hover:not(:disabled) {
  border-color: var(--ch-color-danger-hover);
  color: #fff;
}

.agent-status-delete:active {
  transform: translateY(1px);
}

.agent-status-switch-env {
  height: 26px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-raised);
  color: var(--ch-color-text-muted);
  cursor: pointer;
  font-size: 12px;
  padding: 0 9px;
  transition:
    color var(--ch-motion-fast),
    border-color var(--ch-motion-fast),
    background var(--ch-motion-fast),
    transform 0.08s;
}

.agent-status-switch-env:hover {
  color: var(--ch-color-accent);
  border-color: var(--ch-color-accent);
  background: var(--ch-color-surface);
}

.agent-status-switch-env:active {
  transform: translateY(1px);
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
.abort-button,
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
.abort-button,
.danger-button {
  height: 30px;
  padding: 0 10px;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), color var(--ch-motion-fast), transform var(--ch-motion-fast);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
}

.tool-button:hover,
.primary-button:hover,
.abort-button:hover,
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
.abort-button,
.danger-button,
.task-actions button,
.agent-row button {
  cursor: pointer;
}

.tool-button:disabled,
.primary-button:disabled,
.abort-button:disabled,
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

.abort-button {
  background: var(--ch-color-warning-bg);
  border-color: color-mix(in srgb, var(--ch-color-warning-strong) 65%, var(--ch-color-border-strong));
  color: var(--ch-color-warning);
}

.abort-button:hover {
  background: color-mix(in srgb, var(--ch-color-warning-bg) 72%, var(--ch-color-warning-strong));
  border-color: var(--ch-color-warning-strong);
  color: var(--ch-color-text-strong);
}

.workspace-error {
  padding: 8px 16px;
  background: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.workspace-error__close {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: inherit;
  font-size: 18px;
  line-height: 1;
  border-radius: var(--ch-radius-sm);
  cursor: pointer;
  opacity: 0.7;
  transition: opacity 0.15s, background 0.15s;
}

.workspace-error__close:hover {
  opacity: 1;
  background: color-mix(in srgb, currentColor 15%, transparent);
}

.workspace-layout {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  min-width: 0;
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

.agent-manager-view-switch {
  display: flex;
  gap: 6px;
}

.agent-manager-view-switch button {
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
  padding: 0 12px;
  text-align: left;
}

.agent-manager-view-switch button[data-active='true'] {
  border-color: var(--ch-color-border-strong);
  background: var(--ch-color-surface-control-hover);
  color: var(--ch-color-text);
}

.agent-manager-view-switch span {
  font-size: 12px;
  font-weight: 700;
}

.agent-manager-view-switch strong {
  min-width: 20px;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: currentColor;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
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

.runtime-pill--paused {
  background: var(--ch-color-surface-sunken);
  color: var(--ch-color-text-muted);
}

.empty-inline {
  color: var(--ch-color-text-subtle);
  font-size: 12px;
}

.board {
  position: relative;
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-columns: repeat(5, minmax(220px, 1fr));
  gap: 12px;
  overflow-x: auto;
  overflow-y: hidden;
  padding: 14px;
}

/* Graceful loading skeleton shown over the board during a workspace switch
   or first load. Mirrors the 5-column layout so the transition into real
   content does not shift the page. */
.board-skeleton {
  position: absolute;
  inset: 0;
  z-index: 5;
  display: grid;
  grid-template-columns: repeat(5, minmax(220px, 1fr));
  gap: 12px;
  padding: 14px;
  background: var(--ch-color-surface);
  overflow: hidden;
}

.board-skeleton-column {
  min-width: 220px;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-raised);
  overflow: hidden;
}

.board-skeleton-header {
  padding: 11px 12px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface);
}

.board-skeleton-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
}

.board-skeleton-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface);
}

.board-skeleton-line {
  height: 10px;
  border-radius: 999px;
  background: linear-gradient(
    90deg,
    var(--ch-color-border-muted) 25%,
    var(--ch-color-surface-raised) 37%,
    var(--ch-color-border-muted) 63%
  );
  background-size: 400% 100%;
  animation: board-skeleton-shimmer 1.4s ease-in-out infinite;
}

.board-skeleton-line--title {
  width: 45%;
  height: 12px;
}

.board-skeleton-line--lg {
  width: 85%;
}

.board-skeleton-line--md {
  width: 60%;
}

.board-skeleton-line--sm {
  width: 35%;
  height: 8px;
}

@keyframes board-skeleton-shimmer {
  0% {
    background-position: 100% 50%;
  }
  100% {
    background-position: 0 50%;
  }
}

.board-skeleton-fade-enter-active,
.board-skeleton-fade-leave-active {
  transition: opacity 240ms ease;
}

.board-skeleton-fade-enter-from,
.board-skeleton-fade-leave-to {
  opacity: 0;
}

@media (prefers-reduced-motion: reduce) {
  .board-skeleton-line {
    animation: none;
  }
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
  height: 100%;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface-raised);
  overflow: hidden;
}

.column-header {
  flex: 0 0 auto;
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

.column-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 22px;
  height: 20px;
  padding: 0 7px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 999px;
  background: var(--ch-color-chip-bg-muted);
  color: var(--ch-color-text);
  font-size: 11px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.column-count--empty {
  min-width: 0;
  padding: 0 2px;
  border: 0;
  background: transparent;
  color: var(--ch-color-text-muted);
  font-weight: 400;
  pointer-events: none;
}

/* Live Working / Review columns announce themselves with a top accent
   stripe and subtle header tint so active work is findable at a glance. */
.task-column--live {
  border-top: 2px solid transparent;
}

.task-column--live-working .column-header {
  background: color-mix(in srgb, var(--ch-color-warning-strong) 6%, var(--ch-color-surface));
}

.task-column--live-working {
  border-top-color: var(--ch-color-warning-strong);
}

.task-column--live-review .column-header {
  background: color-mix(in srgb, var(--ch-color-attention-strong) 6%, var(--ch-color-surface));
}

.task-column--live-review {
  border-top-color: var(--ch-color-attention-strong);
}

.task-column--empty .column-header h2 {
  color: var(--ch-color-text-muted);
}

.column-collapse-button {
  display: none;
}

.column-done-toggle {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  font-size: 11px;
  color: var(--ch-color-text-muted);
  background: var(--ch-color-surface-elevated);
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  white-space: nowrap;
}

.column-done-toggle:hover {
  color: var(--ch-color-text);
  border-color: var(--ch-color-border-strong);
}

.task-list {
  flex: 1 1 auto;
  min-height: 0;
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
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow-wrap: anywhere;
  word-break: break-all;
}

.agent-badge,
.session-meta span {
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
  color: var(--ch-color-text-muted);
  font-size: 10px;
  padding: 2px 6px;
  white-space: nowrap;
}

.session-meta .meta-agent {
  color: var(--ch-color-text);
  font-weight: 600;
}

.session-meta .meta-reviewer {
  background: color-mix(in srgb, var(--ch-color-review) 12%, var(--ch-color-chip-bg));
  color: var(--ch-color-review);
}

.session-meta .meta-review-state {
  background: color-mix(in srgb, var(--ch-color-accent) 12%, var(--ch-color-chip-bg));
  color: var(--ch-color-accent);
  font-weight: 600;
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

.task-card-age {
  flex: 0 0 auto;
  color: var(--ch-color-text-muted);
  font-size: 10px;
  line-height: 1.3;
  padding: 2px 0;
  white-space: nowrap;
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

.autonomy-badge {
  display: inline-flex;
  align-items: center;
  border: 1px solid rgba(20, 184, 166, 0.34);
  border-radius: var(--ch-radius-sm);
  background: rgba(20, 184, 166, 0.12);
  color: #5eead4;
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  padding: 5px 7px;
  white-space: nowrap;
}

.origin-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid rgba(167, 139, 250, 0.38);
  border-radius: var(--ch-radius-sm);
  background: rgba(139, 92, 246, 0.14);
  color: #c4b5fd;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.03em;
  line-height: 1;
  padding: 4px 7px;
  white-space: nowrap;
}

.origin-badge::before {
  content: '';
  width: 5px;
  height: 5px;
  border-radius: 999px;
  background: currentColor;
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
  font-weight: 700;
  text-transform: capitalize;
}

.latest-report span {
  margin-left: 4px;
}

/* Subtle tone on the report label + left stripe to surface status
   without competing with card-level left-bars. */
.latest-report[data-report-tone='active'] {
  border-left-color: var(--ch-color-info);
}
.latest-report[data-report-tone='active'] strong {
  color: var(--ch-color-info);
}

.latest-report[data-report-tone='success'] {
  border-left-color: var(--ch-color-success-strong);
}
.latest-report[data-report-tone='success'] strong {
  color: var(--ch-color-success-strong);
}

.latest-report[data-report-tone='attention'] {
  border-left-color: var(--ch-color-attention-strong);
}
.latest-report[data-report-tone='attention'] strong {
  color: var(--ch-color-attention-strong);
}

.latest-report[data-report-tone='muted'] strong {
  color: var(--ch-color-text-muted);
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
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(86px, 1fr));
  gap: 6px;
}

.task-actions button {
  width: 100%;
  min-width: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  overflow: hidden;
  text-align: center;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
}

.task-actions button,
.agent-row button {
  height: 26px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control-active);
  color: var(--ch-color-text);
  padding: 0 8px;
  transition: background 0.12s ease, border-color 0.12s ease, transform 0.08s ease;
  -webkit-tap-highlight-color: var(--ch-color-accent-ring);
}

.task-actions button {
  height: 30px;
}

/* Task-card "more" overflow menu (⋯): desktop-hidden by default; shown on
   narrow viewports to tuck Edit/Delete away so primary actions (Start, Abort,
   Done, Request review, Open tab) sit in a compact 2-column grid without
   spending vertical space on lower-frequency / destructive actions. The
   pattern mirrors .workspace-mobile-menu for visual consistency. */
.task-card-more-menu {
  display: none;
  position: relative;
}

.task-card-more-menu summary {
  list-style: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  cursor: pointer;
}

.task-card-more-menu summary::-webkit-details-marker {
  display: none;
}

.task-card-more-trigger {
  width: 100%;
  height: 100%;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control-active);
  color: var(--ch-color-text-subtle);
  font-size: 18px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 0.12s ease, border-color 0.12s ease, color 0.12s ease;
  -webkit-tap-highlight-color: var(--ch-color-accent-ring);
}

.task-card-more-trigger:hover,
.task-card-more-menu[open] .task-card-more-trigger {
  border-color: var(--ch-color-border-hover);
  color: var(--ch-color-text);
  background: var(--ch-color-surface-control-hover);
}

.task-card-more-panel {
  display: none;
  position: absolute;
  bottom: calc(100% + 6px);
  right: 0;
  z-index: 70;
  min-width: 140px;
  padding: 4px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-glass);
  box-shadow: var(--ch-shadow-soft);
  overscroll-behavior: contain;
  flex-direction: column;
  gap: 2px;
}

.task-card-more-menu[open] .task-card-more-panel {
  display: flex;
}

.task-card-more-item.task-card-more-item {
  /* Double-class to win specificity over `.task-actions button` which sets
     button-shaped defaults that conflict with the menu-item layout. */
  width: 100%;
  min-height: 32px;
  height: auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid transparent;
  border-radius: var(--ch-radius-sm);
  background: transparent;
  color: var(--ch-color-text);
  font-size: 12px;
  font-weight: 600;
  text-align: left;
  justify-content: flex-start;
  cursor: pointer;
  transition: background 0.1s ease, border-color 0.1s ease;
  -webkit-tap-highlight-color: var(--ch-color-accent-ring);
}

.task-card-more-item.task-card-more-item:hover {
  background: var(--ch-color-surface-control-hover);
  border-color: var(--ch-color-border);
}

.task-card-more-item.task-card-more-item:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.task-card-more-item.task-card-more-item--danger {
  background: var(--ch-color-danger-bg);
  border-color: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
}

.task-card-more-item.task-card-more-item--danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--ch-color-danger-bg) 70%, var(--ch-color-danger-border));
  border-color: var(--ch-color-danger-text);
}

/* Hide the mobile-overflow-only copies on desktop where the inline buttons
   are used; shown in mobile media queries below. */
.task-action--hide-mobile {
  display: inline-flex;
}

.task-action--mobile-wide {
  grid-column: auto;
}

.btn-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  line-height: 1;
  opacity: 0.8;
  flex-shrink: 0;
}

.task-actions button:active,
.agent-row button:active,
.tool-button:active,
.primary-button:active,
.abort-button:active,
.danger-button:active {
  transform: translateY(1px);
  background: var(--ch-color-surface-pressed);
}

.task-actions .abort-button {
  background: var(--ch-color-warning-bg);
  border-color: color-mix(in srgb, var(--ch-color-warning-strong) 65%, var(--ch-color-border-strong));
  color: var(--ch-color-warning);
}

.task-actions .abort-button:hover {
  background: color-mix(in srgb, var(--ch-color-warning-bg) 72%, var(--ch-color-warning-strong));
  border-color: var(--ch-color-warning-strong);
  color: var(--ch-color-text-strong);
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

.detail-section-title--with-controls,
.detail-section-title--with-count {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.detail-section-title--with-count > span:last-child {
  border-radius: 999px;
  background: var(--ch-color-surface-control-active);
  color: var(--ch-color-text);
  font-size: 10px;
  padding: 2px 7px;
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
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  line-height: 1;
  padding: 0;
  transition: background var(--ch-motion-fast), border-color var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.icon-button:hover {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
}

.icon-button:active {
  transform: translateY(1px);
}

.detail-body {
  flex: 1;
  min-height: 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 16px 18px;
}

/* Defensive wrap rules so long URLs, markdown-path links, inline code,
   hashes, and file paths inside the task-detail modal do not force the
   panel wider than the viewport on mobile (audit finding 2). pre blocks
   and tables keep horizontal scroll; everything else wraps. */
.detail-body :deep(*) {
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
}

.detail-body :deep(ol),
.detail-body :deep(ul) {
  min-width: 0;
  max-width: 100%;
}

.detail-body :deep(.progress-overview-timeline),
.detail-body :deep(.timeline) {
  /* Allow horizontal-scroll timelines to scroll inside the modal without
     widening the body; shrink vertically stacked timelines too. */
  min-width: 0;
  max-width: 100%;
}

.detail-body :deep(a),
.detail-body :deep(code),
.detail-body :deep(.markdown-path-link),
.detail-body :deep(span),
.detail-body :deep(strong),
.detail-body :deep(em),
.detail-body :deep(p),
.detail-body :deep(li) {
  overflow-wrap: anywhere;
  word-break: break-word;
  hyphens: auto;
  min-width: 0;
}

.detail-body :deep(pre),
.detail-body :deep(table) {
  overflow-x: auto;
  max-width: 100%;
}

.detail-body :deep(pre code) {
  overflow-wrap: normal;
  word-break: normal;
  white-space: pre;
}

.detail-body :deep(img) {
  max-width: 100%;
  height: auto;
}

/* Ensure the reports <details>/<summary> flex row respects the modal width.
   The timestamp + delta meta block (.report-summary-meta) is flex:0 0 auto
   with margin-left:auto; without an explicit width constraint on the
   flex container it can push the summary (and thus detail-body) wider
   than the viewport on mobile. Give details + summary a definite width
   and allow the summary to wrap so chips + message + meta each stay
   inside the panel on narrow screens (audit finding 2). */
.detail-body :deep(.report-card) {
  width: 100%;
  max-width: 100%;
  min-width: 0;
}

.detail-body :deep(.report-card summary) {
  min-width: 0;
  width: 100%;
  max-width: 100%;
  flex-wrap: wrap;
  row-gap: 4px;
  box-sizing: border-box;
}

.detail-body :deep(.report-summary-meta) {
  min-width: 0;
  flex-wrap: wrap;
}

.detail-section {
  min-width: 0;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-lg);
  background: var(--ch-color-surface);
  padding: 14px;
}

.detail-section--collapsible {
  transition: border-color var(--ch-motion-fast), box-shadow var(--ch-motion-fast), background var(--ch-motion-fast);
}

.detail-section--collapsible > summary {
  cursor: pointer;
}

.detail-section--collapsible[open] {
  border-color: var(--ch-color-accent);
  background: var(--ch-color-surface-raised);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--ch-color-accent) 35%, transparent);
}

.detail-section--collapsible[open] > summary {
  margin-bottom: 10px;
}

.detail-section-controls {
  margin-bottom: 10px;
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

.feedback-meta-chip {
  border-radius: 999px;
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 11px;
  font-weight: 700;
  padding: 4px 8px;
}

.detail-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.detail-actions button {
  min-width: 86px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  overflow: hidden;
  text-align: center;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
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
  font-size: 13px;
  line-height: 1.45;
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
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-sunken);
}

.attachment-thumb--clickable {
  /* Rendered as a <button> for keyboard/click affordance; strip the native
     button chrome so it matches the surrounding thumbnail styling. */
  padding: 0;
  flex: none;
  cursor: pointer;
  transition: border-color var(--ch-motion-fast), transform var(--ch-motion-fast);
}

.attachment-thumb--clickable:hover {
  border-color: var(--ch-color-accent);
  transform: scale(1.04);
}

.attachment-thumb--clickable:focus-visible {
  outline: 2px solid var(--ch-color-accent);
  outline-offset: 2px;
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

/* Allow attachment path code to wrap in the detail modal on narrow screens
   rather than forcing horizontal overflow; the white-space: nowrap above is
   preserved for list/row contexts but overridden inside `.detail-body`. */
.detail-body .attachment-meta code {
  white-space: normal;
  overflow-wrap: anywhere;
  word-break: break-all;
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

.autonomous-run-panel .fact-grid {
  margin-bottom: 10px;
}

.autonomous-next-action {
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
  padding: 10px 12px;
}

.autonomous-next-action span,
.autonomous-evaluations > strong {
  display: block;
  color: var(--ch-color-text-muted);
  font-size: 11px;
  font-weight: 700;
  margin-bottom: 5px;
  text-transform: uppercase;
}

.autonomous-next-action strong {
  color: var(--ch-color-text);
  font-size: 13px;
}

.autonomous-evaluations {
  margin-top: 10px;
}

.autonomous-evaluations ol {
  display: grid;
  gap: 6px;
  list-style: none;
  margin: 0;
  padding: 0;
}

.autonomous-evaluations li {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  padding: 8px 10px;
}

.autonomous-evaluations span {
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.autonomous-evaluations .profile-summary {
  flex-basis: 100%;
  color: var(--ch-color-text);
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

.timeline > li {
  position: relative;
  min-width: 0;
}

.timeline > li::before {
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

.progress-overview {
  margin-top: 10px;
  min-width: 0;
  max-width: 100%;
  overflow-x: auto;
  overflow-y: hidden;
  padding: 4px 0 6px;
  -webkit-overflow-scrolling: touch;
}

.progress-overview-timeline {
  list-style: none;
  margin: 0;
  min-width: max-content;
  padding: 0;
  display: flex;
  align-items: stretch;
}

.progress-overview-item {
  position: relative;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-areas:
    "dot dot"
    "main main"
    "time delta";
  column-gap: 8px;
  row-gap: 2px;
  min-width: 132px;
  max-width: 184px;
  padding: 0 18px 0 0;
  color: var(--ch-color-text-muted);
}

.progress-overview-item::after {
  content: '';
  position: absolute;
  top: 4px;
  right: 4px;
  left: 14px;
  height: 1px;
  background: var(--ch-color-border-muted);
}

.progress-overview-item:last-child::after {
  display: none;
}

.progress-overview-dot {
  grid-area: dot;
  position: relative;
  z-index: 1;
  width: 9px;
  height: 9px;
  border: 2px solid var(--ch-color-surface);
  border-radius: 999px;
  background: var(--ch-color-text-muted);
}

.progress-overview-main {
  grid-area: main;
  min-width: 0;
  overflow: hidden;
  color: var(--ch-color-text);
  font-size: 11px;
  font-weight: 700;
  line-height: 1.2;
  text-overflow: ellipsis;
  text-transform: capitalize;
  white-space: nowrap;
}

.progress-overview-time,
.progress-overview-delta {
  font-size: 10px;
  line-height: 1.2;
  white-space: nowrap;
}

.progress-overview-time {
  grid-area: time;
}

.progress-overview-delta {
  grid-area: delta;
  min-width: 0;
  color: var(--ch-color-text-subtle);
}

.progress-overview-item--task .progress-overview-dot {
  background: var(--ch-color-accent);
}

.progress-overview-item--report .progress-overview-dot {
  background: var(--ch-color-warning);
}

.progress-overview-item--terminal .progress-overview-dot {
  background: var(--ch-color-success);
}

.progress-overview-item--live .progress-overview-dot {
  background: var(--ch-color-danger);
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

.report-summary-meta {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  margin-left: auto;
}

.report-summary-label {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--ch-color-text-muted);
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--ch-color-chip-bg);
}

.report-awaiting-acceptance {
  color: var(--ch-color-warning);
  background: var(--ch-color-warning-bg);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.02em;
}

.report-summary-message {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 11px;
  color: var(--ch-color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.report-delta {
  border-radius: 999px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  font-size: 10px;
  line-height: 1;
  padding: 3px 6px;
  white-space: nowrap;
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

.report-file-chip {
  border: 0;
  border-radius: 999px;
  background: var(--ch-color-surface-control-active);
  color: var(--ch-color-text);
  font-size: 10px;
  padding: 3px 7px;
}

.report-file-chip--clickable {
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.report-file-chip:disabled:not(.report-file-chip--clickable) {
  opacity: 1;
}

.markdown-output-section {
  border-color: color-mix(in srgb, var(--ch-color-accent) 45%, var(--ch-color-border-muted));
}

.markdown-output-list {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}

.markdown-output-card {
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 9px;
}

.markdown-output-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.markdown-output-row > div:first-child {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.markdown-output-row strong {
  color: var(--ch-color-text);
  font-size: 13px;
  overflow-wrap: anywhere;
}

.markdown-output-row span {
  color: var(--ch-color-text-muted);
  font-size: 11px;
}

.markdown-output-row code {
  color: var(--ch-color-text-subtle);
  font-size: 11px;
  white-space: normal;
  overflow-wrap: anywhere;
}

.markdown-output-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.markdown-output-preview {
  margin-top: 10px;
}

.report-artifacts {
  display: grid;
  gap: 6px;
}

.report-artifact {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.report-artifact > span {
  overflow-wrap: anywhere;
  color: var(--ch-color-text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 11px;
}

.artifact-preview-button {
  border: 1px solid var(--ch-color-border);
  border-radius: 999px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  font-size: 10px;
  padding: 3px 8px;
}

.artifact-preview-button:disabled {
  cursor: progress;
  opacity: 0.7;
}

.artifact-preview {
  margin-top: 8px;
}

.artifact-preview-header {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  border: 1px solid var(--ch-color-border-muted);
  border-bottom: 0;
  border-radius: 8px 8px 0 0;
  background: var(--ch-color-surface);
  color: var(--ch-color-text-muted);
  font-size: 11px;
  padding: 7px 9px;
}

.artifact-preview-content {
  max-height: 420px;
  overflow: auto;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 0 0 8px 8px;
  background: var(--ch-color-surface);
  padding: 10px;
}

.artifact-preview-status {
  border: 1px solid var(--ch-color-border-muted);
  border-radius: 8px;
  background: var(--ch-color-surface);
  color: var(--ch-color-text-subtle);
  font-size: 12px;
  padding: 8px 10px;
}

.markdown-preview-modal-overlay {
  z-index: 1400;
}

.markdown-preview-modal {
  width: min(960px, 100%);
}

.markdown-preview-modal-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.markdown-preview-modal-header > div {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.markdown-preview-modal-header span {
  color: var(--ch-color-text-muted);
  font-size: 11px;
  text-transform: uppercase;
}

.markdown-preview-modal-header strong {
  color: var(--ch-color-text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
  overflow-wrap: anywhere;
}

.markdown-preview-modal-content {
  max-height: min(70dvh, 720px);
}

.image-lightbox-overlay {
  z-index: 1400;
}

.image-lightbox-img {
  /* Fit within the viewport (minus the overlay padding) while preserving the
     intrinsic aspect ratio; scale down large images, never up small ones. */
  max-width: min(1200px, calc(100vw - 32px));
  max-height: calc(100dvh - 32px);
  width: auto;
  height: auto;
  object-fit: contain;
  border-radius: var(--ch-radius-sm);
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.45);
}

.image-lightbox-close {
  position: fixed;
  top: 16px;
  right: 16px;
}

.artifact-preview-error {
  border-color: var(--ch-color-danger);
  color: var(--ch-color-danger);
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

.acceptance-check-list,
.profile-result-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.45;
}

.acceptance-check-list li,
.profile-result-list li {
  overflow-wrap: anywhere;
}

.acceptance-check-list span,
.profile-result-list span {
  margin-right: 5px;
  color: var(--ch-color-text);
  font-weight: 700;
}

.report-note--inline {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
}

.report-note--inline strong {
  display: inline-flex;
  margin: 0;
}

.report-note--inline > span {
  line-height: 1;
}

.report-subsection {
  padding-top: 2px;
  padding-bottom: 2px;
}

.report-subsection > summary {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 8px;
  cursor: pointer;
  list-style: none;
}

.report-subsection > summary::-webkit-details-marker {
  display: none;
}

.report-subsection > summary::before {
  content: '▸';
  color: var(--ch-color-text-muted);
  font-size: 10px;
  line-height: 1;
  flex: 0 0 auto;
  transition: transform var(--ch-motion-fast);
}

.report-subsection[open] > summary::before {
  transform: rotate(90deg);
}

.report-subsection[open] > summary {
  margin-bottom: 5px;
}

.report-subsection > summary strong {
  display: inline;
  margin: 0;
}

.report-subsection-preview {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--ch-color-text-muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-style: italic;
}

.report-subsection-count {
  flex: 0 0 auto;
  color: var(--ch-color-text-muted);
  font-size: 11px;
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
  display: flex;
  flex-direction: column;
  /* Keep the whole modal within a single viewport height. */
  max-height: calc(100dvh - 32px);
  overflow: hidden;
}

/* Title stays pinned; sections below manage their own scrolling. */
.agent-manager-modal > h3 {
  flex-shrink: 0;
  margin-bottom: 12px;
}

/* Workspace Agents list takes the larger share of the modal and scrolls
   internally instead of pushing the Add Agent form off-screen. */
.agent-manager-modal .modal-section--first {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.agent-manager-modal .modal-section--first .modal-section-header {
  flex-shrink: 0;
}

.agent-manager-modal .modal-section--first .agent-list {
  flex: 1;
  min-height: 80px;
  overflow-y: auto;
}

/* Add Agent form is the compact lower section: smaller share, its own
   scroll only when needed, so the action buttons stay reachable without the
   dialog growing past the screen. */
.agent-manager-modal .agent-create-form {
  flex: 0 1 auto;
  min-height: 0;
  overflow-y: auto;
  margin-top: 14px;
  padding-top: 12px;
}

/* Two controls per row to keep the form short (e.g. Role + Agent Type). */
.agent-manager-modal .modal-field-row {
  display: flex;
  gap: 12px;
}

.agent-manager-modal .modal-field-row .modal-field {
  flex: 1 1 0;
  min-width: 0;
}

/* Tighter vertical rhythm inside the Add Agent form so it fits without
   scrolling on a standard screen. */
.agent-manager-modal .agent-create-form .modal-field {
  margin-bottom: 10px;
}

.lessons-manager-modal {
  width: min(760px, 100%);
}

.modal-heading-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}

.modal-heading-row h3 {
  margin-bottom: 4px;
}

.modal-heading-row p {
  margin: 0;
  color: var(--ch-color-text-muted);
  font-size: 12px;
}

.lessons-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.summary-run-status {
  display: grid;
  gap: 5px;
  margin-bottom: 16px;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 10px 12px;
}

.summary-run-status--queued {
  border-color: var(--ch-color-accent);
}

.summary-run-status--skipped {
  border-color: var(--ch-color-warning);
}

.summary-run-status strong {
  color: var(--ch-color-text);
  font-size: 13px;
}

.summary-run-status p {
  margin: 0;
  color: var(--ch-color-text-muted);
  font-size: 12px;
  line-height: 1.4;
}

.summary-run-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.summary-run-meta span {
  border-radius: 999px;
  background: var(--ch-color-chip-bg-muted);
  color: var(--ch-color-text-muted);
  font-size: 10px;
  font-weight: 700;
  padding: 3px 7px;
}

.lessons-list {
  display: grid;
  gap: 8px;
  /* Bound the list to a viewport-relative height so it stays compact and
     scrolls internally instead of growing with every lesson. */
  max-height: 46dvh;
  overflow-y: auto;
  /* Avoid scrollbar overlapping the lesson row borders. */
  padding-right: 2px;
}

.lesson-row {
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  align-items: start;
  border: 1px solid var(--ch-color-border-muted);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 10px;
}

.lesson-row-main {
  min-width: 0;
}

.lesson-row-main strong {
  display: block;
  color: var(--ch-color-text);
  font-size: 13px;
  overflow-wrap: anywhere;
}

.lesson-row-main p {
  margin: 4px 0 0;
  color: var(--ch-color-text-muted);
  font-size: 12px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.lesson-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 8px;
}

.lesson-tags span,
.lesson-row-actions > span {
  border-radius: 999px;
  background: var(--ch-color-chip-bg-muted);
  color: var(--ch-color-text-muted);
  font-size: 10px;
  font-weight: 700;
  padding: 3px 7px;
}

.lesson-row-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.lesson-create-form textarea {
  min-height: 84px;
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

.modal-label-badge {
  margin-left: 8px;
  border-radius: 999px;
  padding: 1px 8px;
  background: var(--ch-color-surface-sunken);
  color: var(--ch-color-text-muted);
  font-size: 11px;
  font-weight: 700;
}

.periodic-task-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.periodic-task-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
}

.periodic-task-enable {
  flex: 0 0 auto;
  cursor: pointer;
  width: 16px;
  height: 16px;
  accent-color: var(--ch-color-accent);
  /* Reset default browser outline so the focus ring does not stretch to row
     height; we draw a tidy ring on :focus-visible instead. */
  outline: none;
}

.periodic-task-enable:focus-visible {
  outline: 2px solid var(--ch-color-accent-ring-strong);
  outline-offset: 2px;
  border-radius: 2px;
}

.periodic-task-text {
  width: 100%;
}

.periodic-task-text--disabled {
  opacity: 0.55;
  /* Only strike through real text; empty rows showing placeholder should not
     look "deleted" — the faded opacity is already enough to convey disabled.
     We apply line-through via a wrapper-less trick by using ::placeholder
     normalization: browsers do not let text-decoration reach the placeholder
     unless the input has value, so we key the strike-through off an
     :not(:placeholder-shown) selector. Since every row shares the same
     placeholder, this reliably means "the user typed something". */
}

.periodic-task-text--disabled:not(:placeholder-shown) {
  text-decoration: line-through;
}

.periodic-task-text--disabled::placeholder {
  text-decoration: none;
  opacity: 0.6;
}

.periodic-task-remove {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-sm);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text-muted);
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
}

.periodic-task-remove:hover {
  border-color: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
}

.periodic-task-empty {
  font-style: italic;
}

.periodic-task-add {
  margin-top: 8px;
}

.env-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.env-preset-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.env-manage-button {
  white-space: nowrap;
}

.env-template-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-muted);
  padding: 10px;
}

.env-preset-name {
  width: 100%;
}

.env-textarea {
  width: 100%;
  min-height: 92px;
  resize: vertical;
  font-family: monospace !important;
  line-height: 1.45;
}

.modal-field .env-textarea-preview {
  margin-top: 8px;
  width: 100%;
  resize: none;
  min-height: 60px;
  opacity: 0.75;
  cursor: default;
  white-space: pre-wrap;
  font-family: monospace !important;
}

.env-editor-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
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

.segmented-control--three {
  grid-template-columns: repeat(3, minmax(0, 1fr));
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

.autonomy-form {
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-sunken);
  padding: 12px;
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

/* Keep lifecycle action labels on a single line so they never wrap/overflow
   below the button (shared by the workspace and resident modals). */
.modal-actions button {
  white-space: nowrap;
}

.modal-actions .workspace-delete-button {
  margin-right: auto;
}

.resident-agent-section {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--ch-color-border);
}

/* Summary row on the workspace form that opens the resident-agent popup. */
.resident-summary-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.resident-summary-text {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.resident-summary-title {
  color: var(--ch-color-text);
  font-size: 14px;
  font-weight: 500;
}

.resident-summary-status {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--ch-color-border);
  white-space: nowrap;
}

.resident-summary-status.is-on {
  color: var(--ch-color-accent);
  border-color: var(--ch-color-accent-ring);
}

.resident-summary-status.is-off {
  color: var(--ch-color-text-soft);
}

/* Nested popup floats above the workspace modal (1000) but below the
   EnvPresetManager (1100) that AgentConfigFields opens from inside it. */
.resident-agent-modal-overlay {
  z-index: 1050;
}

/* Fixed-height flex column so the popup keeps a stable size regardless of
   how many fields the Enable toggle reveals, and never exceeds the viewport.
   Mirrors the .agent-manager-modal pattern (pinned header + scrolling body). */
.resident-agent-modal {
  display: flex;
  flex-direction: column;
  /* Fixed target height clamped to the viewport: the modal does not
     shrink-to-fit when Enable is unchecked vs checked. Width stays the
     inherited min(520px, 100%) from .workspace-modal. */
  height: min(720px, calc(100dvh - 32px));
  overflow: hidden;
}

/* Title pinned at the top. */
.resident-agent-modal > h3 {
  flex-shrink: 0;
}

/* Enable checkbox is the master toggle; keep it pinned above the scroll. */
.resident-agent-modal .resident-agent-section--first {
  flex-shrink: 0;
}

/* The config fieldset is the only scrolling region (min-height: 0 lets the
   flex child actually scroll instead of growing the modal). */
.resident-agent-modal .resident-config-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

/* Done button pinned at the bottom, separated from the scrolling body. */
.resident-agent-modal .modal-actions {
  flex-shrink: 0;
  margin-top: 16px;
}

/*
  The resident config keeps every field visible at all times; the native
  <fieldset disabled> attribute makes all descendant inputs non-interactive
  while "Enable" is unchecked (including controls inside the shared
  AgentConfigFields child, since disabled fieldsets propagate by DOM tree).
  Reset the browser's default fieldset chrome and dim it when disabled.
*/
.resident-config-body {
  min-width: 0;
  margin: 0;
  /* Small right padding so the internal scrollbar doesn't overlap inputs. */
  padding: 2px 4px 2px 0;
  border: 0;
}

.resident-config-body.is-disabled {
  opacity: 0.55;
}

.resident-config-body.is-disabled :deep(*) {
  cursor: not-allowed;
}

.resident-agent-section--first {
  margin-top: 0;
  padding-top: 0;
  border-top: 0;
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
  border-radius: var(--ch-radius-sm);
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
  border-radius: var(--ch-radius-sm);
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
  border-radius: var(--ch-radius-sm);
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

  .agent-manager-modal .modal-field-row {
    flex-direction: column;
    gap: 0;
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

  .modal-heading-row {
    align-items: center;
  }

  .lessons-toolbar {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .lesson-row {
    grid-template-columns: minmax(0, 1fr);
  }

  .lesson-row-actions {
    justify-content: space-between;
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
    /* Use 'auto' for the avatar column so the 28px md avatar (plus 7px gap)
       fits and does not overlap the name; previously this was a hard 9px
       which caused the avatar to cover the first character of the name
       on mobile (audit finding 1 — " 'ontend bo..."). */
    grid-template-columns: auto minmax(0, 1fr);
    gap: 7px;
    align-items: start;
  }

  .agent-status-line {
    gap: 4px;
  }

  /* Hide agent kind/detail/meta text on mobile to save vertical space.
     Agent-status-actions are NOT hidden here — they are compacted below
     into icon-only square buttons in a wrap row (reviewer AC). */
  .agent-status-kind,
  .agent-status-detail,
  .agent-status-meta {
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

  /* On mobile the board stacks vertically; keep the skeleton overlay in sync
     and only hint at the first couple of columns so it stays compact. */
  .board-skeleton {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 8px;
  }

  .board-skeleton-column:nth-child(n + 3) {
    display: none;
  }

  .task-column {
    min-width: 0;
    height: auto;
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
    border-radius: var(--ch-radius-sm);
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
    flex: 0 0 auto;
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
    -webkit-line-clamp: 3;
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
    height: 32px;
    min-height: 32px;
  }

  /* On mobile, hide inline Edit/Delete and surface the ⋯ overflow menu as a
     grid cell. Primary CTA (Start) spans both columns for emphasis. */
  .task-card-more-menu {
    display: block;
    position: relative;
    min-width: 0;
    height: 32px;
  }

  .task-card-more-trigger {
    height: 100%;
  }

  .task-action--hide-mobile {
    display: none !important;
  }

  .task-action--mobile-wide {
    grid-column: 1 / -1;
  }

  /* Compact agent-status-actions on mobile: icon-only square buttons in a
     wrapping row under the card, instead of hidden (reviewer AC). */
  .agent-status-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    justify-content: flex-end;
    padding-left: 0;
    margin-top: 2px;
    align-items: center;
  }

  .agent-status-actions-sep {
    display: none;
  }

  .agent-status-actions .agent-status-pause,
  .agent-status-actions .agent-status-run-now,
  .agent-status-actions .agent-status-switch-env,
  .agent-status-actions .agent-status-delete {
    width: 28px;
    height: 28px;
    min-width: 28px;
    min-height: 28px;
    padding: 0;
    justify-content: center;
    gap: 0;
    font-size: 0; /* hide text label */
    line-height: 1;
  }

  .agent-status-actions .agent-status-pause .btn-icon,
  .agent-status-actions .agent-status-run-now .btn-icon,
  .agent-status-actions .agent-status-switch-env .btn-icon,
  .agent-status-actions .agent-status-delete .btn-icon {
    font-size: 14px;
    line-height: 1;
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

  .detail-actions button {
    width: auto;
    min-width: 0;
    height: 34px;
  }

  .send-form button {
    width: auto;
    min-width: 0;
    height: 36px;
  }

  .send-form textarea {
    min-height: 44px;
    max-height: 92px;
    font-size: 13px;
    line-height: 1.45;
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
    /* Keep 2-col grid at narrowest mobile; ⋯ overflow handles Edit/Delete so
       we don't need a single-column stack. */
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px;
  }

  /* Narrow viewports: tighter tap targets but same 2-col grid + overflow.
     Height matches 760px breakpoint so buttons stay ≤32px on all mobile. */
  .task-actions button,
  .task-card-more-trigger {
    height: 32px;
    min-height: 32px;
    font-size: 12px;
  }

  .agent-status-card-main {
    /* Narrower viewports (<=480px) still need 'auto' for the avatar column
       so the 28px md avatar does not clip the first character of the name. */
    grid-template-columns: auto minmax(0, 1fr);
    gap: 7px;
  }

  .agent-status-pill {
    grid-column: 2;
    width: fit-content;
    margin-top: 2px;
  }
}

/* ----------------------------------------------------------------
 * Switch Env modal
 * ---------------------------------------------------------------- */
.switch-env-modal {
  width: min(480px, 100%);
}

.switch-env-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.switch-env-icon {
  flex: 0 0 auto;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 18px;
}

.switch-env-title-block {
  min-width: 0;
}

.switch-env-title-block h3 {
  margin: 0;
  font-size: 16px;
}

.switch-env-subtitle {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--ch-color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.switch-env-callout {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin: 0 0 18px;
  padding: 10px 12px;
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  border: 1px solid var(--ch-color-border-muted);
  border-left: 3px solid var(--ch-color-accent);
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--ch-color-text-muted);
}

.switch-env-callout-icon {
  flex: 0 0 auto;
  color: var(--ch-color-accent);
  font-weight: 600;
  line-height: 1.5;
}

.field-hint-inline {
  color: var(--ch-color-text-soft);
  font-weight: 400;
}

.switch-env-submit {
  min-width: 124px;
}

.switch-env-modal .modal-field label code,
.switch-env-modal .modal-hint code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
}

/* ----------------------------------------------------------------
 * Toast / notification stack for workspace mode.
 * Mirrors TabBar.vue so toasts render consistently across modes.
 * ---------------------------------------------------------------- */

.toast-stack {
  position: fixed;
  top: 72px;
  right: 16px;
  z-index: 1000;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: min(420px, calc(100vw - 32px));
  pointer-events: none;
}

.toast {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px 10px 14px;
  border-radius: var(--ch-radius-md);
  border: 1px solid var(--ch-color-border);
  background: var(--ch-color-surface-raised);
  color: var(--ch-color-text);
  box-shadow: var(--ch-shadow-popover);
  font-size: 13px;
  line-height: 1.45;
  overflow: hidden;
  pointer-events: auto;
  animation: ws-toast-in 180ms cubic-bezier(0.2, 0, 0, 1);
}

@keyframes ws-toast-in {
  from {
    opacity: 0;
    transform: translateY(-6px) scale(0.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.toast::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--ch-color-text-muted);
}

.toast__icon {
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  margin-top: 1px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 700;
  line-height: 1;
}

.toast__message {
  flex: 1 1 auto;
  min-width: 0;
  word-break: break-word;
  padding-top: 1px;
}

.toast__close {
  flex: 0 0 auto;
  background: transparent;
  border: none;
  color: var(--ch-color-text-subtle);
  font-size: 16px;
  line-height: 1;
  padding: 2px;
  margin-top: -1px;
  cursor: pointer;
  transition: color var(--ch-motion-fast);
  border-radius: 3px;
}

.toast__close:hover {
  color: var(--ch-color-text);
  background: var(--ch-color-row-hover);
}

.toast__timer {
  position: absolute;
  left: 0;
  bottom: 0;
  height: 2px;
  background: currentColor;
  opacity: 0.35;
  transform-origin: left center;
  animation-name: ws-toast-timer;
  animation-timing-function: linear;
  animation-fill-mode: forwards;
}

@keyframes ws-toast-timer {
  from { width: 100%; }
  to { width: 0%; }
}

/* Type color is carried by the left accent bar + icon; the toast body stays
   on the neutral surface color so it doesn't visually shout. Errors keep a
   tinted background because they genuinely need attention. */
.toast--error::before { background: var(--ch-color-danger); }
.toast--error .toast__icon { color: var(--ch-color-danger); }
.toast--error .toast__icon::after { content: '!'; }

.toast--warning::before { background: var(--ch-color-warning); }
.toast--warning .toast__icon { color: var(--ch-color-warning); }
.toast--warning .toast__icon::after { content: '△'; }

.toast--success::before { background: var(--ch-color-success); }
.toast--success .toast__icon { color: var(--ch-color-success); }
.toast--success .toast__icon::after { content: '✓'; }

.toast--info::before { background: var(--ch-color-info); }
.toast--info .toast__icon { color: var(--ch-color-info); }
.toast--info .toast__icon::after { content: 'i'; }

.toast--error {
  background: var(--ch-color-danger-bg);
  border-color: var(--ch-color-danger-border);
  color: var(--ch-color-danger-text);
}

@media (max-width: 768px) {
  .toast-stack {
    top: 12px;
    right: 8px;
    left: 8px;
    max-width: none;
  }
}
</style>
