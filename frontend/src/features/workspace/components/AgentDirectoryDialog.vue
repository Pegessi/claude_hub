<template>
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
</template>

<script setup lang="ts">
import LoadingButton from '@/components/LoadingButton.vue'
import {
  useWorkspaceAgentManagerContext,
  useWorkspaceDirectoryBrowserContext,
  useWorkspaceRuntimeContext,
} from '../context'
const {
  showAgentFileBrowser,
} = useWorkspaceAgentManagerContext()
const {
  browserPlacement,
  navigateAgentBrowserHome,
  agentBrowserParentPath,
  navigateAgentBrowserParent,
  agentBrowserPathInput,
  loadAgentDirectory,
  refreshAgentDirectory,
  agentBrowserItems,
  handleAgentFileItemClick,
  agentBrowserLoading,
  agentBrowserError,
  selectAgentCurrentDirectory,
} = useWorkspaceDirectoryBrowserContext()
const {
  isPending,
} = useWorkspaceRuntimeContext()
</script>

<style scoped src="../styles/shared-controls.css"></style>

<style scoped src="../styles/shared-task-cards.css"></style>

<style scoped src="../styles/shared-dialogs.css"></style>

<style scoped>
.file-browser-overlay {
  z-index: 1100;
}
</style>

<style scoped src="../styles/shared-form-fields.css"></style>

<style scoped src="../styles/shared-modal-sections.css"></style>

<style scoped>
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
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-soft);
  padding: 8px;
}

.file-browser-path input {
  min-width: 0;
  flex: 1;
  height: 32px;
  padding: 0 10px;
  font-family: var(--ch-font-mono);
  font-size: var(--ch-font-size-sm);
}

.path-nav-button {
  height: 32px;
  border: 1px solid var(--ch-color-border-strong);
  border-radius: var(--ch-radius-md);
  background: var(--ch-color-surface-control);
  color: var(--ch-color-text);
  cursor: pointer;
  padding: 0 12px;
  font-size: var(--ch-font-size-base);
  font-weight: 500;
  font-family: var(--ch-font-sans);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: all var(--ch-motion-fast);
}

.path-nav-button:hover {
  border-color: var(--ch-color-border-hover);
  background: var(--ch-color-surface-control-hover);
}

.file-browser-list {
  flex: 1;
  min-height: 160px;
  overflow-y: auto;
  border: 1px solid var(--ch-color-border);
  border-radius: var(--ch-radius-md);
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
</style>

<style scoped src="../styles/shared-compact-layout.css"></style>

<style scoped>
@media (max-width: 760px) {
  .file-browser-modal {
    height: calc(100dvh - 20px);
  }
}
</style>
