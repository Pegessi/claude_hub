import {
  ref,
  reactive,
} from 'vue'
import {
  type WorkspaceArtifactPreview,
  type WorkspaceMarkdownDocument,
  type AgentReport,
} from '@/types'
import {
  markdownDocumentPreviewKey,
  artifactPreviewKey,
} from '@/features/workspace/presentation/reports'
import type { useWorkspaceRuntime } from './useWorkspaceRuntime'

type WorkspaceArtifactsInputs = Pick<ReturnType<typeof useWorkspaceRuntime>,
  'selectedTask'
  | 'activeWorkspaceId'
  | 'workspaceStore'
>

export function useWorkspaceArtifacts(inputs: WorkspaceArtifactsInputs) {
  const {
    selectedTask,
    activeWorkspaceId,
    workspaceStore,
  } = inputs

  const expandedArtifactKey = ref<string | null>(null)

  const artifactPreviews = reactive<Record<string, WorkspaceArtifactPreview>>({})

  const artifactPreviewErrors = reactive<Record<string, string>>({})

  const artifactPreviewLoading = reactive<Record<string, boolean>>({})

  const markdownPreviewModalPath = ref<string | null>(null)

  const markdownPreviewModalReportId = ref<string | null>(null)

  const markdownPreviewModalContent = ref<WorkspaceArtifactPreview | null>(null)

  const markdownPreviewModalError = ref<string | null>(null)

  const markdownPreviewModalLoading = ref(false)

  const imageLightboxUrl = ref<string | null>(null)

  const imageLightboxAlt = ref('')

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
    }
  }

  return {
    openMarkdownPreviewModal,
    openImageLightbox,
    isArtifactPreviewLoading,
    toggleArtifactPreview,
    artifactPreviewButtonLabel,
    expandedArtifactKey,
    artifactPreviewErrors,
    artifactPreviews,
    isMarkdownDocumentPreviewLoading,
    toggleMarkdownDocumentPreview,
    markdownDocumentPreviewButtonLabel,
    markdownPreviewModalPath,
    closeMarkdownPreviewModal,
    markdownPreviewModalError,
    markdownPreviewModalLoading,
    markdownPreviewModalContent,
    imageLightboxUrl,
    closeImageLightbox,
    imageLightboxAlt,
    handleLightboxKeydown,
  }
}
