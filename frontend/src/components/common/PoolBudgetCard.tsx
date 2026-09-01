import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Wallet } from 'lucide-react'

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
import { type PoolBudget } from '@/lib/api'
import { currentPeriodUtc, fmtMicroUsd, parseUsdToCents } from '@/lib/money'
import { cn } from '@/lib/utils'

/**
 * The tenant's dollar pool: its ceiling, what the ceiling is MADE OF, and how it
 * got that way.
 *
 * ONE component for both roles, and that is the point rather than tidiness. A team
 * lead can set this ceiling, and setting it is the write that ends seat tracking —
 * so a card only the admin sees leaves the one role that can silently leave seat
 * tracking as the one role that cannot see it happened. Two components would also
 * be two renderings of one row, and they would eventually disagree.
 *
 * The mode is rendered as a SENTENCE the backend composes, not as a state name. A
 * field spelling "per_seat" named a state and said nothing an operator could act
 * on: not what the tenant is entitled to, not how the state was entered, and not
 * how to leave it.
 */
export interface PoolBudgetApi {
  setPoolBudget: (
    tenantId: string,
    body:
      | { limit_usd_cents: number; period?: string; status?: 'active' | 'suspended' }
      | { follow_seats: true; period?: string },
  ) => Promise<PoolBudget>
}

export function PoolBudgetCard({
  tenantId,
  pool,
  isLoading,
  onChanged,
  poolApi,
}: {
  tenantId: string
  pool: PoolBudget | null
  isLoading: boolean
  onChanged: () => void
  poolApi: PoolBudgetApi
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
          <div className="space-y-4">
            <p className="text-sm text-muted-foreground" data-testid="pool-mode-sentence">
              {pool.mode_sentence}
            </p>
            <dl
              className="grid gap-x-6 gap-y-3 sm:grid-cols-2"
              data-testid="pool-budget-summary"
            >
              <PoolStat
                label={t('admin_tenant_detail.pool.period_label')}
                value={pool.period}
                mono
              />
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
              {/* SIGNED and never clamped. "Nothing left" and "already $400 over"
                  are different problems, and a figure floored at zero shows them
                  as the same one. */}
              <PoolStat
                label={t('admin_tenant_detail.pool.available_label')}
                value={fmtMicroUsd(pool.available_microusd)}
                emphasise
                negative={pool.available_microusd < 0}
                testId="pool-available"
              />
              <PoolStat
                label={t('admin_tenant_detail.pool.reserved_label')}
                value={fmtMicroUsd(pool.pool_reserved_microusd)}
              />
              <PoolStat
                label={t('admin_tenant_detail.pool.settled_label')}
                value={fmtMicroUsd(pool.pool_settled_microusd)}
              />
              {/* The composition, so the total above can be checked against
                  something. The granted line is simply zero until grants exist,
                  which is why the parts always add up to the total beside them. */}
              <PoolStat
                label={t('admin_tenant_detail.pool.seats_label')}
                value={t('admin_tenant_detail.pool.seats_value', {
                  count: pool.seat_count,
                  entitlement: fmtMicroUsd(pool.seat_entitlement_microusd),
                })}
                testId="pool-seats"
              />
              <PoolStat
                label={t('admin_tenant_detail.pool.granted_label')}
                value={fmtMicroUsd(pool.pool_granted_microusd)}
                testId="pool-granted"
              />
            </dl>
            {pool.over_ceiling_microusd > 0 ? (
              <p className="text-sm text-destructive" data-testid="pool-over-ceiling">
                {t('admin_tenant_detail.pool.over_ceiling_by', {
                  amount: fmtMicroUsd(pool.over_ceiling_microusd),
                })}
              </p>
            ) : null}
            {pool.entitlement_exceeds_figure ? (
              <p
                className="text-sm text-muted-foreground"
                data-testid="pool-entitlement-outgrew"
              >
                {t('admin_tenant_detail.pool.entitlement_outgrew_figure', {
                  entitlement: fmtMicroUsd(pool.seat_entitlement_microusd),
                })}
              </p>
            ) : null}
            {/* The reversal, offered wherever the latch is visible. A figure used
                to be permanent because no request could undo it. */}
            {pool.resume_action ? (
              <FollowSeatsButton
                tenantId={tenantId}
                period={pool.period}
                poolApi={poolApi}
                onDone={onChanged}
              />
            ) : null}
          </div>
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
        poolApi={poolApi}
        onOpenChange={setDialogOpen}
        onDone={onChanged}
      />
    </Card>
  )
}

function FollowSeatsButton({
  tenantId,
  period,
  poolApi,
  onDone,
}: {
  tenantId: string
  period: string
  poolApi: PoolBudgetApi
  onDone: () => void
}) {
  const { t } = useTranslation()
  const [error, setError] = useState<string | null>(null)
  const mutation = useMutation({
    // `follow_seats: true` and never a figure. Sending the seat term as a number
    // would leave a figure behind, and the next hire would not move it.
    mutationFn: () => poolApi.setPoolBudget(tenantId, { follow_seats: true, period }),
    onSuccess: onDone,
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('admin_tenant_detail.pool.error_fallback'))
    },
  })
  return (
    <div className="space-y-1">
      <Button
        variant="outline"
        size="sm"
        disabled={mutation.isPending}
        onClick={() => mutation.mutate()}
        data-testid="pool-follow-seats-button"
      >
        {mutation.isPending
          ? t('admin_tenant_detail.pool.applying')
          : t('admin_tenant_detail.pool.follow_seats_button')}
      </Button>
      {error ? <p className="text-sm text-destructive">{error}</p> : null}
    </div>
  )
}

function PoolStat({
  label,
  value,
  mono,
  emphasise,
  negative,
  testId,
}: {
  label: string
  value: string
  mono?: boolean
  emphasise?: boolean
  negative?: boolean
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
          negative && 'text-destructive',
        )}
        data-testid={testId}
      >
        {value}
      </dd>
    </div>
  )
}

export function PoolBudgetDialog({
  open,
  tenantId,
  current,
  poolApi,
  onOpenChange,
  onDone,
}: {
  open: boolean
  tenantId: string
  current: PoolBudget | null
  poolApi: PoolBudgetApi
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const { t } = useTranslation()
  const [limitUsd, setLimitUsd] = useState(
    // Prefilled from the operator's OWN figure when there is one, not from the
    // total: the total may carry a granted term, and offering it back as the
    // figure would fold the grant into the figure the moment Save is pressed.
    current?.manual_limit_microusd != null
      ? String(Math.round(current.manual_limit_microusd / 1_000_000))
      : '',
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
      return poolApi.setPoolBudget(tenantId, {
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
            {/* Saying so before the click, not after: this is the write that ends
                seat tracking, and it was previously indistinguishable from
                adjusting a number. */}
            <p className="text-xs text-muted-foreground">
              {t('admin_tenant_detail.pool.latch_warning')}
            </p>
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
