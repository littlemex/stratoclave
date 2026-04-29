import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import { Filter } from 'lucide-react'

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
import { api } from '@/lib/api'

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

export default function AdminUsageLogs() {
  const { t } = useTranslation()
  const [tenantId, setTenantId] = useState('')
  const [userId, setUserId] = useState('')
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')
  const [applied, setApplied] = useState<{
    tenant_id?: string
    user_id?: string
    since?: string
    until?: string
  }>({})
  const [cursor, setCursor] = useState<string | undefined>()
  const [cursorStack, setCursorStack] = useState<Array<string | undefined>>([])

  const logsQuery = useQuery({
    queryKey: ['admin', 'usage-logs', applied, cursor],
    queryFn: () =>
      api.admin.usageLogs({
        ...applied,
        cursor,
        limit: 100,
      }),
    placeholderData: (p) => p,
  })

  const apply = () => {
    setApplied({
      tenant_id: tenantId || undefined,
      user_id: userId || undefined,
      since: since || undefined,
      until: until || undefined,
    })
    setCursor(undefined)
    setCursorStack([])
  }

  const nextCursor = logsQuery.data?.next_cursor ?? null

  return (
    <div className="space-y-6">
      <header>
        <h1 className="font-display text-3xl tracking-tight">
          {t('admin_usage_logs.title')}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {t('admin_usage_logs.intro')}
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('admin_usage_logs.filter_title')}
          </CardTitle>
          <CardDescription>
            <Trans
              i18nKey="admin_usage_logs.filter_desc"
              components={{ 1: <code className="font-mono text-xs" /> }}
            />
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3 md:grid-cols-4">
            <div className="space-y-1.5">
              <Label htmlFor="filter-tenant">tenant_id</Label>
              <Input
                id="filter-tenant"
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
                placeholder="default-org"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="filter-user">user_id</Label>
              <Input
                id="filter-user"
                value={userId}
                onChange={(e) => setUserId(e.target.value)}
                placeholder="a4f8..."
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="filter-since">since</Label>
              <Input
                id="filter-since"
                value={since}
                onChange={(e) => setSince(e.target.value)}
                placeholder="2026-04-01T00:00:00Z"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="filter-until">until</Label>
              <Input
                id="filter-until"
                value={until}
                onChange={(e) => setUntil(e.target.value)}
                placeholder="2026-04-30T23:59:59Z"
              />
            </div>
          </div>
          <div className="mt-4 flex gap-2">
            <Button size="sm" onClick={apply}>
              <Filter className="h-4 w-4" />
              {t('admin_usage_logs.apply')}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setTenantId('')
                setUserId('')
                setSince('')
                setUntil('')
                setApplied({})
                setCursor(undefined)
                setCursorStack([])
              }}
            >
              {t('admin_usage_logs.clear')}
            </Button>
          </div>
        </CardContent>
      </Card>

      <div className="border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('admin_usage_logs.col_when')}</TableHead>
              <TableHead>{t('admin_usage_logs.col_user')}</TableHead>
              <TableHead>{t('admin_usage_logs.col_tenant')}</TableHead>
              <TableHead>{t('admin_usage_logs.col_model')}</TableHead>
              <TableHead className="text-right">{t('admin_usage_logs.col_input')}</TableHead>
              <TableHead className="text-right">{t('admin_usage_logs.col_output')}</TableHead>
              <TableHead className="text-right">{t('admin_usage_logs.col_total')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {logsQuery.isLoading ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center text-muted-foreground">
                  {t('common.loading_ellipsis')}
                </TableCell>
              </TableRow>
            ) : (logsQuery.data?.logs.length ?? 0) === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center text-muted-foreground">
                  {t('admin_usage_logs.row_empty')}
                </TableCell>
              </TableRow>
            ) : (
              logsQuery.data!.logs.map((log) => (
                <TableRow key={log.timestamp_log_id}>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                    {formatDate(log.recorded_at)}
                  </TableCell>
                  <TableCell>
                    <div className="text-xs">
                      {log.user_email ?? t('admin_usage_logs.col_user_email_none')}
                    </div>
                    <code className="font-mono text-[10px] text-muted-foreground">
                      {log.user_id}
                    </code>
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {log.tenant_id}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{log.model_id}</TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {fmt(log.input_tokens)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {fmt(log.output_tokens)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs font-semibold">
                    {fmt(log.total_tokens)}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>
          {t('admin_usage_logs.count_showing', {
            shown: logsQuery.data?.logs.length ?? 0,
          })}
          {nextCursor ? t('admin_usage_logs.count_more') : ''}
        </span>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={cursorStack.length === 0}
            onClick={() =>
              setCursorStack((s) => {
                const next = [...s]
                const prev = next.pop()
                setCursor(prev)
                return next
              })
            }
          >
            {t('common.prev')}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={!nextCursor}
            onClick={() => {
              setCursorStack((s) => [...s, cursor])
              setCursor(nextCursor ?? undefined)
            }}
          >
            {t('common.next')}
          </Button>
        </div>
      </div>
    </div>
  )
}
