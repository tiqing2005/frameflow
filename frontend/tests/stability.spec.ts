import { expect, test, type Route } from '@playwright/test'

const projectDetail = (id: string, title: string) => ({
  project: {
    id,
    title,
    status: 'ready',
    input_kind: 'text',
    created_at: '2026-07-14T00:00:00Z',
    updated_at: '2026-07-14T00:00:00Z',
  },
  current_job: null,
  source: { text: `${title}的原始字幕` },
  segments: [
    {
      id: `segment-${id}`,
      project_id: id,
      position: 0,
      text: `${title}的字幕片段`,
      topic: title,
      keywords: [title],
      start_ms: 0,
      end_ms: 2000,
      version: 1,
      recommendations: [],
      selection: null,
    },
  ],
})

const asset = (id: string, name: string, kind: 'image' | 'video' = 'image') => ({
  id,
  name,
  kind,
  url: `/media/${id}.${kind === 'video' ? 'mp4' : 'jpg'}`,
  file_url: `/media/${id}.${kind === 'video' ? 'mp4' : 'jpg'}`,
  thumbnail_url: `/media/${id}-poster.jpg`,
  mime_type: kind === 'video' ? 'video/mp4' : 'image/jpeg',
  tags: ['测试'],
  keywords: [name],
  width: 1280,
  height: 720,
  created_at: '2026-07-14T00:00:00Z',
})

async function mockDashboard(route: Route) {
  if (route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/v1/dashboard') {
    await route.fulfill({
      json: {
        metrics: { projects: 2, total_assets: 3, running_jobs: 0, failed_jobs: 0 },
        recent_projects: [],
        recent_runs: [],
      },
    })
    return true
  }
  return false
}

test('快速切换项目时，旧项目的慢响应不会覆盖当前工作台', async ({ page }) => {
  await page.route('**/api/v1/**', async (route) => {
    if (await mockDashboard(route)) return
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/projects/project-a') {
      await new Promise((resolve) => setTimeout(resolve, 700))
      await route.fulfill({ json: projectDetail('project-a', '慢响应项目 A') })
      return
    }
    if (path === '/api/v1/projects/project-b') {
      await route.fulfill({ json: projectDetail('project-b', '当前项目 B') })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: path, retryable: false } })
  })

  await page.goto('/projects/project-a', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(80)
  await page.evaluate(() => {
    window.history.pushState({}, '', '/projects/project-b')
    window.dispatchEvent(new Event('frameflow:route-change'))
  })

  await expect(page.getByRole('heading', { name: '当前项目 B' })).toBeVisible()
  await page.waitForTimeout(850)
  await expect(page.getByRole('heading', { name: '当前项目 B' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '慢响应项目 A' })).toHaveCount(0)
})

test('素材搜索只接纳最新结果，视频使用文件地址播放并将缩略图作为海报', async ({ page }) => {
  await page.route('**/api/v1/**', async (route) => {
    if (await mockDashboard(route)) return
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() === 'GET' && url.pathname === '/api/v1/assets') {
      const query = url.searchParams.get('q') || ''
      if (query === 'slow') {
        await new Promise((resolve) => setTimeout(resolve, 700))
        await route.fulfill({ json: { items: [asset('slow-result', '陈旧慢结果')], total: 1 } })
        return
      }
      if (query === 'fast') {
        await route.fulfill({ json: { items: [asset('fast-video', '最新视频结果', 'video')], total: 1 } })
        return
      }
      await route.fulfill({ json: { items: [], total: 0 } })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: url.pathname, retryable: false } })
  })

  await page.goto('/assets')
  const search = page.getByPlaceholder('按名称、标签或关键词搜索素材…')
  await search.fill('slow')
  await page.waitForTimeout(380)
  await search.fill('fast')

  await expect(page.getByRole('heading', { name: '最新视频结果' })).toBeVisible()
  const video = page.locator('.asset-card video')
  await expect(video).toHaveAttribute('src', '/media/fast-video.mp4')
  await expect(video).toHaveAttribute('poster', '/media/fast-video-poster.jpg')
  await page.waitForTimeout(800)
  await expect(page.getByRole('heading', { name: '陈旧慢结果' })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '最新视频结果' })).toBeVisible()
})

test('处理中任务的已用时会逐秒更新，而不是只在轮询响应时变化', async ({ page }) => {
  const startedAt = new Date(Date.now() - 3000).toISOString()
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/projects/project-running') {
      await route.fulfill({
        json: {
          ...projectDetail('project-running', '运行中的项目'),
          project: { ...projectDetail('project-running', '运行中的项目').project, status: 'processing' },
          current_job: { id: 'job-running', status: 'running' },
        },
      })
      return
    }
    if (path === '/api/v1/jobs/job-running') {
      await route.fulfill({
        json: {
          job: {
            id: 'job-running',
            project_id: 'project-running',
            status: 'running',
            stage: 'matching',
            progress: 80,
            attempt: 1,
            max_attempts: 3,
            started_at: startedAt,
            created_at: startedAt,
          },
          events: [],
        },
      })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: path, retryable: false } })
  })

  await page.goto('/projects/project-running/processing?job=job-running')
  const elapsed = page.locator('.progress-meta span').first()
  await expect(elapsed).toContainText('已用时')
  const firstValue = await elapsed.textContent()
  await expect.poll(() => elapsed.textContent(), { timeout: 2500 }).not.toBe(firstValue)
})

test('处理页轮询失败后使用退避，不会按固定高频持续请求', async ({ page }) => {
  const requestTimes: number[] = []
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/projects/project-offline') {
      requestTimes.push(Date.now())
      await route.fulfill({ status: 503, json: { code: 'TEMPORARY_UNAVAILABLE', message: '服务暂不可用', retryable: true } })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: path, retryable: false } })
  })

  await page.goto('/projects/project-offline/processing')
  await expect(page.getByText('服务暂不可用')).toBeVisible()
  // 开发模式 StrictMode 会立即重放一次 Effect；校验初始重放之后的真正轮询间隔。
  await expect.poll(() => requestTimes.length, { timeout: 6000 }).toBeGreaterThanOrEqual(3)
  expect(requestTimes[2] - requestTimes[1]).toBeGreaterThanOrEqual(2500)
})
