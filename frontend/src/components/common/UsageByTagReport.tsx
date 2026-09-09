import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, Tags } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { type UsageByTagResponse, type UsageByTagRow } from '@/lib/api'
import { fmtMicroUsd } from '@/lib/money'

/**
 * Spend grouped by the task tag the caller attached to the work.
 *
 * **ONE component for every surface that shows this, and that is the point rather than
 * tidiness** — the same reason `PoolBudgetCard` gives for itself. Most of this screen is
 * DISCLOSURE: `UsageByTagResponse` carries nine fields that are not numbers-of-interest, and
 * each exists to stop one false belief a reader of a bare table would form. That the tag was
 * verified. That a row's total is what the work cost. That "nobody tagged this" and "somebody
 * mistyped a tag for a month" are the same number. That the fold covered the period. That
 * pre-feature history is absent rather than uncounted. That the table was only ever written by
 * the recorder. That history is unbounded with synchronous expiry. That a zero cost means free.
 *
 * A second surface rendering the numbers and a SUBSET of those is the exact defect the fields
 * exist to prevent, and it would arrive by ordinary drift rather than by anyone deciding. Two
 * renderings of one report eventually disagree, and they would disagree about the part that
 * matters.
 *
 * The component knows nothing about tenants, roles or permissions. It is handed a `fetchReport`
 * and a `memberColumn` and renders what it gets.
 */

/**
 * What a caller asks for, in one object.
 *
 * Deliberately one value rather than separate arguments: the query key is built from THIS
 * object and the same object is handed to `fetchReport`, so the key can never name a period or
 * a member that the request did not use. A first version of this seam took only the period and
 * left the member filter to a closure, which is a way for Alice's rows to be cached and then
 * displayed under Bob's submitted filter.
 */
export interface UsageByTagQuery {
  period: string
  /** `undefined` means the whole scope. A blank input is normalised to this, never sent as an
   *  empty `user_id=`, which the route would read as a filter for the empty string. */
  userId?: string
}

export interface UsageByTagReportProps {
  /** The caller's own route. The component cannot reach a route it was not handed. */
  fetchReport: (query: UsageByTagQuery) => Promise<UsageByTagResponse>
  /**
   * Distinguishes cache entries between surfaces and scopes: `['me']`,
   * `['admin', tenantId]`, and so on. Rows are grouped per `(user_id, task_tag)` per tenant, so
   * two scopes sharing a key would serve one scope's rows for another — which needs no bug in
   * the backend at all.
   */
  queryScope: readonly (string | undefined)[]
  /**
   * Show the member each row belongs to, and offer a filter.
   *
   * Off for a self report, where every row is the one reader and a member column is noise. ON
   * for any report covering more than one person: rows are grouped by `(user_id, task_tag)`, so
   * two engineers who both tag `deploy` produce two rows that are otherwise indistinguishable.
   */
  memberColumn?: boolean
}

