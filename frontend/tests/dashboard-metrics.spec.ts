import { expect, test } from '@playwright/test'
import { fulfillDisabledAuth } from './mock-auth'

test('仪表盘分别展示执行中、排队中和失败任务', async ({ page }) => {
  await page.route('**/api/v1/**', async (route) => {
    if (await fulfillDisabledAuth(route)) return
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET' && path === '/api/v1/dashboard') {
      await route.fulfill({
        json: {
          metrics: {
            projects: 8,
            ready_projects: 3,
            total_assets: 30,
            queued_jobs: 4,
            running_jobs: 2,
            failed_jobs: 1,
          },
          recent_projects: [],
          recent_runs: [],
        },
      })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: path } })
  })

  await page.goto('/projects')

  const metric = (label: string) => page.locator('.metric-card').filter({ hasText: label })
  await expect(metric('执行中').locator('strong')).toHaveText('2')
  await expect(metric('排队中').locator('strong')).toHaveText('4')
  await expect(metric('失败').locator('strong')).toHaveText('1')
  await expect(page.getByText('待处理', { exact: true })).toHaveCount(0)
})
