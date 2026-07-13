import type {
  ApiErrorBody,
  Asset,
  AuditEvent,
  CreateProjectResponse,
  Dashboard,
  FaultResponse,
  JobDetail,
  Paged,
  Project,
  ProjectDetail,
  Run,
  Segment,
} from './types'

export const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api/v1').replace(/\/$/, '')

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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  headers.set('Accept', 'application/json')
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers })
  if (!response.ok) {
    let body: Partial<ApiErrorBody> = {}
    try {
      body = await response.json() as Partial<ApiErrorBody>
    } catch {
      body = { message: response.statusText }
    }
    throw new ApiError(response.status, body)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const query = (params: Record<string, string | undefined>) => {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => value && search.set(key, value))
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

export const api = {
  dashboard: () => request<Dashboard>('/dashboard'),
  projects: () => request<Paged<Project> | Project[]>('/projects').then((data) =>
    Array.isArray(data) ? { items: data, total: data.length } : data,
  ),
  createTextProject: (title: string, text: string, idempotencyKey: string) =>
    request<CreateProjectResponse>('/projects/text', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ title, text }),
    }),
  createUploadProject: (title: string, file: File, idempotencyKey: string) => {
    const form = new FormData()
    form.append('title', title)
    form.append('file', file)
    return request<CreateProjectResponse>('/projects/upload', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: form,
    })
  },
  project: (id: string) => request<ProjectDetail>(`/projects/${encodeURIComponent(id)}`),
  deleteProject: (id: string) => request<void>(`/projects/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  job: (id: string) => request<JobDetail>(`/jobs/${encodeURIComponent(id)}`),
  retryJob: (id: string) => request<JobDetail>(`/jobs/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
  cancelJob: (id: string) => request<JobDetail | { job: JobDetail['job'] }>(`/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }),
  updateSegment: (id: string, changes: Partial<Pick<Segment, 'text' | 'topic' | 'keywords'>> & { version: number }) =>
    request<Segment>(`/segments/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(changes) }),
  reorderSegments: (projectId: string, segmentIds: string[]) =>
    request<{ segments?: Segment[] } | Segment[]>(`/projects/${encodeURIComponent(projectId)}/segments/order`, {
      method: 'PUT',
      body: JSON.stringify({ segment_ids: segmentIds }),
    }),
  rematchSegment: (id: string) => request<Segment | { segment: Segment }>(`/segments/${encodeURIComponent(id)}/rematch`, { method: 'POST' }),
  selectAsset: (segmentId: string, assetId: string) =>
    request<Segment | { selection: Segment['selection'] }>(`/segments/${encodeURIComponent(segmentId)}/selection`, {
      method: 'PUT',
      body: JSON.stringify({ asset_id: assetId }),
    }),
  assets: (params: { q?: string; kind?: string; tag?: string } = {}) =>
    request<Paged<Asset> | Asset[]>(`/assets${query(params)}`).then((data) =>
      Array.isArray(data) ? { items: data, total: data.length } : data,
    ),
  uploadAsset: (file: File, name: string, tags: string[], keywords: string[]) => {
    const form = new FormData()
    form.append('file', file)
    form.append('name', name)
    form.append('tags', tags.join(','))
    form.append('keywords', keywords.join(','))
    return request<Asset>('/assets', { method: 'POST', body: form })
  },
  updateAsset: (id: string, changes: Partial<Asset>) =>
    request<Asset>(`/assets/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(changes) }),
  runs: () => request<Paged<Run> | Run[]>('/runs').then((data) =>
    Array.isArray(data) ? { items: data, total: data.length } : data,
  ),
  audit: (projectId?: string) =>
    request<Paged<AuditEvent> | AuditEvent[]>(`/audit${query({ project_id: projectId })}`).then((data) =>
      Array.isArray(data) ? { items: data, total: data.length } : data,
    ),
  setNextFault: (mode: 'ai_degrade' | 'job_fail' | 'none') =>
    request<FaultResponse>('/demo/faults/next', { method: 'POST', body: JSON.stringify({ mode }) }),
}

export function assetUrl(asset?: Asset | null) {
  if (!asset) return ''
  const value = asset.thumbnail_url || asset.file_url || asset.url || ''
  if (!value) return ''
  if (/^(https?:|data:|blob:)/.test(value)) return value
  return value.startsWith('/') ? value : `/${value}`
}

export function errorMessage(error: unknown) {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '发生未知错误，请稍后重试'
}
