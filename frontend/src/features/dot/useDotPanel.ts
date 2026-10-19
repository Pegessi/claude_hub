import { computed, onUnmounted, ref, watch } from 'vue'
import { useDotIdentity } from '@/composables/useDotIdentity'
import { useTerminalStore } from '@/stores/terminalStore'
import { relativeTime } from '@/utils/time'
import {
  ackDotInbox,
  addDotFocus,
  addDotLesson,
  archiveDotCard,
  clearDotAvatar,
  dropDotLesson,
  formatFirstPass,
  formatReviewCoverage,
  loadDotCard,
  loadDotCards,
  loadDotFocus,
  loadDotHealth,
  loadDotInbox,
  loadDotLessons,
  loadDotStatus,
  loadDotWork,
  recallDot,
  unwatchDotTask,
  updateDotFocus,
  updateDotIdentity,
  updateDotLesson,
  uploadDotAvatar,
  watchDotTask,
  type DotCard,
  type DotCardKind,
  type DotFocusItem,
  type DotHealth,
  type DotInboxEvent,
  type DotLesson,
  type DotReviewStats,
  type DotSearchHit,
  type DotStatus,
  type DotWorkItem,
} from '@/utils/dotApi'

export interface DotPanelProps {
  open: boolean
}

export interface DotPanelEvents {
  close: () => void
}

