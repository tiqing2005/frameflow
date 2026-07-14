import type {
  ApiErrorBody,
  AuthSessionInfo,
  Asset,
  AuditEvent,
  CreatePreviewResponse,
  CreateProjectResponse,
  Dashboard,
  FaultResponse,
  JobDetail,
  Paged,
  Project,
  ProjectDetail,
  ProjectPreviewResponse,
  ProjectTimeline,
  Run,
  Segment,
} from './types'

export const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api/v1').replace(/\/$/, '')
const DEFAULT_REQUEST_TIMEOUT = 15_000
const UPLOAD_REQUEST_TIMEOUT = 120_000
let csrfToken: string | null = null

export function setCsrfToken(value: string | null) {
  csrfToken = value
}

export interface ApiCallOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

export class ApiError extends Error {
  status: number
  code: string
  retryable: boolean
  requestId?: string
  details?: unknown

  constructor(status: number, body: Partial<ApiErrorBody> = {}) {
    super(body.message || `请求失败（HTTP ${status}）`)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code || 'HTTP_ERROR'
    this.retryable = body.retryable ?? status >= 500
    this.requestId = body.request_id
    this.details = body.details
  }
}

async function request<T>(path: string, init: RequestInit = {}, options: ApiCallOptions = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  headers.set('Accept', 'application/json')
  const method = (init.method || 'GET').toUpperCase()
  if (csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    headers.set('X-CSRF-Token', csrfToken)
  }
  const controller = new AbortController()
  const timeoutMs = options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT
  const abortFromCaller = () => controller.abort(options.signal?.reason)
  if (options.signal?.aborted) abortFromCaller()
  else options.signal?.addEventListener('abort', abortFromCaller, { once: true })
  const timeout = window.setTimeout(() => controller.abort('timeout'), timeoutMs)

  try {
    const response = await fetch(`${API_BASE}${path}`, { ...init, headers, signal: controller.signal, credentials: 'include' })
    if (!response.ok) {
      let body: Partial<ApiErrorBody> = {}
      try {
        body = await response.json() as Partial<ApiErrorBody>
      } catch {
        body = { message: response.statusText }
      }
      const apiError = new ApiError(response.status, body)
      if (response.status === 401 && !path.startsWith('/auth/')) {
        setCsrfToken(null)
        window.dispatchEvent(new CustomEvent('frameflow:auth-required'))
      }
      throw apiError
    }
    if (response.status === 204) return undefined as T
    return response.json() as Promise<T>
  } catch (error) {
    if (controller.signal.aborted && !options.signal?.aborted && controller.signal.reason === 'timeout') {
      throw new ApiError(408, {
        code: 'REQUEST_TIMEOUT',
        message: `请求超过 ${Math.round(timeoutMs / 1000)} 秒未响应，请稍后重试`,
        retryable: true,
      })
    }
    throw error
  } finally {
    window.clearTimeout(timeout)
    options.signal?.removeEventListener('abort', abortFromCaller)
  }
}

const query = (params: Record<string, string | undefined>) => {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => value && search.set(key, value))
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

