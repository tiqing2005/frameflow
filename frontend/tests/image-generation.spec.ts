import { expect, test, type Page, type Route } from '@playwright/test'
import { fulfillDisabledAuth } from './mock-auth'

const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64')
const PIXEL = `data:image/png;base64,${PNG.toString('base64')}`
const CREATED_AT = '2026-07-15T00:00:00Z'

type GenerationStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'canceled'

function generation(id: string, status: GenerationStatus, overrides: Record<string, unknown> = {}) {
  return {
    id,
    project_id: null,
    segment_id: null,
    segment_version: null,
    source: 'library',
    prompt: '海边日出，真实自然光，画面右侧留白',
    name: '海边日出素材',
    aspect_ratio: '16:9',
    provider: 'image-api',
    model: 'gpt-image-2',
    status,
    progress: status === 'succeeded' ? 100 : null,
    attempt: 1,
    max_attempts: 3,
    retryable: status === 'failed',
    error_code: null,
    error_message: null,
    content_url: status === 'succeeded' ? `/api/v1/image-generations/${id}/content` : null,
    asset_id: null,
    auto_import: false,
    auto_select: false,
    created_at: CREATED_AT,
    started_at: status === 'queued' ? null : '2026-07-15T00:00:01Z',
    finished_at: status === 'succeeded' ? '2026-07-15T00:00:03Z' : null,
    accepted_at: null,
    discarded_at: null,
    expires_at: '2026-07-16T00:00:00Z',
    updated_at: CREATED_AT,
    ...overrides,
  }
}

function generatedAsset(id = 'generated-asset') {
  return {
    id,
    name: '海边日出素材',
    kind: 'image',
    file_url: `/api/v1/assets/${id}/content`,
    thumbnail_url: `/api/v1/assets/${id}/content`,
    mime_type: 'image/png',
    width: 1536,
    height: 864,
    tags: [],
    keywords: [],
    tagging_status: 'queued',
    tagging_source: null,
    is_seed: false,
    active: true,
    created_at: CREATED_AT,
  }
}

