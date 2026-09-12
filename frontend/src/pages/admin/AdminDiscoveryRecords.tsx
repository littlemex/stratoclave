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
import { BlockerList } from '@/components/discovery/BlockerList'
import { api } from '@/lib/api'

/**
 * GET /api/mvp/admin/discovery/records, UNFILTERED. Unlike the pre-existing
 * queue (which deliberately omits a record whose only blocker is permanent),
 * this list carries every discovered record and every blocker on it -- an
 * operator asking "why can I not promote this" gets an answer here even for
 * a permanently-blocked profile, which the queue was never designed to give.
 */
export default function AdminDiscoveryRecords() {
  const { t } = useTranslation()
  const records = useQuery({
    queryKey: ['admin-discovery-records'],
    queryFn: () => api.discovery.records(),
  })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{t('admin_discovery_records.title')}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{t('admin_discovery_records.intro')}</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t('admin_discovery_records.card_title')}</CardTitle>
          <CardDescription>{t('admin_discovery_records.card_desc')}</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {records.isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">{t('common.loading_ellipsis')}</p>
          ) : records.error || !records.data ? (
            <p className="p-6 text-sm text-destructive">
              {t('admin_discovery_records.load_error')}
            </p>
          ) : records.data.records.length === 0 ? (
            <p className="p-6 text-sm text-muted-foreground">
              {t('admin_discovery_records.empty')}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('admin_discovery_records.col_profile')}</TableHead>
                  <TableHead>{t('admin_discovery_records.col_family')}</TableHead>
                  <TableHead>{t('admin_discovery_records.col_scope')}</TableHead>
                  <TableHead>{t('admin_discovery_records.col_jurisdiction')}</TableHead>
                  <TableHead>{t('admin_discovery_records.col_blockers')}</TableHead>
                  <TableHead className="text-right">
                    {t('admin_discovery_records.col_actions')}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {records.data.records.map((r) => (
                  <TableRow key={r.profile_id}>
                    <TableCell className="font-mono text-xs">
                      {r.profile_id}
                      <p className="mt-0.5 text-[10px] text-muted-foreground">
                        {r.provider} · {r.invocation_region}
                      </p>
                    </TableCell>
                    <TableCell className="text-xs">{r.model_family}</TableCell>
                    <TableCell className="text-xs">{r.profile_scope}</TableCell>
                    <TableCell className="text-xs">
                      {r.jurisdiction_bounded ? (
                        <Badge variant="secondary">
                          {t('admin_discovery_records.jurisdiction_bounded')}
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground">
                          {t('admin_discovery_records.jurisdiction_unbounded')}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="max-w-md">
                      <BlockerList blockers={r.blockers} />
                    </TableCell>
                    <TableCell className="text-right">
                      <Button asChild size="sm" variant="outline">
                        <Link
                          to={`/admin/discovery/candidates/new?profile_id=${encodeURIComponent(r.profile_id)}`}
                        >
                          {t('admin_discovery_records.create_candidate')}
                        </Link>
                      </Button>
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
