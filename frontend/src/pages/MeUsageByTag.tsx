import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, Tags } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
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
import { api, type UsageByTagResponse, type UsageByTagRow } from '@/lib/api'
import { fmtMicroUsd } from '@/lib/money'

/**
 * The caller's own spend grouped by the tag they attached to the work.
 *
 * **Most of this screen is disclosure, and that is deliberate.**
 * `UsageByTagResponse` carries eight fields that are not numbers-of-interest, and
 * each exists to stop a specific false belief a reader of a bare table would form:
 * that the tag was verified, that a row's total is what the work cost, that "nobody
 * tagged this" and "somebody mistyped a tag for a month" are the same number, that
 * the fold covered the period, that pre-feature history is absent rather than
 * uncounted, that the table was only ever written by the recorder, and that history
 * is unbounded with synchronous expiry.
 *
 * A table that renders the numbers and drops those is not a smaller version of this
 * report — it is the exact defect the backend spent those fields preventing. So they
 * are persistent text on the page, not a tooltip and not behind a disclosure
 * triangle: the reader who needs them most is the one comparing an approved amount
 * to a spend total, and that reader is not hunting for footnotes.
 *
 * This view calls `GET /me/usage/by-tag`, which takes only a period. It cannot be
 * pointed at another tenant or another member, because there is no parameter for
 * either — the server derives both from the session.
 */
