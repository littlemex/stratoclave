import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { VerdictBadges } from '@/components/discovery/VerdictBadges'
import { api, type DiscoveryCandidate } from '@/lib/api'

/**
 * GET /api/mvp/admin/discovery/candidates. Every promotion candidate, each
 * with its verdict for every invocation ("unverified" rather than an absent
 * key when no probe has run). No promote/probe/activate actions live here --
 * they are on the candidate detail page, reached via `col_actions` below,
 * which `models:promote` alone can act on.
 */
export default function AdminDiscoveryCandidates() {
  const { t } = useTranslation()
  const candidates = useQuery({
    queryKey: ['admin-discovery-candidates'],
    queryFn: () => api.discovery.candidates(),
  })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{t('admin_discovery_candidates.title')}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {t('admin_discovery_candidates.intro')}
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t('admin_discovery_candidates.card_title')}</CardTitle>
          <CardDescription>{t('admin_discovery_candidates.card_desc')}</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {candidates.isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">{t('common.loading_ellipsis')}</p>
          ) : candidates.error || !candidates.data ? (
            <p className="p-6 text-sm text-destructive">
              {t('admin_discovery_candidates.load_error')}
            </p>
          ) : candidates.data.candidates.length === 0 ? (
            <p className="p-6 text-sm text-muted-foreground">
              {t('admin_discovery_candidates.empty')}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('admin_discovery_candidates.col_profile')}</TableHead>
                  <TableHead>{t('admin_discovery_candidates.col_family')}</TableHead>
                  <TableHead>{t('admin_discovery_candidates.col_state')}</TableHead>
                  <TableHead>{t('admin_discovery_candidates.col_identifiers')}</TableHead>
                  <TableHead>{t('admin_discovery_candidates.col_verdicts')}</TableHead>
                  <TableHead className="text-right">
                    {t('admin_discovery_candidates.col_actions')}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {candidates.data.candidates.map((c) => (
                  <CandidateRow key={c.profile_id} candidate={c} />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function CandidateRow({ candidate }: { candidate: DiscoveryCandidate }) {
  const { t } = useTranslation()
  return (
    <TableRow>
      <TableCell className="font-mono text-xs">{candidate.profile_id}</TableCell>
      <TableCell className="text-xs">{candidate.model_family}</TableCell>
      <TableCell className="text-xs">
        <Badge variant={candidate.state === 'suspended' ? 'destructive' : 'secondary'}>
          {candidate.state}
        </Badge>
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {candidate.identifiers.join(', ')}
      </TableCell>
      <TableCell className="text-xs">
        <VerdictBadges verdicts={candidate.verdicts} />
      </TableCell>
      <TableCell className="text-right">
        <Button asChild size="sm" variant="outline">
          <Link to={`/admin/discovery/candidates/${encodeURIComponent(candidate.profile_id)}`}>
            {t('admin_discovery_candidates.view')}
          </Link>
        </Button>
      </TableCell>
    </TableRow>
  )
}
