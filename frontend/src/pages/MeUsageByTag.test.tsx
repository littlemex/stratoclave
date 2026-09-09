// MeUsageByTag — the caller's own spend, grouped by the tag they attached.
//
// The subject of most of these tests is DISCLOSURE, not arithmetic. The backend
// response carries eight fields that are not numbers-of-interest, each existing to
// stop a specific false belief a reader of a bare table would form. A table that
// renders the numbers and drops them is not a smaller version of this report — it is
// the defect those fields were added to prevent. So the tests assert that a person
// sees them, in the same spirit as this repo's other page tests: what a person must
// see, not that a field arrived.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mockByTag = vi.fn()

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    api: { ...actual.api, myUsageByTag: (...a: unknown[]) => mockByTag(...a) },
  }
})

import MeUsageByTag from './MeUsageByTag'

function withClient(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

/** A response with every disclosure field asserted, as the backend always sends them. */
function response(over: Record<string, unknown> = {}) {
  return {
    period: '2026-09',
    rows: [
      {
        user_id: 'me',
        task_tag: 'migration-42',
        requests: 12,
        absent_count: 0,
        dropped_grammar_count: 0,
        cost_microusd: 4_500_000,
        input_tokens: 1000,
        output_tokens: 250,
      },
    ],
    truncated: false,
    legacy_rows: 0,
    malformed_rows: 0,
    tag_is_caller_asserted: true,
    retention_policy_days: 95,
    retention_deletion_is_asynchronous: true,
    retention_boundary_period_may_fold_incompletely: true,
    tag_total_is_a_lower_bound: true,
    ...over,
  }
}

beforeEach(() => {
  mockByTag.mockReset()
  mockByTag.mockResolvedValue(response())
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('MeUsageByTag — the numbers', () => {
  it('shows a tag, its request count and its cost', async () => {
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-row-tag')).toHaveTextContent('migration-42'))
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText(/\$4\.50/)).toBeInTheDocument()
  })

  it('asks for the period it was told to, and only the period', async () => {
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(mockByTag).toHaveBeenCalled())
    // One argument. There is no tenant and no user to pass -- the server derives both
    // from the session -- so this call cannot be steered at somebody else's rows even
    // by a caller who wants to.
    expect(mockByTag.mock.calls[0]).toHaveLength(1)
    expect(mockByTag.mock.calls[0][0]).toMatch(/^\d{4}-\d{2}$/)
  })

  it('refuses a period that is not YYYY-MM instead of asking for it', async () => {
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(mockByTag).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByTestId('bt-period-input'), { target: { value: 'last month' } })
    expect(screen.getByTestId('bt-period-invalid')).toBeInTheDocument()
    expect(screen.getByTestId('bt-load-button')).toBeDisabled()
    fireEvent.click(screen.getByTestId('bt-load-button'))
    expect(mockByTag).toHaveBeenCalledTimes(1)
  })
})

