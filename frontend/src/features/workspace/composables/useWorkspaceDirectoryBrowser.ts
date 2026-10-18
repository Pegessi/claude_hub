import {
  ref,
  computed,
} from 'vue'
import type { FileInfo, BrowserPlacement, DirectoryListing } from '../types'
import type { useWorkspaceAgentManager } from './useWorkspaceAgentManager'
import type { useWorkspaceEditor } from './useWorkspaceEditor'
import type { useWorkspaceRuntime } from './useWorkspaceRuntime'

type WorkspaceDirectoryBrowserInputs = Pick<ReturnType<typeof useWorkspaceAgentManager>,
  'agentOptionsForm'
  | 'showAgentFileBrowser'
>
  & Pick<ReturnType<typeof useWorkspaceEditor>,
  'remoteProfiles'
>
  & Pick<ReturnType<typeof useWorkspaceRuntime>,
  'runPending'
>

export function useWorkspaceDirectoryBrowser(inputs: WorkspaceDirectoryBrowserInputs) {
  const {
    agentOptionsForm,
    showAgentFileBrowser,
    remoteProfiles,
    runPending,
  } = inputs

  const agentBrowserCurrentPath = ref('')

  const agentBrowserPathInput = ref('')

  const agentBrowserParentPath = ref<string | null>(null)

  const agentBrowserItems = ref<FileInfo[]>([])

  const agentBrowserLoading = ref(false)

  const agentBrowserError = ref<string | null>(null)

  const browserPlacement = computed<BrowserPlacement>(() => {
    return {
      target: agentOptionsForm.target,
      remoteProfileId: agentOptionsForm.remote_profile_id,
      cwd: agentOptionsForm.cwd,
      setCwd: (value: string) => {
        agentOptionsForm.cwd = value
      },
    }
  })

  const browserRemoteProfile = computed(() =>
    remoteProfiles.value.find(profile => profile.id === browserPlacement.value.remoteProfileId) || null
  )

  async function listAgentDirectory(path?: string): Promise<DirectoryListing> {
    const placement = browserPlacement.value
    const params = new URLSearchParams()
    if (path) {
      params.append('path', path)
    }
    if (placement.target === 'remote') {
      if (!placement.remoteProfileId) {
        throw new Error('Select a remote server first')
      }
      params.append('profile_id', placement.remoteProfileId)
    }
    const endpoint =
      placement.target === 'remote' ? '/api/remote/filesystem/list' : '/api/filesystem/list'
    const queryString = params.toString()
    const response = await fetch(`${endpoint}${queryString ? `?${queryString}` : ''}`)
    if (!response.ok) {
      const error = await response.text()
      throw new Error(error || 'Failed to list directory')
    }
    return await response.json()
  }

  async function loadAgentDirectory(path?: string, pendingKey = 'agent-browser:load') {
    await runPending(pendingKey, async () => {
      agentBrowserLoading.value = true
      agentBrowserError.value = null
      try {
        const listing = await listAgentDirectory(path)
        agentBrowserCurrentPath.value = listing.current_path
        agentBrowserPathInput.value = listing.current_path
        agentBrowserParentPath.value = listing.parent_path
        agentBrowserItems.value = listing.items
      } catch (e) {
        agentBrowserError.value = e instanceof Error ? e.message : 'Failed to load directory'
      } finally {
        agentBrowserLoading.value = false
      }
    })
  }

  async function openAgentDirectoryBrowser() {
    showAgentFileBrowser.value = true
    await openDirectoryBrowserForPlacement()
  }

  async function openDirectoryBrowserForPlacement() {
    const placement = browserPlacement.value
    if (placement.cwd) {
      await loadAgentDirectory(placement.cwd, 'agent-browser:open')
    } else if (placement.target === 'remote') {
      await loadAgentDirectory(browserRemoteProfile.value?.default_cwd || '~', 'agent-browser:open')
    } else {
      await loadAgentDirectory('~', 'agent-browser:open')
    }
  }

  function navigateAgentBrowserHome() {
    if (browserPlacement.value.target === 'remote') {
      loadAgentDirectory(browserRemoteProfile.value?.default_cwd || '~', 'agent-browser:home')
    } else {
      loadAgentDirectory('~', 'agent-browser:home')
    }
  }

  function navigateAgentBrowserParent() {
    if (!agentBrowserParentPath.value) return
    loadAgentDirectory(agentBrowserParentPath.value, 'agent-browser:up')
  }

  function refreshAgentDirectory() {
    loadAgentDirectory(
      agentBrowserCurrentPath.value || agentBrowserPathInput.value || '~',
      'agent-browser:refresh'
    )
  }

  function handleAgentFileItemClick(item: FileInfo) {
    if (item.is_dir) {
      loadAgentDirectory(item.path)
    }
  }

  function selectAgentCurrentDirectory() {
    browserPlacement.value.setCwd(agentBrowserCurrentPath.value)
    showAgentFileBrowser.value = false
  }

  return {
    openAgentDirectoryBrowser,
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
  }
}
