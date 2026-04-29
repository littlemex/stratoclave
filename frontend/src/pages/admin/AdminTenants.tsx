import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Plus } from 'lucide-react'

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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
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
import { api } from '@/lib/api'

function fmt(n: number): string {
  return n.toLocaleString()
}

export default function AdminTenants() {
  const { t } = useTranslation()
  const [cursor, setCursor] = useState<string | undefined>()
  const [cursorStack, setCursorStack] = useState<Array<string | undefined>>([])
  const [createOpen, setCreateOpen] = useState(false)

  const tenantsQuery = useQuery({
    queryKey: ['admin', 'tenants', cursor],
    queryFn: () => api.admin.listTenants({ cursor, limit: 50 }),
    placeholderData: (p) => p,
  })

  const nextCursor = tenantsQuery.data?.next_cursor ?? null

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="font-display text-3xl tracking-tight">
            {t('admin_tenants.title')}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {t('admin_tenants.intro')}
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          {t('admin_tenants.new_button')}
        </Button>
      </header>

      <div className="border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('admin_tenants.col_name')}</TableHead>
              <TableHead>{t('admin_tenants.col_tenant_id')}</TableHead>
              <TableHead>{t('admin_tenants.col_owner')}</TableHead>
              <TableHead className="text-right">
                {t('admin_tenants.col_default_credit')}
              </TableHead>
              <TableHead>{t('admin_tenants.col_status')}</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {tenantsQuery.isLoading ? (
              <TableRow>
                <TableCell colSpan={6} className="text-center text-muted-foreground">
                  {t('common.loading_ellipsis')}
                </TableCell>
              </TableRow>
            ) : (tenantsQuery.data?.tenants.length ?? 0) === 0 ? (
              <TableRow>
                <TableCell colSpan={6} className="text-center text-muted-foreground">
                  {t('admin_tenants.row_empty')}
                </TableCell>
              </TableRow>
            ) : (
              tenantsQuery.data!.tenants.map((tenant) => (
                <TableRow key={tenant.tenant_id}>
                  <TableCell className="font-medium">{tenant.name}</TableCell>
                  <TableCell>
                    <code className="font-mono text-xs text-muted-foreground">
                      {tenant.tenant_id}
                    </code>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {tenant.team_lead_user_id === 'admin-owned' ? (
                      <Badge variant="muted">admin-owned</Badge>
                    ) : (
                      <code className="font-mono">{tenant.team_lead_user_id}</code>
                    )}
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
                      <Link to={`/admin/tenants/${encodeURIComponent(tenant.tenant_id)}`}>
                        {t('common.details')}
                      </Link>
                    </Button>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>
          {t('admin_tenants.count_showing', {
            shown: tenantsQuery.data?.tenants.length ?? 0,
          })}
          {nextCursor ? t('admin_tenants.count_more') : ''}
        </span>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={cursorStack.length === 0}
            onClick={() =>
              setCursorStack((s) => {
                const next = [...s]
                const prev = next.pop()
                setCursor(prev)
                return next
              })
            }
          >
            {t('common.prev')}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={!nextCursor}
            onClick={() => {
              setCursorStack((s) => [...s, cursor])
              setCursor(nextCursor ?? undefined)
            }}
          >
            {t('common.next')}
          </Button>
        </div>
      </div>

      <Card className="border-primary/30 bg-primary/5">
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('admin_tenants.hint_title')}
          </CardTitle>
          <CardDescription>
            {t('admin_tenants.hint_desc')}
          </CardDescription>
        </CardHeader>
        <CardContent className="text-xs text-muted-foreground">
          {t('admin_tenants.hint_detail')}
        </CardContent>
      </Card>

      <CreateTenantDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  )
}

function CreateTenantDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [teamLeadUserId, setTeamLeadUserId] = useState('admin-owned')
  const [defaultCredit, setDefaultCredit] = useState('')
  const [error, setError] = useState<string | null>(null)

  const teamLeadUsersQuery = useQuery({
    queryKey: ['admin', 'users', 'team_lead'],
    queryFn: () => api.admin.listUsers({ role: 'team_lead', limit: 100 }),
    enabled: open,
  })

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.createTenant({
        name: name.trim(),
        team_lead_user_id: teamLeadUserId,
        default_credit: defaultCredit ? Number(defaultCredit) : undefined,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['admin', 'tenants'] })
      onOpenChange(false)
      setName('')
      setTeamLeadUserId('admin-owned')
      setDefaultCredit('')
      setError(null)
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('admin_tenants.create_error_fallback'))
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) setError(null)
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t('admin_tenants.create_title')}</DialogTitle>
          <DialogDescription>{t('admin_tenants.create_desc')}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="tenant-name">{t('admin_tenants.create_name_label')}</Label>
            <Input
              id="tenant-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('admin_tenants.create_name_placeholder')}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="tenant-owner">{t('admin_tenants.create_owner_label')}</Label>
            <select
              id="tenant-owner"
              value={teamLeadUserId}
              onChange={(e) => setTeamLeadUserId(e.target.value)}
              className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
            >
              <option value="admin-owned">
                {t('admin_tenants.create_owner_admin_owned')}
              </option>
              {(teamLeadUsersQuery.data?.users ?? []).map((u) => (
                <option key={u.user_id} value={u.user_id}>
                  {u.email || u.user_id}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="tenant-default-credit">
              {t('admin_tenants.create_default_label')}
            </Label>
            <Input
              id="tenant-default-credit"
              type="number"
              min={0}
              max={10_000_000}
              value={defaultCredit}
              onChange={(e) => setDefaultCredit(e.target.value)}
              placeholder={t('admin_tenants.create_default_placeholder')}
            />
          </div>
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button
            disabled={!name.trim() || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending
              ? t('admin_tenants.create_submitting')
              : t('admin_tenants.create_submit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