export function UsageByTagReport({
  fetchReport,
  queryScope,
  memberColumn = false,
}: UsageByTagReportProps) {
  const { t } = useTranslation()
  const [period, setPeriod] = useState(() => currentPeriodUtc())
  const [member, setMember] = useState('')
  // What was actually asked for, as one object. The inputs above are what the person is
  // typing; this is what the report on screen is OF.
  const [submitted, setSubmitted] = useState<UsageByTagQuery>(() => ({
    period: currentPeriodUtc(),
  }))

  const periodValid = isPeriod(period)

  const report = useQuery({
    queryKey: [...queryScope, 'usage', 'by-tag', submitted.period, submitted.userId ?? ''],
    queryFn: () => fetchReport(submitted),
    enabled: isPeriod(submitted.period),
  })

  const data = report.data ?? null

  return (
    <div className="space-y-8">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 font-sans text-base font-semibold">
            <Tags className="h-4 w-4 text-muted-foreground" />
            {t('usage_by_tag.query_title')}
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="bt-period">{t('usage_by_tag.period_label')}</Label>
            <Input
              id="bt-period"
              value={period}
              placeholder="2026-09"
              autoComplete="off"
              onChange={(e) => setPeriod(e.target.value)}
              data-testid="bt-period-input"
            />
          </div>
          {memberColumn ? (
            <div className="space-y-1.5">
              <Label htmlFor="bt-member">{t('usage_by_tag.member_label')}</Label>
              <Input
                id="bt-member"
                value={member}
                placeholder={t('usage_by_tag.member_placeholder')}
                autoComplete="off"
                spellCheck={false}
                onChange={(e) => setMember(e.target.value)}
                data-testid="bt-member-input"
              />
            </div>
          ) : null}
          <Button
            disabled={!periodValid || report.isFetching}
            onClick={() =>
              setSubmitted({
                period,
                // Blank means the whole scope, which the route expresses by omitting the
                // parameter. Sending `user_id=` would filter for the empty string.
                userId: member.trim() === '' ? undefined : member.trim(),
              })
            }
            data-testid="bt-load-button"
          >
            {report.isFetching ? t('common.loading') : t('usage_by_tag.load')}
          </Button>
          {!periodValid && period.trim() !== '' ? (
            <p className="text-xs text-destructive" data-testid="bt-period-invalid">
              {t('usage_by_tag.period_invalid')}
            </p>
          ) : null}
        </CardContent>
      </Card>

      {/* Above the table, because a reader who sees the totals first has already drawn the
          conclusion these sentences exist to prevent. */}
      {data ? <ReadingTheseNumbers data={data} /> : null}

      {data && data.malformed_rows > 0 ? (
        /* Not a footnote. `record` refuses to write a row with exactly one of the two tag
           attributes, so a nonzero count means something OTHER than `record` wrote to this
           table. That is a finding about the data, not a caveat about the report. */
        <Card className="border-destructive/50" data-testid="bt-malformed-alarm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 font-sans text-base font-semibold text-destructive">
              <AlertTriangle className="h-4 w-4" />
              {t('usage_by_tag.malformed_title')}
            </CardTitle>
            <CardDescription>
              {t('usage_by_tag.malformed_body', { count: data.malformed_rows })}
            </CardDescription>
          </CardHeader>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('usage_by_tag.table_title')}
          </CardTitle>
          {data ? (
            <CardDescription data-testid="bt-coverage">
              {data.truncated
                ? t('usage_by_tag.truncated')
                : t('usage_by_tag.not_truncated')}
              {data.legacy_rows > 0
                ? ` ${t('usage_by_tag.legacy_rows', { count: data.legacy_rows })}`
                : ''}
              {submitted.userId
                ? ` ${t('usage_by_tag.filtered_to_member', { member: submitted.userId })}`
                : ''}
            </CardDescription>
          ) : null}
        </CardHeader>
        <CardContent className="p-0">
          {report.isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">{t('common.loading')}</p>
          ) : report.isError ? (
            <p className="p-6 text-sm text-destructive" data-testid="bt-error">
              {t('usage_by_tag.load_error')}
            </p>
          ) : data == null || data.rows.length === 0 ? (
            <p className="p-6 text-sm text-muted-foreground" data-testid="bt-empty">
              {/* An id matching nobody returns an empty report rather than an error, so the
                  empty state must read as "nothing for this member" and not as a fault. */}
              {submitted.userId
                ? t('usage_by_tag.empty_for_member', { member: submitted.userId })
                : t('usage_by_tag.empty')}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  {memberColumn ? (
                    <TableHead>{t('usage_by_tag.col_member')}</TableHead>
                  ) : null}
                  <TableHead>{t('usage_by_tag.col_tag')}</TableHead>
                  <TableHead className="text-right">
                    {t('usage_by_tag.col_requests')}
                  </TableHead>
                  <TableHead className="text-right">{t('usage_by_tag.col_cost')}</TableHead>
                  <TableHead className="text-right">
                    {t('usage_by_tag.col_tokens')}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.rows.map((row) => (
                  // Keyed on the identity the backend groups by, never on display text alone:
                  // two rows can share a tag across members, and a key built from repeating
                  // text confuses row state.
                  <TagRow
                    key={`${row.user_id}::${row.task_tag}`}
                    row={row}
                    memberColumn={memberColumn}
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

/**
 * The sentences that decide how the table is read, plus retention.
 *
 * Rendered from the response's own booleans rather than hardcoded, so a backend that ever
 * stops asserting one stops this page asserting it too.
 */
function ReadingTheseNumbers({ data }: { data: UsageByTagResponse }) {
  const { t } = useTranslation()
  return (
    <Card data-testid="bt-disclosures">
      <CardHeader>
        <CardTitle className="font-sans text-base font-semibold">
          {t('usage_by_tag.reading_title')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm text-muted-foreground">
        {data.tag_total_is_a_lower_bound ? (
          <p data-testid="bt-lower-bound">{t('usage_by_tag.lower_bound')}</p>
        ) : null}
        {data.tag_is_caller_asserted ? (
          <p data-testid="bt-caller-asserted">{t('usage_by_tag.caller_asserted')}</p>
        ) : null}
        {/* One sentence saying all three retention facts. A bare "history: N days" would assert
            the query horizon the backend explicitly says this is not: expiry is asynchronous,
            so older rows may still read back, and a period straddling the boundary can fold
            with some rows swept and some not. */}
        <p data-testid="bt-retention">
          {t('usage_by_tag.retention', { days: data.retention_policy_days })}
          {data.retention_deletion_is_asynchronous
            ? ` ${t('usage_by_tag.retention_async')}`
            : ''}
          {data.retention_boundary_period_may_fold_incompletely
            ? ` ${t('usage_by_tag.retention_boundary')}`
            : ''}
        </p>
      </CardContent>
    </Card>
  )
}

function TagRow({ row, memberColumn }: { row: UsageByTagRow; memberColumn: boolean }) {
  const { t } = useTranslation()
  // Both counters are zero on a row carrying a real assertion, so a nonzero value identifies
  // the sentinel row without this component needing to know the sentinel's spelling — which is
  // the gateway's to own, not this page's.
  const isSentinelBucket = row.absent_count > 0 || row.dropped_grammar_count > 0
  return (
    <TableRow>
      {memberColumn ? (
        <TableCell className="font-mono text-xs" data-testid="bt-row-member">
          {row.user_id}
        </TableCell>
      ) : null}
      <TableCell className="text-xs">
        {/* The canonical stored form, as text. React escapes it; a value written by an older
            client under looser rules still renders as the literal text it is. This build does
            not re-validate what is already on the record. */}
        <span className="font-mono" data-testid="bt-row-tag">
          {row.task_tag}
        </span>
        {row.requests_without_cost > 0 ? (
          /* The cost column being EMPTY rather than zero. A tenant enforced in dollars with no
             pool had every row here read $0.00, and a reader concludes the work was free.
             Per row, because "3 of 88" says whether the total is nearly right or nearly
             meaningless where a report-wide flag does not. */
          <div className="mt-1 text-destructive" data-testid="bt-row-missing-cost">
            {t('usage_by_tag.missing_cost', {
              missing: row.requests_without_cost,
              total: row.requests,
            })}
          </div>
        ) : null}
        {isSentinelBucket ? (
          /* Folded into one untagged number, a month of one person's mistyped tag is invisible
             behind everyone who simply never tagged anything. */
          <div className="mt-1 text-muted-foreground" data-testid="bt-row-unlabelled-split">
            {t('usage_by_tag.unlabelled_split', {
              absent: row.absent_count,
              dropped: row.dropped_grammar_count,
            })}
          </div>
        ) : null}
      </TableCell>
      <TableCell className="text-right font-mono text-xs">{row.requests}</TableCell>
      <TableCell className="text-right font-mono text-xs">
        {fmtMicroUsd(row.cost_microusd)}
      </TableCell>
      <TableCell className="text-right font-mono text-xs">
        {row.input_tokens + row.output_tokens}
      </TableCell>
    </TableRow>
  )
}

/**
 * A billing period, checked as a real month rather than as four-two digits.
 *
 * `^\d{4}-\d{2}$` accepts `2026-99`, which both sides of the wire do today. It crosses no
 * tenant and reads as a plainly empty report, so the cost of accepting it is a person
 * concluding they spent nothing in a month that does not exist.
 */
function isPeriod(value: string): boolean {
  const m = /^(\d{4})-(\d{2})$/.exec(value.trim())
  if (!m) return false
  const month = Number(m[2])
  return month >= 1 && month <= 12
}

/** The current UTC billing period. UTC, because the period boundary is the backend's and a
 *  local-time month is wrong for most of the world twice a year. */
function currentPeriodUtc(): string {
  const now = new Date()
  return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}`
}
