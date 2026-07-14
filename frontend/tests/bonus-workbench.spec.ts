import { expect, test, type Page, type Route } from '@playwright/test'
import { fulfillDisabledAuth } from './mock-auth'

const PROJECT_ID = 'project-bonus'

const asset = (id: string, name: string) => ({
  id,
  name,
  kind: 'image' as const,
  file_url: `/media/seed/${id}.jpg`,
  thumbnail_url: `/media/seed/${id}.jpg`,
  mime_type: 'image/jpeg',
  tags: ['商业', '演示'],
  keywords: [name],
})

const primaryAsset = asset('asset-primary', '默认城市画面')
const replacementAsset = asset('asset-replacement', '团队协作特写')

const initialSegments = [
  {
    id: 'segment-1',
    project_id: PROJECT_ID,
    position: 0,
    text: '第一段字幕',
    topic: '开场',
    keywords: ['开场'],
    start_ms: 0,
    end_ms: 2000,
    version: 1,
    recommendations: [],
    selection: { segment_id: 'segment-1', asset_id: primaryAsset.id, source: 'auto', asset: primaryAsset },
  },
  {
    id: 'segment-2',
    project_id: PROJECT_ID,
    position: 1,
    text: '第二段字幕',
    topic: '协作',
    keywords: ['协作'],
    start_ms: 2000,
    end_ms: 4000,
    version: 1,
    recommendations: [],
    selection: { segment_id: 'segment-2', asset_id: primaryAsset.id, source: 'auto', asset: primaryAsset },
  },
  {
    id: 'segment-3',
    project_id: PROJECT_ID,
    position: 2,
    text: '第三段字幕',
    topic: '收束',
    keywords: ['收束'],
    start_ms: 4000,
    end_ms: 6000,
    version: 1,
    recommendations: [],
    selection: { segment_id: 'segment-3', asset_id: primaryAsset.id, source: 'auto', asset: primaryAsset },
  },
]

type SegmentState = typeof initialSegments[number]

interface MockState {
  segments: SegmentState[]
  orderBodies: Array<{ segment_ids: string[] }>
  selectionBodies: Array<{ asset_id: string }>
  failSelection?: boolean
  holdSelection?: boolean
  releaseSelection?: () => void
}

function timeline(state: MockState) {
  return {
    project_id: PROJECT_ID,
    input_hash: `timeline-${state.segments.map((item) => item.id).join('-')}`,
    segment_count: state.segments.length,
    duration_ms: 6000,
    items: state.segments.map((segment) => ({
      segment_id: segment.id,
      position: segment.position,
      text: segment.text,
      topic: segment.topic,
      start_ms: segment.start_ms,
      end_ms: segment.end_ms,
      duration_ms: 2000,
      asset: segment.selection.asset,
    })),
  }
}

