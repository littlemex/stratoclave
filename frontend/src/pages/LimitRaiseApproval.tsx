import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Gavel } from 'lucide-react'

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
import { PoolBudgetCard } from '@/components/common/PoolBudgetCard'
import { usePermissions } from '@/hooks/usePermissions'
import { api, type ApiError, type LimitRaiseRequest } from '@/lib/api'
import { fmtMicroUsd, parseUsdToCents } from '@/lib/money'

/**
 * R12's tenant approval view. ONE component for the global approver
 * (`limits:approve`, any tenant) and the tenant-owning team lead
 * (`limits:approve-own`), on the same discipline `PoolBudgetCard` already
 * established: two renderings of one decision would eventually disagree.
 * `isAdmin` decides which route namespace (`admin` vs `teamLead`) the
 * mutations reach; the backend binds the actual authority inside its own
 * transaction regardless of which one the caller picked.
 */
export default function LimitRaiseApproval() {
  const { t } = useTranslation()
  const { tenantId = '' } = useParams<{ tenantId: string }>()
  const { isAdmin } = usePermissions()
  const qc = useQueryClient()

  const ns = isAdmin ? api.admin : api.teamLead

  const poolQuery = useQuery({
    queryKey: ['limit-raises', 'pool', tenantId, isAdmin],
    queryFn: async () => {
      try {
        return await ns.getPoolBudget(tenantId)
      } catch (err) {
        if ((err as ApiError)?.status === 404) return null
        throw err
      }
    },
    enabled: !!tenantId,
  })

  // R28: shown before it is typed. The bound is a fact about the PERIOD, so
  // it needs no tenant id -- the mirror endpoint exists only because the
  // route namespace does.
  const expiryQuery = useQuery({
    queryKey: ['limit-raises', 'latest-expiry', isAdmin],
    queryFn: () => ns.latestPermissibleExpiry(),
  })

  const queueQuery = useQuery({
    queryKey: ['limit-raises', 'queue', tenantId, isAdmin],
    queryFn: () => ns.listLimitRaises(tenantId, 'pending'),
    enabled: !!tenantId,
  })

  return (
    <div className="space-y-10">
      <header>
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          {t('limit_raise_approval.label')}
        </p>
        <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">
          {t('limit_raise_approval.title', { tenant: tenantId })}
        </h1>
        <p className="mt-2 max-w-xl text-sm text-muted-foreground">
          {t('limit_raise_approval.intro')}
        </p>
      </header>

      {/* R21b + R30 (the tenant's "now" and its ceiling composition) are ONE
          call, already rendered by this shared component -- this view adds
          nothing on top of it, per the contract's own "F3 renders, does not
          compute" rule. */}
      <PoolBudgetCard
        tenantId={tenantId}
        pool={poolQuery.data ?? null}
        isLoading={poolQuery.isLoading}
        onChanged={() => void qc.invalidateQueries({ queryKey: ['limit-raises', 'pool', tenantId] })}
        poolApi={{ setPoolBudget: ns.setPoolBudget }}
      />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 font-sans text-base font-semibold">
            <Gavel className="h-4 w-4 text-muted-foreground" />
            {t('limit_raise_approval.queue_title')}
          </CardTitle>
          <CardDescription>
            {expiryQuery.data ? (
              <span data-testid="lr-latest-permissible-expiry">
                {t('limit_raise_approval.latest_expiry', {
                  when: formatDate(
                    new Date(expiryQuery.data.latest_permissible_expiry * 1000).toISOString(),
                  ),
                })}
              </span>
            ) : (
              t('limit_raise_approval.queue_desc')
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {queueQuery.isLoading ? (
            <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
          ) : (queueQuery.data?.requests.length ?? 0) === 0 ? (
            <p className="text-sm text-muted-foreground">
              {t('limit_raise_approval.queue_empty')}
            </p>
          ) : (
            queueQuery.data!.requests.map((req) => (
              <DecisionRow
                key={req.request_id}
                request={req}
                isAdmin={isAdmin}
                latestPermissibleExpiry={expiryQuery.data?.latest_permissible_expiry ?? null}
                onDecided={() =>
                  void qc.invalidateQueries({ queryKey: ['limit-raises', 'queue', tenantId] })
                }
              />
            ))
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function DecisionRow({
  request,
  isAdmin,
  latestPermissibleExpiry,
  onDecided,
}: {
  request: LimitRaiseRequest
  isAdmin: boolean
  latestPermissibleExpiry: number | null
  onDecided: () => void
}) {
  const { t } = useTranslation()
  const ns = isAdmin ? api.admin : api.teamLead

  const [amountUsd, setAmountUsd] = useState(
    String(Math.round(request.asked_amount_microusd / 1_000_000)),
  )
  const [expiryLocal, setExpiryLocal] = useState(
    latestPermissibleExpiry != null
      ? toLocalInputValue(latestPermissibleExpiry)
      : '',
  )
  const [decisionComment, setDecisionComment] = useState('')
  const [error, setError] = useState<string | null>(null)

  const cents = parseUsdToCents(amountUsd)
  const approvedMicro = cents !== null ? cents * 10_000 : null
  const givingLess = approvedMicro !== null && approvedMicro < request.asked_amount_microusd

  const approve = useMutation({
    mutationFn: () => {
      if (approvedMicro === null) throw new Error('invalid amount')
      const expiresAtEpoch = Math.floor(new Date(expiryLocal).getTime() / 1000)
      return ns.approveLimitRaise(request.request_id, {
        approved_amount_microusd: approvedMicro,
        expires_at: expiresAtEpoch,
        decision_comment: decisionComment.trim() === '' ? undefined : decisionComment.trim(),
      })
    },
    onSuccess: onDecided,
    onError: (err: unknown) => {
      const e = err as ApiError | null
      // R36/B6: an approval that exceeds the remaining grant cap comes back
      // as 422 grant_cap_exceeded -- rendered legibly, not reimplemented
      // (the Interface section's "render an unknown code rather than
      // failing closed" rule applies just as much to a known one).
      setError(e?.detail ?? e?.message ?? t('limit_raise_approval.decide_error_fallback'))
    },
  })

  const reject = useMutation({
    mutationFn: () => {
      if (decisionComment.trim() === '') throw new Error('comment required')
      return ns.rejectLimitRaise(request.request_id, decisionComment.trim())
    },
    onSuccess: onDecided,
    onError: (err: unknown) => {
      const e = err as ApiError | null
      setError(e?.detail ?? e?.message ?? t('limit_raise_approval.decide_error_fallback'))
    },
  })

  return (
    <div className="space-y-3 border-t border-border/40 pt-4 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <p className="text-sm font-medium">
            {t('limit_raise_approval.asked_by', { user: request.user_id })}
          </p>
          <p className="text-xs text-muted-foreground">
            {t('limit_raise_approval.reason_is', { reason: request.reason_code })}
          </p>
        </div>
        <p className="font-mono text-sm font-semibold">
          {fmtMicroUsd(request.asked_amount_microusd)}
        </p>
      </div>
      {/* R12: the requester's own justification, rendered via plain JSX
          interpolation -- never dangerouslySetInnerHTML -- so a comment
          containing markup stays literal text. */}
      <p className="text-sm text-muted-foreground" data-testid="lr-comment">
        {request.comment ?? ''}
      </p>

      {/* R30's "at request time" half. `observed_limit_microusd` /
          `observed_remaining_microusd` are not captured by
          `submit_limit_raise` on this backend yet (a gap named in the F3
          report, not fixed here); rendered for real once they exist rather
          than a second, drifting implementation added later. Stated
          honestly rather than fabricated or silently omitted in the
          meantime. */}
      <p className="text-xs text-muted-foreground" data-testid="lr-snapshot-block">
        {request.observed_limit_microusd != null &&
        request.observed_remaining_microusd != null ? (
          t('limit_raise_approval.observed_snapshot', {
            limit: fmtMicroUsd(request.observed_limit_microusd),
            remaining: fmtMicroUsd(request.observed_remaining_microusd),
            when: request.observed_at ? formatDate(request.observed_at) : '?',
          })
        ) : (
          t('limit_raise_approval.observed_not_recorded')
        )}
      </p>

      <div className="grid gap-4 sm:grid-cols-3">
        <div className="space-y-1.5">
          <Label htmlFor={`amt-${request.request_id}`}>
            {t('limit_raise_approval.approve_amount_label')}
          </Label>
          <Input
            id={`amt-${request.request_id}`}
            inputMode="decimal"
            value={amountUsd}
            onChange={(e) => setAmountUsd(e.target.value)}
            data-testid="lr-approve-amount"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`exp-${request.request_id}`}>
            {t('limit_raise_approval.expiry_label')}
          </Label>
          <input
            id={`exp-${request.request_id}`}
            type="datetime-local"
            value={expiryLocal}
            max={
              latestPermissibleExpiry != null
                ? toLocalInputValue(latestPermissibleExpiry)
                : undefined
            }
            onChange={(e) => setExpiryLocal(e.target.value)}
            className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
            data-testid="lr-expiry-input"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`cmt-${request.request_id}`}>
            {t('limit_raise_approval.decision_comment_label')}
            {givingLess ? ' *' : ''}
          </Label>
          <Input
            id={`cmt-${request.request_id}`}
            value={decisionComment}
            onChange={(e) => setDecisionComment(e.target.value)}
            data-testid="lr-decision-comment"
          />
        </div>
      </div>
      {error ? <p className="text-sm text-destructive">{error}</p> : null}
      <div className="flex gap-2">
        <Button
          size="sm"
          disabled={
            approvedMicro === null ||
            !expiryLocal ||
            (givingLess && decisionComment.trim() === '') ||
            approve.isPending
          }
          onClick={() => {
            setError(null)
            approve.mutate()
          }}
          data-testid="lr-approve-button"
        >
          {t('limit_raise_approval.approve')}
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={decisionComment.trim() === '' || reject.isPending}
          onClick={() => {
            setError(null)
            reject.mutate()
          }}
          data-testid="lr-reject-button"
        >
          {t('limit_raise_approval.reject')}
        </Button>
      </div>
    </div>
  )
}

function toLocalInputValue(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}
