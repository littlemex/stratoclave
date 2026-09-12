import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ArrowLeft } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  api,
  discoveryErrorDetail,
  type ActivateDiscoveryCandidateResponse,
  type DiscoveryVerdict,
  type ProbeDiscoveryCandidateResponse,
} from '@/lib/api'

const INVOCATIONS = ['sync', 'stream'] as const

export default function AdminDiscoveryCandidateDetail() {
  const { t } = useTranslation()
  const { profileId = '' } = useParams<{ profileId: string }>()
  const qc = useQueryClient()

  const candidate = useQuery({
    queryKey: ['admin-discovery-candidate', profileId],
    queryFn: () => api.discovery.candidate(profileId),
    enabled: profileId.length > 0,
  })

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ['admin-discovery-candidate', profileId] })

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/admin/discovery/candidates">
          <ArrowLeft className="h-4 w-4" />
          {t('admin_discovery_candidate_detail.back_to_candidates')}
        </Link>
      </Button>

      {candidate.isLoading ? (
        <p className="text-sm text-muted-foreground">{t('common.loading_ellipsis')}</p>
      ) : candidate.error || !candidate.data ? (
        <p className="text-sm text-destructive">
          {t('admin_discovery_candidate_detail.load_error')}
        </p>
      ) : (
        <>
          <div>
            <h1 className="font-display text-3xl tracking-tight">{candidate.data.profile_id}</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {candidate.data.model_family} · {candidate.data.profile_scope}
            </p>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="font-sans text-base font-semibold">
                {t('admin_discovery_candidate_detail.section_details')}
              </CardTitle>
            </CardHeader>
            <CardContent className="grid grid-cols-2 gap-3 text-sm">
              <Detail
                label={t('admin_discovery_candidate_detail.label_state')}
                value={candidate.data.state}
              />
              <Detail
                label={t('admin_discovery_candidate_detail.label_provider')}
                value={candidate.data.provider}
              />
              <Detail
                label={t('admin_discovery_candidate_detail.label_bedrock_id')}
                value={candidate.data.bedrock_model_id}
                mono
              />
              <Detail
                label={t('admin_discovery_candidate_detail.label_bedrock_region')}
                value={candidate.data.bedrock_region}
              />
              <Detail
                label={t('admin_discovery_candidate_detail.label_pricing_key')}
                value={candidate.data.pricing_key}
                mono
              />
              <Detail
                label={t('admin_discovery_candidate_detail.label_jurisdiction')}
                value={candidate.data.jurisdiction ?? '—'}
              />
              <Detail
                label={t('admin_discovery_candidate_detail.label_wire_protocol')}
                value={candidate.data.wire_protocol}
              />
              <Detail
                label={t('admin_discovery_candidate_detail.label_created')}
                value={`${candidate.data.created_at} (${candidate.data.created_by})`}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="font-sans text-base font-semibold">
                {t('admin_discovery_candidate_detail.section_identifiers')}
              </CardTitle>
              <CardDescription>
                {t('admin_discovery_candidate_detail.identifiers_intro')}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ul className="list-inside list-disc text-sm">
                {candidate.data.identifiers.map((id) => (
                  <li key={id} className="font-mono">
                    {id}
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="font-sans text-base font-semibold">
                {t('admin_discovery_candidate_detail.section_verdicts')}
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('admin_discovery_candidate_detail.col_invocation')}</TableHead>
                    <TableHead>{t('admin_discovery_candidate_detail.col_status')}</TableHead>
                    <TableHead>{t('admin_discovery_candidate_detail.col_verified_at')}</TableHead>
                    <TableHead>{t('admin_discovery_candidate_detail.col_verified_by')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {INVOCATIONS.map((inv) => {
                    const v = candidate.data!.verdicts[inv]
                    return (
                      <TableRow key={inv}>
                        <TableCell className="text-xs">{inv}</TableCell>
                        <TableCell className="text-xs">
                          <Badge
                            variant={
                              v?.state === 'verified'
                                ? 'accent'
                                : v?.state === 'invalidated'
                                  ? 'destructive'
                                  : 'muted'
                            }
                          >
                            {t(
                              `admin_discovery_candidate_detail.verdict_${v?.state ?? 'unverified'}`,
                            )}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {v?.verified_at ?? '—'}
                        </TableCell>
                        <TableCell className="text-xs text-muted-foreground">
                          {v?.verified_by ?? '—'}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          <ProbeCard profileId={profileId} onProbed={invalidate} />

          <ActivateCard
            profileId={profileId}
            verdicts={candidate.data.verdicts}
            onActivated={invalidate}
          />
        </>
      )}
    </div>
  )
}

function Detail({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className={mono ? 'font-mono text-xs' : 'text-sm'}>{value}</p>
    </div>
  )
}

/**
 * The probe: a completed check, not an error. `passed: false` is a normal
 * 200 response, rendered as a neutral/amber result panel -- never the
 * destructive red banner an actual refusal (a permanent blocker, an
 * indeterminate provider timeout, a ledger refusal) gets, because those
 * never ran at all and this did.
 */
function ProbeCard({ profileId, onProbed }: { profileId: string; onProbed: () => void }) {
  const { t } = useTranslation()
  const [invocation, setInvocation] = useState<string>('')
  const [result, setResult] = useState<ProbeDiscoveryCandidateResponse | null>(null)

  const probe = useMutation({
    mutationFn: () => api.admin.probeDiscoveryCandidate(profileId, { invocation }),
    onSuccess: (resp) => {
      setResult(resp)
      onProbed()
    },
  })

  const err = probe.error
    ? discoveryErrorDetail(probe.error, t('admin_discovery_candidate_detail.probe_error_fallback'))
    : null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="font-sans text-base font-semibold">
          {t('admin_discovery_candidate_detail.section_probe')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-end gap-3">
          {/* The sibling create form labels its protocol select; this one did
              not, so a screen reader announced an unnamed combobox and a
              sighted operator saw a dropdown offering "sync" and "stream"
              with nothing saying what was being chosen. */}
          <div className="space-y-1.5">
            <Label htmlFor="dc-probe-invocation">
              {t('admin_discovery_candidate_detail.field_invocation')}
            </Label>
            <select
              id="dc-probe-invocation"
              value={invocation}
              onChange={(e) => {
                setInvocation(e.target.value)
                setResult(null)
              }}
              className="flex h-10 rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <option value="">—</option>
              {INVOCATIONS.map((inv) => (
                <option key={inv} value={inv}>
                  {inv}
                </option>
              ))}
            </select>
          </div>
          <Button
            disabled={!invocation || probe.isPending}
            onClick={() => {
              setResult(null)
              probe.mutate()
            }}
          >
            {probe.isPending
              ? t('admin_discovery_candidate_detail.probe_pending')
              : t('admin_discovery_candidate_detail.probe_button')}
          </Button>
        </div>

        {result ? (
          <div
            className={
              result.passed
                ? 'rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-xs text-emerald-700 dark:text-emerald-300'
                : 'rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300'
            }
          >
            <p className="font-medium">
              {result.passed
                ? t('admin_discovery_candidate_detail.probe_result_passed')
                : t('admin_discovery_candidate_detail.probe_result_failed')}
            </p>
            {result.blocker ? <p className="mt-1">{result.blocker.evidence}</p> : null}
            {result.charged_microusd != null ? (
              <p className="mt-1">
                {t('admin_discovery_candidate_detail.probe_charged', {
                  amount: result.charged_microusd,
                })}
              </p>
            ) : null}
          </div>
        ) : null}

        {err ? (
          <p className="text-sm text-destructive">
            {err.message}
            {err.type ? <span className="ml-2 font-mono text-xs">{err.type}</span> : null}
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}

/**
 * Activation reads the verified-at identity from the candidate the operator
 * is already looking at -- never a text field -- so it cannot activate
 * against a verdict the operator never saw.
 */
function ActivateCard({
  profileId,
  verdicts,
  onActivated,
}: {
  profileId: string
  verdicts: Record<string, DiscoveryVerdict>
  onActivated: () => void
}) {
  const { t } = useTranslation()
  const [result, setResult] = useState<ActivateDiscoveryCandidateResponse | null>(null)
  const [activatingInvocation, setActivatingInvocation] = useState<string | null>(null)

  const activate = useMutation({
    mutationFn: (invocation: string) =>
      api.admin.activateDiscoveryCandidate(profileId, {
        invocation,
        verified_at: verdicts[invocation]!.verified_at!,
      }),
    onSuccess: (resp) => {
      setResult(resp)
      onActivated()
    },
    onSettled: () => setActivatingInvocation(null),
  })

  const err = activate.error
    ? discoveryErrorDetail(
        activate.error,
        t('admin_discovery_candidate_detail.activate_error_fallback'),
      )
    : null

  // Activation sends the verdict's own `verified_at` as the identity to
  // compare-and-set against, and `verified_at` is optional on the wire. A
  // verdict that reads verified without one is therefore not activatable: the
  // request would omit the identity and be refused for a reason the operator
  // cannot act on. Excluding it here shows the "run a probe first" line
  // instead, which is the actionable answer.
  const verifiedInvocations = INVOCATIONS.filter(
    (inv) => verdicts[inv]?.state === 'verified' && !!verdicts[inv]?.verified_at,
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle className="font-sans text-base font-semibold">
          {t('admin_discovery_candidate_detail.section_activate')}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {verifiedInvocations.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            {t('admin_discovery_candidate_detail.activate_requires_verified')}
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {verifiedInvocations.map((inv) => (
              <Button
                key={inv}
                size="sm"
                disabled={activate.isPending}
                onClick={() => {
                  setActivatingInvocation(inv)
                  activate.mutate(inv)
                }}
              >
                {activate.isPending && activatingInvocation === inv
                  ? t('admin_discovery_candidate_detail.activate_pending')
                  : `${t('admin_discovery_candidate_detail.activate_button')} (${inv})`}
              </Button>
            ))}
          </div>
        )}

        {result ? (
          <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-xs text-emerald-700 dark:text-emerald-300">
            <p className="font-medium">
              {t('admin_discovery_candidate_detail.activate_success_title')}
            </p>
            <p className="mt-1 font-mono">{result.identifiers.join(', ')}</p>
          </div>
        ) : null}

        {err ? (
          <p className="text-sm text-destructive">
            {err.message}
            {err.type ? <span className="ml-2 font-mono text-xs">{err.type}</span> : null}
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}