async function mockApi(page: Page, state: MockState) {
  await page.route('**/api/v1/**', async (route: Route) => {
    if (await fulfillDisabledAuth(route)) return
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname

    if (request.method() === 'GET' && path === `/api/v1/projects/${PROJECT_ID}`) {
      await route.fulfill({
        json: {
          project: {
            id: PROJECT_ID,
            title: '前端加分项验收',
            status: 'ready',
            input_kind: 'text',
            created_at: '2026-07-14T00:00:00Z',
            updated_at: '2026-07-14T00:00:00Z',
          },
          current_job: null,
          source: { text: state.segments.map((item) => item.text).join('\n') },
          segments: state.segments,
        },
      })
      return
    }

    if (request.method() === 'GET' && path === `/api/v1/projects/${PROJECT_ID}/preview`) {
      await route.fulfill({ json: { preview: null, timeline: timeline(state) } })
      return
    }

    if (request.method() === 'GET' && path === '/api/v1/assets') {
      await route.fulfill({ json: { items: [replacementAsset], total: 1 } })
      return
    }

    if (request.method() === 'PUT' && path === `/api/v1/projects/${PROJECT_ID}/segments/order`) {
      const body = request.postDataJSON() as { segment_ids: string[] }
      state.orderBodies.push(body)
      state.segments = body.segment_ids.map((id, position) => ({
        ...state.segments.find((item) => item.id === id)!,
        position,
      }))
      await route.fulfill({ json: { segments: state.segments } })
      return
    }

    const selectionMatch = path.match(/^\/api\/v1\/segments\/([^/]+)\/selection$/)
    if (request.method() === 'PUT' && selectionMatch) {
      const body = request.postDataJSON() as { asset_id: string }
      state.selectionBodies.push(body)
      if (state.holdSelection) {
        await new Promise<void>((resolve) => { state.releaseSelection = resolve })
      }
      if (state.failSelection) {
        await route.fulfill({
          status: 500,
          json: { code: 'SELECTION_FAILED', message: '模拟替换失败', retryable: true },
        })
        return
      }
      const segment = state.segments.find((item) => item.id === selectionMatch[1])!
      segment.selection = {
        segment_id: segment.id,
        asset_id: replacementAsset.id,
        source: 'manual',
        asset: replacementAsset,
      }
      await route.fulfill({ json: { ...segment.selection, selection: segment.selection } })
      return
    }

    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: path, retryable: false } })
  })
}

function createState(overrides: Partial<MockState> = {}): MockState {
  return {
    segments: structuredClone(initialSegments),
    orderBodies: [],
    selectionBodies: [],
    ...overrides,
  }
}

test('拖动字幕片段会立即更新顺序并持久化完整片段 ID 列表', async ({ page }) => {
  const state = createState()
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  const first = page.locator('[data-segment-id="segment-1"]')
  const third = page.locator('[data-segment-id="segment-3"]')
  await first.dragTo(third)

  await expect.poll(() => state.orderBodies).toEqual([
    { segment_ids: ['segment-2', 'segment-3', 'segment-1'] },
  ])
  await expect(page.locator('.segment-item').nth(0)).toHaveAttribute('data-segment-id', 'segment-2')
  await expect(page.locator('.segment-item').nth(2)).toHaveAttribute('data-segment-id', 'segment-1')
  await expect(page.getByText('片段顺序已保存')).toBeVisible()
})

test('搜索素材后可一键快速替换，并在接口完成前先更新画面预览', async ({ page }) => {
  const state = createState({ holdSelection: true })
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  await page.getByPlaceholder('搜索素材库并替换…').fill('团队')
  const replacement = page.locator('.replacement-card').filter({ hasText: replacementAsset.name })
  await expect(replacement).toBeVisible()
  await replacement.click()

  await expect.poll(() => state.selectionBodies).toEqual([{ asset_id: replacementAsset.id }])
  await expect(page.locator('.preview-caption')).toContainText(replacementAsset.name)
  await expect(replacement).toBeDisabled()

  state.releaseSelection?.()
  await expect(page.getByText(`已将「${replacementAsset.name}」设为当前画面`)).toBeVisible()
  await expect(page.getByPlaceholder('搜索素材库并替换…')).toHaveValue('')
  await expect(page.locator('.preview-caption')).toContainText('人工选择')
})

test('素材快速替换失败时回滚原画面并保留搜索结果以便重试', async ({ page }) => {
  const state = createState({ failSelection: true })
  await mockApi(page, state)
  await page.goto(`/projects/${PROJECT_ID}`)

  await page.getByPlaceholder('搜索素材库并替换…').fill('团队')
  const replacement = page.locator('.replacement-card').filter({ hasText: replacementAsset.name })
  await expect(replacement).toBeVisible()
  await replacement.click()

  await expect(page.getByText('模拟替换失败')).toBeVisible()
  await expect(page.locator('.preview-caption')).toContainText(primaryAsset.name)
  await expect(page.getByPlaceholder('搜索素材库并替换…')).toHaveValue('团队')
  await expect(replacement).toBeEnabled()
})