test('素材库文生图完成 queued→running→succeeded，重新生成创建新任务并可加入素材库', async ({ page }) => {
  const createBodies: Array<Record<string, unknown>> = []
  const acceptBodies: Array<Record<string, unknown>> = []
  let retryCalls = 0
  let phase: GenerationStatus = 'queued'
  let currentId = ''
  let accepted = false

  await page.route('**/api/v1/**', async (route: Route) => {
    if (await fulfillDisabledAuth(route)) return
    const request = route.request()
    const path = new URL(request.url()).pathname

    if (request.method() === 'GET' && path === '/api/v1/assets') {
      await route.fulfill({ json: { items: [], total: 0 } })
      return
    }
    if (request.method() === 'GET' && path === '/api/v1/image-generations') {
      await route.fulfill({ json: { items: [], total: 0 } })
      return
    }
    if (request.method() === 'POST' && path === '/api/v1/image-generations') {
      createBodies.push(request.postDataJSON() as Record<string, unknown>)
      currentId = `generation-${createBodies.length}`
      phase = 'queued'
      await route.fulfill({ status: 202, json: { generation: generation(currentId, phase), idempotent_replay: false } })
      return
    }

    const contentMatch = path.match(/^\/api\/v1\/image-generations\/([^/]+)\/content$/)
    if (request.method() === 'GET' && contentMatch) {
      await route.fulfill({ body: PNG, contentType: 'image/png' })
      return
    }
    const retryMatch = path.match(/^\/api\/v1\/image-generations\/([^/]+)\/retry$/)
    if (request.method() === 'POST' && retryMatch) {
      retryCalls += 1
      await route.fulfill({
        status: 409,
        json: { code: 'SUCCESS_RETRY_FORBIDDEN', message: '成功结果必须创建新的生成任务', retryable: false },
      })
      return
    }
    const acceptMatch = path.match(/^\/api\/v1\/image-generations\/([^/]+)\/accept$/)
    if (request.method() === 'POST' && acceptMatch) {
      acceptBodies.push(request.postDataJSON() as Record<string, unknown>)
      accepted = true
      const asset = generatedAsset()
      await route.fulfill({
        json: {
          generation: generation(acceptMatch[1], 'succeeded', { asset_id: asset.id, accepted_at: '2026-07-15T00:00:04Z' }),
          asset,
          selection: null,
          segment: null,
          idempotent_replay: false,
        },
      })
      return
    }
    const detailMatch = path.match(/^\/api\/v1\/image-generations\/([^/]+)$/)
    if (request.method() === 'GET' && detailMatch) {
      await route.fulfill({
        json: {
          generation: generation(detailMatch[1], phase, accepted ? { asset_id: 'generated-asset', accepted_at: '2026-07-15T00:00:04Z' } : {}),
          asset: accepted ? generatedAsset() : null,
          selection: null,
          segment: null,
        },
      })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: `${request.method()} ${path}`, retryable: false } })
  })

  await page.goto('/assets')
  await page.getByRole('link', { name: '生成图片' }).click()
  await expect(page).toHaveURL('/assets/generate')
  await page.getByLabel(/画面描述/).fill('海边日出，真实自然光，画面右侧留白')
  await page.getByLabel('画面风格').selectOption({ label: '商业摄影' })
  await page.getByLabel('素材名称').fill('海边日出素材')
  await page.getByRole('button', { name: '生成 1 张图片' }).click()

  await expect(page.getByText('等待生成服务', { exact: true }).first()).toBeVisible()
  await expect.poll(() => createBodies.length).toBe(1)
  expect(String(createBodies[0].prompt)).toContain('画面风格：克制的商业摄影')
  expect(createBodies[0]).toMatchObject({ aspect_ratio: '16:9', auto_import: false, auto_select: false })

  phase = 'running'
  await expect(page.getByText('图像模型正在生成', { exact: true }).first()).toBeVisible({ timeout: 8_000 })
  phase = 'succeeded'
  await expect(page.getByText('图片已生成', { exact: true }).first()).toBeVisible({ timeout: 8_000 })
  const image = page.locator('.generation-preview img')
  await expect(image).toBeVisible()
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0)

  const download = page.getByRole('link', { name: '下载原图' })
  await expect(download).toHaveAttribute('href', `/api/v1/image-generations/${currentId}/content`)
  await expect(download).toHaveAttribute('download', '海边日出素材.png')

  await page.getByRole('button', { name: '重新生成', exact: true }).click()
  await expect.poll(() => createBodies.length, { message: '成功结果重新生成应创建全新的持久任务' }).toBe(2)
  expect(retryCalls).toBe(0)
  expect(currentId).toBe('generation-2')

  phase = 'running'
  await expect(page.getByText('图像模型正在生成', { exact: true }).first()).toBeVisible({ timeout: 8_000 })
  phase = 'succeeded'
  await expect(page.getByText('图片已生成', { exact: true }).first()).toBeVisible({ timeout: 8_000 })
  await page.getByRole('button', { name: '加入素材库', exact: true }).click()

  await expect.poll(() => acceptBodies.length).toBe(1)
  expect(acceptBodies[0]).toEqual({ name: '海边日出素材', select_for_segment: false, expected_segment_version: null })
  await expect(page.getByText('“海边日出素材”已加入素材库')).toBeVisible()
  await expect(page.getByText('后台 AI 标签任务正在处理，结果以素材库显示为准。')).toBeVisible()
})

test('生成任务在刷新后按 URL 与持久 ID 恢复并继续轮询', async ({ page }) => {
  const generationId = 'generation-recover'
  let phase: GenerationStatus = 'running'
  let detailReads = 0

  await page.route('**/api/v1/**', async (route: Route) => {
    if (await fulfillDisabledAuth(route)) return
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET' && path === '/api/v1/image-generations') {
      await route.fulfill({ json: { items: [generation(generationId, phase)], total: 1 } })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/image-generations/${generationId}`) {
      detailReads += 1
      await route.fulfill({ json: { generation: generation(generationId, phase), asset: null, selection: null, segment: null } })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/image-generations/${generationId}/content`) {
      await route.fulfill({ body: PNG, contentType: 'image/png' })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: `${request.method()} ${path}`, retryable: false } })
  })

  await page.goto('/assets/generate')
  await expect(page).toHaveURL(`/assets/generate?generation=${generationId}`)
  await expect(page.getByText('图像模型正在生成', { exact: true }).first()).toBeVisible()

  await page.reload()
  await expect(page.getByText('图像模型正在生成', { exact: true }).first()).toBeVisible()
  expect(detailReads).toBeGreaterThan(0)
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem('frameflow:image-generation:library'))).toBe(generationId)

  phase = 'succeeded'
  await expect(page.locator('.generation-preview img')).toBeVisible({ timeout: 8_000 })
})

