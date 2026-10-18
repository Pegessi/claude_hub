import {
  type ExecutionTarget,
} from '@/types'

export interface TaskStartOptions {
  target_session_id: string
  related_task_id: string
  clear_context: boolean
}

export interface FileInfo {
  name: string
  path: string
  is_dir: boolean
}

export interface DirectoryListing {
  current_path: string
  parent_path: string | null
  items: FileInfo[]
}

export type WorkspaceSessionView = 'agents' | 'reviewers'

export interface BrowserPlacement {
  target: ExecutionTarget
  remoteProfileId: string
  cwd: string
  setCwd: (value: string) => void
}
