import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import { ArrowLeft, Archive, Edit3, UserCog, Wallet } from 'lucide-react'

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
import { api, type PoolBudget, type TenantItem, type TenantRoutingConfig } from '@/lib/api'
import { currentPeriodUtc, fmtMicroUsd, parseUsdToCents } from '@/lib/money'
import { cn } from '@/lib/utils'

function fmt(n: number): string {
  return n.toLocaleString()
}

export default function AdminTenantDetail() {
  const { t } = useTranslation()
  const { tenantId = '' } = useParams<{ tenantId: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const tenantQuery = useQuery({
    queryKey: ['admin', 'tenants', 'detail', tenantId],
    queryFn: () => api.admin.getTenant(tenantId),
    enabled: !!tenantId,
  })
  const membersQuery = useQuery({
    queryKey: ['admin', 'tenants', 'members', tenantId],
    queryFn: () => api.admin.tenantUsers(tenantId),
    enabled: !!tenantId,
  })
  const usageQuery = useQuery({
    queryKey: ['admin', 'tenants', 'usage', tenantId],
    queryFn: () => api.admin.tenantUsage(tenantId, 30),
    enabled: !!tenantId,
  })
  // A-1: pool budget for the current period. A 404 means "no pool set" — a
  // normal state, not an error — so we swallow it and render the empty card.
  const poolQuery = useQuery({
    queryKey: ['admin', 'tenants', 'pool', tenantId],
    queryFn: async () => {
      try {
        return await api.admin.getPoolBudget(tenantId)
      } catch (err) {
        if ((err as { status?: number } | null)?.status === 404) return null
        throw err
      }
    },
    enabled: !!tenantId,
  })
  // P0-11: tenant routing config (chain / quotas / allowlist). Always returns a
  // config (configured=false when unset), so no 404 handling needed.
  const routingQuery = useQuery({
    queryKey: ['admin', 'tenants', 'routing', tenantId],
    queryFn: () => api.admin.getRoutingConfig(tenantId),
    enabled: !!tenantId,
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['admin', 'tenants'] })
  }

  if (tenantQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">{t('admin_tenant_detail.loading')}</p>
  }
  if (tenantQuery.isError || !tenantQuery.data) {
    return <p className="text-sm text-destructive">{t('admin_tenant_detail.not_found')}</p>
  }

  const tenant = tenantQuery.data
  const usage = usageQuery.data
  const members = membersQuery.data?.members ?? []
  const usageByModel = Object.entries(usage?.by_model ?? {}).sort(
    (a, b) => b[1] - a[1],
  )
  const totalTokens = usage?.total_tokens ?? 0

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/admin/tenants">
          <ArrowLeft className="h-4 w-4" />
          {t('admin_tenant_detail.back_to_list')}
        </Link>
      </Button>

      <div>
        <div className="flex items-center gap-2">
          <h1 className="font-display text-3xl tracking-tight">{tenant.name}</h1>
          <Badge variant={tenant.status === 'archived' ? 'muted' : 'secondary'}>
            {tenant.status}
          </Badge>
        </div>
        <code className="mt-1 block font-mono text-xs text-muted-foreground">{tenant.tenant_id}</code>
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
              {t('admin_tenant_detail.card_owner_title')}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1">
            {tenant.team_lead_user_id === 'admin-owned' ? (
              <Badge variant="muted">admin-owned</Badge>
            ) : (
              <code className="font-mono text-xs text-muted-foreground">
                {tenant.team_lead_user_id}
              </code>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              {t('admin_tenant_detail.card_usage_title')}
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
                ? t('admin_tenant_detail.card_usage_footer', {
                    samples: fmt(usage.sample_size),
                  })
                : ' '}
            </p>
          </CardContent>
        </Card>
      </section>

      <ActionBar
        tenant={tenant}
        onChanged={invalidate}
        onDeleted={() => navigate('/admin/tenants')}
      />

      <PoolBudgetCard
        tenantId={tenant.tenant_id}
        pool={poolQuery.data ?? null}
        isLoading={poolQuery.isLoading}
        onChanged={() =>
          void qc.invalidateQueries({
            queryKey: ['admin', 'tenants', 'pool', tenantId],
          })
        }
      />

      <RoutingConfigCard
        tenantId={tenant.tenant_id}
        config={routingQuery.data ?? null}
        isLoading={routingQuery.isLoading}
        onChanged={() =>
          void qc.invalidateQueries({
            queryKey: ['admin', 'tenants', 'routing', tenantId],
          })
        }
      />

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('admin_tenant_detail.members_title')}
          </CardTitle>
          <CardDescription>
            {t('admin_tenant_detail.members_desc')}
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('common.email')}</TableHead>
                <TableHead>{t('dashboard.stat_role')}</TableHead>
                <TableHead className="text-right">
                  {t('tenant.remaining_credit')}
                </TableHead>
                <TableHead className="text-right">{t('tenant.usage')}</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {members.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={5}
                    className="py-6 text-center text-muted-foreground"
                  >
                    {t('admin_tenant_detail.members_empty')}
                  </TableCell>
                </TableRow>
              ) : (
                members.map((m) => (
                  <TableRow key={m.user_id}>
                    <TableCell>
                      <div className="font-medium">
                        {m.email || t('common.email_unset')}
                      </div>
                      <code className="mt-0.5 block truncate font-mono text-xs text-muted-foreground">
                        {m.user_id}
                      </code>
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
                    <TableCell className="text-right">
                      <Button asChild variant="ghost" size="sm">
                        <Link to={`/admin/users/${encodeURIComponent(m.user_id)}`}>
                          {t('common.details')}
                        </Link>
                      </Button>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('admin_tenant_detail.usage_by_model_title')}
          </CardTitle>
          <CardDescription>
            {t('admin_tenant_detail.usage_by_model_desc')}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {usageByModel.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {t('admin_tenant_detail.usage_by_model_empty')}
            </p>
          ) : (
            <ul className="space-y-2">
              {usageByModel.map(([model, tokens]) => {
                const pct = totalTokens > 0 ? Math.round((tokens / totalTokens) * 100) : 0
                return (
                  <li key={model} className="space-y-1">
                    <div className="flex items-baseline justify-between gap-3">
                      <code className="truncate font-mono text-xs text-muted-foreground">{model}</code>
                      <span className="text-sm font-medium">
                        {fmt(tokens)}{' '}
                        <span className="text-xs text-muted-foreground">
                          {t('common.tokens')} ({pct}%)
                        </span>
                      </span>
                    </div>
                    <div className="h-1 w-full overflow-hidden rounded-sm bg-muted">
                      <div className={cn('h-full bg-primary transition-all')} style={{ width: `${pct}%` }} />
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
            {t('admin_tenant_detail.usage_by_user_title')}
          </CardTitle>
          <CardDescription>
            {t('admin_tenant_detail.usage_by_user_desc')}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {usage && Object.keys(usage.by_user ?? {}).length > 0 ? (
            <ul className="space-y-2">
              {Object.entries(usage.by_user ?? {})
                .sort((a, b) => b[1] - a[1])
                .map(([user, tokens]) => {
                  const pct = totalTokens > 0 ? Math.round((tokens / totalTokens) * 100) : 0
                  return (
                    <li key={user} className="space-y-1">
                      <div className="flex items-baseline justify-between gap-3">
                        <span className="truncate text-sm">{user}</span>
                        <span className="text-sm font-medium">
                          {fmt(tokens)}{' '}
                          <span className="text-xs text-muted-foreground">
                            {t('common.tokens')} ({pct}%)
                          </span>
                        </span>
                      </div>
                      <div className="h-1 w-full overflow-hidden rounded-sm bg-muted">
                        <div className={cn('h-full bg-accent transition-all')} style={{ width: `${pct}%` }} />
                      </div>
                    </li>
                  )
                })}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">
              {t('admin_tenant_detail.usage_by_user_empty')}
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// ------------------------------------------------------------------
// Action bar: edit / owner / archive
// ------------------------------------------------------------------
function ActionBar({
  tenant,
  onChanged,
  onDeleted,
}: {
  tenant: TenantItem
  onChanged: () => void
  onDeleted: () => void
}) {
  const { t } = useTranslation()
  const [editOpen, setEditOpen] = useState(false)
  const [ownerOpen, setOwnerOpen] = useState(false)
  const [archiveOpen, setArchiveOpen] = useState(false)

  const isDefaultOrg = tenant.tenant_id === 'default-org'

  return (
    <section className="flex flex-wrap gap-2">
      <Button variant="outline" size="sm" onClick={() => setEditOpen(true)}>
        <Edit3 className="h-4 w-4" />
        {t('admin_tenant_detail.edit')}
      </Button>
      <Button variant="outline" size="sm" onClick={() => setOwnerOpen(true)}>
        <UserCog className="h-4 w-4" />
        {t('admin_tenant_detail.reassign_owner')}
      </Button>
      <Button
        variant="destructive"
        size="sm"
        disabled={isDefaultOrg || tenant.status === 'archived'}
        onClick={() => setArchiveOpen(true)}
        title={
          isDefaultOrg ? t('admin_tenant_detail.archive_default_org_hint') : undefined
        }
      >
        <Archive className="h-4 w-4" />
        {t('admin_tenant_detail.archive')}
      </Button>

      <EditDialog
        open={editOpen}
        tenant={tenant}
        onOpenChange={setEditOpen}
        onDone={onChanged}
      />
      <OwnerDialog
        open={ownerOpen}
        tenant={tenant}
        onOpenChange={setOwnerOpen}
        onDone={onChanged}
      />
      <ArchiveDialog
        open={archiveOpen}
        tenant={tenant}
        onOpenChange={setArchiveOpen}
        onDone={onDeleted}
      />
    </section>
  )
}

// ------------------------------------------------------------------
// Pool budget: shared dollar ceiling for the tenant (A-1)
// ------------------------------------------------------------------
function PoolBudgetCard({
  tenantId,
  pool,
  isLoading,
  onChanged,
}: {
  tenantId: string
  pool: PoolBudget | null
  isLoading: boolean
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const [dialogOpen, setDialogOpen] = useState(false)

  return (
    <Card data-testid="pool-budget-card">
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2 font-sans text-base font-semibold">
              <Wallet className="h-4 w-4 text-muted-foreground" />
              {t('admin_tenant_detail.pool.title')}
            </CardTitle>
            <CardDescription>{t('admin_tenant_detail.pool.desc')}</CardDescription>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setDialogOpen(true)}
            data-testid="pool-budget-set-button"
          >
            {pool
              ? t('admin_tenant_detail.pool.edit_button')
              : t('admin_tenant_detail.pool.set_button')}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
        ) : pool ? (
          <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2" data-testid="pool-budget-summary">
            <PoolStat label={t('admin_tenant_detail.pool.period_label')} value={pool.period} mono />
            <PoolStat
              label={t('admin_tenant_detail.pool.status_label')}
              value={pool.status}
            />
            <PoolStat
              label={t('admin_tenant_detail.pool.limit_label')}
              value={fmtMicroUsd(pool.pool_limit_microusd)}
              emphasise
              testId="pool-limit"
            />
            <PoolStat
              label={t('admin_tenant_detail.pool.remaining_label')}
              value={fmtMicroUsd(pool.remaining_microusd)}
              emphasise
              testId="pool-remaining"
            />
            <PoolStat
              label={t('admin_tenant_detail.pool.reserved_label')}
              value={fmtMicroUsd(pool.pool_reserved_microusd)}
            />
            <PoolStat
              label={t('admin_tenant_detail.pool.settled_label')}
              value={fmtMicroUsd(pool.pool_settled_microusd)}
            />
          </dl>
        ) : (
          <div className="space-y-1" data-testid="pool-budget-empty">
            <p className="text-sm font-medium">{t('admin_tenant_detail.pool.none_title')}</p>
            <p className="text-sm text-muted-foreground">
              {t('admin_tenant_detail.pool.none_desc', { period: currentPeriodUtc() })}
            </p>
          </div>
        )}
      </CardContent>

      <PoolBudgetDialog
        open={dialogOpen}
        tenantId={tenantId}
        current={pool}
        onOpenChange={setDialogOpen}
        onDone={onChanged}
      />
    </Card>
  )
}

function PoolStat({
  label,
  value,
  mono,
  emphasise,
  testId,
}: {
  label: string
  value: string
  mono?: boolean
  emphasise?: boolean
  testId?: string
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/40 pb-2">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd
        className={cn(
          'text-sm',
          mono && 'font-mono',
          emphasise && 'font-display text-base tracking-tight',
        )}
        data-testid={testId}
      >
        {value}
      </dd>
    </div>
  )
}

function PoolBudgetDialog({
  open,
  tenantId,
  current,
  onOpenChange,
  onDone,
}: {
  open: boolean
  tenantId: string
  current: PoolBudget | null
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const { t } = useTranslation()
  const [limitUsd, setLimitUsd] = useState(
    current ? String(Math.round(current.pool_limit_usd_cents / 100)) : '',
  )
  const [period, setPeriod] = useState(current?.period ?? '')
  const [status, setStatus] = useState<'active' | 'suspended'>(
    current?.status === 'suspended' ? 'suspended' : 'active',
  )
  const [error, setError] = useState<string | null>(null)

  const cents = parseUsdToCents(limitUsd)
  const amountValid = cents !== null

  const mutation = useMutation({
    mutationFn: () => {
      if (cents === null) throw new Error('invalid amount')
      return api.admin.setPoolBudget(tenantId, {
        limit_usd_cents: cents,
        period: period.trim() === '' ? undefined : period.trim(),
        status,
      })
    },
    onSuccess: () => {
      onOpenChange(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('admin_tenant_detail.pool.error_fallback'))
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) setError(null)
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t('admin_tenant_detail.pool.dialog_title')}</DialogTitle>
          <DialogDescription>{t('admin_tenant_detail.pool.dialog_desc')}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="pool-limit-usd">
              {t('admin_tenant_detail.pool.limit_usd_label')}
            </Label>
            <Input
              id="pool-limit-usd"
              inputMode="decimal"
              autoComplete="off"
              value={limitUsd}
              placeholder={t('admin_tenant_detail.pool.limit_usd_placeholder')}
              onChange={(e) => setLimitUsd(e.target.value)}
              data-testid="pool-limit-usd-input"
            />
            {limitUsd.trim() !== '' && !amountValid ? (
              <p className="text-xs text-destructive">
                {t('admin_tenant_detail.pool.invalid_amount')}
              </p>
            ) : null}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="pool-period">
              {t('admin_tenant_detail.pool.period_input_label')}
            </Label>
            <Input
              id="pool-period"
              autoComplete="off"
              value={period}
              placeholder="2026-07"
              onChange={(e) => setPeriod(e.target.value)}
              data-testid="pool-period-input"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="pool-status">{t('admin_tenant_detail.pool.status_label')}</Label>
            <select
              id="pool-status"
              value={status}
              onChange={(e) => setStatus(e.target.value === 'suspended' ? 'suspended' : 'active')}
              className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
            >
              <option value="active">{t('admin_tenant_detail.pool.status_active')}</option>
              <option value="suspended">{t('admin_tenant_detail.pool.status_suspended')}</option>
            </select>
          </div>
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button
            disabled={!amountValid || mutation.isPending}
            onClick={() => mutation.mutate()}
            data-testid="pool-budget-submit"
          >
            {mutation.isPending
              ? t('admin_tenant_detail.pool.applying')
              : t('admin_tenant_detail.pool.apply')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function EditDialog({
  open,
  tenant,
  onOpenChange,
  onDone,
}: {
  open: boolean
  tenant: TenantItem
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const { t } = useTranslation()
  const [name, setName] = useState(tenant.name)
  const [defaultCredit, setDefaultCredit] = useState(String(tenant.default_credit))
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.updateTenant(tenant.tenant_id, {
        name: name !== tenant.name ? name : undefined,
        default_credit:
          Number(defaultCredit) !== tenant.default_credit ? Number(defaultCredit) : undefined,
      }),
    onSuccess: () => {
      onOpenChange(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('admin_tenant_detail.edit_error_fallback'))
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) setError(null)
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t('admin_tenant_detail.edit_title')}</DialogTitle>
          <DialogDescription>{t('admin_tenant_detail.edit_desc')}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="edit-name">{t('tenant.name')}</Label>
            <Input id="edit-name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="edit-default">{t('tenant.default_credit')}</Label>
            <Input
              id="edit-default"
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
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? t('common.updating') : t('common.update')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function OwnerDialog({
  open,
  tenant,
  onOpenChange,
  onDone,
}: {
  open: boolean
  tenant: TenantItem
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const { t } = useTranslation()
  const [owner, setOwner] = useState(tenant.team_lead_user_id ?? 'admin-owned')
  const [error, setError] = useState<string | null>(null)

  const teamLeadUsersQuery = useQuery({
    queryKey: ['admin', 'users', 'team_lead'],
    queryFn: () => api.admin.listUsers({ role: 'team_lead', limit: 100 }),
    enabled: open,
  })

  const mutation = useMutation({
    mutationFn: () => api.admin.setOwner(tenant.tenant_id, owner),
    onSuccess: () => {
      onOpenChange(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('admin_tenant_detail.owner_error_fallback'))
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) setError(null)
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t('admin_tenant_detail.owner_title')}</DialogTitle>
          <DialogDescription>
            {t('admin_tenant_detail.owner_desc')}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label htmlFor="owner-select">
            {t('admin_tenant_detail.owner_new_label')}
          </Label>
          <select
            id="owner-select"
            value={owner}
            onChange={(e) => setOwner(e.target.value)}
            className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
          >
            <option value="admin-owned">
              {t('admin_tenants.create_owner_admin_owned')}
            </option>
            {(teamLeadUsersQuery.data?.users ?? []).map((u) => (
              <option key={u.user_id} value={u.user_id}>
                {u.email || u.user_id}
              </option>
            ))}
          </select>
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button
            disabled={owner === tenant.team_lead_user_id || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending
              ? t('admin_tenant_detail.owner_applying')
              : t('admin_tenant_detail.owner_apply')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ArchiveDialog({
  open,
  tenant,
  onOpenChange,
  onDone,
}: {
  open: boolean
  tenant: TenantItem
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const { t } = useTranslation()
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => api.admin.archiveTenant(tenant.tenant_id),
    onSuccess: () => {
      onOpenChange(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('admin_tenant_detail.archive_error_fallback'))
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) {
          setConfirm('')
          setError(null)
        }
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="text-destructive">
            {t('admin_tenant_detail.archive_title')}
          </DialogTitle>
          <DialogDescription>
            {t('admin_tenant_detail.archive_desc')}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label htmlFor="archive-confirm">
            <Trans
              i18nKey="admin_tenant_detail.archive_confirm_label"
              values={{ id: tenant.tenant_id }}
              components={{
                1: <code className="font-mono text-foreground" />,
              }}
            />
          </Label>
          <Input
            id="archive-confirm"
            autoComplete="off"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder={tenant.tenant_id}
          />
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button
            variant="destructive"
            disabled={confirm !== tenant.tenant_id || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending
              ? t('admin_tenant_detail.archive_submitting')
              : t('admin_tenant_detail.archive_submit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// P0-11: view + edit the tenant routing config (chain / quotas / allowlist) —
// the config the per-model-quota + cascading-fallback enforcement reads. The
// editor takes raw JSON: the shape is structured (ordered chain, per-model
// quota map) so a JSON body is the honest input, and the backend validates it
// (model ids, quota limits, coherence) returning a 400 that names the offending
// field. This closes the P0 gap where routing was enforced but unconfigurable.
function RoutingConfigCard({
  tenantId,
  config,
  isLoading,
  onChanged,
}: {
  tenantId: string
  config: TenantRoutingConfig | null
  isLoading: boolean
  onChanged: () => void
}) {
  const [dialogOpen, setDialogOpen] = useState(false)
  const configured = !!config?.configured
  return (
    <Card data-testid="routing-config-card">
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div className="space-y-1.5">
            <CardTitle className="font-sans text-base font-semibold">
              Routing config
            </CardTitle>
            <CardDescription>
              Per-model quota + cascading fallback (chain, quotas, allowlist).
              Absent = passthrough on the requested model.
            </CardDescription>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setDialogOpen(true)}
            data-testid="routing-config-edit-button"
          >
            {configured ? 'Edit' : 'Set'}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : configured ? (
          <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2" data-testid="routing-config-summary">
            <RoutingStat label="Chain" value={config!.chain.join(' → ') || '(none)'} />
            <RoutingStat label="Allowlist" value={config!.allowlist.join(', ') || '(unrestricted)'} />
            <RoutingStat
              label="Quotas"
              value={
                Object.keys(config!.quotas).length
                  ? Object.entries(config!.quotas)
                      .map(([m, q]) => `${m}: ${q.limit ?? '∞'}`)
                      .join('; ')
                  : '(none)'
              }
            />
            <RoutingStat label="Fallback default" value={config!.fallback_default} />
            <RoutingStat
              label="Shadow VSR (advisory)"
              value={
                config!.shadow_vsr === true
                  ? 'on'
                  : config!.shadow_vsr === false
                    ? 'off'
                    : 'default'
              }
            />
          </dl>
        ) : (
          <div className="space-y-1" data-testid="routing-config-empty">
            <p className="text-sm font-medium">No routing config</p>
            <p className="text-sm text-muted-foreground">
              This tenant serves the requested model with no chain/quota/allowlist.
            </p>
          </div>
        )}
      </CardContent>
      <RoutingConfigDialog
        open={dialogOpen}
        tenantId={tenantId}
        current={config}
        onOpenChange={setDialogOpen}
        onDone={onChanged}
      />
    </Card>
  )
}

function RoutingStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/40 pb-2">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="text-sm font-mono">{value}</dd>
    </div>
  )
}

function RoutingConfigDialog({
  open,
  tenantId,
  current,
  onOpenChange,
  onDone,
}: {
  open: boolean
  tenantId: string
  current: TenantRoutingConfig | null
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const initial = () =>
    JSON.stringify(
      {
        chain: current?.chain ?? [],
        allowlist: current?.allowlist ?? [],
        quotas: current?.quotas ?? {},
        fallback_default: current?.fallback_default ?? 'off',
        ...(current?.free_tier_model ? { free_tier_model: current.free_tier_model } : {}),
        // Advisory only — no effect on execution, billing, or routing; controls
        // whether the shadow judge records potential-saving advisories for the
        // Savings Certificate. Seeded so a full-replace save never silently drops
        // it (true/false explicit, null = follow the global default).
        shadow_vsr: current?.shadow_vsr ?? null,
      },
      null,
      2,
    )
  const [text, setText] = useState(initial)
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => {
      let body: Record<string, unknown>
      try {
        body = JSON.parse(text)
      } catch (e) {
        throw new Error(`Invalid JSON: ${(e as Error).message}`)
      }
      return api.admin.setRoutingConfig(tenantId, body)
    },
    onSuccess: () => {
      onOpenChange(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? 'Failed to save routing config')
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (v) {
          setText(initial())
          setError(null)
        }
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Routing config</DialogTitle>
          <DialogDescription>
            Full replace. Every model id is validated against the registry;
            quotas are monthly micro-USD caps. Errors name the offending field.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <textarea
            data-testid="routing-config-json"
            className="h-64 w-full rounded-md border border-input bg-background p-2 font-mono text-xs"
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
          />
          {error && (
            <p className="text-sm text-destructive" data-testid="routing-config-error">
              {error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
            data-testid="routing-config-save"
          >
            {mutation.isPending ? 'Saving…' : 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
