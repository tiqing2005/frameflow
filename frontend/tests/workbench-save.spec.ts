import { expect, test, type Page, type Route } from '@playwright/test'

const segments = [
  {
    id: 'segment-1',
    project_id: 'project-1',
    position: 0,
    text: '第一段原文',
    topic: '第一段',
    keywords: ['第一'],
    start_ms: 0,
    end_ms: 2000,
    version: 1,
    recommendations: [],
    selection: null,
  },
  {
    id: 'segment-2',
    project_id: 'project-1',
    position: 1,
    text: '第二段原文',
    topic: '第二段',
    keywords: ['第二'],
    start_ms: 2000,
    end_ms: 4000,
    version: 1,
    recommendations: [],
    selection: null,
  },
]

const projectDetail = {
  project: {
    id: 'project-1',
    title: '字幕保存竞态测试',
    status: 'ready',
    input_kind: 'text',
    created_at: '2026-07-14T00:00:00Z',
    updated_at: '2026-07-14T00:00:00Z',
  },
  current_job: null,
  source: { text: '第一段原文\n第二段原文' },
  segments,
}

const dashboard = {
  metrics: { projects: 1, total_assets: 0, running_jobs: 0, failed_jobs: 0 },
  recent_projects: [projectDetail.project],
  recent_runs: [],
}

type PatchHandler = (route: Route, body: Record<string, unknown>, segmentId: string) => Promise<void>

async function mockApi(page: Page, patchHandler: PatchHandler) {
  let rematchCalls = 0
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() === 'GET' && url.pathname === '/api/v1/projects/project-1') {
      await route.fulfill({ json: projectDetail })
      return
    }
    if (request.method() === 'GET' && url.pathname === '/api/v1/dashboard') {
      await route.fulfill({ json: dashboard })
      return
    }
    const patchMatch = url.pathname.match(/^\/api\/v1\/segments\/([^/]+)$/)
    if (request.method() === 'PATCH' && patchMatch) {
      await patchHandler(route, request.postDataJSON() as Record<string, unknown>, patchMatch[1])
      return
    }
    if (request.method() === 'POST' && url.pathname.endsWith('/rematch')) {
      rematchCalls += 1
      await route.fulfill({ json: segments[0] })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: url.pathname, retryable: false } })
  })
  return { rematchCalls: () => rematchCalls }
}

function successfulSegment(body: Record<string, unknown>, segmentId: string) {
  const original = segments.find((segment) => segment.id === segmentId) ?? segments[0]
  return {
    ...original,
    text: body.text,
    topic: body.topic,
    keywords: body.keywords,
    version: Number(body.version) + 1,
  }
}

test('编辑后立即切换会等待原片段保存完成', async ({ page }) => {
  let releaseSave: (() => void) | undefined
  let savedBody: Record<string, unknown> | undefined
  let savedSegmentId = ''
  let markSaveStarted: (() => void) | undefined
  const saveStarted = new Promise<void>((resolve) => { markSaveStarted = resolve })
  await mockApi(page, async (route, body, segmentId) => {
    savedBody = body
    savedSegmentId = segmentId
    markSaveStarted?.()
    await new Promise<void>((release) => { releaseSave = release })
    await route.fulfill({ json: successfulSegment(body, segmentId) })
  })

  await page.goto('/projects/project-1')
  const editor = page.getByLabel('字幕文本')
  await editor.fill('切换前必须保存的草稿')
  await page.locator('.segment-open').nth(1).click()
  await saveStarted

  await expect(editor).toHaveValue('切换前必须保存的草稿')
  await expect(page.locator('.segment-item').nth(0)).toHaveClass(/active/)
  expect(savedSegmentId).toBe('segment-1')
  expect(savedBody?.text).toBe('切换前必须保存的草稿')

  releaseSave?.()
  await expect(editor).toHaveValue('第二段原文')
  await expect(page.locator('.segment-item').nth(1)).toHaveClass(/active/)
})

test('编辑后立即返回项目台会在保存完成前停留并合并重复点击', async ({ page }) => {
  let releaseSave: (() => void) | undefined
  let patchCalls = 0
  let markSaveStarted: (() => void) | undefined
  const saveStarted = new Promise<void>((resolve) => { markSaveStarted = resolve })
  await mockApi(page, async (route, body, segmentId) => {
    patchCalls += 1
    markSaveStarted?.()
    await new Promise<void>((release) => { releaseSave = release })
    await route.fulfill({ json: successfulSegment(body, segmentId) })
  })

  await page.goto('/projects/project-1')
  await page.getByLabel('字幕文本').fill('返回前保存的草稿')
  const back = page.getByRole('link', { name: '返回项目台' })
  await back.click()
  await back.click()
  await saveStarted

  await expect(page).toHaveURL(/\/projects\/project-1$/)
  expect(patchCalls).toBe(1)
  releaseSave?.()
  await expect(page).toHaveURL(/\/projects$/)
})

test('保存期间连续点击不同导航时，保存完成后执行最后一次导航意图', async ({ page }) => {
  let releaseSave: (() => void) | undefined
  let markSaveStarted: (() => void) | undefined
  const saveStarted = new Promise<void>((resolve) => { markSaveStarted = resolve })
  await mockApi(page, async (route, body, segmentId) => {
    markSaveStarted?.()
    await new Promise<void>((release) => { releaseSave = release })
    await route.fulfill({ json: successfulSegment(body, segmentId) })
  })

  await page.goto('/projects/project-1')
  await page.getByLabel('字幕文本').fill('需要先保存再执行最新导航')
  await page.getByRole('link', { name: '返回项目台' }).click()
  await saveStarted
  await page.locator('a[href="/assets"]').evaluate((node: HTMLAnchorElement) => node.click())

  await expect(page).toHaveURL(/\/projects\/project-1$/)
  releaseSave?.()
  await expect(page).toHaveURL(/\/assets$/)
})