export const api = {
  authSession: (options?: ApiCallOptions) => request<AuthSessionInfo>('/auth/session', {}, options),
  login: (username: string, password: string, options?: ApiCallOptions) =>
    request<AuthSessionInfo>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }, options),
  logout: (options?: ApiCallOptions) => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }, options),
  dashboard: (options?: ApiCallOptions) => request<Dashboard>('/dashboard', {}, options),
  projects: (options?: ApiCallOptions) => request<Paged<Project> | Project[]>('/projects', {}, options).then((data) =>
    Array.isArray(data) ? { items: data, total: data.length } : data,
  ),
  createTextProject: (title: string, text: string, idempotencyKey: string, options?: ApiCallOptions) =>
    request<CreateProjectResponse>('/projects/text', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ title, text }),
    }, options),
  createUploadProject: (title: string, file: File, idempotencyKey: string, options?: ApiCallOptions) => {
    const form = new FormData()
    form.append('title', title)
    form.append('file', file)
    return request<CreateProjectResponse>('/projects/upload', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: form,
    }, { timeoutMs: UPLOAD_REQUEST_TIMEOUT, ...options })
  },
  project: (id: string, options?: ApiCallOptions) => request<ProjectDetail>(`/projects/${encodeURIComponent(id)}`, {}, options),
  projectTimeline: (id: string, options?: ApiCallOptions) => request<ProjectTimeline>(`/projects/${encodeURIComponent(id)}/timeline`, {}, options),
  projectPreview: (id: string, options?: ApiCallOptions) => request<ProjectPreviewResponse>(`/projects/${encodeURIComponent(id)}/preview`, {}, options),
  createProjectPreview: (id: string, force = false, options?: ApiCallOptions) => request<CreatePreviewResponse>(`/projects/${encodeURIComponent(id)}/preview`, {
    method: 'POST',
    body: JSON.stringify({ force }),
  }, options),
  deleteProject: (id: string, options?: ApiCallOptions) => request<void>(`/projects/${encodeURIComponent(id)}`, { method: 'DELETE' }, options),
  job: (id: string, options?: ApiCallOptions) => request<JobDetail>(`/jobs/${encodeURIComponent(id)}`, {}, options),
  retryJob: (id: string, options?: ApiCallOptions) => request<JobDetail>(`/jobs/${encodeURIComponent(id)}/retry`, { method: 'POST' }, options),
  cancelJob: (id: string, options?: ApiCallOptions) => request<JobDetail | { job: JobDetail['job'] }>(`/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }, options),
  updateSegment: (id: string, changes: Partial<Pick<Segment, 'text' | 'topic' | 'keywords'>> & { version: number }, options?: ApiCallOptions) =>
    request<Segment>(`/segments/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(changes) }, options),
  reorderSegments: (projectId: string, segmentIds: string[], options?: ApiCallOptions) =>
    request<{ segments?: Segment[] } | Segment[]>(`/projects/${encodeURIComponent(projectId)}/segments/order`, {
      method: 'PUT',
      body: JSON.stringify({ segment_ids: segmentIds }),
    }, options),
  rematchSegment: (id: string, options?: ApiCallOptions) => request<Segment | { segment: Segment }>(`/segments/${encodeURIComponent(id)}/rematch`, { method: 'POST' }, options),
  selectAsset: (segmentId: string, assetId: string, options?: ApiCallOptions) =>
    request<Segment | { selection: Segment['selection'] }>(`/segments/${encodeURIComponent(segmentId)}/selection`, {
      method: 'PUT',
      body: JSON.stringify({ asset_id: assetId }),
    }, options),
  assets: (params: { q?: string; kind?: string; tag?: string } = {}, options?: ApiCallOptions) =>
    request<Paged<Asset> | Asset[]>(`/assets${query(params)}`, {}, options).then((data) =>
      Array.isArray(data) ? { items: data, total: data.length } : data,
    ),
  uploadAsset: (file: File, name: string, tags: string[], keywords: string[], options?: ApiCallOptions) => {
    const form = new FormData()
    form.append('file', file)
    form.append('name', name)
    form.append('tags', tags.join(','))
    form.append('keywords', keywords.join(','))
    return request<Asset>('/assets', { method: 'POST', body: form }, { timeoutMs: UPLOAD_REQUEST_TIMEOUT, ...options })
  },
  updateAsset: (id: string, changes: Partial<Asset>, options?: ApiCallOptions) =>
    request<Asset>(`/assets/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(changes) }, options),
  deleteAsset: (id: string, options?: ApiCallOptions) =>
    request<void>(`/assets/${encodeURIComponent(id)}`, { method: 'DELETE' }, options),
  runs: (options?: ApiCallOptions) => request<Paged<Run> | Run[]>('/runs', {}, options).then((data) =>
    Array.isArray(data) ? { items: data, total: data.length } : data,
  ),
  audit: (projectId?: string, options?: ApiCallOptions) =>
    request<Paged<AuditEvent> | AuditEvent[]>(`/audit${query({ project_id: projectId })}`, {}, options).then((data) =>
      Array.isArray(data) ? { items: data, total: data.length } : data,
    ),
  setNextFault: (mode: 'ai_degrade' | 'job_fail' | 'none', options?: ApiCallOptions) =>
    request<FaultResponse>('/demo/faults/next', { method: 'POST', body: JSON.stringify({ mode }) }, options),
}

export function mediaUrl(value?: string | null) {
  if (!value) return ''
  if (/^(https?:|data:|blob:)/.test(value)) return value
  return value.startsWith('/') ? value : `/${value}`
}

export function assetFileUrl(asset?: Asset | null) {
  if (!asset) return ''
  return mediaUrl(asset.file_url || asset.url || asset.thumbnail_url)
}

export function assetPosterUrl(asset?: Asset | null) {
  if (!asset) return ''
  return mediaUrl(asset.thumbnail_url || (asset.kind === 'image' ? asset.file_url || asset.url : ''))
}

export function assetUrl(asset?: Asset | null) {
  return asset?.kind === 'video' ? assetFileUrl(asset) : assetPosterUrl(asset) || assetFileUrl(asset)
}

export function isAbortError(error: unknown) {
  return error instanceof Error && error.name === 'AbortError'
}

export function errorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '发生未知错误，请稍后重试'
}
