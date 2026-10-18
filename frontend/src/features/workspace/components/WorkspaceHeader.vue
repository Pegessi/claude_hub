<template>
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
        @click="openLessonsModal"
      >
        Lessons
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
          <AppExtensionActions @select="closeWorkspaceMobileMenu" />
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
</template>

<script setup lang="ts">
import LoadingButton from '@/components/LoadingButton.vue'
import AppExtensionActions from '@/components/AppExtensionActions.vue'
import {
  useWorkspaceRuntimeContext,
  useWorkspaceEditorContext,
  useWorkspaceLessonsContext,
  useWorkspaceAgentManagerContext,
  useWorkspaceNavigationContext,
} from '../context'
const {
  activeWorkspace,
  mobileWorkspaceSummary,
  isPending,
  selectedWorkspaceId,
  handleWorkspaceChange,
  workspaces,
  activeWorkspaceId,
  openTaskModal,
  colorScheme,
} = useWorkspaceRuntimeContext()
const {
  openWorkspaceModal,
  openEditWorkspaceModal,
} = useWorkspaceEditorContext()
const {
  openLessonsModal,
} = useWorkspaceLessonsContext()
const {
  openAgentOptionsModal,
} = useWorkspaceAgentManagerContext()
const {
  goToTerminalMode,
  closeWorkspaceMobileMenu,
  openWorkspaceModalFromMenu,
  openEditWorkspaceModalFromMenu,
  openLessonsModalFromMenu,
  openAgentOptionsModalFromMenu,
  refreshAgentStatusesFromMenu,
  toggleThemeFromMenu,
  workspaceMobileMenuRef,
} = useWorkspaceNavigationContext()
</script>

<style scoped src="../styles/shared-workspace-layout.css"></style>

<style scoped>
.workspace-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--ch-color-border-muted);
  background: var(--ch-color-surface-raised);
}

.workspace-header h1 {
  font-size: 16px;
  line-height: 1.2;
  margin: 0 0 2px;
  color: var(--ch-color-text-strong);
  font-weight: 600;
}

.workspace-header p {
  margin: 0;
  color: var(--ch-color-text-muted);
  font-size: 11px;
}

.workspace-title-block {
  min-width: 0;
}

.workspace-mobile-identity,
.workspace-mobile-menu {
  display: none;
}

.workspace-actions {
  justify-content: flex-end;
  flex-wrap: wrap;
}

.workspace-desktop-action {
  border-color: transparent;
  background: transparent;
  color: var(--ch-color-text-muted);
  height: 30px;
  padding: 0 10px;
  font-size: 12px;
}

.workspace-desktop-action:hover {
  border-color: var(--ch-color-border-muted);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
}

.workspace-select-shell {
  position: relative;
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
</style>

<style scoped src="../styles/shared-controls.css"></style>

<style scoped>
.workspace-select-shell {
  position: relative;
  min-width: 220px;
}

.workspace-select {
  width: 100%;
  min-width: 160px;
  padding-right: 32px;
  height: 30px;
  font-size: 12px;
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
</style>

<style scoped src="../styles/shared-task-cards.css"></style>

<style scoped>
@media (max-width: 760px) {
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
}

@media (max-width: 480px) {
  .workspace-actions {
    grid-template-columns: minmax(0, 1fr) auto auto;
  }

  .workspace-select-shell {
    grid-column: auto;
  }
}
</style>
