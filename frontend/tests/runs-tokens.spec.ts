import { expect, test, type Page } from '@playwright/test'

interface RunFixture {
  id: string
  operation: string
  status: string
  degraded?: boolean
  input_tokens?: number | null
  output_tokens?: number | null
  total_tokens?: number | null
  created_at: string
}

async function mockRuns(page: Page, runs: RunFixture[]) {
  await page.route('**/api/v1/runs', async (route) => {
    await route.fulfill({ json: { items: runs, total: runs.length } })
  })
}

const createdAt = '2026-07-14T00:00:00Z'

test('Token 总计使用规范字段并忽略无 Token 的规则记录', async ({ page }) => {
  await mockRuns(page, [
    {
      id: 'ai-run-1',
      operation: 'AI 标签生成',
      status: 'succeeded',
      input_tokens: 120,
      output_tokens: 30,
      total_tokens: 150,
      created_at: createdAt,
    },
    {
      id: 'ai-run-2',
      operation: 'AI 片段分析',
      status: 'succeeded',
      input_tokens: 80,
      output_tokens: 20,
      total_tokens: 100,
      created_at: createdAt,
    },
    {
      id: 'rule-run',
      operation: '规则匹配',
      status: 'succeeded',
      degraded: true,
      created_at: createdAt,
    },
  ])

  await page.goto('/runs')

  const tokenMetric = page.locator('.metric-card').filter({ hasText: 'Token 用量' })
  await expect(tokenMetric.locator('strong')).toHaveText('250')

  await page.getByRole('button', { name: /AI 标签生成/ }).click()
  await expect(page.locator('.run-detail').getByText('120 输入 · 30 输出 · 150 总计')).toBeVisible()

  await page.getByRole('button', { name: /规则匹配/ }).click()
  await expect(page.locator('.run-detail').getByText('未产生 Token')).toBeVisible()
  await expect(page.locator('.run-detail')).not.toContainText('0 输入')
})

test('全部记录均无 Token 时总计显示破折号而不是 0', async ({ page }) => {
  await mockRuns(page, [
    {
      id: 'rule-run',
      operation: '确定性规则任务',
      status: 'succeeded',
      degraded: true,
      created_at: createdAt,
    },
  ])

  await page.goto('/runs')

  const tokenMetric = page.locator('.metric-card').filter({ hasText: 'Token 用量' })
  await expect(tokenMetric.locator('strong')).toHaveText('—')
})

test('API 明确返回的零 Token 保留为真实的 0', async ({ page }) => {
  await mockRuns(page, [
    {
      id: 'zero-token-run',
      operation: '零 Token 模型调用',
      status: 'succeeded',
      input_tokens: 0,
      output_tokens: 0,
      total_tokens: 0,
      created_at: createdAt,
    },
  ])

  await page.goto('/runs')

  const tokenMetric = page.locator('.metric-card').filter({ hasText: 'Token 用量' })
  await expect(tokenMetric.locator('strong')).toHaveText('0')
  await page.getByRole('button', { name: /零 Token 模型调用/ }).click()
  await expect(page.locator('.run-detail').getByText('0 输入 · 0 输出 · 0 总计')).toBeVisible()
})
