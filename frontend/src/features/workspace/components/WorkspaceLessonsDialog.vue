<template>
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
          x
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
</template>

<script setup lang="ts">
import LoadingButton from '@/components/LoadingButton.vue'
import {
  feedbackSummaryTone,
  feedbackSummaryTitle,
  feedbackSummaryDescription,
  lessonTitle,
  lessonDescription,
  lessonTags,
} from '@/features/workspace/presentation/feedback'
import {
  useWorkspaceLessonsContext,
  useWorkspaceRuntimeContext,
} from '../context'
const {
  showLessonsModal,
  closeLessonsModal,
  handleSummarizeLessons,
  refreshFeedbackLessons,
  lastFeedbackSummaryRun,
  shortId,
  feedbackLessonMatchSummary,
  lessonActionKey,
  deleteLesson,
  handleCreateLesson,
  lessonForm,
  resetLessonForm,
} = useWorkspaceLessonsContext()
const {
  activeFeedbackLessons,
  activeWorkspaceId,
  isPending,
} = useWorkspaceRuntimeContext()
</script>

<style scoped src="../styles/shared-workspace-layout.css"></style>

<style scoped src="../styles/shared-controls.css"></style>

<style scoped src="../styles/shared-agent-manager.css"></style>

<style scoped src="../styles/shared-task-cards.css"></style>

<style scoped src="../styles/shared-task-detail.css"></style>

<style scoped>
.feedback-meta-chip {
  border-radius: 999px;
  background: var(--ch-color-accent-soft);
  color: var(--ch-color-accent);
  font-size: 11px;
  font-weight: 700;
  padding: 4px 8px;
}
</style>

<style scoped src="../styles/shared-dialogs.css"></style>

<style scoped>
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
</style>

<style scoped>
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
</style>

<style scoped src="../styles/shared-form-fields.css"></style>

<style scoped src="../styles/shared-modal-sections.css"></style>

<style scoped src="../styles/shared-compact-layout.css"></style>

<style scoped>
@media (max-width: 760px) {
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
}
</style>