export function useDotPanel(props: Readonly<DotPanelProps>, events: DotPanelEvents) {
  type Section = 'focus' | 'work' | 'lessons' | 'memory' | 'health'

  const SECTIONS: { key: Section, label: string }[] = [
    { key: 'focus', label: 'Focus' },
    { key: 'work', label: 'Work' },
    { key: 'lessons', label: 'Lessons' },
    { key: 'memory', label: 'Memory' },
    { key: 'health', label: 'Health' },
  ]
  const KINDS: DotCardKind[] = ['user', 'work', 'knowledge', 'concept']
  const DEFAULT_STALL_HOURS = 2
  const STALL_CHOICES = [1, 2, 4, 8, 24]
  const ALERT_EVENTS = new Set(['task_failed', 'task_needs_input', 'task_stalled', 'task_missing'])
  const AVATAR_MAX_BYTES = 2 * 1024 * 1024

  const section = ref<Section>('focus')
  const loading = ref(false)
  const errorText = ref('')
  const status = ref<DotStatus | null>(null)
  const focus = ref<DotFocusItem[]>([])
  const work = ref<DotWorkItem[]>([])
  const inbox = ref<DotInboxEvent[]>([])
  const cards = ref<(DotCard | DotSearchHit)[]>([])
  const health = ref<DotHealth | null>(null)
  const lessons = ref<DotLesson[]>([])
  const reviewStats = ref<DotReviewStats | null>(null)
  const newLesson = ref('')
  const editingLesson = ref<string | null>(null)
  const lessonDraft = ref('')
  const selectedCard = ref<DotCard | null>(null)
  const newFocus = ref('')
  const newPriority = ref(3)
  const searchText = ref('')
  const kindFilter = ref<DotCardKind | ''>('')
  const watchId = ref('')
  const watchStallHours = ref(DEFAULT_STALL_HOURS)
  const { identity, setIdentity } = useDotIdentity()
  const terminalStore = useTerminalStore()
  const editingIdentity = ref(false)
  const executionBusy = ref(false)
  const nameDraft = ref('')
  const avatarDraft = ref('')
  let searchTimer: ReturnType<typeof setTimeout> | null = null

  const causeText = computed(() =>
    Object.entries(reviewStats.value?.causes ?? {}).map(([cause, count]) => `${cause} ${count}`).join(', '),
  )

  const cardTotal = computed(() =>
    Object.values(status.value?.cards ?? {}).reduce((sum, count) => sum + (count ?? 0), 0),
  )

  // "just now", not "just now ago".
  function ago(iso: string | null | undefined): string {
    const age = relativeTime(iso)
    return age && age !== 'just now' ? `${age} ago` : age
  }

  // Actions can overlap (a search while a section loads): loading ends with the last one.
  let running = 0

  async function run(action: () => Promise<void>) {
    errorText.value = ''
    running += 1
    loading.value = true
    try {
      await action()
    } catch (error) {
      errorText.value = error instanceof Error ? error.message : String(error)
    } finally {
      running -= 1
      loading.value = running > 0
    }
  }

  async function loadSection() {
    await run(async () => {
      status.value = await loadDotStatus()
      if (status.value.identity) setIdentity(status.value.identity)
      if (section.value === 'focus') focus.value = await loadDotFocus()
      else if (section.value === 'work') [work.value, inbox.value] = await Promise.all([loadDotWork(), loadDotInbox()])
      else if (section.value === 'lessons') await loadLessons()
      else if (section.value === 'memory') await loadCards()
      else health.value = await loadDotHealth()
    })
  }

  // A slow answer for an older search must not replace the newer one.
  let cardsRequest = 0

  async function loadCards() {
    const request = ++cardsRequest
    const kind = kindFilter.value || undefined
    const text = searchText.value.trim()
    const found = text ? await recallDot(text, kind) : await loadDotCards(kind)
    if (request === cardsRequest) cards.value = found
  }

  function addFocus() {
    const title = newFocus.value.trim()
    if (!title) return
    // Cleared before the request, so a double Enter cannot add it twice.
    newFocus.value = ''
    void run(async () => {
      try {
        await addDotFocus({ title, priority: newPriority.value })
      } catch (error) {
        newFocus.value ||= title
        throw error
      }
      focus.value = await loadDotFocus()
    })
  }

  function closeFocus(focusId: string, state: 'done' | 'dropped') {
    void run(async () => {
      await updateDotFocus(focusId, { status: state })
      focus.value = await loadDotFocus()
    })
  }

  async function loadLessons() {
    const data = await loadDotLessons()
    lessons.value = data.lessons
    reviewStats.value = data.stats
  }

  function addLesson() {
    const rule = newLesson.value.trim()
    if (!rule) return
    newLesson.value = ''
    void run(async () => {
      try {
        await addDotLesson(rule)
      } catch (error) {
        newLesson.value ||= rule
        throw error
      }
      await loadLessons()
    })
  }

  function editLesson(lesson: DotLesson) {
    editingLesson.value = lesson.id
    lessonDraft.value = lesson.rule
  }

  function saveLesson(lessonId: string) {
    const rule = lessonDraft.value.trim()
    if (!rule) return
    void run(async () => {
      await updateDotLesson(lessonId, rule)
      editingLesson.value = null
      await loadLessons()
    })
  }

  function dropLesson(lessonId: string) {
    void run(async () => {
      await dropDotLesson(lessonId)
      await loadLessons()
    })
  }

  function toggleIdentityEditor() {
    if (executionBusy.value) return
    editingIdentity.value = !editingIdentity.value
    nameDraft.value = identity.value.name
    avatarDraft.value = identity.value.avatar
  }

  function setExecutionBusy(busy: boolean) {
    executionBusy.value = busy
  }

  function closePanel() {
    if (!executionBusy.value) events.close()
  }

  function saveIdentity() {
    if (executionBusy.value) return
    void run(async () => {
      const renamed = nameDraft.value.trim() !== identity.value.name
      setIdentity(await updateDotIdentity({ name: nameDraft.value.trim(), avatar: avatarDraft.value.trim() }))
      editingIdentity.value = false
      // The backend renamed the Dot tab too; refresh so the tab bar shows it now.
      if (renamed) await terminalStore.fetchTabs({ force: true })
    })
  }

  function uploadAvatar(event: Event) {
    const input = event.target as HTMLInputElement
    const file = input.files?.[0]
    input.value = ''
    if (!file || executionBusy.value) return
    if (file.size > AVATAR_MAX_BYTES) {
      errorText.value = 'The picture must be at most 2 MB.'
      return
    }
    void run(async () => {
      setIdentity(await uploadDotAvatar(file))
    })
  }

  function removeAvatar() {
    if (executionBusy.value) return
    void run(async () => {
      setIdentity(await clearDotAvatar())
    })
  }

  function watchTask() {
    const taskId = watchId.value.trim()
    if (!taskId) return
    void run(async () => {
      await watchDotTask(taskId, watchStallHours.value)
      watchId.value = ''
      work.value = await loadDotWork()
    })
  }

  function unwatchTask(item: DotWorkItem) {
    if (item.review_due && !window.confirm(`Stop tracking "${item.title || item.task_id}"? Its review is dropped.`)) return
    void run(async () => {
      await unwatchDotTask(item.task_id)
      work.value = await loadDotWork()
    })
  }

  function ackInbox() {
    void run(async () => {
      // Only what is on screen: an event that arrived since must stay unread.
      await ackDotInbox(inbox.value.map(event => event.id))
      inbox.value = await loadDotInbox()
    })
  }

  // Only the latest open counts: a slower earlier load (or one left with Back) must
  // not show its card.
  let cardRequest = 0

  function openCard(cardId: string) {
    section.value = 'memory'
    const request = ++cardRequest
    void run(async () => {
      const card = await loadDotCard(cardId)
      if (request === cardRequest) selectedCard.value = card
    })
  }

  function closeCard() {
    cardRequest++
    selectedCard.value = null
  }

  function archiveCard(cardId: string) {
    void run(async () => {
      await archiveDotCard(cardId)
      selectedCard.value = null
      await loadCards()
      status.value = await loadDotStatus()
    })
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') closePanel()
  }

  watch(
    () => props.open,
    isOpen => {
      if (isOpen) {
        closeCard()
        editingIdentity.value = false
        editingLesson.value = null
        void loadSection()
        window.addEventListener('keydown', handleKeydown)
      } else {
        window.removeEventListener('keydown', handleKeydown)
      }
    },
  )
  watch(section, () => {
    if (props.open) void loadSection()
  })
  watch([searchText, kindFilter], () => {
    if (searchTimer) clearTimeout(searchTimer)
    searchTimer = setTimeout(() => { void run(loadCards) }, 250)
  })

  onUnmounted(() => {
    if (searchTimer) clearTimeout(searchTimer)
    window.removeEventListener('keydown', handleKeydown)
  })
    return {
      SECTIONS,
      KINDS,
      DEFAULT_STALL_HOURS,
      STALL_CHOICES,
      ALERT_EVENTS,
      section,
      loading,
      errorText,
      status,
      focus,
      work,
      inbox,
      cards,
      health,
      lessons,
      reviewStats,
      newLesson,
      editingLesson,
      lessonDraft,
      selectedCard,
      newFocus,
      newPriority,
      searchText,
      kindFilter,
      watchId,
      watchStallHours,
      identity,
      editingIdentity,
      executionBusy,
      nameDraft,
      avatarDraft,
      causeText,
      cardTotal,
      ago,
      addFocus,
      closeFocus,
      addLesson,
      editLesson,
      saveLesson,
      dropLesson,
      toggleIdentityEditor,
      setExecutionBusy,
      closePanel,
      loadSection,
      saveIdentity,
      uploadAvatar,
      removeAvatar,
      watchTask,
      unwatchTask,
      ackInbox,
      openCard,
      closeCard,
      archiveCard,
      formatFirstPass,
      formatReviewCoverage
    }
}
