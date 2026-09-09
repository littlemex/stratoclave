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
import { TASK_TAG_PATTERN } from '@/lib/limits'
import { fmtMicroUsd, parseUsdToCents } from '@/lib/money'

/**
 * The two ceilings a member can ask to have raised. The wire values are the
 * backend's `RESERVE_LIMITS` keys verbatim (`mvp/reserve_limits.py`), because
 * `submit_limit_raise` validates `limit_kind` against that registry and refuses
 * anything else -- a display-friendly spelling here would be rejected there.
 */
const POOL_WALL = 'tenant_dollar_pool'
const USER_DOLLAR_WALL = 'user_dollar_quota'

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

/**
 * The request already holding this wall's day, off a `DailySlotOccupied` refusal.
 *
 * Narrowed field by field rather than cast: `detailBody` is `unknown` because any
 * endpoint can produce one, and a refusal whose shape has moved must degrade to
 * "no holder" -- which falls back to rendering the message -- rather than putting
 * `undefined` on screen where a request id belongs.
 */
interface SlotHolder {
  request_id: string
  status: string
  reset_at: string
}

function readSlotHolder(detailBody: unknown): SlotHolder | null {
  if (typeof detailBody !== 'object' || detailBody === null) return null
  const d = detailBody as Record<string, unknown>
  if (d.type !== 'limit_raise_daily_slot_occupied') return null
  const id = typeof d.holder_request_id === 'string' ? d.holder_request_id : ''
  if (id === '') return null
  return {
    request_id: id,
    status: typeof d.holder_status === 'string' ? d.holder_status : '',
    reset_at: typeof d.reset_at === 'string' ? d.reset_at : '',
  }
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
  const [taskTag, setTaskTag] = useState('')
  const [wall, setWall] = useState(POOL_WALL)
  const [error, setError] = useState<string | null>(null)
  // The request already on file, when the backend refuses because this wall's
  // once-a-day slot is taken. Held separately from `error` so an ambiguous retry
  // resolves into a fact ("here is what you filed") instead of a red sentence.
  const [slotHolder, setSlotHolder] = useState<SlotHolder | null>(null)

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
  // Grammar only. Empty is valid: a raise with no tag is accepted, because the
  // tag is attribution and not authorisation.
  const tagValid = taskTag.trim() === '' || TASK_TAG_PATTERN.test(taskTag.trim())

  const submit = useMutation({
    mutationFn: () => {
      if (cents === null) throw new Error('invalid amount')
      return api.submitLimitRaise({
        asked_amount_microusd: cents * 10_000,
        reason_code: reasonCode,
        // ONE TOKEN PER PRESS, not one per mount. `submit_limit_raise` treats a
        // repeated token as a replay and returns the request the FIRST call
        // produced -- so a token held for the component's lifetime meant that
        // changing the amount (or now the wall) and pressing again silently
        // returned the earlier request while this page cleared the form and
        // reported success. The requester saw a submission of something they had
        // not asked for, with nothing to indicate it.
        //
        // A fresh token per press cannot double-file: the once-per-wall-per-UTC-day
        // slot is what prevents that, and a second press after an ambiguous
        // failure gets a refusal naming the request already on file -- which is
        // what somebody unsure whether their submission landed needs to see.
        client_token: crypto.randomUUID(),
        limit_kind: wall,
        comment: comment.trim() === '' ? undefined : comment.trim(),
        // Sent verbatim, never lowercased here: the gateway canonicalises and
        // this page must not show a different string from the one it filed.
        task_tag: taskTag.trim() === '' ? undefined : taskTag.trim(),
      })
    },
    onSuccess: () => {
      setComment('')
      setAmountUsd('')
      setTaskTag('')
      setAmountTouched(false)
      void queryClient.invalidateQueries({ queryKey: ['me', 'limit-raises'] })
    },
    onError: (err: unknown) => {
      const e = err as ApiError | null
      const holder = readSlotHolder(e?.detailBody)
      setSlotHolder(holder)
      setError(
        holder != null
          ? null
          : (e?.detail ?? e?.message ?? t('me_limit_raises.submit_error_fallback')),
      )
    },
  })

  const pool = wallStatus.data?.pool ?? null
  // `undefined` and `null` mean different things and the difference is visible to
  // the requester: `null` is "this tenant has no per-user ceiling", while
  // `undefined` is a backend that predates the block and therefore cannot say.
  const userDollar = wallStatus.data?.user_dollar
  const userWallKnown = userDollar !== undefined

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
        <CardContent className="space-y-6">
          {wallStatus.isLoading ? (
            <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
          ) : (
            <>
              {/* Two walls, two headed groups. Before this change the card showed
                  two pool figures with no wall named, which was unambiguous only
                  while one wall existed -- with a personal ceiling in play, an
                  unlabelled pair of money figures reads as one budget. */}
              <section data-testid="wall-pool">
                <h3 className="mb-2 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                  {t('me_limit_raises.wall_pool_title')}
                </h3>
                {pool == null ? (
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
              </section>

              <section data-testid="wall-user-dollar">
                <h3 className="mb-2 text-xs font-medium uppercase tracking-[0.14em] text-muted-foreground">
                  {t('me_limit_raises.wall_user_dollar_title')}
                </h3>
                {!userWallKnown ? (
                  /* An older backend cannot answer, and saying so is not the same
                     as saying the wall is off. Reporting "not configured" here
                     would invent a fact from a missing field. */
                  <p className="text-sm text-muted-foreground" data-testid="user-dollar-unknown">
                    {t('me_limit_raises.user_dollar_unknown')}
                  </p>
                ) : userDollar == null ? (
                  <p className="text-sm text-muted-foreground" data-testid="user-dollar-absent">
                    {t('me_limit_raises.user_dollar_absent')}
                  </p>
                ) : (
                  <>
                    <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
                      <Stat
                        label={t('me_limit_raises.user_dollar_remaining_label')}
                        value={fmtMicroUsd(userDollar.remaining_microusd)}
                        negative={userDollar.remaining_microusd < 0}
                      />
                      <Stat
                        label={t('me_limit_raises.user_dollar_ceiling_label')}
                        value={fmtMicroUsd(userDollar.ceiling_microusd)}
                      />
                    </dl>
                    {userDollar.granted_microusd > 0 ? (
                      <p
                        className="mt-2 text-xs text-muted-foreground"
                        data-testid="user-dollar-granted"
                      >
                        {t('me_limit_raises.user_dollar_granted', {
                          base: fmtMicroUsd(userDollar.base_microusd),
                          granted: fmtMicroUsd(userDollar.granted_microusd),
                        })}
                      </p>
                    ) : null}
                  </>
                )}
              </section>
            </>
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
            <Label htmlFor="lr-wall">{t('me_limit_raises.wall_label')}</Label>
            <select
              id="lr-wall"
              value={wall}
              onChange={(e) => {
                setWall(e.target.value)
                // A previous refusal was about the previous wall. Clearing it
                // stops a stale "you already filed today" sitting above a
                // selector that now names a different, unfiled wall.
                setSlotHolder(null)
                setError(null)
              }}
              className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
              data-testid="lr-wall-select"
            >
              <option value={POOL_WALL}>{t('me_limit_raises.wall_option_pool')}</option>
              <option value={USER_DOLLAR_WALL}>
                {t('me_limit_raises.wall_option_user_dollar')}
              </option>
            </select>
            {/* The cost of choosing wrong is a day, and the backend only says so
                after the fact -- the slot is per wall per person per UTC day. */}
            <p className="text-xs text-muted-foreground" data-testid="lr-wall-slot-note">
              {t('me_limit_raises.wall_slot_note')}
            </p>
            {wall === USER_DOLLAR_WALL && userWallKnown && userDollar == null ? (
              <p className="text-xs text-destructive" data-testid="lr-wall-unconfigured">
                {t('me_limit_raises.wall_user_dollar_unconfigured')}
              </p>
            ) : null}
          </div>
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
            <Label htmlFor="lr-tag">{t('me_limit_raises.task_tag_label')}</Label>
            <Input
              id="lr-tag"
              autoComplete="off"
              spellCheck={false}
              value={taskTag}
              placeholder="migration-42"
              onChange={(e) => setTaskTag(e.target.value)}
              data-testid="lr-task-tag-input"
            />
            {/* Says where the value goes, not just what shape it must be. The tag
                is readable by anyone who can read this tenant's billing records,
                which is the fact that decides whether something belongs in it. */}
            <p className="text-xs text-muted-foreground" data-testid="lr-task-tag-note">
              {t('me_limit_raises.task_tag_note')}
            </p>
            {!tagValid ? (
              <p className="text-xs text-destructive" data-testid="lr-task-tag-invalid">
                {t('me_limit_raises.task_tag_invalid')}
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
          {slotHolder ? (
            /* Not an error: the day's filing already happened and this names it.
               A red sentence would tell somebody who is unsure whether their
               submission landed that something went wrong, when the truthful
               answer is that it went through. */
            <div
              className="rounded-md border border-border bg-muted/40 p-3 text-sm"
              data-testid="lr-slot-occupied"
            >
              <p>{t('me_limit_raises.slot_occupied')}</p>
              <p className="mt-1 font-mono text-xs text-muted-foreground">
                {slotHolder.request_id}
                {slotHolder.status ? ` — ${slotHolder.status}` : ''}
              </p>
              {slotHolder.reset_at ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  {t('me_limit_raises.slot_resets_at', { at: slotHolder.reset_at })}
                </p>
              ) : null}
            </div>
          ) : null}
          <Button
            disabled={
              !amountValid || reasonCode === '' || conflict || !tagValid || submit.isPending
            }
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
                  <TableHead>{t('me_limit_raises.col_wall')}</TableHead>
                  <TableHead>{t('me_limit_raises.col_reason')}</TableHead>
                  <TableHead>{t('me_limit_raises.col_task_tag')}</TableHead>
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
                      {/* Without this a requester holding raises against both walls
                          cannot tell them apart, and the two are different asks
                          with different approvers. The registry key is shown as-is
                          when this build has no label for it, rather than blank. */}
                      {wallLabel(row.limit_kind, t)}
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
                    <TableCell className="font-mono text-xs">
                      {/* The CANONICAL stored form, which may differ in case from
                          what was typed, rendered as text. Not re-validated: a tag
                          stored by an older client was checked by that client's
                          rules, and this column reports what is on the record. */}
                      {row.task_tag ? (
                        <span data-testid="lr-row-task-tag">{row.task_tag}</span>
                      ) : (
                        <span className="text-muted-foreground">
                          {t('me_limit_raises.task_tag_none')}
                        </span>
                      )}
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

/**
 * A wall's human label, falling back to the registry key.
 *
 * The fallback is the point: `RESERVE_LIMITS` can gain a wall this build has never
 * heard of, and showing its key is honest where showing nothing would erase the
 * one field that distinguishes two rows. Same rule the refusal renderer follows,
 * and the same rule `api.ts` states for `blocker` and `router_mode`.
 */
function wallLabel(limitKind: string, t: (k: string) => string): string {
  if (limitKind === POOL_WALL) return t('me_limit_raises.wall_option_pool')
  if (limitKind === USER_DOLLAR_WALL) return t('me_limit_raises.wall_option_user_dollar')
  return limitKind
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
