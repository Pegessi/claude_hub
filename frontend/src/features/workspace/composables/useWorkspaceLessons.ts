import {
  ref,
  reactive,
  computed,
} from 'vue'
import {
  type FeedbackSummaryRun,
  type FeedbackLesson,
} from '@/types'
import {
  feedbackLessonMatchSummary as projectFeedbackLessonMatchSummary,
  lessonTitle,
} from '@/features/workspace/presentation/feedback'
import type { useWorkspaceRuntime } from './useWorkspaceRuntime'

type WorkspaceLessonsInputs = Pick<ReturnType<typeof useWorkspaceRuntime>,
  'tasks'
  | 'activeFeedbackLessons'
  | 'workspaceStore'
  | 'runPending'
  | 'activeWorkspaceId'
>

export function useWorkspaceLessons(inputs: WorkspaceLessonsInputs) {
  const {
    tasks,
    activeFeedbackLessons,
    workspaceStore,
    runPending,
    activeWorkspaceId,
  } = inputs

  const showLessonsModal = ref(false)

  const lastFeedbackSummaryRun = ref<FeedbackSummaryRun | null>(null)

  const lessonForm = reactive({
    title: '',
    description: '',
    tags: '',
  })

  function shortId(value: string): string {
    return value.slice(0, 8)
  }

  const feedbackLessonMatchSummary = computed(() =>
    projectFeedbackLessonMatchSummary(tasks.value, activeFeedbackLessons.value)
  )

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

  async function refreshFeedbackLessons() {
    await runPending('feedback:refresh', async () => {
      await workspaceStore.fetchFeedbackLessons()
    })
  }

  return {
    openLessonsModal,
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
  }
}
