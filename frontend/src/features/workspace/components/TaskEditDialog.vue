<template>
  <div
    v-if="showEditTaskModal"
    class="workspace-modal-overlay"
    @click.self="closeEditTaskModal"
  >
    <div
      ref="modalRef"
      class="workspace-modal"
      tabindex="-1"
    >
      <h3>Edit Task</h3>
      <form @submit.prevent="handleUpdateTask">
        <div class="modal-field">
          <label>Title</label>
          <input
            ref="titleInputRef"
            v-model="editTaskForm.title"
          >
        </div>
        <div class="modal-field">
          <label>Task description</label>
          <textarea v-model="editTaskForm.prompt" />
        </div>
        <div class="modal-field">
          <label for="task-edit-goal">Goal (optional)</label>
          <input
            id="task-edit-goal"
            v-model="editTaskForm.goal"
            placeholder="The outcome this task should achieve"
          >
        </div>
        <div class="modal-field">
          <label for="task-edit-done_when">Done when (optional)</label>
          <textarea
            id="task-edit-done_when"
            v-model="editTaskForm.done_when"
            placeholder="One acceptance criterion per line"
            rows="3"
          />
        </div>
        <div class="modal-field">
          <label for="task-edit-out_of_scope">Out of scope (optional)</label>
          <textarea
            id="task-edit-out_of_scope"
            v-model="editTaskForm.out_of_scope"
            placeholder="One exclusion per line"
            rows="3"
          />
        </div>
        <div class="modal-field">
          <label for="task-edit-related">Related task</label>
          <p class="modal-hint">
            Select only when you need to continue another task’s context.
          </p>
          <select
            id="task-edit-related"
            v-model="editTaskForm.related_task_id"
          >
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
import { nextTick, ref, watch } from 'vue'
import LoadingButton from '@/components/LoadingButton.vue'
import { shouldAutoFocusTextInput } from '@/utils/mobileInputFocus'
import {
  useWorkspaceTaskActionsContext,
  useWorkspaceRuntimeContext,
} from '../context'
const modalRef = ref<HTMLDivElement | null>(null)
const titleInputRef = ref<HTMLInputElement | null>(null)
const {
  showEditTaskModal,
  closeEditTaskModal,
  handleUpdateTask,
  editTaskForm,
  editingTaskId,
} = useWorkspaceTaskActionsContext()
const {
  tasks,
  isLoading,
  isPending,
  taskActionKey,
} = useWorkspaceRuntimeContext()

watch(showEditTaskModal, open => {
  if (open) {
    void nextTick(() => {
      const target = shouldAutoFocusTextInput() ? titleInputRef.value : modalRef.value
      target?.focus()
    })
  }
})
</script>

<style scoped src="../styles/shared-controls.css"></style>

<style scoped src="../styles/shared-task-cards.css"></style>

<style scoped src="../styles/shared-dialogs.css"></style>

<style scoped src="../styles/shared-form-fields.css"></style>

<style scoped src="../styles/shared-modal-sections.css"></style>

<style scoped src="../styles/shared-compact-layout.css"></style>
