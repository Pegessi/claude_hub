import {
  ref,
} from 'vue'
import type { useWorkspaceRuntime } from './useWorkspaceRuntime'
import type { useWorkspaceEditor } from './useWorkspaceEditor'
import type { useWorkspaceLessons } from './useWorkspaceLessons'

type WorkspaceNavigationInputs = Pick<ReturnType<typeof useWorkspaceRuntime>,
  'appStore'
  | 'refreshAgentStatuses'
>
  & Pick<ReturnType<typeof useWorkspaceEditor>,
  'openWorkspaceModal'
  | 'openEditWorkspaceModal'
>
  & Pick<ReturnType<typeof useWorkspaceLessons>,
  'openLessonsModal'
>

export function useWorkspaceNavigation(inputs: WorkspaceNavigationInputs) {
  const {
    appStore,
    refreshAgentStatuses,
    openWorkspaceModal,
    openEditWorkspaceModal,
    openLessonsModal,
  } = inputs

  const workspaceMobileMenuRef = ref<HTMLDetailsElement | null>(null)

  function closeWorkspaceMobileMenu() {
    if (workspaceMobileMenuRef.value) {
      workspaceMobileMenuRef.value.open = false
    }
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

  function openLessonsModalFromMenu() {
    openLessonsModal()
    closeWorkspaceMobileMenu()
  }

  function openAppearanceFromMenu() {
    appStore.openAppearance()
    closeWorkspaceMobileMenu()
  }

  async function refreshAgentStatusesFromMenu() {
    await refreshAgentStatuses()
    closeWorkspaceMobileMenu()
  }

  return {
    goToTerminalMode,
    closeWorkspaceMobileMenu,
    openWorkspaceModalFromMenu,
    openEditWorkspaceModalFromMenu,
    openLessonsModalFromMenu,
    refreshAgentStatusesFromMenu,
    openAppearanceFromMenu,
    workspaceMobileMenuRef,
    handleWorkspaceDocumentPointerDown,
  }
}
