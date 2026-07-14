import { expect, test, type Page, type Route } from '@playwright/test'

const PROJECT_ID = 'project-preview'
const JOB_ID = 'job-preview'
const OUTPUT_URL = '/media/previews/project-preview/final.mp4'

const asset = (id: string, name: string, kind: 'image' | 'video' = 'image') => ({
  id,
  name,
  kind,
  file_url: `/media/seed/${id}.${kind === 'video' ? 'mp4' : 'jpg'}`,
  thumbnail_url: `/media/seed/${id}-poster.jpg`,
  mime_type: kind === 'video' ? 'video/mp4' : 'image/jpeg',
  tags: ['演示'],
  keywords: [name],
})

const timeline = {
  project_id: PROJECT_ID,
  input_hash: 'timeline-hash-v1',
  segment_count: 3,
  duration_ms: 12_000,
  items: [
    { segment_id: 'segment-1', position: 0, text: '第一段字幕', topic: '开场', start_ms: 0, end_ms: 3000, duration_ms: 3000, asset: asset('city', '城市开场') },
    { segment_id: 'segment-2', position: 1, text: '第二段字幕', topic: '协作', start_ms: 3000, end_ms: 8000, duration_ms: 5000, asset: asset('team', '团队协作', 'video') },
    { segment_id: 'segment-3', position: 2, text: '第三段字幕', topic: '收束', start_ms: 8000, end_ms: 12_000, duration_ms: 4000, asset: asset('result', '成果收束') },
  ],
}

const segments = timeline.items.map((item) => ({
  id: item.segment_id,
  project_id: PROJECT_ID,
  position: item.position,
  text: item.text,
  topic: item.topic,
  keywords: [item.topic],
  start_ms: item.start_ms,
  end_ms: item.end_ms,
  version: 1,
  recommendations: [],
  selection: { segment_id: item.segment_id, asset_id: item.asset.id, source: 'manual', asset: item.asset },
}))

const projectDetail = {
  project: {
    id: PROJECT_ID,
    title: '组合预览测试项目',
    status: 'ready',
    input_kind: 'text',
    created_at: '2026-07-14T00:00:00Z',
    updated_at: '2026-07-14T00:00:00Z',
  },
  current_job: null,
  source: { text: segments.map((segment) => segment.text).join('\n') },
  segments,
}

function previewJob(status: 'queued' | 'running' | 'succeeded', progress: number) {
  return {
    id: JOB_ID,
    project_id: PROJECT_ID,
    kind: 'preview',
    status,
    stage: status === 'succeeded' ? 'completed' : progress > 80 ? 'preview_finalizing' : 'preview_rendering',
    progress,
    attempt: 1,
    max_attempts: 3,
    created_at: '2026-07-14T00:00:00Z',
  }
}

function preview(status: 'queued' | 'running' | 'succeeded', progress: number) {
  return {
    id: 'preview-1',
    project_id: PROJECT_ID,
    job_id: JOB_ID,
    input_hash: timeline.input_hash,
    status,
    output_url: status === 'succeeded' ? OUTPUT_URL : null,
    duration_ms: status === 'succeeded' ? timeline.duration_ms : null,
    segment_count: timeline.segment_count,
    error_message: null,
    job: previewJob(status, progress),
  }
}

async function mockApi(page: Page, state: { finished: boolean; jobPolls: number; postBodies: unknown[]; stale?: boolean; rematched?: boolean; previewReads?: number }) {
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET' && path === `/api/v1/projects/${PROJECT_ID}`) {
      await route.fulfill({ json: projectDetail })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/projects/${PROJECT_ID}/preview`) {
      state.previewReads = (state.previewReads || 0) + 1
      const currentTimeline = state.rematched ? { ...timeline, input_hash: 'timeline-hash-v2' } : timeline
      const currentPreview = state.finished ? { ...preview('succeeded', 100), input_hash: state.stale ? 'old-timeline-hash' : timeline.input_hash } : null
      await route.fulfill({ json: { preview: currentPreview, timeline: currentTimeline } })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/projects/${PROJECT_ID}/timeline`) {
      await route.fulfill({ json: timeline })
      return
    }
    if (request.method() === 'POST' && path === `/api/v1/projects/${PROJECT_ID}/preview`) {
      state.postBodies.push(request.postDataJSON())
      await route.fulfill({ status: 202, json: { preview: preview('queued', 0), timeline, idempotent_replay: false } })
      return
    }
    if (request.method() === 'POST' && path === '/api/v1/segments/segment-1/rematch') {
      state.rematched = true
      await route.fulfill({ json: segments[0] })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/jobs/${JOB_ID}`) {
      state.jobPolls += 1
      const succeeded = state.jobPolls >= 2
      if (succeeded) state.finished = true
      const status = succeeded ? 'succeeded' : 'running'
      const progress = succeeded ? 100 : 46
      await route.fulfill({ json: { job: previewJob(status, progress), events: [] } })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: path, retryable: false } })
  })
}

test('时间线可视化片段并完成预览创建、任务轮询与视频播放', async ({ page }) => {
  const state = { finished: false, jobPolls: 0, postBodies: [] as unknown[] }
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  const timelineSection = page.getByRole('region', { name: '时间线' })
  await expect(timelineSection).toBeVisible()
  await expect(timelineSection.locator('.timeline-clip')).toHaveCount(3)
  await expect(timelineSection.getByText('3 个片段 · 12.0s')).toBeVisible()

  await timelineSection.getByRole('button', { name: '生成预览视频' }).click()
  await expect.poll(() => state.postBodies).toEqual([{ force: false }])
  await expect(timelineSection.getByRole('status')).toContainText('正在组合画面与字幕')
  await expect.poll(() => state.jobPolls, { timeout: 5000 }).toBeGreaterThanOrEqual(2)

  const output = timelineSection.getByLabel('FrameFlow 组合预览视频')
  await expect(output).toBeVisible()
  await expect(output).toHaveAttribute('src', OUTPUT_URL)
  await expect(timelineSection.getByText('预览已生成')).toBeVisible()

  await page.setViewportSize({ width: 390, height: 844 })
  await expect(timelineSection).toBeVisible()
  await expect(timelineSection.locator('.timeline-track')).toBeVisible()
  await expect(output).toBeVisible()
  const hasPageOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)
  expect(hasPageOverflow).toBe(false)
})

test('时间线变化后隐藏旧视频并明确提示重新生成', async ({ page }) => {
  const state = { finished: true, jobPolls: 0, postBodies: [] as unknown[], stale: true }
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  const timelineSection = page.getByRole('region', { name: '时间线' })
  await expect(timelineSection.getByText('原预览已过期', { exact: false })).toBeVisible()
  await expect(timelineSection.getByLabel('FrameFlow 组合预览视频')).toHaveCount(0)
  await timelineSection.getByRole('button', { name: '重新生成预览' }).click()
  await expect.poll(() => state.postBodies).toEqual([{ force: true }])
})

test('重新匹配成功后刷新时间线与预览概览', async ({ page }) => {
  const state = { finished: false, jobPolls: 0, postBodies: [] as unknown[], previewReads: 0 }
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)
  await expect.poll(() => state.previewReads).toBeGreaterThanOrEqual(1)
  const readsBeforeRematch = state.previewReads || 0

  await page.getByTitle('根据当前文本重新匹配').click()

  await expect.poll(() => state.previewReads || 0).toBeGreaterThan(readsBeforeRematch)
  await expect(page.getByRole('region', { name: '时间线' }).getByText('原预览已过期', { exact: false })).toHaveCount(0)
})
