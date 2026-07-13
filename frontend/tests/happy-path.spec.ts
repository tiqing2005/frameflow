import { expect, test, type Page, type Route } from '@playwright/test'

/**
 * Full happy-path contract test covering the interview acceptance flow
 * (题面验收 1–6): paste subtitle -> job succeeded -> workbench shows >=3
 * candidates with match reasons -> reload persists the selection.
 *
 * All API responses are mocked, so this is deterministic and does not need a
 * real backend. It mirrors the mock pattern in workbench-save.spec.ts.
 */

const PROJECT_ID = 'proj-happy'
const JOB_ID = 'job-happy'
const SEGMENT_ID = 'seg-happy'
const ASSET_AUTO = 'asset-auto'

const recommendation = (assetId: string, rank: number, filler = false) => ({
  id: `rec-${assetId}`,
  segment_id: SEGMENT_ID,
  asset_id: assetId,
  rank,
  total_score: 0.5 - rank * 0.1,
  tfidf_score: 0.4 - rank * 0.05,
  keyword_score: 0.6,
  tag_score: 0.3,
  matched_terms: ['人工智能', '效率'],
  explanation: filler
    ? '相关性较弱，作为多样性兜底候选。'
    : '命中关键词「人工智能、效率」；字幕与素材描述存在字符语义相似；主题与素材标签一致。',
  is_diversity_filler: filler,
})

const asset = (id: string, name: string) => ({
  id,
  name,
  kind: 'image' as const,
  url: `/media/seed/${id}.jpg`,
  file_url: `/media/seed/${id}.jpg`,
  thumbnail_url: `/media/seed/${id}.jpg`,
  mime_type: 'image/jpeg',
  size_bytes: 1024,
  tags: ['科技'],
  keywords: ['人工智能', '效率'],
  active: true,
  is_seed: true,
  created_at: '2026-07-14T00:00:00Z',
})

const buildProjectDetail = (selectionAssetId: string | null) => ({
  project: {
    id: PROJECT_ID,
    title: '完整闭环演示',
    status: 'ready',
    input_kind: 'text',
    created_at: '2026-07-14T00:00:00Z',
    updated_at: '2026-07-14T00:00:00Z',
  },
  current_job: null,
  source: { text: '人工智能正在提升办公效率。' },
  segments: [
    {
      id: SEGMENT_ID,
      project_id: PROJECT_ID,
      position: 0,
      text: '人工智能正在提升办公效率。',
      topic: '科技',
      keywords: ['人工智能', '效率'],
      start_ms: 0,
      end_ms: 2000,
      version: 1,
      // Embed a resolved asset per recommendation so the workbench can render
      // candidate cards. This matches serializers.recommendation_dict shape.
      recommendations: [
        { ...recommendation(ASSET_AUTO, 1), asset: asset(ASSET_AUTO, '自动匹配素材') },
        { ...recommendation('asset-other-1', 2), asset: asset('asset-other-1', '候选素材二') },
        { ...recommendation('asset-other-2', 3), asset: asset('asset-other-2', '候选素材三') },
      ],
      selection: selectionAssetId
        ? {
            segment_id: SEGMENT_ID,
            asset_id: selectionAssetId,
            source: 'manual' as const,
            asset: asset(selectionAssetId, '已选素材'),
          }
        : null,
    },
  ],
})

let jobStage = 0
const STAGES = [
  { stage: 'validating', progress: 4, status: 'running' },
  { stage: 'segmenting', progress: 48, status: 'running' },
  { stage: 'matching', progress: 80, status: 'running' },
  { stage: 'completed', progress: 100, status: 'succeeded' },
]