export default function MeUsageByTag() {
  const { t } = useTranslation()
  const [period, setPeriod] = useState(() => currentPeriodUtc())
  const [submitted, setSubmitted] = useState(() => currentPeriodUtc())

  const periodValid = /^\d{4}-\d{2}$/.test(period)

  const report = useQuery({
    // `submitted` is in the key so a period change is a distinct cache entry
    // rather than a refetch that overwrites the previous answer. No tenant or
    // user in the key because neither is a parameter of this call, and the whole
    // client cache is cleared on any identity change (`AuthContext`).
    queryKey: ['me', 'usage', 'by-tag', submitted],
    queryFn: () => api.myUsageByTag(submitted),
    enabled: /^\d{4}-\d{2}$/.test(submitted),
  })

  const data = report.data ?? null

  return (
    <div className="space-y-10">
      <header>
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          {t('me_usage_by_tag.label')}
        </p>
        <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">
          {t('me_usage_by_tag.title')}
        </h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">{t('me_usage_by_tag.intro')}</p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 font-sans text-base font-semibold">
            <Tags className="h-4 w-4 text-muted-foreground" />
            {t('me_usage_by_tag.period_title')}
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="bt-period">{t('me_usage_by_tag.period_label')}</Label>
            <Input
              id="bt-period"
              value={period}
              placeholder="2026-09"
              autoComplete="off"
              onChange={(e) => setPeriod(e.target.value)}
              data-testid="bt-period-input"
            />
          </div>
          <Button
            disabled={!periodValid || report.isFetching}
            onClick={() => setSubmitted(period)}
            data-testid="bt-load-button"
          >
            {report.isFetching ? t('common.loading') : t('me_usage_by_tag.load')}
          </Button>
          {!periodValid && period.trim() !== '' ? (
            <p className="text-xs text-destructive" data-testid="bt-period-invalid">
              {t('me_usage_by_tag.period_invalid')}
            </p>
          ) : null}
        </CardContent>
      </Card>

      {/* The two facts that change how every number below should be read. Above the
          table, because a reader who sees the totals first has already drawn the
          conclusion these sentences exist to prevent. */}
      {data ? <ReadingTheseNumbers data={data} /> : null}

      {data && data.malformed_rows > 0 ? (
        /* Not a footnote. The backend's own words: `record` refuses to write a row
           with exactly one of the two tag attributes, so a nonzero count means
           something OTHER than `record` wrote to this table. That is a finding
           about the data, not a caveat about the report. */
        <Card className="border-destructive/50" data-testid="bt-malformed-alarm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 font-sans text-base font-semibold text-destructive">
              <AlertTriangle className="h-4 w-4" />
              {t('me_usage_by_tag.malformed_title')}
            </CardTitle>
            <CardDescription>
              {t('me_usage_by_tag.malformed_body', { count: data.malformed_rows })}
            </CardDescription>
          </CardHeader>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('me_usage_by_tag.table_title')}
          </CardTitle>
          {data ? (
            <CardDescription data-testid="bt-coverage">
              {data.truncated ? t('me_usage_by_tag.truncated') : t('me_usage_by_tag.not_truncated')}
              {data.legacy_rows > 0
                ? ` ${t('me_usage_by_tag.legacy_rows', { count: data.legacy_rows })}`
                : ''}
            </CardDescription>
          ) : null}
        </CardHeader>
        <CardContent className="p-0">
          {report.isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">{t('common.loading')}</p>
          ) : report.isError ? (
            <p className="p-6 text-sm text-destructive" data-testid="bt-error">
              {t('me_usage_by_tag.load_error')}
            </p>
          ) : data == null || data.rows.length === 0 ? (
            <p className="p-6 text-sm text-muted-foreground" data-testid="bt-empty">
              {t('me_usage_by_tag.empty')}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('me_usage_by_tag.col_tag')}</TableHead>
                  <TableHead className="text-right">{t('me_usage_by_tag.col_requests')}</TableHead>
                  <TableHead className="text-right">{t('me_usage_by_tag.col_cost')}</TableHead>
                  <TableHead className="text-right">{t('me_usage_by_tag.col_tokens')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.rows.map((row) => (
                  // Keyed on the identity the backend groups by, never on display
                  // text alone: two rows can share a tag across users, and a key
                  // built from text that repeats confuses row state.
                  <TagRow key={`${row.user_id}::${row.task_tag}`} row={row} />
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
 * The two sentences that decide how the table is read, plus retention.
 *
 * `tag_total_is_a_lower_bound` and `tag_is_caller_asserted` are rendered from the
 * response's own booleans rather than hardcoded, so a backend that ever stops
 * asserting one stops this page asserting it too.
 */
function ReadingTheseNumbers({ data }: { data: UsageByTagResponse }) {
  const { t } = useTranslation()
  return (
    <Card data-testid="bt-disclosures">
      <CardHeader>
        <CardTitle className="font-sans text-base font-semibold">
          {t('me_usage_by_tag.reading_title')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm text-muted-foreground">
        {data.tag_total_is_a_lower_bound ? (
          <p data-testid="bt-lower-bound">{t('me_usage_by_tag.lower_bound')}</p>
        ) : null}
        {data.tag_is_caller_asserted ? (
          <p data-testid="bt-caller-asserted">{t('me_usage_by_tag.caller_asserted')}</p>
        ) : null}
        {/* One sentence saying all three retention facts. A bare "history: N days"
            would assert the query horizon the backend explicitly says this is not:
            expiry is asynchronous, so older rows may still read back, and a period
            straddling the boundary can fold with some rows swept and some not. */}
        <p data-testid="bt-retention">
          {t('me_usage_by_tag.retention', {
            days: data.retention_policy_days,
          })}
          {data.retention_deletion_is_asynchronous
            ? ` ${t('me_usage_by_tag.retention_async')}`
            : ''}
          {data.retention_boundary_period_may_fold_incompletely
            ? ` ${t('me_usage_by_tag.retention_boundary')}`
            : ''}
        </p>
      </CardContent>
    </Card>
  )
}

function TagRow({ row }: { row: UsageByTagRow }) {
  const { t } = useTranslation()
  // Both counters are zero on a row carrying a real assertion, so a nonzero value
  // identifies the sentinel row without this component needing to know the
  // sentinel's spelling — which is the gateway's to own, not this page's.
  const isSentinelBucket = row.absent_count > 0 || row.dropped_grammar_count > 0
  return (
    <TableRow>
      <TableCell className="text-xs">
        {/* The canonical stored form, as text. React escapes it; a value written by
            an older client under looser rules still renders as the literal text it
            is. This build does not re-validate what is already on the record. */}
        <span className="font-mono" data-testid="bt-row-tag">
          {row.task_tag}
        </span>
        {row.requests_without_cost > 0 ? (
          /* The disclosure this page was missing, and the reason it was missing is worth
             stating: eight disclosures were built to stop a reader believing something
             false about these numbers, and none of them covered the cost column being
             EMPTY rather than zero. A tenant enforced in dollars with no pool had every
             row here read $0.00, and a person reading that concludes the work was free.
             Rendered per row rather than per report, because "3 of 88 requests have no
             cost" tells a reader whether the total is nearly right or nearly meaningless,
             and a single report-wide flag does not. */
          <div className="mt-1 text-destructive" data-testid="bt-row-missing-cost">
            {t('me_usage_by_tag.missing_cost', {
              missing: row.requests_without_cost,
              total: row.requests,
            })}
          </div>
        ) : null}
        {isSentinelBucket ? (
          /* The split is the whole reason these two counters exist. Folded into one
             "untagged" number, a month of somebody's mistyped tag is invisible
             behind people who simply never tagged anything. */
          <div className="mt-1 text-muted-foreground" data-testid="bt-row-unlabelled-split">
            {t('me_usage_by_tag.unlabelled_split', {
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

/** The current UTC billing period. UTC, because the period boundary is the
 *  backend's and a local-time month is wrong for most of the world twice a year. */
function currentPeriodUtc(): string {
  const now = new Date()
  return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}`
}
