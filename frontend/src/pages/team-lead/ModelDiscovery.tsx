import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { BlockerList } from '@/components/discovery/BlockerList'
import { VerdictBadges } from '@/components/discovery/VerdictBadges'
import { api } from '@/lib/api'

/**
 * team_lead holds `models:discover` (same as admin) but NOT
 * `models:promote` -- so this page reads the SAME two discovery routes the
 * admin screens read, and stops there. Deliberately its own file rather
 * than the admin screens with a permission flag toggling the
 * create/probe/activate controls: a shared component holding both the
 * read data and the write actions is exactly the shape that tempts a
 * later change into showing those actions to a role that cannot use
 * them, since the flag is a runtime check nobody re-reads once it works.
 * This file contains no code path that can create, probe or activate a
 * candidate -- there is nothing to gate, because there is nothing here.
 *
 * Structurally derived from `GrantsInventory.tsx` (see that file): the same
 * header block (an eyebrow label, a title, an intro paragraph), the same
 * `Card` per data set with a `CardHeader`/`CardContent` `Table`, and the
 * same loading/empty-state prose convention. It does NOT reuse
 * `GrantsInventory`'s tenant-lookup form -- discovered records and
 * promotion candidates are not scoped to a tenant, so there is no tenant ID
 * to ask for before this data can be shown.
 */
export default function ModelDiscovery() {
  const { t } = useTranslation()

  const records = useQuery({
    queryKey: ['team-lead-discovery-records'],
    queryFn: () => api.discovery.records(),
  })
  const candidates = useQuery({
    queryKey: ['team-lead-discovery-candidates'],
    queryFn: () => api.discovery.candidates(),
  })

  return (
    <div className="space-y-10">
      <header>
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          {t('team_lead_model_discovery.label')}
        </p>
        <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">
          {t('team_lead_model_discovery.title')}
        </h1>
        <p className="mt-2 max-w-xl text-sm text-muted-foreground">
          {t('team_lead_model_discovery.intro')}
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('team_lead_model_discovery.records_card_title')}
          </CardTitle>
          <CardDescription>{t('team_lead_model_discovery.records_card_desc')}</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {records.isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">{t('common.loading_ellipsis')}</p>
          ) : records.error || !records.data ? (
            <p className="p-6 text-sm text-destructive">
              {t('team_lead_model_discovery.load_error')}
            </p>
          ) : records.data.records.length === 0 ? (
            <p className="p-6 text-sm text-muted-foreground">
              {t('team_lead_model_discovery.records_empty')}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('team_lead_model_discovery.col_profile')}</TableHead>
                  <TableHead>{t('team_lead_model_discovery.col_family')}</TableHead>
                  <TableHead>{t('team_lead_model_discovery.col_scope')}</TableHead>
                  <TableHead>{t('team_lead_model_discovery.col_jurisdiction')}</TableHead>
                  <TableHead>{t('team_lead_model_discovery.col_blockers')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {records.data.records.map((r) => (
                  <TableRow key={r.profile_id}>
                    <TableCell className="font-mono text-xs">{r.profile_id}</TableCell>
                    <TableCell className="text-xs">{r.model_family}</TableCell>
                    <TableCell className="text-xs">{r.profile_scope}</TableCell>
                    <TableCell className="text-xs">
                      {r.jurisdiction_bounded ? (
                        <Badge variant="secondary">
                          {t('team_lead_model_discovery.jurisdiction_bounded')}
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground">
                          {t('team_lead_model_discovery.jurisdiction_unbounded')}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="max-w-md">
                      <BlockerList blockers={r.blockers} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('team_lead_model_discovery.candidates_card_title')}
          </CardTitle>
          <CardDescription>{t('team_lead_model_discovery.candidates_card_desc')}</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {candidates.isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">{t('common.loading_ellipsis')}</p>
          ) : candidates.error || !candidates.data ? (
            <p className="p-6 text-sm text-destructive">
              {t('team_lead_model_discovery.load_error')}
            </p>
          ) : candidates.data.candidates.length === 0 ? (
            <p className="p-6 text-sm text-muted-foreground">
              {t('team_lead_model_discovery.candidates_empty')}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('team_lead_model_discovery.col_profile')}</TableHead>
                  <TableHead>{t('team_lead_model_discovery.col_family')}</TableHead>
                  <TableHead>{t('team_lead_model_discovery.col_identifiers')}</TableHead>
                  <TableHead>{t('team_lead_model_discovery.col_verdicts')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {candidates.data.candidates.map((c) => (
                  <TableRow key={c.profile_id}>
                    <TableCell className="font-mono text-xs">{c.profile_id}</TableCell>
                    <TableCell className="text-xs">{c.model_family}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {c.identifiers.join(', ')}
                    </TableCell>
                    <TableCell className="text-xs">
                      <VerdictBadges verdicts={c.verdicts} />
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
