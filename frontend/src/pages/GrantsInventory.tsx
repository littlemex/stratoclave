import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Landmark } from 'lucide-react'

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
import { usePermissions } from '@/hooks/usePermissions'
import { api, type ApiError, type GrantReconciliationRow, type LimitGrant } from '@/lib/api'
import { fmtMicroUsd } from '@/lib/money'

/**
 * R25, confirmed standalone (not a tab on the tenant detail page). Renders
 * ONE total PER TARGET ROW -- never a grand total across rows -- because a
 * single tenant-wide sum is exactly wrong during a late sweep across a
 * rollover (B4): the prior period's row can still be carrying a grant that
 * bears capacity nobody has given back.
 */
export default function GrantsInventory() {
  const { t } = useTranslation()
  const { isAdmin } = usePermissions()
  const ns = isAdmin ? api.admin : api.teamLead
  const qc = useQueryClient()

  const [tenantId, setTenantId] = useState('')
  const [submittedTenantId, setSubmittedTenantId] = useState('')

  const grantsQuery = useQuery({
    queryKey: ['limit-grants', submittedTenantId, isAdmin],
    queryFn: () => ns.listLimitGrants(submittedTenantId),
    enabled: submittedTenantId.length > 0,
  })

  const grantsByRow = (row: GrantReconciliationRow) =>
    (grantsQuery.data?.grants ?? []).filter(
      (g) => g.target_pk === row.target_pk && g.target_sk === row.target_sk,
    )

  return (
    <div className="space-y-10">
      <header>
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          {t('grants_inventory.label')}
        </p>
        <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">
          {t('grants_inventory.title')}
        </h1>
        <p className="mt-2 max-w-xl text-sm text-muted-foreground">
          {t('grants_inventory.intro')}
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 font-sans text-base font-semibold">
            <Landmark className="h-4 w-4 text-muted-foreground" />
            {t('grants_inventory.lookup_title')}
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="gi-tenant-id">{t('grants_inventory.tenant_id_label')}</Label>
            <Input
              id="gi-tenant-id"
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
              placeholder="acme-eng"
              data-testid="gi-tenant-id-input"
            />
          </div>
          <Button
            onClick={() => setSubmittedTenantId(tenantId.trim())}
            disabled={tenantId.trim() === ''}
            data-testid="gi-lookup-button"
          >
            {t('grants_inventory.lookup')}
          </Button>
        </CardContent>
      </Card>

      {submittedTenantId ? (
        grantsQuery.isLoading ? (
          <p className="text-sm text-muted-foreground">{t('common.loading')}</p>
        ) : (grantsQuery.data?.reconciliation.rows.length ?? 0) === 0 ? (
          <p className="text-sm text-muted-foreground">{t('grants_inventory.no_rows')}</p>
        ) : (
          grantsQuery.data!.reconciliation.rows.map((row) => (
            <Card key={`${row.target_pk}#${row.target_sk}`}>
              <CardHeader>
                <CardTitle className="font-sans text-base font-semibold">
                  {t('grants_inventory.period_title', { period: row.period })}
                </CardTitle>
                <CardDescription data-testid="gi-row-total">
                  {t('grants_inventory.row_total', {
                    // This row's OWN total, next to this row's OWN period --
                    // never summed with any other row's.
                    total: fmtMicroUsd(row.pool_granted_microusd),
                    cap: fmtMicroUsd(row.effective_grant_cap_microusd),
                  })}
                  {row.cap_exceeded ? (
                    <span className="ml-2 text-destructive">
                      {t('grants_inventory.cap_exceeded')}
                    </span>
                  ) : null}
                  {row.drift_microusd !== 0 ? (
                    <span className="ml-2 text-destructive">
                      {t('grants_inventory.drift', {
                        amount: fmtMicroUsd(row.drift_microusd),
                      })}
                    </span>
                  ) : null}
                </CardDescription>
              </CardHeader>
              <CardContent className="p-0">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t('grants_inventory.col_request')}</TableHead>
                      <TableHead>{t('grants_inventory.col_approved')}</TableHead>
                      <TableHead>{t('grants_inventory.col_approver')}</TableHead>
                      <TableHead>{t('grants_inventory.col_expires')}</TableHead>
                      <TableHead>{t('grants_inventory.col_status')}</TableHead>
                      <TableHead className="text-right">
                        {t('grants_inventory.col_action')}
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {grantsByRow(row).map((g) => (
                      <GrantLine
                        key={g.grant_id}
                        grant={g}
                        isAdmin={isAdmin}
                        onRevoked={() =>
                          void qc.invalidateQueries({
                            queryKey: ['limit-grants', submittedTenantId],
                          })
                        }
                      />
                    ))}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>
          ))
        )
      ) : null}
    </div>
  )
}

function GrantLine({
  grant,
  isAdmin,
  onRevoked,
}: {
  grant: LimitGrant
  isAdmin: boolean
  onRevoked: () => void
}) {
  const { t } = useTranslation()
  const ns = isAdmin ? api.admin : api.teamLead
  const [error, setError] = useState<string | null>(null)

  const revoke = useMutation({
    mutationFn: () => ns.revokeLimitGrant(grant.tenant_id, grant.grant_id),
    onSuccess: onRevoked,
    onError: (err: unknown) => {
      const e = err as ApiError | null
      setError(e?.detail ?? e?.message ?? t('grants_inventory.revoke_error_fallback'))
    },
  })

  return (
    <TableRow>
      <TableCell className="font-mono text-xs text-muted-foreground">
        {grant.request_id}
      </TableCell>
      <TableCell className="font-mono text-xs">
        {fmtMicroUsd(grant.approved_amount_microusd)}
      </TableCell>
      <TableCell className="text-xs">{grant.approver_user_id}</TableCell>
      <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
        {new Date(grant.expires_at * 1000).toLocaleString()}
      </TableCell>
      <TableCell className="text-xs">
        {grant.status}
        {grant.revoke_blocked && grant.revoke_blocked_reason ? (
          <p className="mt-0.5 text-destructive" data-testid="gi-revoke-blocked-reason">
            {grant.revoke_blocked_reason}
          </p>
        ) : null}
        {error ? <p className="mt-0.5 text-destructive">{error}</p> : null}
      </TableCell>
      <TableCell className="text-right">
        {/* The wire value is the row's stored spelling verbatim (uppercase --
            `GRANT_ACTIVE`/`GRANT_REVOKE_BLOCKED` in
            `backend/dynamo/quota_events.py`), so this comparison matches
            that spelling rather than a lowercase copy of it. */}
        {grant.status === 'ACTIVE' || grant.status === 'REVOKE_BLOCKED' ? (
          <Button
            size="sm"
            variant="outline"
            disabled={revoke.isPending}
            onClick={() => {
              setError(null)
              revoke.mutate()
            }}
            data-testid="gi-revoke-button"
          >
            {t('grants_inventory.revoke')}
          </Button>
        ) : null}
      </TableCell>
    </TableRow>
  )
}
