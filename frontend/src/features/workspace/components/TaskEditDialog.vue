<template>
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
          <p class="modal-hint">
            Session/context reuse hint — not a Task parent (<code>parent_task_id</code>).
            Task Graph tree UI is follow-up <code>487c630c</code>; not implemented here.
          </p>
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
</template>

<script setup lang="ts">
import LoadingButton from '@/components/LoadingButton.vue'
import {
  useWorkspaceTaskActionsContext,
  useWorkspaceRuntimeContext,
} from '../context'
const {
  showEditTaskModal,
  closeEditTaskModal,
  handleUpdateTask,
  editTaskForm,
  editingTaskId,
} = useWorkspaceTaskActionsContext()
const {
  workspaceAgents,
  tasks,
  isLoading,
  isPending,
  taskActionKey,
} = useWorkspaceRuntimeContext()
</script>

<style scoped src="../styles/shared-controls.css"></style>

<style scoped src="../styles/shared-task-cards.css"></style>

<style scoped src="../styles/shared-dialogs.css"></style>

<style scoped src="../styles/shared-form-fields.css"></style>

<style scoped src="../styles/shared-modal-sections.css"></style>

<style scoped src="../styles/shared-compact-layout.css"></style>
