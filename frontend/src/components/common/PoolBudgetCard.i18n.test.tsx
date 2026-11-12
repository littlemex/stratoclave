import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
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
 * A sentinel `mode_sentence` below proves the card no longer reads that field, and
 * every money figure in the fixtures is distinct, so printing the wrong one fails.
 */

const SENTINEL = 'BACKEND-ENGLISH-SENTENCE-MUST-NOT-RENDER'
const label = () => i18n.t('admin_tenant_detail.pool.follow_seats_button')

// manual $500, entitlement $600, granted $50, limit $550: four different numbers.
function manualPool(overrides: Partial<PoolBudget> = {}): PoolBudget {
  return {
    tenant_id: 'acme',
    period: '2026-11',
    status: 'active',
    pool_limit_microusd: 550_000_000,
    pool_reserved_microusd: 0,
    pool_settled_microusd: 0,
    remaining_microusd: 550_000_000,
    over_ceiling_microusd: 0,
    pool_limit_usd_cents: 55_000,
    remaining_usd_cents: 55_000,
    mode_sentence: SENTINEL,
    seat_tracked: false,
    seat_count: 3,
    seat_rate_microusd: 200_000_000,
    seat_entitlement_microusd: 600_000_000,
    manual_limit_microusd: 500_000_000,
    pool_granted_microusd: 50_000_000,
    baseline_microusd: 500_000_000,
    entitlement_exceeds_figure: true,
    resume_action: 'follow_seats',
    grant_cap_microusd: null,
    effective_grant_cap_microusd: 500_000_000,
    grant_cap_is_derived: true,
    remaining_grant_cap_microusd: 450_000_000,
    ...overrides,
  }
}

// 3 seats entitle $600, granted $50, limit $650.
function seatPool(): PoolBudget {
  return manualPool({
    seat_tracked: true,
    manual_limit_microusd: null,
    resume_action: null,
    entitlement_exceeds_figure: false,
    baseline_microusd: 600_000_000,
    pool_limit_microusd: 650_000_000,
    remaining_microusd: 650_000_000,
  })
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
  expect(document.body.textContent).not.toContain(SENTINEL)
  return screen.getByTestId('pool-mode-sentence').textContent ?? ''
}

afterEach(async () => {
  cleanup()
  await i18n.changeLanguage('en')
})

describe('the pool mode sentence is localized by the console', () => {
  it.each(['en', 'ja'])(
    'a manual pool names its own figure and the entitlement (%s)',
    async (lang) => {
      await i18n.changeLanguage(lang)
      const text = renderCard(manualPool())
      expect(text).toContain('$500.00')
      expect(text).toContain('$600.00')
      expect(text).not.toContain('$550.00')
      expect(text).not.toContain('follow_seats')
      if (lang === 'ja') {
        expect(text).toMatch(/手動/)
        expect(text).not.toMatch(/This pool|a figure set by hand/)
      }
    },
  )

  it.each(['en', 'ja'])(
    'the sentence names the button exactly when the button is there (%s)',
    async (lang) => {
      await i18n.changeLanguage(lang)
      expect(renderCard(manualPool())).toContain(label())
      expect(screen.getByTestId('pool-follow-seats-button').textContent).toContain(label())
      cleanup()
      const withoutResume = renderCard(manualPool({ resume_action: null }))
      expect(withoutResume).not.toContain(label())
      expect(screen.queryByTestId('pool-follow-seats-button')).not.toBeInTheDocument()
    },
  )

  it.each(['en', 'ja'])(
    'a seat-tracked pool names the seat count and the entitlement (%s)',
    async (lang) => {
      await i18n.changeLanguage(lang)
      const text = renderCard(seatPool())
      expect(text).toContain('(3)')
      expect(text).toContain('$600.00')
      expect(text).not.toContain('$650.00')
      expect(text).not.toContain(label())
      if (lang === 'ja') {
        expect(text).toMatch(/シート数/)
        expect(text).not.toMatch(/This pool/)
      }
    },
  )
})

describe('no console screen renders backend prose meant for API callers', () => {
  it('only the API type declaration names mode_sentence outside tests', () => {
    const root = join(__dirname, '..', '..')
    const offenders: string[] = []
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const p = join(dir, name)
        if (statSync(p).isDirectory()) walk(p)
        else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) {
          const code = readFileSync(p, 'utf8')
            .replace(/\/\*[\s\S]*?\*\//g, '')
            .replace(/\/\/.*$/gm, '')
          if (/\bmode_sentence\b/.test(code)) offenders.push(relative(root, p))
        }
      }
    }
    walk(root)
    expect(offenders).toEqual(['lib/api.ts'])
  })
})
