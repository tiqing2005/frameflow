import { expect, test, type Route } from '@playwright/test'

test('登录、会话恢复和退出形成完整闭环', async ({ page }) => {
  let authenticated = false
  let csrfToken: string | null = null
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v1/auth/session') {
      await route.fulfill({ json: {
        auth_enabled: true,
        configured: true,
        authenticated,
        user: authenticated ? { username: 'reviewer', display_name: '评审账号', role: 'admin' } : null,
        csrf_token: authenticated ? csrfToken : null,
      } })
      return
    }
    if (path === '/api/v1/auth/login') {
      const payload = request.postDataJSON()
      if (payload.username !== 'reviewer' || payload.password !== 'demo-password') {
        await route.fulfill({ status: 401, json: { code: 'INVALID_CREDENTIALS', message: '用户名或密码错误', retryable: false } })
        return
      }
      authenticated = true
      csrfToken = 'csrf-for-browser-test'
      await route.fulfill({ json: {
        auth_enabled: true,
        configured: true,
        authenticated: true,
        user: { username: 'reviewer', display_name: '评审账号', role: 'admin' },
        csrf_token: csrfToken,
      } })
      return
    }
    if (path === '/api/v1/auth/logout') {
      expect(request.headers()['x-csrf-token']).toBe(csrfToken)
      authenticated = false
      await route.fulfill({ json: { ok: true } })
      return
    }
    if (path === '/api/v1/dashboard') {
      await route.fulfill({ json: { metrics: { projects: 0, total_assets: 0, running_jobs: 0, failed_jobs: 0 }, recent_projects: [], recent_runs: [] } })
      return
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_MOCKED', message: path, retryable: false } })
  })

  await page.goto('/projects')
  await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible()
  await page.getByLabel('用户名').fill('reviewer')
  await page.getByLabel('密码', { exact: true }).fill('wrong')
  await page.getByRole('button', { name: '进入工作空间' }).click()
  await expect(page.getByText('用户名或密码错误')).toBeVisible()

  await page.getByLabel('密码', { exact: true }).fill('demo-password')
  await page.getByRole('button', { name: '进入工作空间' }).click()
  await expect(page.getByText('评审账号')).toBeVisible()
  await page.getByRole('button', { name: '退出登录' }).click()
  await expect(page.getByRole('heading', { name: '登录工作空间' })).toBeVisible()
})