describe('MeUsageByTag — what the numbers do not mean', () => {
  it('says a tag total is a FLOOR, before the table rather than after it', async () => {
    // The reader this exists for is comparing an approved amount to a spend total. A
    // caveat below the figures arrives after they have drawn the conclusion.
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-lower-bound')).toBeInTheDocument())
    const disclosures = screen.getByTestId('bt-disclosures')
    const table = screen.getByTestId('bt-row-tag').closest('table')!
    expect(
      disclosures.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('says the tag was never checked against anything', async () => {
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-caller-asserted')).toBeInTheDocument())
  })

  it('states all three retention facts, not just the number of days', async () => {
    // A bare "history: 95 days" asserts a query horizon the backend explicitly says
    // this is not: expiry is asynchronous, so older rows may still read back, and a
    // period at the boundary can fold with some rows swept and some not.
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-retention')).toBeInTheDocument())
    const text = screen.getByTestId('bt-retention').textContent ?? ''
    expect(text).toMatch(/95/)
    expect(text.length).toBeGreaterThan(60)
  })

  it('drops a disclosure only when the backend stops asserting it', async () => {
    // Rendered from the response's own booleans rather than hardcoded, so the page
    // cannot keep claiming something the API has stopped claiming.
    mockByTag.mockResolvedValue(
      response({ tag_total_is_a_lower_bound: false, tag_is_caller_asserted: false }),
    )
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-retention')).toBeInTheDocument())
    expect(screen.queryByTestId('bt-lower-bound')).toBeNull()
    expect(screen.queryByTestId('bt-caller-asserted')).toBeNull()
  })

  it('says when the fold did not cover the period', async () => {
    // Every total on screen becomes a different kind of number, so this cannot be a
    // footnote somewhere else.
    mockByTag.mockResolvedValue(response({ truncated: true }))
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-coverage')).toHaveTextContent(/partial/i))
  })

  it('counts pre-feature rows rather than pretending they are absent', async () => {
    mockByTag.mockResolvedValue(response({ legacy_rows: 431 }))
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-coverage')).toHaveTextContent(/431/))
  })
})

describe('MeUsageByTag — the unlabelled bucket', () => {
  it('splits "nobody tagged this" from "somebody mistyped a tag"', async () => {
    // Folded into one untagged number, a month of one person's mistyped tag is
    // invisible behind everyone who simply never tagged anything. The split is the
    // entire reason the backend carries two counters.
    mockByTag.mockResolvedValue(
      response({
        rows: [
          {
            user_id: 'me',
            task_tag: 'unlabelled',
            requests: 140,
            absent_count: 120,
            dropped_grammar_count: 20,
            cost_microusd: 9_000_000,
            input_tokens: 10,
            output_tokens: 10,
          },
        ],
      }),
    )
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-row-unlabelled-split')).toBeInTheDocument())
    const text = screen.getByTestId('bt-row-unlabelled-split').textContent ?? ''
    expect(text).toMatch(/120/)
    expect(text).toMatch(/20/)
  })

  it('does not show the split on a row that carries a real assertion', async () => {
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-row-tag')).toBeInTheDocument())
    expect(screen.queryByTestId('bt-row-unlabelled-split')).toBeNull()
  })
})

describe('MeUsageByTag — malformed rows are a finding, not a caveat', () => {
  it('raises an alarm when rows carry half their tag information', async () => {
    // The recorder refuses to write such a row, so a nonzero count means something
    // else wrote to that table. That is a fact about the data, not a note about the
    // report, and it must not sit in the same grey text as the retention policy.
    mockByTag.mockResolvedValue(response({ malformed_rows: 3 }))
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-malformed-alarm')).toBeInTheDocument())
    expect(screen.getByTestId('bt-malformed-alarm')).toHaveTextContent(/3/)
  })

  it('shows no alarm when there are none', async () => {
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-row-tag')).toBeInTheDocument())
    expect(screen.queryByTestId('bt-malformed-alarm')).toBeNull()
  })
})

describe('MeUsageByTag — a stored tag is text, whatever it contains', () => {
  it('renders a tag an older client stored under looser rules as literal text', async () => {
    // This build never re-validates what is already on the record: the value was
    // checked by the client that filed it. So the guarantee has to be the sink --
    // React escapes a text node -- and this asserts the markup is inert rather than
    // absent.
    const hostile = '<img src=x onerror=alert(1)>'
    mockByTag.mockResolvedValue(
      response({
        rows: [
          {
            user_id: 'me',
            task_tag: hostile,
            requests: 1,
            absent_count: 0,
            dropped_grammar_count: 0,
            cost_microusd: 1,
            input_tokens: 0,
            output_tokens: 0,
          },
        ],
      }),
    )
    render(withClient(<MeUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-row-tag')).toHaveTextContent(hostile))
    // The literal string is on screen and no element was created from it.
    expect(document.querySelector('img')).toBeNull()
  })
})
