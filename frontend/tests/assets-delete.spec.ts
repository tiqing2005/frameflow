import { expect, test, type Page, type Route } from '@playwright/test'

const UPLOADED_ASSET_ID = 'uploaded-asset-delete'

const uploadedAsset = {
  id: UPLOADED_ASSET_ID,
  name: '待删除的测试素材',
  kind: 'image' as const,
  url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
  file_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
  thumbnail_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=',
  mime_type: 'image/gif',
  size_bytes: 43,
  width: 1,
  height: 1,
  tags: ['测试'],
  keywords: ['删除'],
  active: true,
  is_seed: false,
  created_at: '2026-07-14T00:00:00Z',
}

async function mockAssetApi(page: Page, deletedRequests: string[]) {
  let deleted = false
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname

    if (request.method() === 'GET' && path === '/api/v1/auth/session') {
      await route.fulfill({
        json: {
          auth_enabled: false,
          configured: false,
          authenticated: true,
          user: null,
          csrf_token: null,
        },
      })
      return
    }
    if (request.method() === 'GET' && path === '/api/v1/assets') {
      const items = deleted ? [] : [uploadedAsset]
      await route.fulfill({ json: { items, total: items.length } })
      return
    }
    if (path === `/api/v1/assets/${UPLOADED_ASSET_ID}`) {
      if (request.method() === 'GET') {
        await route.fulfill({ json: uploadedAsset })
        return
      }
      if (request.method() !== 'DELETE') {
        await route.fulfill({
          status: 405,
          json: { code: 'HTTP_ERROR', message: 'Method Not Allowed', retryable: false },
        })
        return
      }
      deletedRequests.push(request.method())
      deleted = true
      await route.fulfill({ status: 204, body: '' })
      return
    }
    await route.fulfill({
      status: 404,
      json: { code: 'NOT_MOCKED', message: `${request.method()} ${path}`, retryable: false },
    })
  })
}

test('用户上传素材可从详情抽屉删除并同步更新素材总数', async ({ page }) => {
  const deletedRequests: string[] = []
  await mockAssetApi(page, deletedRequests)
  page.on('dialog', (dialog) => dialog.accept())

  await page.goto('/assets')
  await expect(page.getByRole('heading', { name: '待删除的测试素材' })).toBeVisible()
  await expect(page.getByText('1 项素材')).toBeVisible()

  await page.getByRole('button', { name: /待删除的测试素材/ }).click()
  await expect(page.getByRole('dialog', { name: '素材详情' })).toBeVisible()
  await page.getByRole('button', { name: '删除素材' }).click()

  await expect.poll(() => deletedRequests).toEqual(['DELETE'])
  await expect(page.getByRole('dialog', { name: '素材详情' })).toBeHidden()
  await expect(page.getByText('0 项素材')).toBeVisible()
  await expect(page.getByRole('heading', { name: '待删除的测试素材' })).toHaveCount(0)
  await expect(page.getByText('素材已删除')).toBeVisible()
})
