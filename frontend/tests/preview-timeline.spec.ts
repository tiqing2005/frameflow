import { expect, test, type Page, type Route } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { fulfillDisabledAuth } from './mock-auth'
import type { ProjectTimeline, Segment } from '../src/types'

const PROJECT_ID = 'project-preview'
const JOB_ID = 'job-preview'
const OUTPUT_URL = '/media/previews/project-preview/final.mp4'
const SAMPLE_VIDEO = fileURLToPath(new URL('../../backend/seed_media/video-smart-city.mp4', import.meta.url))

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

const timeline: ProjectTimeline = {
  project_id: PROJECT_ID,
  input_hash: 'a'.repeat(64),
  segment_count: 3,
  duration_ms: 12_000,
  limits: {
    segment_min_duration_ms: 1_000,
    segment_max_duration_ms: 30_000,
    timeline_max_duration_ms: 180_000,
    frame_duration_ms: 40,
  },
  items: [
    { segment_id: 'segment-1', position: 0, text: '第一段字幕', topic: '开场', start_ms: 0, end_ms: 3000, duration_ms: 3000, render_duration_ms: null, auto_duration_ms: 3000, effective_duration_ms: 3000, duration_source: 'auto', asset: asset('city', '城市开场') },
    { segment_id: 'segment-2', position: 1, text: '第二段字幕', topic: '协作', start_ms: 3000, end_ms: 8000, duration_ms: 5000, render_duration_ms: null, auto_duration_ms: 5000, effective_duration_ms: 5000, duration_source: 'auto', asset: asset('team', '团队协作', 'video') },
    { segment_id: 'segment-3', position: 2, text: '第三段字幕', topic: '收束', start_ms: 8000, end_ms: 12_000, duration_ms: 4000, render_duration_ms: null, auto_duration_ms: 4000, effective_duration_ms: 4000, duration_source: 'auto', asset: asset('result', '成果收束') },
  ],
}

const segments: Segment[] = timeline.items.map((item) => ({
  id: item.segment_id,
  project_id: PROJECT_ID,
  position: item.position,
  text: item.text,
  topic: item.topic,
  keywords: [item.topic],
  start_ms: item.start_ms,
  end_ms: item.end_ms,
  render_duration_ms: null,
  version: 1,
  recommendations: [],
  selection: { segment_id: item.segment_id, asset_id: item.asset.id, source: 'manual', asset: item.asset },
}))

interface PreviewMockState {
  finished: boolean
  jobPolls: number
  postBodies: unknown[]
  stale?: boolean
  rematched?: boolean
  previewReads?: number
  previewUnavailable?: boolean
  timelineOverride?: ProjectTimeline
  segmentsOverride?: Segment[]
  segmentTimingBodies?: unknown[]
  timelineTimingBodies?: unknown[]
  segmentSaveBodies?: unknown[]
  timingRevision?: number
  timingDelayMs?: number
  segmentSaveDelayMs?: number
}

function rebuildTimeline(base: ProjectTimeline, items: ProjectTimeline['items'], revision: number): ProjectTimeline {
  let cursor = 0
  const positioned = items.map((item) => {
    const durationMs = item.effective_duration_ms ?? item.duration_ms
    const next = { ...item, start_ms: cursor, end_ms: cursor + durationMs, duration_ms: durationMs, effective_duration_ms: durationMs }
    cursor += durationMs
    return next
  })
  return { ...base, input_hash: String.fromCharCode(98 + (revision % 20)).repeat(64), duration_ms: cursor, items: positioned }
}

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

