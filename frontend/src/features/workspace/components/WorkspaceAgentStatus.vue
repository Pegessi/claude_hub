<template>
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
              class="agent-status-meta"
            >
              <span>last run {{ residentLastRunLabel }}</span>
              <span v-if="residentNextRunLabel">next run {{ residentNextRunLabel }}</span>
              <span v-if="latestResidentReport">{{ latestResidentReport.message }}</span>
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
            ⚙ Env
          </LoadingButton>
          <LoadingButton
            v-if="isResidentAgent(agent)"
            type="button"
            class="agent-status-pause"
            :loading="isPending('workspace:resident-pause')"
            :loading-label="isResidentPaused ? 'Resuming agent' : 'Pausing agent'"
            @click="toggleResidentPaused"
          >
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
            {{ residentRunPending ? 'Run queued' : 'Run now' }}
          </LoadingButton>
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
</template>

<script setup lang="ts">
import LoadingButton from '@/components/LoadingButton.vue'
import AgentAvatar from '@/components/AgentAvatar.vue'
import {
  useWorkspaceRuntimeContext,
  useWorkspaceEditorContext,
  useWorkspaceAgentManagerContext,
} from '../context'
const {
  activeWorkspaceId,
  workspaceSessionView,
  workspaceAgents,
  reviewerSessions,
  isPending,
  refreshAgentStatuses,
  visibleWorkspaceSessions,
  sessionActionKey,
  openSession,
  agentRuntimeStatus,
  agentRoleLabel,
  agentRuntimeDetail,
  agentTaskLabel,
  agentRuntimeText,
  canSwitchAgentEnv,
  canDeleteAgent,
  agentDeleteTitle,
  agentActionKey,
  deleteAgent,
  visibleWorkspaceSessionsEmptyText,
} = useWorkspaceRuntimeContext()
const {
  isResidentAgent,
  isResidentPaused,
  isResidentMaster,
  residentLastRunLabel,
  residentNextRunLabel,
  latestResidentReport,
  toggleResidentPaused,
  residentRunPending,
  handleRunResidentNow,
} = useWorkspaceEditorContext()
const {
  openSwitchEnvModal,
} = useWorkspaceAgentManagerContext()
</script>

<style scoped src="../styles/shared-workspace-layout.css"></style>

<style scoped>
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
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
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
  outline: 2px solid var(--ch-color-accent-ring);
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

.agent-status-cli[data-kind='traex'] {
  background: rgba(91, 108, 255, 0.18);
  color: #7b8aff;
}

.agent-status-cli[data-kind='cursor'] {
  background: rgba(120, 120, 120, 0.22);
  color: var(--ch-color-text);
}

.agent-status-cli[data-kind='antigravity'] {
  background: rgba(138, 180, 248, 0.18);
  color: #8ab4f8;
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
}

.agent-status-pause:hover {
  border-color: var(--ch-color-border-hover);
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
}

.agent-status-run-now:hover:not(:disabled) {
  border-color: var(--ch-color-accent);
  color: var(--ch-color-accent);
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
  transition: color var(--ch-motion-fast),
    border-color var(--ch-motion-fast),
    background var(--ch-motion-fast);
}

.agent-status-switch-env:hover {
  color: var(--ch-color-accent);
  border-color: var(--ch-color-accent);
  background: var(--ch-color-surface);
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
</style>

<style scoped>
@media (max-width: 760px) {
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
}

@media (max-width: 480px) {
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
