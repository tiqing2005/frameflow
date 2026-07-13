import type { Run } from './types'

type RunTokenFields = Pick<Run, 'input_tokens' | 'output_tokens' | 'total_tokens'>

function hasTokenValue(value: number | null | undefined): value is number {
  return value != null
}

export function runTokenTotal(run: RunTokenFields): number | null {
  if (hasTokenValue(run.total_tokens)) return run.total_tokens
  if (!hasTokenValue(run.input_tokens) && !hasTokenValue(run.output_tokens)) return null
  return (run.input_tokens ?? 0) + (run.output_tokens ?? 0)
}

export function totalRunTokens(runs: RunTokenFields[]): number | null {
  let hasTokens = false
  const total = runs.reduce((sum, run) => {
    const runTotal = runTokenTotal(run)
    if (runTotal == null) return sum
    hasTokens = true
    return sum + runTotal
  }, 0)
  return hasTokens ? total : null
}

export function formatRunTokenUsage(run: RunTokenFields): string {
  const total = runTokenTotal(run)
  if (total == null) return '未产生 Token'

  const input = hasTokenValue(run.input_tokens) ? run.input_tokens.toLocaleString() : '—'
  const output = hasTokenValue(run.output_tokens) ? run.output_tokens.toLocaleString() : '—'
  return `${input} 输入 · ${output} 输出 · ${total.toLocaleString()} 总计`
}
