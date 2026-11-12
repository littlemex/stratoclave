import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { PoolBudgetCard } from './PoolBudgetCard'
import { type PoolBudget } from '@/lib/api'
import i18n from '@/lib/i18n'

/**
 * The pool's mode sentence is composed by the console, in the reader's language.
 *
 * It used to render the backend's `mode_sentence`, which is English prose for API
 * callers, so the Japanese tenant screen showed an English paragraph, and told an
 * operator with a button for it to "send {"follow_seats": true} to this endpoint".
 * A sentinel `mode_sentence` below proves the card no longer reads that field.
 */

const SENTINEL = 'BACKEND-ENGLISH-SENTENCE-MUST-NOT-RENDER'

function pool(overrides: Partial<PoolBudget> = {}): PoolBudget {
  return {
    tenant_id: 'acme',
    period: '2026-11',
    status: 'active',
    pool_limit_microusd: 500_000_000,
    pool_reserved_microusd: 0,
    pool_settled_microusd: 0,
    remaining_microusd: 500_000_000,
    over_ceiling_microusd: 0,
    pool_limit_usd_cents: 50_000,
    remaining_usd_cents: 50_000,
    mode_sentence: SENTINEL,
    seat_tracked: false,
    seat_count: 3,
    seat_rate_microusd: 200_000_000,
    seat_entitlement_microusd: 600_000_000,
    manual_limit_microusd: 500_000_000,
    pool_granted_microusd: 0,
    baseline_microusd: 500_000_000,
    entitlement_exceeds_figure: true,
    resume_action: 'follow_seats',
    grant_cap_microusd: null,
    effective_grant_cap_microusd: 500_000_000,
    grant_cap_is_derived: true,
    remaining_grant_cap_microusd: 500_000_000,
    ...overrides,
  }
}

function renderCard(p: PoolBudget) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <PoolBudgetCard
        tenantId="acme"
        pool={p}
        isLoading={false}
        onChanged={() => {}}
        poolApi={{ setPoolBudget: async () => p }}
      />
    </QueryClientProvider>,
  )
  return screen.getByTestId('pool-mode-sentence').textContent ?? ''
}

afterEach(async () => {
  await i18n.changeLanguage('en')
})

describe('the pool mode sentence is localized by the console', () => {
  it('a manual pool, in English, names the figure and the entitlement', async () => {
    await i18n.changeLanguage('en')
    const text = renderCard(pool())
    expect(text).not.toContain(SENTINEL)
    expect(text).toContain('$500.00')
    expect(text).toContain('$600.00')
    expect(text).toContain('Follow the seat count again')
    expect(text).not.toContain('follow_seats')
  })

  it('a manual pool, in Japanese, is Japanese and names the same figures', async () => {
    await i18n.changeLanguage('ja')
    const text = renderCard(pool())
    expect(text).not.toContain(SENTINEL)
    expect(text).not.toMatch(/This pool|a figure set by hand/)
    expect(text).toMatch(/手動/)
    expect(text).toContain('$500.00')
    expect(text).toContain('$600.00')
    expect(text).toContain(i18n.t('admin_tenant_detail.pool.follow_seats_button'))
  })

  it('a seat-tracked pool names the seat count and the monthly entitlement', async () => {
    for (const lang of ['en', 'ja']) {
      await i18n.changeLanguage(lang)
      const text = renderCard(
        pool({
          seat_tracked: true,
          manual_limit_microusd: null,
          resume_action: null,
          entitlement_exceeds_figure: false,
          pool_limit_microusd: 600_000_000,
          baseline_microusd: 600_000_000,
        }),
      )
      expect(text).not.toContain(SENTINEL)
      expect(text).toContain('3')
      expect(text).toContain('$600.00')
      document.body.innerHTML = ''
    }
  })
})

describe('no console screen renders backend prose meant for API callers', () => {
  it('nothing outside tests reads `.mode_sentence`', () => {
    const root = join(__dirname, '..', '..')
    const offenders: string[] = []
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const p = join(dir, name)
        if (statSync(p).isDirectory()) walk(p)
        else if (
          /\.tsx?$/.test(name) &&
          !/\.test\.tsx?$/.test(name) &&
          /\.mode_sentence\b/.test(readFileSync(p, 'utf8'))
        )
          offenders.push(p)
      }
    }
    walk(root)
    expect(offenders).toEqual([])
  })
})
