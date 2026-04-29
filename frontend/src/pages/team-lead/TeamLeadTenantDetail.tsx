import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ArrowLeft, Edit3 } from 'lucide-react'

import { AccessDenied } from '@/components/common/AccessDenied'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
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
import { api, type TenantItem } from '@/lib/api'
import { cn } from '@/lib/utils'

function fmt(n: number): string {
  return n.toLocaleString()
}

export default function TeamLeadTenantDetail() {
  const { t } = useTranslation()
  const { tenantId = '' } = useParams<{ tenantId: string }>()
  const qc = useQueryClient()
  const [days, setDays] = useState(30)

  const ranges: Array<{ labelKey: string; days: number }> = [
    { labelKey: 'team_lead_tenant_detail.range_7d', days: 7 },
    { labelKey: 'team_lead_tenant_detail.range_30d', days: 30 },
    { labelKey: 'team_lead_tenant_detail.range_90d', days: 90 },
  ]

  const tenantQuery = useQuery({
    queryKey: ['team-lead', 'tenants', 'detail', tenantId],
    queryFn: () => api.teamLead.getTenant(tenantId),
    enabled: !!tenantId,
    retry: false,
  })

  const membersQuery = useQuery({
    queryKey: ['team-lead', 'tenants', 'members', tenantId],
    queryFn: () => api.teamLead.members(tenantId),
    enabled: !!tenantId && tenantQuery.isSuccess,
  })

  const usageQuery = useQuery({
    queryKey: ['team-lead', 'tenants', 'usage', tenantId, days],
    queryFn: () => api.teamLead.usage(tenantId, days),
    enabled: !!tenantId && tenantQuery.isSuccess,
  })

  const invalidateTenant = () => {
    void qc.invalidateQueries({ queryKey: ['team-lead', 'tenants', 'detail', tenantId] })
    void qc.invalidateQueries({ queryKey: ['team-lead', 'tenants'] })
  }

  if (tenantQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">{t('common.loading_ellipsis')}</p>
  }

  // Backend returns 404 for:
  //   1. a non-owner hitting somebody else's tenant (enumeration guard)
  //   2. a tenant_id that does not exist
  if (tenantQuery.isError || !tenantQuery.data) {
    return (
      <AccessDenied
        title={t('team_lead_tenant_detail.access_denied_title')}
        description={t('team_lead_tenant_detail.access_denied_desc')}
        homeHref="/team-lead/tenants"
      />
    )
  }

  const tenant = tenantQuery.data
  const usage = usageQuery.data
  const totalTokens = usage?.total_tokens ?? 0
  const byModel = Object.entries(usage?.by_model ?? {}).sort((a, b) => b[1] - a[1])
  const byUserEmail = Object.entries(usage?.by_user_email ?? {}).sort(
    (a, b) => b[1] - a[1],
  )
  const members = membersQuery.data?.members ?? []

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/team-lead/tenants">
          <ArrowLeft className="h-4 w-4" />
          {t('team_lead_tenant_detail.back_to_list')}
        </Link>
      </Button>

      <div>
        <div className="flex items-center gap-2">
          <h1 className="font-display text-3xl tracking-tight">{tenant.name}</h1>
          <Badge variant={tenant.status === 'archived' ? 'muted' : 'secondary'}>
            {tenant.status}
          </Badge>
        </div>
        <code className="mt-1 block font-mono text-xs text-muted-foreground">
          {tenant.tenant_id}
        </code>
      </div>

      <section className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              {t('tenant.default_credit')}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-display text-2xl tracking-tight">
              {fmt(tenant.default_credit)}
              <span className="ml-1 text-xs font-sans font-normal text-muted-foreground">
                {t('common.tokens')}
              </span>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              {t('tenant.members')}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-display text-2xl tracking-tight">
              {members.length}
              <span className="ml-1 text-xs font-sans font-normal text-muted-foreground">
                {t('team_lead_tenant_detail.stat_members_unit')}
              </span>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              {t('team_lead_tenant_detail.stat_usage_title')}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-display text-2xl tracking-tight">
              {fmt(totalTokens)}
              <span className="ml-1 text-xs font-sans font-normal text-muted-foreground">
                {t('common.tokens')}
              </span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {usage
                ? t('team_lead_tenant_detail.stat_usage_footer', {
                    samples: fmt(usage.sample_size),
                  })
                : ' '}
            </p>
          </CardContent>
        </Card>
      </section>

      <EditButton tenant={tenant} onDone={invalidateTenant} />

      <Card>
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div>
            <CardTitle className="font-sans text-base font-semibold">
              {t('team_lead_tenant_detail.members_card_title')}
            </CardTitle>
            <CardDescription>
              {t('team_lead_tenant_detail.members_card_desc')}
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('common.email')}</TableHead>
                <TableHead>{t('dashboard.stat_role')}</TableHead>
                <TableHead className="text-right">{t('tenant.remaining_credit')}</TableHead>
                <TableHead className="text-right">{t('tenant.usage')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {members.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                    {t('team_lead_tenant_detail.members_empty')}
                  </TableCell>
                </TableRow>
              ) : (
                members.map((m) => (
                  <TableRow key={m.email}>
                    <TableCell className="font-medium">
                      {m.email || t('common.email_unset')}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          m.role === 'admin'
                            ? 'accent'
                            : m.role === 'team_lead'
                              ? 'default'
                              : 'secondary'
                        }
                      >
                        {m.role}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {fmt(m.remaining_credit)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs text-muted-foreground">
                      {fmt(m.credit_used)} / {fmt(m.total_credit)}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div>
            <CardTitle className="font-sans text-base font-semibold">
              {t('team_lead_tenant_detail.usage_card_title')}
            </CardTitle>
            <CardDescription>
              {t('team_lead_tenant_detail.usage_card_desc')}
            </CardDescription>
          </div>
          <div
            className="flex gap-2"
            role="radiogroup"
            aria-label={t('team_lead_tenant_detail.range_aria')}
          >
            {ranges.map((r) => (
              <Button
                key={r.days}
                role="radio"
                aria-checked={days === r.days}
                variant={days === r.days ? 'default' : 'outline'}
                size="sm"
                onClick={() => setDays(r.days)}
              >
                {t(r.labelKey)}
              </Button>
            ))}
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          <section>
            <h3 className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
              {t('team_lead_tenant_detail.by_model')}
            </h3>
            {byModel.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {t('team_lead_tenant_detail.by_model_empty')}
              </p>
            ) : (
              <ul className="space-y-2">
                {byModel.map(([model, tokens]) => {
                  const pct = totalTokens > 0 ? Math.round((tokens / totalTokens) * 100) : 0
                  return (
                    <li key={model} className="space-y-1">
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
                      <div className="h-1 w-full overflow-hidden rounded-sm bg-muted">
                        <div
                          className={cn('h-full bg-primary transition-all')}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>

          <section>
            <h3 className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
              {t('team_lead_tenant_detail.by_member')}
            </h3>
            {byUserEmail.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {t('team_lead_tenant_detail.by_member_empty')}
              </p>
            ) : (
              <ul className="space-y-2">
                {byUserEmail.map(([email, tokens]) => {
                  const pct = totalTokens > 0 ? Math.round((tokens / totalTokens) * 100) : 0
                  return (
                    <li key={email} className="space-y-1">
                      <div className="flex items-baseline justify-between gap-3">
                        <span className="truncate text-sm">{email}</span>
                        <span className="text-sm font-medium">
                          {fmt(tokens)}{' '}
                          <span className="text-xs text-muted-foreground">
                            {t('common.tokens')} ({pct}%)
                          </span>
                        </span>
                      </div>
                      <div className="h-1 w-full overflow-hidden rounded-sm bg-muted">
                        <div
                          className={cn('h-full bg-accent transition-all')}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </CardContent>
      </Card>
    </div>
  )
}

function EditButton({
  tenant,
  onDone,
}: {
  tenant: TenantItem
  onDone: () => void
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState(tenant.name)
  const [defaultCredit, setDefaultCredit] = useState(String(tenant.default_credit))
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () =>
      api.teamLead.updateTenant(tenant.tenant_id, {
        name: name !== tenant.name ? name : undefined,
        default_credit:
          Number(defaultCredit) !== tenant.default_credit ? Number(defaultCredit) : undefined,
      }),
    onSuccess: () => {
      setOpen(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('team_lead_tenant_detail.edit_error_fallback'))
    },
  })

  return (
    <>
      <div>
        <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
          <Edit3 className="h-4 w-4" />
          {t('team_lead_tenant_detail.edit_button')}
        </Button>
      </div>
      <Dialog
        open={open}
        onOpenChange={(v) => {
          if (!v) {
            setError(null)
            setName(tenant.name)
            setDefaultCredit(String(tenant.default_credit))
          }
          setOpen(v)
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t('team_lead_tenant_detail.edit_title')}</DialogTitle>
            <DialogDescription>
              {t('team_lead_tenant_detail.edit_desc')}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="tl-edit-name">{t('tenant.name')}</Label>
              <Input id="tl-edit-name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="tl-edit-default">{t('tenant.default_credit')}</Label>
              <Input
                id="tl-edit-default"
                type="number"
                min={0}
                max={10_000_000}
                value={defaultCredit}
                onChange={(e) => setDefaultCredit(e.target.value)}
              />
            </div>
          </div>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button disabled={mutation.isPending} onClick={() => mutation.mutate()}>
              {mutation.isPending ? t('common.updating') : t('common.update')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
