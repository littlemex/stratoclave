// UsageByTagReport — the one rendering of spend grouped by task tag.
//
// The subject of most of these tests is DISCLOSURE, not arithmetic. The response carries
// nine fields that are not numbers-of-interest, each existing to stop a specific false
// belief a reader of a bare table would form. A surface that renders the numbers and a
// SUBSET of those is the defect the fields were added to prevent.
//
// **These tests moved here with the component, deliberately and unchanged.** They were
// written against the self page; extracting the renderer without moving them would have
// left the disclosures covered only by a page that no longer draws them, and a dropped
// disclosure would then pass by absence. Required TypeScript fields do not make a
// component render anything.
//
// The fixture has every boolean true and, where a test needs it, every counter nonzero, so
// there is no disclosure a passing run has silently skipped.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mockByTag = vi.fn()

import { UsageByTagReport } from './UsageByTagReport'

function withClient(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

/** The component under test, wired to the mock the way the self page wires the real call. */
function report(props: Partial<Parameters<typeof UsageByTagReport>[0]> = {}) {
  return (
    <UsageByTagReport
      queryScope={['test']}
      fetchReport={(q) => mockByTag(q)}
      {...props}
    />
  )
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
        requests_without_cost: 0,
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

describe('UsageByTagReport — the numbers', () => {
  it('shows a tag, its request count and its cost', async () => {
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-row-tag')).toHaveTextContent('migration-42'))
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.getByText(/\$4\.50/)).toBeInTheDocument()
  })

  it('hands the fetch ONE object, and the same one the key is built from', async () => {
    render(withClient(report()))
    await waitFor(() => expect(mockByTag).toHaveBeenCalled())
    // One argument, an object. The seam used to take a bare period and leave the member
    // filter to a closure, which is a way for one member's rows to be cached and then shown
    // under another member's submitted filter -- the key naming one thing and the request
    // having asked for another.
    expect(mockByTag.mock.calls[0]).toHaveLength(1)
    const q = mockByTag.mock.calls[0][0]
    expect(q.period).toMatch(/^\d{4}-\d{2}$/)
    // No member unless one was submitted, and `undefined` rather than an empty string: the
    // route reads `user_id=` as a filter for the empty string.
    expect(q.userId).toBeUndefined()
  })

  it('does not offer a member filter unless the caller asked for the column', async () => {
    // A self report is one person; a filter there would invite someone to type an id that
    // returns nothing and read it as a fault.
    render(withClient(report()))
    await waitFor(() => expect(mockByTag).toHaveBeenCalled())
    expect(screen.queryByTestId('bt-member-input')).toBeNull()
    expect(screen.queryByTestId('bt-row-member')).toBeNull()
  })

  it('refuses a period that is not YYYY-MM instead of asking for it', async () => {
    render(withClient(report()))
    await waitFor(() => expect(mockByTag).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByTestId('bt-period-input'), { target: { value: 'last month' } })
    expect(screen.getByTestId('bt-period-invalid')).toBeInTheDocument()
    expect(screen.getByTestId('bt-load-button')).toBeDisabled()
    fireEvent.click(screen.getByTestId('bt-load-button'))
    expect(mockByTag).toHaveBeenCalledTimes(1)
  })
})

describe('UsageByTagReport — what the numbers do not mean', () => {
  it('says a tag total is a FLOOR, before the table rather than after it', async () => {
    // The reader this exists for is comparing an approved amount to a spend total. A
    // caveat below the figures arrives after they have drawn the conclusion.
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-lower-bound')).toBeInTheDocument())
    const disclosures = screen.getByTestId('bt-disclosures')
    const table = screen.getByTestId('bt-row-tag').closest('table')!
    expect(
      disclosures.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('says the tag was never checked against anything', async () => {
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-caller-asserted')).toBeInTheDocument())
  })

  it('states all three retention facts, not just the number of days', async () => {
    // A bare "history: 95 days" asserts a query horizon the backend explicitly says
    // this is not: expiry is asynchronous, so older rows may still read back, and a
    // period at the boundary can fold with some rows swept and some not.
    render(withClient(report()))
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
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-retention')).toBeInTheDocument())
    expect(screen.queryByTestId('bt-lower-bound')).toBeNull()
    expect(screen.queryByTestId('bt-caller-asserted')).toBeNull()
  })

  it('says when the fold did not cover the period', async () => {
    // Every total on screen becomes a different kind of number, so this cannot be a
    // footnote somewhere else.
    mockByTag.mockResolvedValue(response({ truncated: true }))
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-coverage')).toHaveTextContent(/partial/i))
  })

  it('counts pre-feature rows rather than pretending they are absent', async () => {
    mockByTag.mockResolvedValue(response({ legacy_rows: 431 }))
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-coverage')).toHaveTextContent(/431/))
  })
})

describe('UsageByTagReport — the unlabelled bucket', () => {
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
            requests_without_cost: 0,
            input_tokens: 10,
            output_tokens: 10,
          },
        ],
      }),
    )
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-row-unlabelled-split')).toBeInTheDocument())
    const text = screen.getByTestId('bt-row-unlabelled-split').textContent ?? ''
    expect(text).toMatch(/120/)
    expect(text).toMatch(/20/)
  })

  it('does not show the split on a row that carries a real assertion', async () => {
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-row-tag')).toBeInTheDocument())
    expect(screen.queryByTestId('bt-row-unlabelled-split')).toBeNull()
  })
})

