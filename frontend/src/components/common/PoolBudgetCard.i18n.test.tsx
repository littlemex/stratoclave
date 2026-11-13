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
 * `mode_sentence` is no longer in the console's `PoolBudget` type, so reading it is a
 * type error; the fixtures still carry a sentinel value under that name, as the API
 * does, to show it never reaches the page.
 *
 * The fields the sentence could print by mistake (baseline, limit, grant cap) have
 * values different from the two it should print, and each figure is matched with
 * the words around it, so printing the wrong field, or swapping two, fails.
 */

const SENTINEL = 'BACKEND-ENGLISH-SENTENCE-MUST-NOT-RENDER'
const label = () => i18n.t('admin_tenant_detail.pool.follow_seats_button')
const LABELS: Record<string, string> = {
  en: 'Follow the seat count again',
  ja: 'シート数への連動に戻す',
}

// manual $500, entitlement $600, baseline $510, granted $50, limit $550, cap $520.
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
    seat_tracked: false,
    seat_count: 3,
    seat_rate_microusd: 200_000_000,
    seat_entitlement_microusd: 600_000_000,
    manual_limit_microusd: 500_000_000,
    pool_granted_microusd: 50_000_000,
    baseline_microusd: 510_000_000,
    entitlement_exceeds_figure: true,
    resume_action: 'follow_seats',
    grant_cap_microusd: null,
    effective_grant_cap_microusd: 520_000_000,
    grant_cap_is_derived: true,
    remaining_grant_cap_microusd: 470_000_000,
    ...overrides,
  }
}

// 3 seats entitle $600; baseline $610, granted $50, limit $660, cap $620.
function seatPool(): PoolBudget {
  return manualPool({
    seat_tracked: true,
    manual_limit_microusd: null,
    resume_action: null,
    entitlement_exceeds_figure: false,
    baseline_microusd: 610_000_000,
    pool_limit_microusd: 660_000_000,
    remaining_microusd: 660_000_000,
    effective_grant_cap_microusd: 620_000_000,
    remaining_grant_cap_microusd: 570_000_000,
  })
}

const OTHER_FIGURES = ['$510.00', '$520.00', '$550.00', '$610.00', '$620.00', '$660.00']

function renderCard(p: PoolBudget) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const fromApi = { ...p, mode_sentence: SENTINEL } as PoolBudget
  render(
    <QueryClientProvider client={client}>
      <PoolBudgetCard
        tenantId="acme"
        pool={fromApi}
        isLoading={false}
        onChanged={() => {}}
        poolApi={{ setPoolBudget: async () => fromApi }}
      />
    </QueryClientProvider>,
  )
  expect(document.body.textContent).not.toContain(SENTINEL)
  const text = screen.getByTestId('pool-mode-sentence').textContent ?? ''
  for (const other of OTHER_FIGURES) expect(text).not.toContain(other)
  return text
}

afterEach(async () => {
  cleanup()
  await i18n.changeLanguage('en')
})

describe('the pool mode sentence is localized by the console', () => {
  it('a manual pool with no figure shows none rather than $0.00', async () => {
    await i18n.changeLanguage('en')
    const text = renderCard(manualPool({ manual_limit_microusd: null }))
    expect(text).not.toContain('$0.00')
    expect(text).toMatch(/held at —/)
  })

  it('a manual pool, in English, puts each figure in its place', async () => {
    await i18n.changeLanguage('en')
    const text = renderCard(manualPool())
    expect(text).toMatch(/held at \$500\.00, a figure set by hand/)
    expect(text).toMatch(/entitle it to \$600\.00/)
    expect(text).not.toContain('follow_seats')
  })

  it('a manual pool, in Japanese, puts each figure in its place', async () => {
    await i18n.changeLanguage('ja')
    const text = renderCard(manualPool())
    expect(text).toMatch(/手動で設定した \$500\.00/)
    expect(text).toMatch(/\$600\.00 分の枠/)
    expect(text).not.toMatch(/This pool|a figure set by hand/)
    expect(text).not.toMatch(/。 /)
  })

  it.each(['en', 'ja'])(
    'the sentence names the button exactly when the button is there (%s)',
    async (lang) => {
      await i18n.changeLanguage(lang)
      // Pinned once per language, so a missing or renamed key (which would render the
      // same raw key path in the sentence and on the button) fails here.
      expect(label()).toBe(LABELS[lang])
      expect(renderCard(manualPool())).toContain(label())
      expect(screen.getByTestId('pool-follow-seats-button').textContent).toContain(label())
      cleanup()
      expect(renderCard(manualPool({ resume_action: null }))).not.toContain(label())
      expect(screen.queryByTestId('pool-follow-seats-button')).not.toBeInTheDocument()
    },
  )

  it.each(['en', 'ja'])(
    'the entitlement that outgrew the figure is shown on its own line (%s)',
    async (lang) => {
      await i18n.changeLanguage(lang)
      renderCard(manualPool())
      expect(screen.getByTestId('pool-entitlement-outgrew').textContent).toContain('$600.00')
      cleanup()
      renderCard(manualPool({ entitlement_exceeds_figure: false }))
      expect(screen.queryByTestId('pool-entitlement-outgrew')).not.toBeInTheDocument()
    },
  )

  it('a seat-tracked pool names the seat count and the entitlement, in both languages', async () => {
    await i18n.changeLanguage('en')
    let text = renderCard(seatPool())
    expect(text).toMatch(/seat count \(3\), which entitles it to \$600\.00 a month/)
    expect(text).not.toContain(label())
    cleanup()
    await i18n.changeLanguage('ja')
    text = renderCard(seatPool())
    expect(text).toMatch(/シート数 \(3\) に従い、月 \$600\.00 分の枠/)
    expect(text).not.toContain(label())
  })
})
