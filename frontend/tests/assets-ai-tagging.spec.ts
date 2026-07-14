import { expect, test, type Page, type Route } from '@playwright/test'
import type { Asset } from '../src/types'
import { fulfillDisabledAuth } from './mock-auth'

const SEED_ASSET_ID = 'seed-vision-retag'
const pixel = 'data:image/gif;base64,R0lGODlhAQABAAAAACw='

const seedAsset: Asset = {
  id: SEED_ASSET_ID,
  name: '城市夜景种子素材',
  kind: 'image' as const,
  url: pixel,
  file_url: pixel,
  thumbnail_url: pixel,
  mime_type: 'image/gif',
  width: 1,
  height: 1,
  tags: ['旧标签'],
  keywords: ['旧关键词'],
  active: true,
  is_seed: true,
  tagging_status: 'idle',
  tagging_source: null,
  tagging_requested_at: null,
  tagging_started_at: null,
  tagging_finished_at: null,
  created_at: '2026-07-14T00:00:00Z',
}

async function mockTaggingApi(page: Page, calls: string[]) {
  let current: Asset = { ...seedAsset }
  let retagAccepted = false
  let activeReads = 0

  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname

    if (await fulfillDisabledAuth(route)) return
    if (request.method() === 'GET' && path === '/api/v1/assets') {
      await route.fulfill({ json: { items: [current], total: 1 } })
      return
    }
    if (request.method() === 'POST' && path === `/api/v1/assets/${SEED_ASSET_ID}/retag`) {
      calls.push('POST retag')
      retagAccepted = true
      current = {
        ...current,
        tagging_status: 'queued',
        tagging_source: null,
        tagging_requested_at: '2026-07-14T01:00:00Z',
      }
      await route.fulfill({ status: 202, json: current })
      return
    }
    if (request.method() === 'GET' && path === `/api/v1/assets/${SEED_ASSET_ID}`) {
      calls.push('GET detail')
      if (!retagAccepted) {
        await route.fulfill({ json: current })
        return
      }
      activeReads += 1
      if (activeReads === 1) {
        await route.fulfill({
          status: 503,
          json: { code: 'TEMPORARY_UNAVAILABLE', message: '暂时无法读取状态', retryable: true },
        })
        return
      }
      if (activeReads === 2) {
        current = {
          ...current,
          tagging_status: 'running',
          tagging_started_at: '2026-07-14T01:00:01Z',
        }
      } else {
        current = {
          ...current,
          tags: ['城市', '夜景'],
          keywords: ['摩天楼', '灯光'],
          tagging_status: 'degraded',
          tagging_source: 'text_llm',
          tagging_finished_at: '2026-07-14T01:00:03Z',
        }
      }
      await route.fulfill({ json: current })
      return
    }
    await route.fulfill({
      status: 404,
      json: { code: 'NOT_MOCKED', message: `${request.method()} ${path}`, retryable: false },
    })
  })
}

test('seed 素材可重新生成标签并在轮询失败后恢复到诚实的降级状态', async ({ page }) => {
  const calls: string[] = []
  await mockTaggingApi(page, calls)
  page.on('dialog', (dialog) => dialog.accept())

  await page.goto('/assets')
  await page.getByRole('button', { name: /城市夜景种子素材/ }).click()

  const drawer = page.getByRole('dialog', { name: '素材详情' })
  await expect(drawer.getByText('尚未运行画面识别')).toBeVisible()
  await expect(drawer.getByText(/第三方模型网关/)).toBeVisible()
  await expect(drawer.getByRole('button', { name: '删除素材' })).toHaveCount(0)

  await drawer.getByRole('button', { name: 'AI 重新生成标签' }).click()
  await expect.poll(() => calls.filter((call) => call === 'POST retag').length).toBe(1)
  await expect(drawer.getByRole('button', { name: /等待识别|正在识别/ })).toBeDisabled()
  await expect(drawer.getByRole('button', { name: '编辑名称与标签' })).toBeDisabled()

  await expect(drawer.getByText('文本 AI 降级完成')).toBeVisible({ timeout: 10_000 })
  await expect(drawer.getByText('城市', { exact: true })).toBeVisible()
  await expect(drawer.getByText('摩天楼、灯光')).toBeVisible()
  await expect(drawer.getByRole('button', { name: 'AI 重新生成标签' })).toBeEnabled()

  await drawer.getByRole('button', { name: '编辑名称与标签' }).click()
  await expect(drawer.getByLabel('主题标签')).toHaveValue('城市，夜景')
  await expect(drawer.getByLabel('关键词')).toHaveValue('摩天楼，灯光')
})

test('上传弹窗说明留空后台识别及第三方画面传输', async ({ page }) => {
  await page.route('**/api/v1/**', async (route) => {
    if (await fulfillDisabledAuth(route)) return
    if (route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/v1/assets') {
      await route.fulfill({ json: { items: [], total: 0 } })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: 'not mocked', retryable: false } })
  })

  await page.goto('/assets')
  await page.getByRole('button', { name: '上传素材' }).click()
  const dialog = page.getByRole('dialog', { name: '上传新素材' })
  await expect(dialog.getByText('标签或关键词留空时，上传后将由 AI 自动补充')).toBeVisible()
  await expect(dialog.getByText(/一张归一化画面/)).toBeVisible()
  await expect(dialog.getByText(/敏感素材/)).toBeVisible()
})
