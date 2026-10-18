import type { AgentReport, ManagedSession, WorkspaceMarkdownDocument } from './agentSessions'
import type { Workspace, WorkspaceTask } from './workspace'

export interface BoardTasksPagination {
  total_count: number
  has_more: boolean
  next_cursor?: string | null
  limit?: number | null
  status_counts?: Record<string, number>
}

export interface WorkspaceBoard {
  workspace: Workspace
  tasks: WorkspaceTask[]
  sessions: ManagedSession[]
  reports: AgentReport[]
  markdown_documents?: WorkspaceMarkdownDocument[]
  snapshot_path?: string | null
  tasks_pagination?: BoardTasksPagination | null
}

export type FeedbackLessonScope = 'workspace' | 'family' | 'global'
export type FeedbackLessonStatus = 'draft' | 'active' | 'archived' | 'rejected'

export interface FeedbackLesson {
  id: string
  workspace_id: string
  title?: string
  fingerprint?: string
  scope: FeedbackLessonScope
  status: FeedbackLessonStatus
  summary: string
  applies_when?: string[]
  do?: string
  avoid?: string
  tags?: string[]
  evidence_task_ids?: string[]
  source_draft_ids?: string[]
  source_record_ids?: string[]
  merged_from_ids?: string[]
  superseded_by_id?: string | null
  hit_count?: number
  success_count?: number
  confidence?: number | null
  last_seen_at?: string | null
  last_used_at?: string | null
  last_validated_at?: string | null
  created_at: string
  updated_at: string
}

export interface FeedbackLessonCreate {
  id?: string | null
  title?: string | null
  fingerprint?: string | null
  summary: string
  applies_when?: string[]
  do?: string
  avoid?: string
  tags?: string[]
  scope?: FeedbackLessonScope
  confidence?: number | null
}

export type FeedbackSummaryMode = 'incremental' | 'full'

export interface FeedbackSummaryRequest {
  mode?: FeedbackSummaryMode
  limit?: number
  force?: boolean
  clear_context?: boolean
}

export interface FeedbackSummaryRun {
  id: string
  workspace_id: string
  task_id?: string | null
  mode: FeedbackSummaryMode
  input_record_ids: string[]
  cache_hit: boolean
  prompt_version: number
  created_lesson_ids: string[]
  merged_lesson_ids: string[]
  skipped_reason?: string | null
  created_at: string
  completed_at?: string | null
}