async function mockApi(page: Page, state: PreviewMockState) {
  await page.route('**/api/v1/**', async (route: Route) => {
    if (await fulfillDisabledAuth(route)) return
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET' && path === `/api/v1/projects/${PROJECT_ID}`) {
      await route.fulfill({ json: { ...projectDetail, segments: state.segmentsOverride || segments } })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/projects/${PROJECT_ID}/preview`) {
      state.previewReads = (state.previewReads || 0) + 1
      if (state.previewUnavailable) {
        await route.fulfill({ status: 404, json: { code: 'NOT_FOUND', message: '请求的资源不存在', retryable: false } })
        return
      }
      const currentTimeline = state.timelineOverride || (state.rematched ? { ...timeline, input_hash: 'b'.repeat(64) } : timeline)
      const currentPreview = state.finished ? { ...preview('succeeded', 100), input_hash: state.stale ? 'old-timeline-hash' : timeline.input_hash } : null
      await route.fulfill({ json: { preview: currentPreview, timeline: currentTimeline } })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/projects/${PROJECT_ID}/timeline`) {
      await route.fulfill({ json: state.timelineOverride || timeline })
      return
    }
    if (request.method() === 'PATCH' && path === '/api/v1/segments/segment-1') {
      const body = request.postDataJSON() as { text: string; topic: string; keywords: string[]; version: number }
      state.segmentSaveBodies?.push(body)
      if (state.segmentSaveDelayMs) await new Promise((resolve) => setTimeout(resolve, state.segmentSaveDelayMs))
      const currentSegments = state.segmentsOverride || segments
      let updatedSegment = currentSegments[0]
      state.segmentsOverride = currentSegments.map((segment) => {
        if (segment.id !== 'segment-1') return segment
        updatedSegment = { ...segment, text: body.text, topic: body.topic, keywords: body.keywords, version: segment.version + 1 }
        return updatedSegment
      })
      const currentTimeline = state.timelineOverride || timeline
      state.timingRevision = (state.timingRevision || 0) + 1
      state.timelineOverride = rebuildTimeline(currentTimeline, currentTimeline.items.map((item) => item.segment_id === 'segment-1' ? { ...item, text: body.text, topic: body.topic } : item), state.timingRevision)
      await route.fulfill({ json: updatedSegment })
      return
    }
    if (request.method() === 'PATCH' && path === '/api/v1/segments/segment-1/timing') {
      const body = request.postDataJSON() as { duration_ms: number | null; version: number }
      state.segmentTimingBodies?.push(body)
      if (state.timingDelayMs) await new Promise((resolve) => setTimeout(resolve, state.timingDelayMs))
      const currentTimeline = state.timelineOverride || timeline
      const autoDurationMs = currentTimeline.items[0].auto_duration_ms || 3000
      const normalizedDurationMs = body.duration_ms == null ? null : Math.round(body.duration_ms / 40) * 40
      const effectiveDurationMs = normalizedDurationMs ?? autoDurationMs
      const items = currentTimeline.items.map((item) => item.segment_id === 'segment-1' ? {
        ...item,
        render_duration_ms: normalizedDurationMs,
        duration_source: normalizedDurationMs == null ? 'auto' as const : 'manual' as const,
        duration_ms: effectiveDurationMs,
        effective_duration_ms: effectiveDurationMs,
      } : item)
      state.timingRevision = (state.timingRevision || 0) + 1
      state.timelineOverride = rebuildTimeline(currentTimeline, items, state.timingRevision)
      const currentSegments = state.segmentsOverride || segments
      let updatedSegment = currentSegments[0]
      state.segmentsOverride = currentSegments.map((segment) => {
        if (segment.id !== 'segment-1') return segment
        updatedSegment = { ...segment, render_duration_ms: normalizedDurationMs, version: segment.version + 1 }
        return updatedSegment
      })
      await route.fulfill({ json: { segment: updatedSegment, timeline: state.timelineOverride } })
      return
    }
    if (request.method() === 'PUT' && path === `/api/v1/projects/${PROJECT_ID}/timeline/timing`) {
      const body = request.postDataJSON() as { action: 'fit' | 'restore_auto'; target_duration_ms?: number; strategy: 'text' | 'current' | 'equal'; expected_input_hash: string }
      state.timelineTimingBodies?.push(body)
      const currentTimeline = state.timelineOverride || timeline
      const requestedTargetDuration = body.target_duration_ms || currentTimeline.duration_ms
      const targetDuration = body.action === 'fit' ? Math.round(requestedTargetDuration / 40) * 40 : requestedTargetDuration
      const each = Math.floor(targetDuration / currentTimeline.items.length)
      let assigned = 0
      const items = currentTimeline.items.map((item, index) => {
        const autoDurationMs = item.auto_duration_ms || timeline.items[index].duration_ms
        const durationMs = body.action === 'restore_auto'
          ? autoDurationMs
          : index === currentTimeline.items.length - 1 ? targetDuration - assigned : each
        assigned += durationMs
        return {
          ...item,
          render_duration_ms: body.action === 'restore_auto' ? null : durationMs,
          duration_source: body.action === 'restore_auto' ? 'auto' as const : 'manual' as const,
          duration_ms: durationMs,
          effective_duration_ms: durationMs,
        }
      })
      state.timingRevision = (state.timingRevision || 0) + 1
      state.timelineOverride = rebuildTimeline(currentTimeline, items, state.timingRevision)
      const currentSegments = state.segmentsOverride || segments
      state.segmentsOverride = currentSegments.map((segment, index) => ({
        ...segment,
        render_duration_ms: body.action === 'restore_auto' ? null : items[index].duration_ms,
        version: segment.version + 1,
      }))
      await route.fulfill({ json: state.timelineOverride })
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
  await page.route(`**${OUTPUT_URL}`, (route) => route.fulfill({ path: SAMPLE_VIDEO, contentType: 'video/mp4' }))
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

test('预览文件加载失败时不再静默黑屏，并提供重新加载入口', async ({ page }) => {
  const state = { finished: true, jobPolls: 0, postBodies: [] as unknown[] }
  await page.route(`**${OUTPUT_URL}`, (route) => route.fulfill({ status: 404, body: 'missing' }))
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  const timelineSection = page.getByRole('region', { name: '时间线' })
  await expect(timelineSection.getByText('预览文件暂时无法播放')).toBeVisible()
  await expect(timelineSection.getByRole('button', { name: '重新加载视频' })).toBeVisible()
  await expect(timelineSection.getByText('预览文件不可用')).toBeVisible()
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

test('预览接口暂不可用时仍展示时间线，并给出可操作的后端提示', async ({ page }) => {
  const state = { finished: false, jobPolls: 0, postBodies: [] as unknown[], previewUnavailable: true }
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  const timelineSection = page.getByRole('region', { name: '时间线' })
  await expect(timelineSection.locator('.timeline-clip')).toHaveCount(3)
  await expect(timelineSection.getByText('组合预览接口暂不可用', { exact: false })).toBeVisible()
  await expect(timelineSection.getByRole('button', { name: '生成预览视频' })).toBeEnabled()
})

test('可设置目标总时长和单片段时长，并恢复自动节奏且不立即重渲染', async ({ page }) => {
  const state: PreviewMockState = {
    finished: true,
    jobPolls: 0,
    postBodies: [],
    segmentTimingBodies: [],
    timelineTimingBodies: [],
  }
  await page.route(`**${OUTPUT_URL}`, (route) => route.fulfill({ path: SAMPLE_VIDEO, contentType: 'video/mp4' }))
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  const timelineSection = page.getByRole('region', { name: '时间线' })
  await expect(timelineSection.getByText('片段 01 展示时长')).toBeVisible()
  await expect(timelineSection.getByText('自动', { exact: true })).toBeVisible()

  await timelineSection.getByRole('button', { name: '调整节奏' }).click()
  await expect(timelineSection.getByRole('button', { name: '15 秒' })).toBeVisible()
  await expect(timelineSection.getByRole('button', { name: '30 秒' })).toBeVisible()
  await expect(timelineSection.getByRole('button', { name: '60 秒' })).toBeVisible()
  await timelineSection.getByRole('button', { name: '30 秒' }).click()
  await timelineSection.getByRole('button', { name: '自定义' }).click()
  await timelineSection.locator('.timeline-custom-duration input').fill('30.11')
  await timelineSection.getByLabel('分配方式').selectOption('equal')
  await timelineSection.getByRole('button', { name: '应用总时长' }).click()

  await expect.poll(() => state.timelineTimingBodies).toEqual([{
    action: 'fit',
    target_duration_ms: 30_110,
    strategy: 'equal',
    expected_input_hash: timeline.input_hash,
  }])
  await expect(timelineSection.getByText('3 个片段 · 30.12s')).toBeVisible()
  await expect(timelineSection.locator('.timeline-custom-duration input')).toHaveValue('30.12')
  await expect(page.getByText('已将总时长适配为 30.12s')).toBeVisible()
  await expect(timelineSection.getByText('原预览已过期', { exact: false })).toContainText('当前总时长 30.12s')
  await expect(timelineSection.getByLabel('FrameFlow 组合预览视频')).toHaveCount(0)
  expect(state.postBodies).toEqual([])

  const segmentDuration = timelineSection.getByLabel('片段展示时长（秒）')
  await segmentDuration.fill('7.51')
  await timelineSection.getByRole('button', { name: '应用', exact: true }).click()
  await expect.poll(() => state.segmentTimingBodies?.[0]).toEqual({ duration_ms: 7510, version: 2 })
  await expect(segmentDuration).toHaveValue('7.52')
  await expect(timelineSection.locator('.timeline-clip').first().getByText('7.52s')).toBeVisible()
  await expect(page.getByText('片段时长已设为 7.52s')).toBeVisible()
  await expect(timelineSection.getByText('手动', { exact: true })).toBeVisible()

  await segmentDuration.fill('30')
  await expect(timelineSection.getByRole('button', { name: '增加约 0.5 秒' })).toBeDisabled()
  await segmentDuration.fill('9.52')
  await timelineSection.getByRole('button', { name: '增加约 0.5 秒' }).click()
  await expect.poll(() => state.segmentTimingBodies?.[1]).toEqual({ duration_ms: 10_040, version: 3 })
  await expect(segmentDuration).toHaveValue('10.04')

  await timelineSection.getByRole('button', { name: '恢复自动', exact: true }).click()
  await expect.poll(() => state.segmentTimingBodies?.[2]).toEqual({ duration_ms: null, version: 4 })
  await expect(timelineSection.getByText('自动', { exact: true })).toBeVisible()

  await timelineSection.getByRole('button', { name: '恢复全部自动' }).click()
  await expect.poll(() => state.timelineTimingBodies?.at(-1)).toMatchObject({ action: 'restore_auto', strategy: 'equal' })
  expect(state.postBodies).toEqual([])

  await page.setViewportSize({ width: 390, height: 844 })
  const hasPageOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)
  expect(hasPageOverflow).toBe(false)
})

test('时长请求等待期间冻结冲突编辑，快速双击只提交一次', async ({ page }) => {
  const state: PreviewMockState = {
    finished: false,
    jobPolls: 0,
    postBodies: [],
    segmentTimingBodies: [],
    timelineTimingBodies: [],
    timingDelayMs: 300,
  }
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  const timelineSection = page.getByRole('region', { name: '时间线' })
  const increase = timelineSection.getByRole('button', { name: '增加约 0.5 秒' })
  await increase.evaluate((element) => {
    const button = element as HTMLButtonElement
    button.click()
    button.click()
  })

  await expect.poll(() => state.segmentTimingBodies?.length).toBe(1)
  await expect(timelineSection).toHaveAttribute('aria-busy', 'true')
  await expect(page.getByLabel('字幕文本')).toBeDisabled()
  await expect(page.getByTitle('根据当前文本重新匹配')).toBeDisabled()
  await expect(page.locator('.segment-open').first()).toBeDisabled()

  await expect(timelineSection.getByText('手动', { exact: true })).toBeVisible()
  await expect(timelineSection).toHaveAttribute('aria-busy', 'false')
  expect(state.segmentTimingBodies).toEqual([{ duration_ms: 3520, version: 1 }])
  await expect(page.getByLabel('字幕文本')).toBeEnabled()
})

test('有未保存字幕时先完成保存并用最新指纹调整总时长', async ({ page }) => {
  const state: PreviewMockState = {
    finished: false,
    jobPolls: 0,
    postBodies: [],
    segmentSaveBodies: [],
    segmentTimingBodies: [],
    timelineTimingBodies: [],
  }
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  const timelineSection = page.getByRole('region', { name: '时间线' })
  await timelineSection.getByRole('button', { name: '调整节奏' }).click()
  await page.getByLabel('字幕文本').fill('保存后的第一段字幕')
  await timelineSection.getByRole('button', { name: '应用总时长' }).click()

  await expect.poll(() => state.segmentSaveBodies?.length).toBe(1)
  await expect.poll(() => state.timelineTimingBodies?.length).toBe(1)
  const timingBody = state.timelineTimingBodies?.[0] as { expected_input_hash: string }
  expect(timingBody.expected_input_hash).not.toBe(timeline.input_hash)
  expect(timingBody.expected_input_hash).toHaveLength(64)
  await expect(page.getByLabel('字幕文本')).toHaveValue('保存后的第一段字幕')
  await expect(page.getByLabel('字幕文本')).toBeEnabled()
})

test('等待字幕保存时预览创建与节奏调整互斥，快速点击只创建一个预览', async ({ page }) => {
  const state: PreviewMockState = {
    finished: false,
    jobPolls: 0,
    postBodies: [],
    segmentSaveBodies: [],
    segmentTimingBodies: [],
    timelineTimingBodies: [],
    segmentSaveDelayMs: 300,
  }
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  const timelineSection = page.getByRole('region', { name: '时间线' })
  await timelineSection.getByRole('button', { name: '调整节奏' }).click()
  await page.getByLabel('字幕文本').fill('生成预览前保存这段字幕')
  const generate = await timelineSection.getByRole('button', { name: '生成预览视频' }).elementHandle()
  const fit = await timelineSection.getByRole('button', { name: '应用总时长' }).elementHandle()
  expect(generate).not.toBeNull()
  expect(fit).not.toBeNull()
  if (!generate || !fit) throw new Error('预览与节奏按钮未渲染')
  await page.evaluate(({ generateButton, fitButton }) => {
    const generateElement = generateButton as HTMLButtonElement
    const fitElement = fitButton as HTMLButtonElement
    generateElement.click()
    generateElement.click()
    fitElement.click()
  }, { generateButton: generate, fitButton: fit })

  await expect.poll(() => state.segmentSaveBodies?.length).toBe(1)
  await expect.poll(() => state.postBodies.length).toBe(1)
  expect(state.timelineTimingBodies).toEqual([])
  await expect(timelineSection.getByRole('status')).toContainText('正在组合画面与字幕')
})
