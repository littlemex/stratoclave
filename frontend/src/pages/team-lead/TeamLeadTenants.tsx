import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Building2, Plus } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { api } from '@/lib/api'

function fmt(n: number): string {
  return n.toLocaleString()
}

export default function TeamLeadTenants() {
  const { t } = useTranslation()
  const tenantsQuery = useQuery({
    queryKey: ['team-lead', 'tenants'],
    queryFn: () => api.teamLead.listTenants(),
  })

  const tenants = tenantsQuery.data?.tenants ?? []

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="font-display text-3xl tracking-tight">{t('team_lead_tenants.title')}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {t('team_lead_tenants.intro')}
          </p>
        </div>
        <Button asChild>
          <Link to="/team-lead/tenants/new">
            <Plus className="h-4 w-4" />
            {t('team_lead_tenants.new')}
          </Link>
        </Button>
      </header>

      {tenantsQuery.isLoading ? (
        <p className="text-sm text-muted-foreground">{t('common.loading_ellipsis')}</p>
      ) : tenants.length === 0 ? (
        <Card className="border-dashed">
          <CardHeader>
            <div className="flex items-center gap-2 text-muted-foreground">
              <Building2 className="h-5 w-5" aria-hidden />
              <CardTitle className="font-sans text-base font-semibold">
                {t('team_lead_tenants.empty_title')}
              </CardTitle>
            </div>
            <CardDescription>
              {t('team_lead_tenants.empty_desc')}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button asChild>
              <Link to="/team-lead/tenants/new">
                <Plus className="h-4 w-4" />
                {t('team_lead_tenants.new')}
              </Link>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('tenant.name')}</TableHead>
                <TableHead>{t('tenant.tenant_id')}</TableHead>
                <TableHead className="text-right">{t('tenant.default_credit')}</TableHead>
                <TableHead>{t('tenant.status')}</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {tenants.map((tenant) => (
                <TableRow key={tenant.tenant_id}>
                  <TableCell className="font-medium">{tenant.name}</TableCell>
                  <TableCell>
                    <code className="font-mono text-xs text-muted-foreground">
                      {tenant.tenant_id}
                    </code>
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm">
                    {fmt(tenant.default_credit)}
                  </TableCell>
                  <TableCell>
                    <Badge variant={tenant.status === 'archived' ? 'muted' : 'secondary'}>
                      {tenant.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <Button asChild variant="ghost" size="sm">
                      <Link to={`/team-lead/tenants/${encodeURIComponent(tenant.tenant_id)}`}>
                        {t('common.details')}
                      </Link>
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Card className="border-primary/30 bg-primary/5">
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('team_lead_tenants.hint_title')}
          </CardTitle>
          <CardDescription>
            {t('team_lead_tenants.hint_desc')}
          </CardDescription>
        </CardHeader>
      </Card>
    </div>
  )
}