async function mockApi(page: Page, selectionState: { assetId: string | null; requests: string[] }) {
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname

    if (request.method() === 'GET' && path === '/api/v1/dashboard') {
      await route.fulfill({
        json: {
          metrics: { projects: 1, total_assets: 12, running_jobs: 0, failed_jobs: 0 },
          recent_projects: [],
          recent_runs: [],
        },
      })
      return
    }
    if (request.method() === 'POST' && path === '/api/v1/projects/text') {
      jobStage = 0
      await route.fulfill({
        status: 202,
        json: {
          project: { id: PROJECT_ID, title: '完整闭环演示', status: 'queued', input_kind: 'text', created_at: '2026-07-14T00:00:00Z', updated_at: '2026-07-14T00:00:00Z' },
          job: { id: JOB_ID, project_id: PROJECT_ID, status: 'queued', stage: 'validating', progress: 0, attempt: 1, max_attempts: 3, created_at: '2026-07-14T00:00:00Z' },
          idempotent_replay: false,
        },
      })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/projects/${PROJECT_ID}`) {
      await route.fulfill({ json: buildProjectDetail(selectionState.assetId) })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/jobs/${JOB_ID}`) {
      const stage = STAGES[Math.min(jobStage, STAGES.length - 1)]
      jobStage = Math.min(jobStage + 1, STAGES.length - 1)
      await route.fulfill({
        json: {
          job: {
            id: JOB_ID,
            project_id: PROJECT_ID,
            status: stage.status,
            stage: stage.stage,
            progress: stage.progress,
            attempt: 1,
            max_attempts: 3,
            created_at: '2026-07-14T00:00:00Z',
          },
          events: [
            { id: 'e1', job_id: JOB_ID, stage: stage.stage, progress: stage.progress, message: '阶段推进', level: 'info', created_at: '2026-07-14T00:00:00Z' },
          ],
        },
      })
      return
    }
    if (request.method() === 'GET' && path === '/api/v1/assets') {
      await route.fulfill({
        json: {
          items: [
            asset(ASSET_AUTO, '自动匹配素材'),
            asset('asset-manual', '手动替换素材'),
            asset('asset-other-1', '其他素材一'),
            asset('asset-other-2', '其他素材二'),
          ],
          total: 4,
        },
      })
      return
    }
    if (request.method() === 'PUT' && path === `/api/v1/segments/${SEGMENT_ID}/selection`) {
      const body = request.postDataJSON() as { asset_id: string }
      selectionState.requests.push(body.asset_id)
      selectionState.assetId = body.asset_id
      await route.fulfill({
        json: {
          segment_id: SEGMENT_ID,
          asset_id: body.asset_id,
          source: 'manual',
          asset: asset(body.asset_id, '已选素材'),
          selection: { segment_id: SEGMENT_ID, asset_id: body.asset_id, source: 'manual', asset: asset(body.asset_id, '已选素材') },
        },
      })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: path, retryable: false } })
  })
}

test('完整闭环：创建到成功，工作台展示候选与匹配理由，刷新后选择仍存在', async ({ page }) => {
  test.setTimeout(60000)
  const selectionState: { assetId: string | null; requests: string[] } = { assetId: null, requests: [] }
  await mockApi(page, selectionState)

  // 1. NewProject 页粘贴字幕并提交（文案需 ≥ 20 字以通过前端校验）
  await page.goto('/projects/new', { waitUntil: 'domcontentloaded' })
  await page.locator('#project-title').fill('完整闭环演示')
  await page.locator('#project-text').fill('人工智能正在快速提升我们的日常办公效率，让重复性工作自动化。')
  await page.getByRole('button', { name: /创建并开始匹配/ }).click()

  // 2. 进入 Processing 页，轮询直到 succeeded
  await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_ID}/processing`))
  await expect(page.getByText('100%')).toBeVisible({ timeout: 15000 })
  // 点击"进入三栏工作台"按钮进入工作台
  await page.getByRole('button', { name: /进入三栏工作台/ }).click()

  // 3. 进入工作台，断言每片段 >=3 候选、匹配理由、命中词
  await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT_ID}`))
  const cards = page.locator('.candidate-card')
  await expect(cards.nth(0)).toBeVisible({ timeout: 10000 })
  await expect(cards).toHaveCount(3) // 三个候选满足"至少 3 个"
  // 匹配理由（命中词）应出现
  await expect(page.getByText('人工智能').first()).toBeVisible()

  // 4. 选择非默认候选，断言真实发出持久化请求
  await cards.nth(1).getByRole('button', { name: '采用此画面' }).click()
  await expect.poll(() => selectionState.requests).toEqual(['asset-other-1'])
  await expect(cards.nth(1)).toHaveClass(/selected/)
  await expect(cards.nth(1).getByRole('button', { name: '当前使用' })).toBeVisible()

  // 5. 重新加载后 mock 按服务端持久化状态返回选择，断言仍是同一候选
  await page.reload()
  await expect(cards.nth(0)).toBeVisible({ timeout: 10000 })
  await expect(cards).toHaveCount(3)
  await expect(cards.nth(1)).toHaveClass(/selected/)
  await expect(cards.nth(1).getByRole('button', { name: '当前使用' })).toBeVisible()
  expect(selectionState.assetId).toBe('asset-other-1')
})