test('保存过程中继续输入会用新版本串行保存最新草稿', async ({ page }) => {
  let releaseFirstSave: (() => void) | undefined
  let markFirstSaveStarted: (() => void) | undefined
  const firstSaveStarted = new Promise<void>((resolve) => { markFirstSaveStarted = resolve })
  const requests: Record<string, unknown>[] = []
  await mockApi(page, async (route, body, segmentId) => {
    requests.push(body)
    if (requests.length === 1) {
      markFirstSaveStarted?.()
      await new Promise<void>((release) => { releaseFirstSave = release })
    }
    await route.fulfill({ json: successfulSegment(body, segmentId) })
  })

  await page.goto('/projects/project-1')
  const editor = page.getByLabel('字幕文本')
  await editor.fill('第一次输入')
  await page.waitForTimeout(800)
  await firstSaveStarted
  await editor.fill('保存中追加的最终文本')
  releaseFirstSave?.()

  await expect.poll(() => requests.length).toBe(2)
  expect(requests[0].text).toBe('第一次输入')
  expect(requests[0].version).toBe(1)
  expect(requests[1].text).toBe('保存中追加的最终文本')
  expect(requests[1].version).toBe(2)
  await expect(editor).toHaveValue('保存中追加的最终文本')
  await expect(page.getByText('已自动保存', { exact: true })).toBeVisible()
})

test('浏览器历史返回同样会等待草稿保存', async ({ page }) => {
  let releaseSave: (() => void) | undefined
  let markSaveStarted: (() => void) | undefined
  const saveStarted = new Promise<void>((resolve) => { markSaveStarted = resolve })
  await mockApi(page, async (route, body, segmentId) => {
    markSaveStarted?.()
    await new Promise<void>((release) => { releaseSave = release })
    await route.fulfill({ json: successfulSegment(body, segmentId) })
  })

  await page.goto('/projects')
  await page.getByRole('button', { name: /字幕保存竞态测试/ }).click()
  const editor = page.getByLabel('字幕文本')
  await editor.fill('浏览器返回前的草稿')
  await page.evaluate(() => window.history.back())
  await saveStarted

  await expect(editor).toHaveValue('浏览器返回前的草稿')
  releaseSave?.()
  await expect(page.getByRole('heading', { name: '项目', exact: true })).toBeVisible()
})

test('保存失败时保留草稿并阻止切换和重新匹配', async ({ page }) => {
  const apiState = await mockApi(page, async (route) => {
    await route.fulfill({
      status: 500,
      json: { code: 'SAVE_FAILED', message: '模拟保存失败', retryable: true },
    })
  })

  await page.goto('/projects/project-1')
  const editor = page.getByLabel('字幕文本')
  await editor.fill('不能丢失的失败草稿')
  await page.locator('.segment-open').nth(1).click()

  await expect(editor).toHaveValue('不能丢失的失败草稿')
  await expect(page.getByText('保存失败', { exact: true })).toBeVisible()
  await expect(page.locator('.segment-item').nth(0)).toHaveClass(/active/)
  const unloadPrevented = await page.evaluate(() => {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event.defaultPrevented
  })
  expect(unloadPrevented).toBe(true)

  await page.getByTitle('根据当前文本重新匹配').click()
  await expect.poll(apiState.rematchCalls).toBe(0)
  await expect(editor).toHaveValue('不能丢失的失败草稿')
})

test('移动端保存失败后提供可见的重试按钮并可恢复保存', async ({ page }) => {
  let patchCalls = 0
  await mockApi(page, async (route, body, segmentId) => {
    patchCalls += 1
    if (patchCalls === 1) {
      await route.fulfill({
        status: 500,
        json: { code: 'SAVE_FAILED', message: '模拟保存失败', retryable: true },
      })
      return
    }
    await route.fulfill({ json: successfulSegment(body, segmentId) })
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/projects/project-1')

  await page.getByLabel('字幕文本').fill('移动端需要重试的草稿')
  await expect(page.getByText('保存失败', { exact: true })).toBeVisible()
  const retry = page.getByRole('button', { name: '重试保存' })
  await expect(retry).toBeVisible()
  await retry.click()

  await expect.poll(() => patchCalls).toBe(2)
  await expect(page.getByText('已自动保存', { exact: true })).toBeVisible()
  await expect(page.getByLabel('字幕文本')).toHaveValue('移动端需要重试的草稿')
})

test('409 冲突保留本地草稿、显示冲突提示并阻止离开', async ({ page }) => {
  await mockApi(page, async (route) => {
    await route.fulfill({
      status: 409,
      json: {
        code: 'SEGMENT_VERSION_CONFLICT',
        message: '片段已被其他操作更新，请刷新后重试',
        retryable: false,
        details: { expected: 2, received: 1 },
      },
    })
  })

  await page.goto('/projects/project-1')
  const editor = page.getByLabel('字幕文本')
  await editor.fill('发生冲突也要保留的草稿')
  await page.getByRole('link', { name: '返回项目台' }).click()

  await expect(page).toHaveURL(/\/projects\/project-1$/)
  await expect(editor).toHaveValue('发生冲突也要保留的草稿')
  await expect(page.getByText('此片段已在其他会话更新，本地草稿尚未覆盖远端版本。')).toBeVisible()
  await expect(page.getByRole('button', { name: '放弃草稿并重新加载' })).toBeVisible()
})
