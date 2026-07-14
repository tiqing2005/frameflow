export type ProjectStatus = 'queued' | 'processing' | 'ready' | 'failed' | 'canceled'
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled'
export type AssetTaggingStatus = 'idle' | 'queued' | 'running' | 'succeeded' | 'degraded'
export type AssetTaggingSource = 'vision' | 'text_llm' | 'rules'
export type ImageGenerationStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled'
export type ImageAspectRatio = '16:9' | '1:1' | '9:16'
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

export interface AuthUser {
  username: string
  display_name: string
  role: 'admin' | string
}

export interface AuthSessionInfo {
  auth_enabled: boolean
  configured: boolean
  setup_required: boolean
  setup_available: boolean
  authenticated: boolean
  user: AuthUser | null
  csrf_token: string | null
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
  kind?: 'pipeline' | 'preview' | string
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
  is_seed?: boolean
  active?: boolean
  tagging_status?: AssetTaggingStatus
  tagging_source?: AssetTaggingSource | null
  tagging_requested_at?: string | null
  tagging_started_at?: string | null
  tagging_finished_at?: string | null
  source?: 'seed' | 'upload' | 'generated' | string
  generation_id?: string | null
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
  source: 'auto' | 'manual' | 'generated'
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
  render_duration_ms?: number | null
  version: number
  recommendations: Recommendation[]
  selection: Selection | null
}

export type SegmentSummary = Omit<Segment, 'recommendations' | 'selection'>

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

export interface TimelineItem {
  segment_id: string
  position: number
  text: string
  topic?: string | null
  start_ms?: number | null
  end_ms?: number | null
  duration_ms: number
  render_duration_ms?: number | null
  auto_duration_ms?: number
  effective_duration_ms?: number
  duration_source?: 'auto' | 'manual'
  asset?: Asset | null
}

export interface ProjectTimeline {
  project_id: string
  input_hash: string
  segment_count: number
  duration_ms: number
  limits?: {
    segment_min_duration_ms: number
    segment_max_duration_ms: number
    timeline_max_duration_ms: number
    frame_duration_ms?: number
  }
  items: TimelineItem[]
}

export interface SegmentTimingResponse {
  segment: Segment
  timeline: ProjectTimeline
}

export type TimelineTimingStrategy = 'text' | 'current' | 'equal'

export interface PreviewRender {
  id: string
  project_id?: string
  job_id?: string | null
  input_hash?: string
  status: JobStatus | string
  output_url?: string | null
  duration_ms?: number | null
  segment_count?: number
  error_message?: string | null
  job?: Job | null
  created_at?: string | null
  updated_at?: string | null
}

export interface ProjectPreviewResponse {
  preview: PreviewRender | null
  timeline: ProjectTimeline
}

export interface CreatePreviewResponse {
  preview: PreviewRender
  timeline: ProjectTimeline
  idempotent_replay?: boolean
}

export interface Dashboard {
  metrics: {
    projects: number
    ready_projects?: number
    total_assets: number
    queued_jobs: number
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
  image_count?: number | null
  error_message?: string | null
  created_at: string
}

export interface ImageGeneration {
  id: string
  project_id?: string | null
  segment_id?: string | null
  segment_version?: number | null
  source?: 'library' | 'segment' | string
  prompt: string
  name?: string | null
  aspect_ratio: ImageAspectRatio
  provider?: string | null
  model?: string | null
  status: ImageGenerationStatus
  progress?: number | null
  attempt?: number
  max_attempts?: number
  retryable?: boolean
  error_code?: string | null
  error_message?: string | null
  content_url?: string | null
  asset_id?: string | null
  auto_import?: boolean
  auto_select?: boolean
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  accepted_at?: string | null
  discarded_at?: string | null
  expires_at?: string | null
  updated_at?: string | null
}

export interface ImageGenerationCreateResponse {
  generation: ImageGeneration
  idempotent_replay?: boolean
}

export interface ImageGenerationDetailResponse {
  generation: ImageGeneration
  asset?: Asset | null
  selection?: Selection | null
  segment?: SegmentSummary | null
}

export interface ImageGenerationAcceptResponse extends ImageGenerationDetailResponse {
  asset: Asset
  idempotent_replay?: boolean
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