test('持久 ID 已过期时同轮回退到最新可恢复任务', async ({ page }) => {
  const staleId = 'generation-stale'
  const recoveredId = 'generation-recovered'
  let listReads = 0

  await page.addInitScript(({ key, value }) => window.localStorage.setItem(key, value), {
    key: 'frameflow:image-generation:library',
    value: staleId,
  })
  await page.route('**/api/v1/**', async (route: Route) => {
    if (await fulfillDisabledAuth(route)) return
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET' && path === `/api/v1/image-generations/${staleId}`) {
      await route.fulfill({ status: 410, json: { code: 'IMAGE_DRAFT_EXPIRED', message: '临时结果已过期', retryable: false } })
      return
    }
    if (request.method() === 'GET' && path === '/api/v1/image-generations') {
      listReads += 1
      await route.fulfill({ json: { items: [generation(recoveredId, 'running')], total: 1 } })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/image-generations/${recoveredId}`) {
      await route.fulfill({ json: { generation: generation(recoveredId, 'running'), asset: null, selection: null, segment: null } })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: `${request.method()} ${path}`, retryable: false } })
  })

  await page.goto('/assets/generate')
  await expect(page.getByText('图像模型正在生成', { exact: true }).first()).toBeVisible()
  await expect(page).toHaveURL(`/assets/generate?generation=${recoveredId}`)
  await expect.poll(() => listReads).toBeGreaterThan(0)
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem('frameflow:image-generation:library'))).toBe(recoveredId)
})

test('修改素材名称后创建请求使用新的幂等键', async ({ page }) => {
  const idempotencyKeys: string[] = []
  let createCalls = 0

  await page.route('**/api/v1/**', async (route: Route) => {
    if (await fulfillDisabledAuth(route)) return
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET' && path === '/api/v1/image-generations') {
      await route.fulfill({ json: { items: [], total: 0 } })
      return
    }
    if (request.method() === 'POST' && path === '/api/v1/image-generations') {
      createCalls += 1
      idempotencyKeys.push(request.headers()['idempotency-key'] || '')
      if (createCalls === 1) {
        await route.abort('connectionreset')
        return
      }
      await route.fulfill({ status: 202, json: { generation: generation('generation-renamed', 'queued', { name: '第二个名称' }), idempotent_replay: false } })
      return
    }
    if (request.method() === 'GET' && path === '/api/v1/image-generations/generation-renamed') {
      await route.fulfill({ json: { generation: generation('generation-renamed', 'queued', { name: '第二个名称' }), asset: null, selection: null, segment: null } })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: `${request.method()} ${path}`, retryable: false } })
  })

  await page.goto('/assets/generate')
  await page.getByLabel(/画面描述/).fill('晨光中的玻璃建筑，写实摄影')
  await page.getByLabel('素材名称').fill('第一个名称')
  const submit = page.getByRole('button', { name: '生成 1 张图片' })
  await submit.click()
  await expect.poll(() => createCalls).toBe(1)
  await expect(submit).toBeEnabled()

  await page.getByLabel('素材名称').fill('第二个名称')
  await submit.click()
  await expect.poll(() => createCalls).toBe(2)
  expect(idempotencyKeys[0]).toBeTruthy()
  expect(idempotencyKeys[1]).toBeTruthy()
  expect(idempotencyKeys[1]).not.toBe(idempotencyKeys[0])
})

test('取消后保留 canceled 状态，用户清除后才丢弃记录', async ({ page }) => {
  const generationId = 'generation-cancel'
  let phase: GenerationStatus = 'running'
  let discarded = false
  let cancelCalls = 0
  let discardCalls = 0

  await page.route('**/api/v1/**', async (route: Route) => {
    if (await fulfillDisabledAuth(route)) return
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET' && path === '/api/v1/image-generations') {
      await route.fulfill({ json: { items: discarded ? [] : [generation(generationId, phase)], total: discarded ? 0 : 1 } })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/image-generations/${generationId}`) {
      await route.fulfill({ json: { generation: generation(generationId, phase), asset: null, selection: null, segment: null } })
      return
    }
    if (request.method() === 'POST' && path === `/api/v1/image-generations/${generationId}/cancel`) {
      cancelCalls += 1
      phase = 'canceled'
      await route.fulfill({ json: { generation: generation(generationId, phase, { retryable: false, finished_at: '2026-07-15T00:00:05Z' }) } })
      return
    }
    if (request.method() === 'DELETE' && path === `/api/v1/image-generations/${generationId}`) {
      discardCalls += 1
      discarded = true
      await route.fulfill({ status: 204, body: '' })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: `${request.method()} ${path}`, retryable: false } })
  })

  await page.goto('/assets/generate')
  await page.getByRole('button', { name: '取消本次生成' }).click()
  await expect.poll(() => cancelCalls).toBe(1)
  await expect(page.getByText('任务已取消', { exact: true }).first()).toBeVisible()
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem('frameflow:image-generation:library'))).toBe(generationId)

  await page.getByRole('button', { name: '清除任务' }).click()
  await expect.poll(() => discardCalls).toBe(1)
  await expect(page.getByText('等待画面描述', { exact: true })).toBeVisible()
  await expect(page).toHaveURL('/assets/generate')
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem('frameflow:image-generation:library'))).toBeNull()
})