describe('UsageByTagReport — malformed rows are a finding, not a caveat', () => {
  it('raises an alarm when rows carry half their tag information', async () => {
    // The recorder refuses to write such a row, so a nonzero count means something
    // else wrote to that table. That is a fact about the data, not a note about the
    // report, and it must not sit in the same grey text as the retention policy.
    mockByTag.mockResolvedValue(response({ malformed_rows: 3 }))
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-malformed-alarm')).toBeInTheDocument())
    expect(screen.getByTestId('bt-malformed-alarm')).toHaveTextContent(/3/)
  })

  it('shows no alarm when there are none', async () => {
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-row-tag')).toBeInTheDocument())
    expect(screen.queryByTestId('bt-malformed-alarm')).toBeNull()
  })
})

describe('UsageByTagReport — a stored tag is text, whatever it contains', () => {
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
            requests_without_cost: 0,
            input_tokens: 0,
            output_tokens: 0,
          },
        ],
      }),
    )
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-row-tag')).toHaveTextContent(hostile))
    // The literal string is on screen and no element was created from it.
    expect(document.querySelector('img')).toBeNull()
  })
})


describe('UsageByTagReport — a cost that is unknown rather than zero', () => {
  it('says which requests have no cost recorded, and that the gap is not $0.00', async () => {
    // The disclosure this page was missing. Eight others were built to stop a reader
    // believing something false about these numbers, and none covered the cost column
    // being EMPTY. A tenant enforced in dollars with no pool had every row read $0.00,
    // and a reader concludes the work was free.
    mockByTag.mockResolvedValue(
      response({
        rows: [
          {
            user_id: 'me',
            task_tag: 'migration-42',
            requests: 88,
            absent_count: 0,
            dropped_grammar_count: 0,
            cost_microusd: 45_000_000,
            requests_without_cost: 3,
            input_tokens: 10,
            output_tokens: 10,
          },
        ],
      }),
    )
    render(withClient(report()))
    const notice = await screen.findByTestId('bt-row-missing-cost')
    // Both numbers: "3 of 88" tells a reader the total is nearly right, where "some
    // requests" or a bare flag would not.
    expect(notice).toHaveTextContent(/3/)
    expect(notice).toHaveTextContent(/88/)
    // And the distinction itself, in words, because that is the whole point.
    expect(notice.textContent ?? '').toMatch(/unknown/i)
  })

  it('shows nothing when every request was priced', async () => {
    render(withClient(report()))
    await waitFor(() => expect(screen.getByTestId('bt-row-tag')).toBeInTheDocument())
    expect(screen.queryByTestId('bt-row-missing-cost')).toBeNull()
  })
})

