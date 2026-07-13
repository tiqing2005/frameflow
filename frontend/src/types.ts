export type ProjectStatus = 'queued' | 'processing' | 'ready' | 'failed' | 'canceled'
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled'
export type JobStage =
  | 'validating'
  | 'extracting'
  | 'transcribing'
  | 'segmenting'
  | 'keywording'
  | 'matching'
  | 'persisting'
  | 'completed'

export interface ApiErrorBody {
  code: string
  message: string
  retryable: boolean
  request_id?: string
  details?: unknown
}

export interface Project {
  id: string
  title: string
  status: ProjectStatus
  input_kind?: 'text' | 'audio' | 'video' | string
  input_type?: string
  duration_ms?: number | null
  segment_count?: number
  created_at: string
  updated_at: string
}

export interface Job {
  id: string
  project_id: string
  status: JobStatus
  stage: JobStage | string
  progress: number
  attempt?: number
  max_attempts?: number
  error_code?: string | null
  error_message?: string | null
  retryable?: boolean
  created_at: string
  started_at?: string | null
  finished_at?: string | null
}

export interface JobEvent {
  id: string
  stage: JobStage | string
  progress: number
  message: string
  level?: 'info' | 'warning' | 'error' | string
  created_at: string
}

export interface Asset {
  id: string
  name: string
  kind?: 'image' | 'video' | string
  url?: string
  file_url?: string
  thumbnail_url?: string
  mime_type?: string
  tags: string[]
  keywords: string[]
  width?: number | null
  height?: number | null
  created_at?: string
  updated_at?: string
}

export interface Recommendation {
  id: string
  segment_id: string
  asset: Asset
  asset_id?: string
  rank: number
  total_score: number
  tfidf_score: number
  keyword_score: number
  tag_score: number
  matched_terms: string[]
  explanation: string
  is_diversity_filler?: boolean
}

export interface Selection {
  id?: string
  segment_id: string
  asset_id: string
  source: 'auto' | 'manual'
  asset?: Asset
  updated_at?: string
}

export interface Segment {
  id: string
  project_id: string
  position: number
  text: string
  topic?: string
  keywords: string[]
  start_ms?: number | null
  end_ms?: number | null
  version: number
  recommendations: Recommendation[]
  selection: Selection | null
}

export interface ProjectDetail {
  project: Project
  current_job: Job | null
  source?: { text?: string; transcript?: string; filename?: string; [key: string]: unknown } | null
  segments: Segment[]
  trace_summary?: {
    degraded?: boolean
    ai_runs?: number
    audit_events?: number
    [key: string]: unknown
  }
}

export interface Dashboard {
  metrics: {
    projects: number
    total_assets: number
    running_jobs: number
    failed_jobs: number
  }
  recent_projects: Project[]
  recent_runs: Run[]
}

export interface Run {
  id: string
  project_id?: string
  project_title?: string
  provider?: string
  model?: string
  operation?: string
  status?: string
  degraded?: boolean
  latency_ms?: number | null
  input_tokens?: number | null
  output_tokens?: number | null
  total_tokens?: number | null
  error_message?: string | null
  created_at: string
}

export interface AuditEvent {
  id: string
  project_id?: string
  action?: string
  entity_type?: string
  entity_id?: string
  summary?: string
  details?: Record<string, unknown>
  created_at: string
}

export interface Paged<T> {
  items: T[]
  total: number
}

export interface CreateProjectResponse {
  project: Project
  job: Job
}

export interface JobDetail {
  job: Job
  events: JobEvent[]
}

export interface FaultResponse {
  mode?: string
  message?: string
}