test('结果未知时提示可能已计费，并仅在用户确认后再次调用', async ({ page }) => {
  const generationId = 'generation-unknown-result'
  let phase: GenerationStatus = 'failed'
  let retryCalls = 0

  await page.route('**/api/v1/**', async (route: Route) => {
    if (await fulfillDisabledAuth(route)) return
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET' && path === '/api/v1/image-generations') {
      await route.fulfill({
        json: {
          items: [generation(generationId, phase, {
            retryable: true,
            error_code: 'IMAGE_PROVIDER_RESULT_UNKNOWN',
            error_message: 'Provider submission result is unknown',
          })],
          total: 1,
        },
      })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/image-generations/${generationId}`) {
      await route.fulfill({
        json: {
          generation: generation(generationId, phase, phase === 'failed' ? {
            retryable: true,
            error_code: 'IMAGE_PROVIDER_RESULT_UNKNOWN',
            error_message: 'Provider submission result is unknown',
          } : {}),
          asset: null,
          selection: null,
          segment: null,
        },
      })
      return
    }
    if (request.method() === 'POST' && path === `/api/v1/image-generations/${generationId}/retry`) {
      retryCalls += 1
      phase = 'queued'
      await route.fulfill({ status: 202, json: { generation: generation(generationId, phase) } })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: `${request.method()} ${path}`, retryable: false } })
  })

  await page.goto('/assets/generate')
  await expect(page.getByText('生成结果待确认', { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('alert')).toContainText('可能已经被模型服务商接收并计费')
  expect(retryCalls).toBe(0)

  await page.getByRole('button', { name: '确认再次调用' }).click()
  await expect.poll(() => retryCalls).toBe(1)
  await expect(page.getByText('等待生成服务', { exact: true }).first()).toBeVisible()
})

const PROJECT_ID = 'project-image-generation'
const SEGMENT_ID = 'segment-image-generation'
const originalAsset = {
  id: 'asset-original',
  name: '原始候选画面',
  kind: 'image',
  file_url: PIXEL,
  thumbnail_url: PIXEL,
  mime_type: 'image/png',
  tags: ['城市'],
  keywords: ['城市'],
}

function workbenchSegment() {
  return {
    id: SEGMENT_ID,
    project_id: PROJECT_ID,
    position: 0,
    text: '旧字幕内容',
    topic: '城市生活',
    keywords: ['城市', '清晨'],
    start_ms: 0,
    end_ms: 3000,
    version: 1,
    recommendations: [{
      id: 'recommendation-original',
      segment_id: SEGMENT_ID,
      asset: originalAsset,
      asset_id: originalAsset.id,
      rank: 1,
      total_score: 0.62,
      tfidf_score: 0.6,
      keyword_score: 0.7,
      tag_score: 0.5,
      matched_terms: ['城市'],
      explanation: '命中城市主题',
    }],
    selection: { segment_id: SEGMENT_ID, asset_id: originalAsset.id, source: 'auto', asset: originalAsset },
  }
}

interface WorkbenchMockState {
  segment: ReturnType<typeof workbenchSegment>
  phase: GenerationStatus
  calls: string[]
  createBodies: Array<Record<string, unknown>>
  acceptBodies: Array<Record<string, unknown>>
  acceptConflictsRemaining: number
  accepted: boolean
}

function workbenchTimeline(state: WorkbenchMockState) {
  return {
    project_id: PROJECT_ID,
    input_hash: state.accepted ? 'timeline-generated' : 'timeline-original',
    segment_count: 1,
    duration_ms: 3000,
    items: [{
      segment_id: SEGMENT_ID,
      position: 0,
      text: state.segment.text,
      topic: state.segment.topic,
      start_ms: 0,
      end_ms: 3000,
      duration_ms: 3000,
      asset: state.segment.selection.asset,
    }],
  }
}

async function mockWorkbenchImageGeneration(page: Page, state: WorkbenchMockState) {
  await page.route('**/api/v1/**', async (route: Route) => {
    if (await fulfillDisabledAuth(route)) return
    const request = route.request()
    const path = new URL(request.url()).pathname

    if (request.method() === 'GET' && path === `/api/v1/projects/${PROJECT_ID}`) {
      await route.fulfill({
        json: {
          project: { id: PROJECT_ID, title: '文生图工作台验收', status: 'ready', input_kind: 'text', created_at: CREATED_AT, updated_at: CREATED_AT },
          current_job: null,
          source: { text: state.segment.text },
          segments: [state.segment],
        },
      })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/projects/${PROJECT_ID}/preview`) {
      await route.fulfill({ json: { preview: null, timeline: workbenchTimeline(state) } })
      return
    }
    if (request.method() === 'GET' && path === '/api/v1/image-generations') {
      await route.fulfill({ json: { items: [], total: 0 } })
      return
    }
    if (request.method() === 'PATCH' && path === `/api/v1/segments/${SEGMENT_ID}`) {
      state.calls.push('PATCH segment')
      const body = request.postDataJSON() as { text: string; topic: string; keywords: string[] }
      state.segment = { ...state.segment, ...body, version: 2 }
      await route.fulfill({ json: state.segment })
      return
    }
    if (request.method() === 'POST' && path === `/api/v1/segments/${SEGMENT_ID}/image-generations`) {
      state.calls.push('POST segment generation')
      state.createBodies.push(request.postDataJSON() as Record<string, unknown>)
      state.phase = 'queued'
      await route.fulfill({
        status: 202,
        json: {
          generation: generation('generation-segment', state.phase, {
            project_id: PROJECT_ID,
            segment_id: SEGMENT_ID,
            segment_version: state.segment.version,
            source: 'segment_shortfall',
            prompt: String(state.createBodies[0].prompt),
          }),
          idempotent_replay: false,
        },
      })
      return
    }
    if (request.method() === 'GET' && path === '/api/v1/image-generations/generation-segment/content') {
      await route.fulfill({ body: PNG, contentType: 'image/png' })
      return
    }
    if (request.method() === 'GET' && path === '/api/v1/image-generations/generation-segment') {
      await route.fulfill({
        json: {
          generation: generation('generation-segment', state.phase, {
            project_id: PROJECT_ID,
            segment_id: SEGMENT_ID,
            segment_version: state.segment.version,
            source: 'segment_shortfall',
          }),
          asset: null,
          selection: null,
          segment: state.segment,
        },
      })
      return
    }
    if (request.method() === 'POST' && path === '/api/v1/image-generations/generation-segment/accept') {
      state.calls.push('POST accept')
      const acceptBody = request.postDataJSON() as Record<string, unknown>
      state.acceptBodies.push(acceptBody)
      if (acceptBody.select_for_segment && state.acceptConflictsRemaining > 0) {
        state.acceptConflictsRemaining -= 1
        await route.fulfill({
          status: 409,
          json: {
            code: 'IMAGE_SEGMENT_VERSION_CONFLICT',
            message: '字幕片段已更新，请确认新内容后再选择生成图片',
            retryable: false,
          },
        })
        return
      }
      state.accepted = true
      const asset = { ...generatedAsset('asset-segment-generated'), name: '城市生活配图' }
      const selection = { segment_id: SEGMENT_ID, asset_id: asset.id, source: 'generated', asset }
      if (acceptBody.select_for_segment) state.segment = { ...state.segment, selection }
      await route.fulfill({
        json: {
          generation: generation('generation-segment', 'succeeded', {
            project_id: PROJECT_ID,
            segment_id: SEGMENT_ID,
            segment_version: state.segment.version,
            source: 'segment_shortfall',
            asset_id: asset.id,
            accepted_at: '2026-07-15T00:00:05Z',
          }),
          asset,
          selection: acceptBody.select_for_segment ? selection : null,
          segment: state.segment,
          idempotent_replay: false,
        },
      })
      return
    }
    if (request.method() === 'GET' && path === '/api/v1/assets') {
      await route.fulfill({ json: { items: [], total: 0 } })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: `${request.method()} ${path}`, retryable: false } })
  })
}

