import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
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
                remainingGrantCapMicrousd={poolQuery.data?.remaining_grant_cap_microusd ?? null}
                grantCapIsDerived={poolQuery.data?.grant_cap_is_derived ?? false}
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
  remainingGrantCapMicrousd,
  grantCapIsDerived,
  onDecided,
}: {
  request: LimitRaiseRequest
  isAdmin: boolean
  latestPermissibleExpiry: number | null
  remainingGrantCapMicrousd: number | null
  grantCapIsDerived: boolean
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
  // Held apart from `error` because these two refusals are not "the decision
  // failed" -- one says somebody else has to act first and this request survives,
  // the other says the request is over. A shared red line makes them identical.
  const [refusal, setRefusal] = useState<DecisionRefusal | null>(null)

  const cents = parseUsdToCents(amountUsd)
  const approvedMicro = cents !== null ? cents * 10_000 : null
  const givingLess = approvedMicro !== null && approvedMicro < request.asked_amount_microusd

  // R36/B6: the same bound the expiry field carries as its `max`, but for
  // the amount field -- which cannot express a bound as an attribute
  // (`inputMode="decimal"` free text, not a numeric input with `max`). Read
  // from the SAME pool call `PoolBudgetCard` already uses above; this is a
  // second line of defence in front of the transaction's own condition
  // check, not a replacement for it, so it can go stale behind a concurrent
  // approval without being wrong to show now.
  const overCap =
    remainingGrantCapMicrousd != null &&
    approvedMicro !== null &&
    approvedMicro > remainingGrantCapMicrousd

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
      const structured = readDecisionRefusal(e?.detailBody)
      setRefusal(structured)
      setError(
        structured != null
          ? null
          : (e?.detail ?? e?.message ?? t('limit_raise_approval.decide_error_fallback')),
      )
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
          {/* Shown BEFORE the field is typed into -- driven by the pool
              read alone, never by what has been entered -- and states
              whether the figure is a stored one or derived from the
              baseline, because "the cap is $0.00" with no explanation
              reads as a bug. */}
          {remainingGrantCapMicrousd != null ? (
            <p
              className="text-xs text-muted-foreground"
              data-testid="lr-remaining-grant-cap"
            >
              {t('limit_raise_approval.remaining_grant_cap', {
                remaining: fmtMicroUsd(remainingGrantCapMicrousd),
                derived: t(
                  grantCapIsDerived
                    ? 'limit_raise_approval.grant_cap_derived'
                    : 'limit_raise_approval.grant_cap_fixed',
                ),
              })}
            </p>
          ) : null}
          {overCap ? (
            <p className="text-xs text-destructive" data-testid="lr-amount-over-cap">
              {t('limit_raise_approval.amount_over_cap', {
                remaining: fmtMicroUsd(remainingGrantCapMicrousd ?? 0),
              })}
            </p>
          ) : null}
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
      {refusal ? <DecisionRefusalNotice refusal={refusal} request={request} /> : null}
      <div className="flex gap-2">
        <Button
          size="sm"
          disabled={
            approvedMicro === null ||
            !expiryLocal ||
            (givingLess && decisionComment.trim() === '') ||
            overCap ||
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

/**
 * A structured refusal from a decision, narrowed from `detailBody`.
 *
 * `extra` is deliberately NOT spread into named fields for the unknown case: an
 * unknown code's own fields were not written for this audience, so the renderer
 * below shows the code and a generic sentence rather than whatever prose or
 * figures arrived. Known codes get their fields read explicitly, by name.
 */
interface DecisionRefusal {
  type: string
  /** Only read for KNOWN codes. Never rendered for an unknown one. */
  message: string
  observedHeadroomMicrousd: number | null
  approvedAmountMicrousd: number | null
  filedPeriod: string
  currentPeriod: string
}

function readDecisionRefusal(detailBody: unknown): DecisionRefusal | null {
  if (typeof detailBody !== 'object' || detailBody === null) return null
  const d = detailBody as Record<string, unknown>
  if (typeof d.type !== 'string' || d.type === '') return null
  const num = (v: unknown): number | null => (typeof v === 'number' ? v : null)
  const str = (v: unknown): string => (typeof v === 'string' ? v : '')
  return {
    type: d.type,
    message: str(d.message),
    observedHeadroomMicrousd: num(d.observed_headroom_microusd),
    approvedAmountMicrousd: num(d.approved_amount_microusd),
    filedPeriod: str(d.filed_period),
    currentPeriod: str(d.current_period),
  }
}

/**
 * The two 409s the per-user wall introduced, told apart — plus a default arm.
 *
 * The distinction is the whole component. `pool_headroom_short` leaves the request
 * PENDING and re-approvable once somebody raises the tenant pool; an approver who
 * reads it as a generic failure tells the requester to refile, which burns her
 * once-a-day slot and produces a second request that will be refused identically.
 * `limit_raise_period_elapsed` is terminal — nothing can make a pinned period
 * current again — so waiting for it to clear is waiting for a state that cannot
 * arrive. Two mistakes in opposite directions from one indistinguishable red line.
 *
 * The default arm satisfies `api.ts`'s rule that an unknown code must render
 * rather than fail closed, WITHOUT rendering the refusal's own `message`: a future
 * refusal's sentence may be written for an operator, and this surface has no way to
 * know. The code itself is shown, because a short machine token is what makes the
 * refusal reportable, and a caller can read the body if they need more.
 */
function DecisionRefusalNotice({
  refusal,
  request,
}: {
  refusal: DecisionRefusal
  request: LimitRaiseRequest
}) {
  const { t } = useTranslation()

  if (refusal.type === 'pool_headroom_short') {
    const shortfall =
      refusal.approvedAmountMicrousd != null && refusal.observedHeadroomMicrousd != null
        ? refusal.approvedAmountMicrousd - refusal.observedHeadroomMicrousd
        : null
    return (
      <div
        className="space-y-2 rounded-md border border-border bg-muted/40 p-3 text-sm"
        data-testid="refusal-pool-headroom-short"
      >
        <p className="font-medium">{t('limit_raise_approval.refusal_pool_short_title')}</p>
        <p>{t('limit_raise_approval.refusal_pool_short_body')}</p>
        {refusal.observedHeadroomMicrousd != null ? (
          <p className="font-mono text-xs" data-testid="refusal-pool-headroom-figures">
            {t('limit_raise_approval.refusal_pool_short_figures', {
              headroom: fmtMicroUsd(refusal.observedHeadroomMicrousd),
              needed:
                refusal.approvedAmountMicrousd != null
                  ? fmtMicroUsd(refusal.approvedAmountMicrousd)
                  : '?',
            })}
          </p>
        ) : null}
        {/* The composition PR 4's design leaned on, as a ROUTE and not a
            transaction: the approver is taken to the pool-raise surface with the
            shortfall in hand, and nothing is filed for them. An approval that
            raised the pool by itself is the leg PR 4 deliberately removed — it
            would let a personal-raise approver create tenant-wide capacity that
            any other member could then spend.

            The shortfall travels in in-memory router state, never a query param: a
            tenant balance in a URL lands in browser history, copied links and proxy
            logs. The destination re-derives its own numbers; this is a hint. */}
        {shortfall != null && shortfall > 0 ? (
          <Link
            to="/team-lead"
            state={{ poolRaisePrefill: { shortfall_microusd: shortfall, tenant_id: request.tenant_id } }}
            className="inline-block text-sm underline underline-offset-4"
            data-testid="refusal-pool-raise-link"
          >
            {t('limit_raise_approval.refusal_pool_short_cta')}
          </Link>
        ) : null}
        <p className="text-xs text-muted-foreground">
          {t('limit_raise_approval.refusal_pool_short_still_pending')}
        </p>
      </div>
    )
  }

  if (refusal.type === 'limit_raise_period_elapsed') {
    return (
      <div
        className="space-y-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm"
        data-testid="refusal-period-elapsed"
      >
        <p className="font-medium">{t('limit_raise_approval.refusal_elapsed_title')}</p>
        <p>
          {t('limit_raise_approval.refusal_elapsed_body', {
            filed: refusal.filedPeriod || '?',
            current: refusal.currentPeriod || '?',
          })}
        </p>
        <p className="text-xs text-muted-foreground">
          {t('limit_raise_approval.refusal_elapsed_terminal')}
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-1 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm"
      data-testid="refusal-unknown"
    >
      <p>{t('limit_raise_approval.refusal_unknown')}</p>
      <p className="font-mono text-xs text-muted-foreground" data-testid="refusal-unknown-code">
        {refusal.type}
      </p>
    </div>
  )
}