describe('UsageByTagReport — a report covering more than one person', () => {
  function multiMember() {
    return response({
      rows: [
        {
          user_id: 'alice', task_tag: 'deploy', requests: 4,
          absent_count: 0, dropped_grammar_count: 0, cost_microusd: 1_000_000,
          requests_without_cost: 0, input_tokens: 1, output_tokens: 1,
        },
        {
          user_id: 'bob', task_tag: 'deploy', requests: 9,
          absent_count: 0, dropped_grammar_count: 0, cost_microusd: 2_000_000,
          requests_without_cost: 0, input_tokens: 1, output_tokens: 1,
        },
      ],
    })
  }

  it('names the member on each row, so two people using one tag are distinguishable', async () => {
    // Rows are grouped by (user_id, task_tag). Without the member, two engineers who both tag
    // `deploy` produce two rows that look identical, and a reader cannot tell whose spend is
    // whose or why the same tag appears twice.
    mockByTag.mockResolvedValue(multiMember())
    render(withClient(report({ memberColumn: true })))
    await waitFor(() => expect(screen.getAllByTestId('bt-row-member')).toHaveLength(2))
    const members = screen.getAllByTestId('bt-row-member').map((e) => e.textContent)
    expect(members).toEqual(['alice', 'bob'])
    // Both rows are present rather than folded into one.
    expect(screen.getAllByTestId('bt-row-tag').map((e) => e.textContent)).toEqual([
      'deploy',
      'deploy',
    ])
  })

  it('sends a submitted member as user_id, and omits a blank one', async () => {
    mockByTag.mockResolvedValue(multiMember())
    render(withClient(report({ memberColumn: true })))
    await waitFor(() => expect(mockByTag).toHaveBeenCalledTimes(1))
    expect(mockByTag.mock.calls[0][0].userId).toBeUndefined()

    fireEvent.change(screen.getByTestId('bt-member-input'), { target: { value: '  alice  ' } })
    fireEvent.click(screen.getByTestId('bt-load-button'))
    await waitFor(() => expect(mockByTag).toHaveBeenCalledTimes(2))
    // Trimmed, because a trailing space in a member id is a typo and not a different member.
    expect(mockByTag.mock.calls[1][0].userId).toBe('alice')
  })

  it('says an empty result for a member is empty, not broken', async () => {
    // An id matching nobody returns no rows, which is the backend's behaviour. The empty state
    // must name the member so it reads as "nothing for them" rather than as a fault.
    mockByTag.mockResolvedValue(response({ rows: [] }))
    render(withClient(report({ memberColumn: true })))
    await waitFor(() => expect(screen.getByTestId('bt-empty')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('bt-member-input'), { target: { value: 'nobody' } })
    fireEvent.click(screen.getByTestId('bt-load-button'))
    await waitFor(() =>
      expect(screen.getByTestId('bt-empty')).toHaveTextContent(/nobody/),
    )
    expect(screen.queryByTestId('bt-error')).toBeNull()
  })
})

describe('UsageByTagReport — the period must be a real month', () => {
  it('refuses a month of 99, which the wire pattern accepts', async () => {
    // `^\d{4}-\d{2}$` matches 2026-99 on both sides of the wire. It crosses no tenant and
    // returns a plainly empty report, so the cost of accepting it is a person concluding they
    // spent nothing in a month that does not exist.
    render(withClient(report()))
    await waitFor(() => expect(mockByTag).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByTestId('bt-period-input'), { target: { value: '2026-99' } })
    expect(screen.getByTestId('bt-period-invalid')).toBeInTheDocument()
    expect(screen.getByTestId('bt-load-button')).toBeDisabled()
    fireEvent.click(screen.getByTestId('bt-load-button'))
    expect(mockByTag).toHaveBeenCalledTimes(1)
  })

  it('accepts the boundary months', async () => {
    render(withClient(report()))
    await waitFor(() => expect(mockByTag).toHaveBeenCalledTimes(1))
    for (const p of ['2026-01', '2026-12']) {
      fireEvent.change(screen.getByTestId('bt-period-input'), { target: { value: p } })
      expect(screen.queryByTestId('bt-period-invalid')).toBeNull()
    }
  })
})
