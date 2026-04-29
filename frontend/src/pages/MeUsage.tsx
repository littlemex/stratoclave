import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Layers } from 'lucide-react'

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'

function fmt(n: number): string {
  return n.toLocaleString()
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

export default function MeUsage() {
  const { t } = useTranslation()
  const [days, setDays] = useState(30)

  const ranges: Array<{ labelKey: string; days: number }> = [
    { labelKey: 'me_usage.range_7d', days: 7 },
    { labelKey: 'me_usage.range_30d', days: 30 },
    { labelKey: 'me_usage.range_90d', days: 90 },
  ]

  const summary = useQuery({
    queryKey: ['me', 'usage-summary', days],
    queryFn: () => api.usageSummary(days),
  })
  const history = useQuery({
    queryKey: ['me', 'usage-history', days],
    queryFn: () => api.usageHistory({ since_days: days, limit: 50 }),
  })

  const byModel = useMemo(() => {
    const d = summary.data?.by_model ?? {}
    return Object.entries(d).sort((a, b) => b[1] - a[1])
  }, [summary.data])

  const totalUsedInRange = useMemo(
    () => Object.values(summary.data?.by_model ?? {}).reduce((a, b) => a + b, 0),
    [summary.data],
  )

  return (
    <div className="space-y-10">
      <header className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
            {t('me_usage.label')}
          </p>
          <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">
            {t('me_usage.title')}
          </h1>
          <p className="mt-2 max-w-xl text-sm text-muted-foreground">
            {t('me_usage.intro')}
          </p>
        </div>
        <div
          className="flex gap-1 border border-border bg-card p-0.5"
          role="radiogroup"
          aria-label={t('me_usage.range_aria')}
        >
          {ranges.map((r) => (
            <button
              key={r.days}
              role="radio"
              aria-checked={days === r.days}
              onClick={() => setDays(r.days)}
              className={cn(
                'px-3 py-1.5 text-xs font-medium transition-colors',
                days === r.days
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {t(r.labelKey)}
            </button>
          ))}
        </div>
      </header>

      <section className="grid gap-4 md:grid-cols-3">
        <StatBlock label={t('me_usage.stat_total')}>
          <div className="flex items-baseline gap-2">
            <span className="strato-stat font-display text-3xl font-semibold tracking-tight">
              {summary.data ? fmt(totalUsedInRange) : '—'}
            </span>
            <span className="text-xs text-muted-foreground">{t('common.tokens')}</span>
          </div>
          {summary.data ? (
            <p className="mt-2 font-mono text-[11px] text-muted-foreground">
              {t('me_usage.stat_total_footer', {
                samples: fmt(summary.data.sample_size),
                days: summary.data.since_days,
              })}
            </p>
          ) : null}
        </StatBlock>

        <StatBlock label={t('me_usage.stat_remaining')}>
          <div className="flex items-baseline gap-2">
            <span className="strato-stat font-display text-3xl font-semibold tracking-tight">
              {summary.data ? fmt(summary.data.remaining_credit) : '—'}
            </span>
            <span className="text-xs text-muted-foreground">{t('common.tokens')}</span>
          </div>
          {summary.data ? (
            <p className="mt-2 font-mono text-[11px] text-muted-foreground">
              {fmt(summary.data.credit_used)} / {fmt(summary.data.total_credit)}
            </p>
          ) : null}
        </StatBlock>

        <StatBlock label={t('me_usage.stat_tenants')}>
          <div className="flex items-baseline gap-2">
            <span className="strato-stat font-display text-3xl font-semibold tracking-tight">
              {summary.data ? Object.keys(summary.data.by_tenant).length : '—'}
            </span>
            <span className="text-xs text-muted-foreground">
              {t('me_usage.stat_tenants_unit')}
            </span>
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground">
            {t('me_usage.stat_tenants_footer')}
          </p>
        </StatBlock>
      </section>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('me_usage.by_model_title')}
          </CardTitle>
          <CardDescription>
            {t('me_usage.by_model_desc')}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {summary.isLoading ? (
            <p className="text-sm text-muted-foreground">{t('common.loading_ellipsis')}</p>
          ) : byModel.length === 0 ? (
            <EmptyState message={t('me_usage.empty_models')} />
          ) : (
            <ul className="space-y-3">
              {byModel.map(([model, tokens]) => {
                const pct = totalUsedInRange > 0
                  ? Math.round((tokens / totalUsedInRange) * 100)
                  : 0
                return (
                  <li key={model} className="space-y-1.5">
                    <div className="flex items-baseline justify-between gap-3">
                      <code className="truncate font-mono text-xs text-muted-foreground">
                        {model}
                      </code>
                      <span className="text-sm font-medium">
                        {fmt(tokens)}{' '}
                        <span className="text-xs text-muted-foreground">
                          {t('common.tokens')} ({pct}%)
                        </span>
                      </span>
                    </div>
                    <div className="h-1 w-full overflow-hidden bg-muted/70">
                      <div
                        className="h-full bg-primary transition-all"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('me_usage.recent_title')}
          </CardTitle>
          <CardDescription>
            {t('me_usage.recent_desc')}
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {history.isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">{t('common.loading_ellipsis')}</p>
          ) : (history.data?.history.length ?? 0) === 0 ? (
            <EmptyState message={t('me_usage.empty_history')} />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('me_usage.col_when')}</TableHead>
                  <TableHead>{t('me_usage.col_model')}</TableHead>
                  <TableHead>{t('me_usage.col_tenant')}</TableHead>
                  <TableHead className="text-right">{t('me_usage.col_input')}</TableHead>
                  <TableHead className="text-right">{t('me_usage.col_output')}</TableHead>
                  <TableHead className="text-right">{t('me_usage.col_total')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {history.data!.history.map((row) => (
                  <TableRow key={row.recorded_at + row.model_id}>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {formatDate(row.recorded_at)}
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {row.model_id}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {row.tenant_name ?? row.tenant_id}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {fmt(row.input_tokens)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {fmt(row.output_tokens)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs font-semibold">
                      {fmt(row.total_tokens)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function StatBlock({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
          {label}
        </p>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-10 text-center text-sm text-muted-foreground">
      <Layers className="h-5 w-5 text-muted-foreground/60" aria-hidden />
      <p>{message}</p>
    </div>
  )
}
