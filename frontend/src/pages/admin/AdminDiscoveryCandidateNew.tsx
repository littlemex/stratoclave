import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ArrowLeft } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { BlockerList } from '@/components/discovery/BlockerList'
import { api, discoveryErrorDetail, type CreateDiscoveryCandidateResponse } from '@/lib/api'

const WIRE_PROTOCOLS = ['messages', 'responses'] as const

/**
 * Create a promotion candidate from a discovered record plus the three
 * human decisions (aliases, pricing key, jurisdiction) and the declared
 * wire protocol. `profile_id` arrives as a query param from the records
 * list; the record (and its CURRENT `revision`) is re-fetched here rather
 * than trusted from the caller, so the compare-and-set the backend enforces
 * is against what this form actually saw, not a value that may have gone
 * stale while the operator was reading the records list.
 */
export default function AdminDiscoveryCandidateNew() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const profileId = params.get('profile_id') ?? ''

  const [aliases, setAliases] = useState('')
  const [pricingKey, setPricingKey] = useState('')
  const [jurisdiction, setJurisdiction] = useState('')
  const [wireProtocol, setWireProtocol] = useState<string>('')
  const [success, setSuccess] = useState<CreateDiscoveryCandidateResponse | null>(null)

  const record = useQuery({
    queryKey: ['admin-discovery-record', profileId],
    queryFn: () => api.discovery.record(profileId),
    enabled: profileId.length > 0,
  })

  const create = useMutation({
    mutationFn: () =>
      api.admin.createDiscoveryCandidate({
        profile_id: profileId,
        revision: record.data!.revision,
        aliases: aliases.trim()
          ? aliases
              .split(',')
              .map((a) => a.trim())
              .filter((a) => a.length > 0)
          : undefined,
        pricing_key: pricingKey.trim() || undefined,
        jurisdiction: jurisdiction.trim() || undefined,
        wire_protocol: wireProtocol,
      }),
    onSuccess: (resp) => setSuccess(resp),
  })

  if (!profileId) {
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <p className="text-sm text-destructive">
          {t('admin_discovery_candidate_new.missing_profile_id')}
        </p>
        <Button asChild variant="ghost" size="sm">
          <Link to="/admin/discovery/records">
            <ArrowLeft className="h-4 w-4" />
            {t('admin_discovery_candidate_new.back_to_records')}
          </Link>
        </Button>
      </div>
    )
  }

  if (success) {
    return (
      <div className="mx-auto max-w-2xl space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>{t('admin_discovery_candidate_new.success_title')}</CardTitle>
            <CardDescription>
              {t('admin_discovery_candidate_new.success_identifiers_intro')}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <ul className="list-inside list-disc text-sm">
              {success.newly_live_identifiers.map((id) => (
                <li key={id} className="font-mono">
                  {id}
                </li>
              ))}
            </ul>
            {success.default_model_collision_warning ? (
              <p className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-300">
                {success.default_model_collision_warning}
              </p>
            ) : null}
          </CardContent>
        </Card>
        <Button
          onClick={() =>
            navigate(
              `/admin/discovery/candidates/${encodeURIComponent(success.candidate.profile_id)}`,
            )
          }
        >
          {t('admin_discovery_candidate_new.continue_to_candidate')}
        </Button>
      </div>
    )
  }

  const err = create.error
    ? discoveryErrorDetail(create.error, t('admin_discovery_candidate_new.error_fallback'))
    : null
  const isValid = wireProtocol.length > 0 && !!record.data

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/admin/discovery/records">
          <ArrowLeft className="h-4 w-4" />
          {t('admin_discovery_candidate_new.back_to_records')}
        </Link>
      </Button>

      <div>
        <h1 className="font-display text-3xl tracking-tight">
          {t('admin_discovery_candidate_new.title')}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {t('admin_discovery_candidate_new.intro')}
        </p>
      </div>

      {record.isLoading ? (
        <p className="text-sm text-muted-foreground">{t('common.loading_ellipsis')}</p>
      ) : record.error || !record.data ? (
        <p className="text-sm text-destructive">{t('admin_discovery_candidate_new.load_error')}</p>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            create.mutate()
          }}
          className="space-y-5"
        >
          <Card>
            <CardHeader>
              <CardTitle className="font-sans text-base font-semibold">
                {t('admin_discovery_candidate_new.record_card_title')}
              </CardTitle>
              <CardDescription className="font-mono text-xs">
                {record.data.profile_id} · {record.data.provider} · {record.data.model_family}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <BlockerList blockers={record.data.blockers} />
            </CardContent>
          </Card>

          <Card>
            <CardContent className="space-y-4 pt-6">
              <div className="space-y-1.5">
                <Label htmlFor="dc-aliases">
                  {t('admin_discovery_candidate_new.field_aliases')}
                </Label>
                <Input
                  id="dc-aliases"
                  value={aliases}
                  onChange={(e) => setAliases(e.target.value)}
                  placeholder="my-model-alias, my-model-alias-v2"
                />
                <p className="text-xs text-muted-foreground">
                  {t('admin_discovery_candidate_new.field_aliases_help')}
                </p>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="dc-pricing-key">
                  {t('admin_discovery_candidate_new.field_pricing_key')}
                </Label>
                <Input
                  id="dc-pricing-key"
                  value={pricingKey}
                  onChange={(e) => setPricingKey(e.target.value)}
                  className="font-mono"
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="dc-jurisdiction">
                  {t('admin_discovery_candidate_new.field_jurisdiction')}
                </Label>
                <Input
                  id="dc-jurisdiction"
                  value={jurisdiction}
                  onChange={(e) => setJurisdiction(e.target.value)}
                />
                {record.data.jurisdiction_bounded ? (
                  <p className="text-xs text-muted-foreground">
                    {t('admin_discovery_candidate_new.field_jurisdiction_help_bounded')}
                  </p>
                ) : null}
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="dc-wire-protocol">
                  {t('admin_discovery_candidate_new.field_wire_protocol')}
                </Label>
                <select
                  id="dc-wire-protocol"
                  value={wireProtocol}
                  onChange={(e) => setWireProtocol(e.target.value)}
                  className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  <option value="">—</option>
                  {WIRE_PROTOCOLS.map((p) => (
                    <option key={p} value={p}>
                      {t(`admin_discovery_candidate_new.wire_protocol_${p}`)}
                    </option>
                  ))}
                </select>
              </div>
            </CardContent>
          </Card>

          {err ? (
            <p className="text-sm text-destructive">
              {err.message}
              {err.type ? (
                <span className="ml-2 font-mono text-xs">
                  {t('admin_discovery_candidate_new.error_type_label')}: {err.type}
                </span>
              ) : null}
              {err.field ? (
                <span className="ml-2 font-mono text-xs">
                  {t('admin_discovery_candidate_new.error_field_label')}: {err.field}
                </span>
              ) : null}
            </p>
          ) : null}

          <div className="flex justify-end gap-3">
            <Button
              type="button"
              variant="ghost"
              onClick={() => navigate('/admin/discovery/records')}
              disabled={create.isPending}
            >
              {t('common.cancel')}
            </Button>
            <Button type="submit" disabled={!isValid || create.isPending}>
              {create.isPending
                ? t('admin_discovery_candidate_new.submit_pending')
                : t('admin_discovery_candidate_new.submit')}
            </Button>
          </div>
        </form>
      )}
    </div>
  )
}
