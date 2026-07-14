import type { Page, Route } from '@playwright/test'

const disabledSession = {
  auth_enabled: false,
  configured: false,
  setup_required: false,
  setup_available: false,
  authenticated: true,
  user: null,
  csrf_token: null,
}

export async function fulfillDisabledAuth(route: Route) {
  const request = route.request()
  if (request.method() !== 'GET' || new URL(request.url()).pathname !== '/api/v1/auth/session') {
    return false
  }
  await route.fulfill({ json: disabledSession })
  return true
}

export async function installDisabledAuth(page: Page) {
  await page.route('**/api/v1/auth/session', (route) => route.fulfill({ json: disabledSession }))
}
