import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { HandCoins } from 'lucide-react'

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
import { api, type ApiError, type LimitRaiseRequest, type RaiseHint } from '@/lib/api'
import { fmtMicroUsd, parseUsdToCents } from '@/lib/money'

/**
 * U4 (contract journey amendment): the hint travels in router state from the
 * refusal that produced it. A screen opened WITHOUT that state -- a deep
 * link, a reload, a bookmark, or (today) simply every path into this page,
 * since nothing in this console yet sends a chat/completions request that
 * could 402 -- renders with no pre-filled amount and no wall named, rather
 * than reconstructing a "current" answer to "what refused you" that can
 * disagree with the refusal itself.
 */
interface LimitRaiseNavigationState {
  raiseHint?: RaiseHint
}

function useIncomingHint(): RaiseHint | null {
  const location = useLocation()
  const state = location.state as LimitRaiseNavigationState | null
  return state?.raiseHint ?? null
}

export default function MeLimitRaises() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const hint = useIncomingHint()

  const wallStatus = useQuery({
    queryKey: ['me', 'limit-raises', 'wall-status'],
    queryFn: () => api.myWallStatus(),
  })
  const mine = useQuery({
    queryKey: ['me', 'limit-raises'],
    queryFn: () => api.listMyLimitRaises(),
  })

  // U4/B6: pre-fill ONLY when the hint says the smallest grantable raise is
  // one an approver could actually grant. A conflict renders instead of an
  // amount, per the contract's own wording (item 4) -- never both.
  const conflict =
    hint != null && hint.minimum_raise_microusd > hint.remaining_cap_microusd
  const prefillUsd =
    hint != null && !conflict && hint.minimum_raise_microusd > 0
      ? (hint.minimum_raise_microusd / 1_000_000).toFixed(2)
      : ''

  const [reasonCode, setReasonCode] = useState('')
  const [comment, setComment] = useState('')
  const [amountUsd, setAmountUsd] = useState(prefillUsd)
  const [clientToken] = useState(() => crypto.randomUUID())
  const [error, setError] = useState<string | null>(null)

  // Re-apply the pre-fill if the hint changes (a fresh 402 while this tab is
  // already open) -- but never clobber text the requester has since typed.
  const [amountTouched, setAmountTouched] = useState(false)
  useEffect(() => {
    if (!amountTouched) setAmountUsd(prefillUsd)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefillUsd])

  const reasonCodes = mine.data?.reason_codes ?? hint?.reason_codes ?? []

  const cents = parseUsdToCents(amountUsd)
  const amountValid = cents !== null && cents > 0

  const submit = useMutation({
    mutationFn: () => {
      if (cents === null) throw new Error('invalid amount')
      return api.submitLimitRaise({
        asked_amount_microusd: cents * 10_000,
        reason_code: reasonCode,
        client_token: clientToken,
        comment: comment.trim() === '' ? undefined : comment.trim(),
      })
    },
    onSuccess: () => {
      setComment('')
      setAmountUsd('')
      setAmountTouched(false)
      void queryClient.invalidateQueries({ queryKey: ['me', 'limit-raises'] })
    },
    onError: (err: unknown) => {
      const e = err as ApiError | null
      setError(e?.detail ?? e?.message ?? t('me_limit_raises.submit_error_fallback'))
    },
  })

  const pool = wallStatus.data?.pool ?? null

  return (
    <div className="space-y-10">
      <header>
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          {t('me_limit_raises.label')}
        </p>
        <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">
          {t('me_limit_raises.title')}
        </h1>
        <p className="mt-2 max-w-xl text-sm text-muted-foreground">
          {t('me_limit_raises.intro')}
        </p>
      </header>

      <Card data-testid="wall-status-card">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 font-sans text-base font-semibold">
            <HandCoins className="h-4 w-4 text-muted-foreground" />
            {t('me_limit_raises.wall_status_title')}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {wallStatus.isLoading ? (
            <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
          ) : pool == null ? (
            <p className="text-sm text-muted-foreground">
              {t('me_limit_raises.no_pool')}
            </p>
          ) : (
            <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
              <Stat
                label={t('me_limit_raises.remaining_label')}
                value={fmtMicroUsd(pool.remaining_microusd)}
                negative={pool.remaining_microusd < 0}
              />
              <Stat
                label={t('me_limit_raises.remaining_grant_cap_label')}
                value={fmtMicroUsd(pool.remaining_grant_cap_microusd)}
              />
            </dl>
          )}
        </CardContent>
      </Card>

      {hint ? (
        <Card data-testid="raise-hint-card">
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">
              {t('me_limit_raises.hint_title')}
            </CardTitle>
            <CardDescription>
              {t('me_limit_raises.hint_desc', {
                wall: hint.candidates[0]?.blocker ?? '',
                model: hint.requested_model_id ?? '?',
              })}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {/* Interface note: the tenant this request will be filed
                against is carried from the hint alone, never from ambient
                client context (a query param, a stale session tenant, ...).
                Read-only display -- `submit_limit_raise` does not accept a
                `tenant_id` at all (it derives the caller's tenant from their
                session); this is provenance shown to the requester, not a
                field that travels on the submit call. */}
            <div className="space-y-1.5">
              <Label htmlFor="lr-tenant">{t('me_limit_raises.tenant_label')}</Label>
              <Input
                id="lr-tenant"
                value={hint.tenant_id ?? ''}
                readOnly
                disabled
                data-testid="lr-tenant-input"
              />
            </div>
            {hint.target_shortfall_microusd != null ? (
              <p data-testid="hint-target-shortfall">
                {t('me_limit_raises.hint_shortfall', {
                  amount: fmtMicroUsd(hint.target_shortfall_microusd),
                })}
              </p>
            ) : null}
            {hint.unattempted_model_ids.length > 0 ? (
              <p className="text-muted-foreground" data-testid="hint-unattempted">
                {t('me_limit_raises.hint_unattempted', {
                  models: hint.unattempted_model_ids.join(', '),
                  count: hint.unattempted_model_ids.length,
                })}
              </p>
            ) : null}
            {conflict ? (
              <p className="text-destructive" data-testid="hint-conflict">
                {t('me_limit_raises.hint_conflict', {
                  minimum: fmtMicroUsd(hint.minimum_raise_microusd),
                  remaining: fmtMicroUsd(hint.remaining_cap_microusd),
                })}
              </p>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('me_limit_raises.form_title')}
          </CardTitle>
          <CardDescription>{t('me_limit_raises.form_desc')}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="lr-reason">{t('me_limit_raises.reason_label')}</Label>
            <select
              id="lr-reason"
              value={reasonCode}
              onChange={(e) => setReasonCode(e.target.value)}
              className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
              data-testid="lr-reason-select"
            >
              <option value="">{t('me_limit_raises.reason_placeholder')}</option>
              {reasonCodes.map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="lr-amount">{t('me_limit_raises.amount_label')}</Label>
            <Input
              id="lr-amount"
              inputMode="decimal"
              autoComplete="off"
              value={amountUsd}
              disabled={conflict}
              placeholder="500"
              onChange={(e) => {
                setAmountTouched(true)
                setAmountUsd(e.target.value)
              }}
              data-testid="lr-amount-input"
            />
            {amountUsd.trim() !== '' && !amountValid ? (
              <p className="text-xs text-destructive">
                {t('me_limit_raises.invalid_amount')}
              </p>
            ) : null}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="lr-comment">{t('me_limit_raises.comment_label')}</Label>
            {/* Plain textarea: the comment is rendered elsewhere via ordinary
                JSX text interpolation, never dangerouslySetInnerHTML -- this
                is only the WRITE side, but kept next to it for the reader. */}
            <textarea
              id="lr-comment"
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              rows={3}
              className="flex w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
              data-testid="lr-comment-input"
            />
          </div>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
          <Button
            disabled={!amountValid || reasonCode === '' || conflict || submit.isPending}
            onClick={() => {
              setError(null)
              submit.mutate()
            }}
            data-testid="lr-submit-button"
          >
            {submit.isPending
              ? t('me_limit_raises.submitting')
              : t('me_limit_raises.submit')}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('me_limit_raises.mine_title')}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {mine.isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">{t('common.loading')}</p>
          ) : (mine.data?.requests.length ?? 0) === 0 ? (
            <p className="p-6 text-sm text-muted-foreground">
              {t('me_limit_raises.mine_empty')}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('me_limit_raises.col_when')}</TableHead>
                  <TableHead>{t('me_limit_raises.col_reason')}</TableHead>
                  <TableHead className="text-right">
                    {t('me_limit_raises.col_asked')}
                  </TableHead>
                  <TableHead>{t('me_limit_raises.col_status')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {mine.data!.requests.map((row) => (
                  <TableRow key={row.request_id}>
                    <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                      {formatDate(row.created_at)}
                    </TableCell>
                    <TableCell className="text-xs">
                      <div>{row.reason_code}</div>
                      {/* Plain JSX interpolation -- never dangerouslySetInnerHTML.
                          A comment containing `<b>`, an `onerror` attribute or a
                          literal `&amp;` renders as the literal text it is. */}
                      {row.decision_comment ? (
                        <div className="mt-1 text-muted-foreground">
                          {row.decision_comment}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {fmtMicroUsd(row.asked_amount_microusd)}
                    </TableCell>
                    <TableCell className="text-xs">
                      <RequestStatus row={row} />
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

/**
 * R24's "must state, not just carry": a decided request renders the
 * approved amount and its expiry, never the bare status string -- and a
 * PENDING one says plainly that nothing has changed yet, so it cannot be
 * mistaken for queued work.
 */
function RequestStatus({ row }: { row: LimitRaiseRequest }) {
  const { t } = useTranslation()
  // The wire value is the row's stored spelling verbatim (uppercase --
  // `STATUS_APPROVED`/`STATUS_REJECTED`/`STATUS_PENDING` in
  // `backend/dynamo/quota_events.py`), so this comparison matches that
  // spelling rather than a lowercase copy of it.
  if (row.status === 'APPROVED' && row.approved_amount_microusd != null) {
    return (
      <span data-testid="lr-status-approved">
        {t('me_limit_raises.status_approved', {
          amount: fmtMicroUsd(row.approved_amount_microusd),
          expires: row.expires_at != null ? formatExpiryUtc(row.expires_at) : '?',
        })}
        {/* R24: the approver must be visibly identified -- `approver_id` is
            a stable id (never an address); resolving it to a display name
            is a console-side lookup this row does not perform itself. The
            id is its own element (not folded into one interpolated
            sentence) so it renders as an identifiable, independently
            selectable piece of text. */}
        {row.approver_id ? (
          <div className="text-muted-foreground" data-testid="lr-status-approver">
            {t('me_limit_raises.approved_by_label')}
            {' '}
            <span>{row.approver_id}</span>
          </div>
        ) : null}
      </span>
    )
  }
  if (row.status === 'REJECTED') {
    return (
      <span data-testid="lr-status-rejected">
        {t('me_limit_raises.status_rejected')}
        {row.decision_comment ? `: ${row.decision_comment}` : ''}
      </span>
    )
  }
  if (row.status === 'PENDING') {
    return (
      <span data-testid="lr-status-pending">{t('me_limit_raises.status_pending')}</span>
    )
  }
  return <span>{row.status}</span>
}

function Stat({
  label,
  value,
  negative,
}: {
  label: string
  value: string
  negative?: boolean
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/40 pb-2">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className={`text-sm font-mono ${negative ? 'text-destructive' : ''}`}>{value}</dd>
    </div>
  )
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

/**
 * The expiry's own wording -- e.g. "Approved $50.00, expires Aug 31, 2026
 * 23:59 UTC" -- always UTC and always with the month spelled out, unlike
 * `formatDate`'s locale/timezone-dependent `toLocaleString()`: an approval's
 * deadline must read the same for every viewer regardless of their own
 * locale or timezone. `expires_at` is the epoch-SECONDS int every surface
 * in this codebase uses for it.
 */
function formatExpiryUtc(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000)
  const month = d.toLocaleString(undefined, { month: 'short', timeZone: 'UTC' })
  const day = d.getUTCDate()
  const year = d.getUTCFullYear()
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  return `${month} ${day}, ${year} ${hh}:${mm} UTC`
}