function createWorkbenchState(): WorkbenchMockState {
  return {
    segment: workbenchSegment(),
    phase: 'queued',
    calls: [],
    createBodies: [],
    acceptBodies: [],
    acceptConflictsRemaining: 0,
    accepted: false,
  }
}

test('工作台先保存最新字幕，再按片段生成并用生成素材更新 selection 与预览', async ({ page }) => {
  const state = createWorkbenchState()
  await mockWorkbenchImageGeneration(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  await page.getByLabel('字幕文本').fill('刚刚编辑完成的最新字幕内容')
  await page.getByRole('button', { name: /候选都不合适/ }).click()
  const dialog = page.getByRole('dialog', { name: '为当前字幕生成画面' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel(/画面描述/)).toContainText('刚刚编辑完成的最新字幕内容')
  await dialog.getByRole('button', { name: '生成 1 张图片' }).click()

  await expect.poll(() => state.calls.slice(0, 2)).toEqual(['PATCH segment', 'POST segment generation'])
  expect(state.createBodies[0]).toMatchObject({ aspect_ratio: '16:9', auto_import: false, auto_select: false })
  expect(String(state.createBodies[0].prompt)).toContain('刚刚编辑完成的最新字幕内容')

  state.phase = 'succeeded'
  await expect(dialog.locator('.generation-preview img')).toBeVisible({ timeout: 8_000 })
  await dialog.getByRole('button', { name: '使用并加入素材库' }).click()

  await expect.poll(() => state.acceptBodies.length).toBe(1)
  expect(state.acceptBodies[0]).toEqual({ name: '城市生活配图', select_for_segment: true, expected_segment_version: 2 })
  await expect(dialog).toHaveCount(0)
  await expect(page.locator('.preview-caption')).toContainText('城市生活配图')
  await expect(page.locator('.preview-caption')).toContainText('文生图')
})

test('字幕版本冲突后由用户明确选择仅入库，不静默替换当前片段', async ({ page }) => {
  const state = createWorkbenchState()
  state.acceptConflictsRemaining = 1
  await mockWorkbenchImageGeneration(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  await page.getByRole('button', { name: /候选都不合适/ }).click()
  const dialog = page.getByRole('dialog', { name: '为当前字幕生成画面' })
  await dialog.getByRole('button', { name: '生成 1 张图片' }).click()
  state.phase = 'succeeded'
  await expect(dialog.locator('.generation-preview img')).toBeVisible({ timeout: 8_000 })
  await dialog.getByRole('button', { name: '使用并加入素材库' }).click()

  await expect.poll(() => state.acceptBodies.length).toBe(1)
  expect(state.acceptBodies[0]).toEqual({ name: '城市生活配图', select_for_segment: true, expected_segment_version: 1 })
  await expect(dialog.getByRole('alert')).toContainText('旧图片不能直接替换新内容')
  const importOnly = dialog.getByRole('button', { name: '仅加入素材库（不替换当前片段）' })
  await expect(importOnly).toBeVisible()

  await importOnly.click()
  await expect.poll(() => state.acceptBodies.length).toBe(2)
  expect(state.acceptBodies[1]).toEqual({ name: '城市生活配图', select_for_segment: false, expected_segment_version: null })
  await expect(dialog).toHaveCount(0)
  await expect(page.locator('.preview-caption')).toContainText('原始候选画面')
  await expect(page.locator('.preview-caption')).not.toContainText('文生图')
})

test('移动端工作台文生图抽屉不产生横向溢出', async ({ page }) => {
  const state = createWorkbenchState()
  await page.setViewportSize({ width: 390, height: 844 })
  await mockWorkbenchImageGeneration(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)
  await page.getByRole('button', { name: '候选' }).click()
  await page.getByRole('button', { name: /候选都不合适/ }).click()

  const dialog = page.getByRole('dialog', { name: '为当前字幕生成画面' })
  await expect(dialog).toBeVisible()
  const bounds = await dialog.boundingBox()
  expect(bounds).not.toBeNull()
  expect(bounds!.x).toBeGreaterThanOrEqual(-1)
  expect(bounds!.width).toBeLessThanOrEqual(391)
  const overflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    dialog: (() => {
      const element = document.querySelector<HTMLElement>('.generation-dialog')
      return element ? element.scrollWidth - element.clientWidth : 999
    })(),
  }))
  expect(overflow.document).toBeLessThanOrEqual(1)
  expect(overflow.dialog).toBeLessThanOrEqual(1)
})
