import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import { Info, Plus } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
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
import { api, type ProvisioningPolicy } from '@/lib/api'
import { cn } from '@/lib/utils'

function fmt(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString()
}

export default function AdminTrustedAccounts() {
  const { t } = useTranslation()
  const [createOpen, setCreateOpen] = useState(false)
  const listQuery = useQuery({
    queryKey: ['admin', 'trusted-accounts'],
    queryFn: () => api.admin.listTrustedAccounts({ limit: 100 }),
  })

  const accounts = listQuery.data?.accounts ?? []

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
            {t('admin_trusted_accounts.header_eyebrow')}
          </p>
          <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">
            {t('admin_trusted_accounts.title')}
          </h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            {t('admin_trusted_accounts.intro')}
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          {t('admin_trusted_accounts.new_button')}
        </Button>
      </header>

      <Card className="border-accent/30 bg-accent/5">
        <CardHeader>
          <div className="flex items-center gap-2 text-accent">
            <Info className="h-4 w-4" aria-hidden />
            <CardTitle className="font-sans text-base font-semibold text-foreground">
              {t('admin_trusted_accounts.instance_profile_title')}
            </CardTitle>
          </div>
          <CardDescription>
            {t('admin_trusted_accounts.instance_profile_desc')}
          </CardDescription>
        </CardHeader>
      </Card>

      <div className="border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('admin_trusted_accounts.col_account_id')}</TableHead>
              <TableHead>{t('admin_trusted_accounts.col_description')}</TableHead>
              <TableHead>{t('admin_trusted_accounts.col_policy')}</TableHead>
              <TableHead>{t('admin_trusted_accounts.col_role_patterns')}</TableHead>
              <TableHead>{t('admin_trusted_accounts.col_iam_user')}</TableHead>
              <TableHead>{t('admin_trusted_accounts.col_instance_profile')}</TableHead>
              <TableHead className="text-right">
                {t('admin_trusted_accounts.col_default_credit')}
              </TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {listQuery.isLoading ? (
              <TableRow>
                <TableCell colSpan={8} className="text-center text-muted-foreground">
                  {t('common.loading_ellipsis')}
                </TableCell>
              </TableRow>
            ) : accounts.length === 0 ? (
              <TableRow>
                <TableCell colSpan={8} className="py-10 text-center text-muted-foreground">
                  {t('admin_trusted_accounts.row_empty_line1')}
                  <br />
                  {t('admin_trusted_accounts.row_empty_line2')}
                </TableCell>
              </TableRow>
            ) : (
              accounts.map((a) => (
                <TableRow key={a.account_id}>
                  <TableCell>
                    <code className="font-mono text-xs">{a.account_id}</code>
                  </TableCell>
                  <TableCell className="max-w-xs truncate">{a.description || '—'}</TableCell>
                  <TableCell>
                    <Badge
                      variant={
                        a.provisioning_policy === 'auto_provision' ? 'default' : 'secondary'
                      }
                    >
                      {a.provisioning_policy}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {a.allowed_role_patterns.length === 0 ? (
                      <span className="text-xs text-muted-foreground">
                        {t('admin_trusted_accounts.all_roles')}
                      </span>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {a.allowed_role_patterns.slice(0, 2).map((p) => (
                          <code
                            key={p}
                            className="rounded-sm border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
                          >
                            {p}
                          </code>
                        ))}
                        {a.allowed_role_patterns.length > 2 ? (
                          <span className="text-[10px] text-muted-foreground">
                            +{a.allowed_role_patterns.length - 2}
                          </span>
                        ) : null}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <Badge variant={a.allow_iam_user ? 'destructive' : 'muted'}>
                      {a.allow_iam_user
                        ? t('admin_trusted_accounts.badge_allow')
                        : t('admin_trusted_accounts.badge_deny')}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={a.allow_instance_profile ? 'destructive' : 'muted'}>
                      {a.allow_instance_profile
                        ? t('admin_trusted_accounts.badge_allow')
                        : t('admin_trusted_accounts.badge_deny')}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm">
                    {fmt(a.default_credit)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button asChild variant="ghost" size="sm">
                      <Link to={`/admin/trusted-accounts/${encodeURIComponent(a.account_id)}`}>
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

      <CreateAccountDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  )
}

function CreateAccountDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [accountId, setAccountId] = useState('')
  const [description, setDescription] = useState('')
  const [policy, setPolicy] = useState<ProvisioningPolicy>('invite_only')
  const [rolePatterns, setRolePatterns] = useState('')
  const [allowIamUser, setAllowIamUser] = useState(false)
  const [allowInstanceProfile, setAllowInstanceProfile] = useState(false)
  const [defaultTenantId, setDefaultTenantId] = useState('')
  const [defaultCredit, setDefaultCredit] = useState('')
  const [error, setError] = useState<string | null>(null)

  const tenantsQuery = useQuery({
    queryKey: ['admin', 'tenants', 'select'],
    queryFn: () => api.admin.listTenants({ limit: 100 }),
    enabled: open,
  })

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.createTrustedAccount({
        account_id: accountId.trim(),
        description: description.trim() || undefined,
        provisioning_policy: policy,
        allowed_role_patterns: rolePatterns
          .split(/[,\n]/)
          .map((s) => s.trim())
          .filter(Boolean),
        allow_iam_user: allowIamUser,
        allow_instance_profile: allowInstanceProfile,
        default_tenant_id: defaultTenantId || undefined,
        default_credit: defaultCredit ? Number(defaultCredit) : undefined,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['admin', 'trusted-accounts'] })
      reset()
      onOpenChange(false)
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('admin_trusted_accounts.error_fallback'))
    },
  })

  const reset = () => {
    setAccountId('')
    setDescription('')
    setPolicy('invite_only')
    setRolePatterns('')
    setAllowIamUser(false)
    setAllowInstanceProfile(false)
    setDefaultTenantId('')
    setDefaultCredit('')
    setError(null)
  }

  const isValid = /^\d{12}$/.test(accountId.trim())

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) reset()
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('admin_trusted_accounts.create_title')}</DialogTitle>
          <DialogDescription>
            {t('admin_trusted_accounts.create_desc')}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="ta-account">
              {t('admin_trusted_accounts.account_id_label')}
            </Label>
            <Input
              id="ta-account"
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              placeholder="123456789012"
              inputMode="numeric"
              maxLength={12}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ta-desc">{t('admin_trusted_accounts.desc_label')}</Label>
            <Input
              id="ta-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t('admin_trusted_accounts.desc_placeholder')}
            />
          </div>
          <div className="space-y-1.5">
            <Label>{t('admin_trusted_accounts.policy_label')}</Label>
            <div className="grid gap-2">
              {(['invite_only', 'auto_provision'] as ProvisioningPolicy[]).map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => setPolicy(p)}
                  aria-pressed={policy === p}
                  className={cn(
                    'rounded-md border p-3 text-left text-sm transition-colors',
                    policy === p
                      ? 'border-primary bg-primary/10'
                      : 'border-border hover:border-primary/40',
                  )}
                >
                  <div className="font-medium">{p}</div>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {p === 'invite_only'
                      ? t('admin_trusted_accounts.policy_invite_only_desc')
                      : t('admin_trusted_accounts.policy_auto_provision_desc')}
                  </p>
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ta-roles">
              {t('admin_trusted_accounts.roles_label')}
            </Label>
            <textarea
              id="ta-roles"
              value={rolePatterns}
              onChange={(e) => setRolePatterns(e.target.value)}
              placeholder={t('admin_trusted_accounts.roles_placeholder')}
              rows={3}
              className="flex w-full rounded-md border border-input bg-input px-3 py-2 font-mono text-xs text-foreground placeholder:text-muted-foreground/70 focus-visible:outline-none focus-visible:border-primary/70 focus-visible:ring-2 focus-visible:ring-ring/60"
            />
            <p className="text-[11px] text-muted-foreground">
              <Trans
                i18nKey="admin_trusted_accounts.roles_help"
                components={{ 1: <code className="font-mono" /> }}
              />
            </p>
          </div>
          <div className="grid gap-2 rounded-md border border-border bg-muted/30 p-3">
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={allowIamUser}
                onChange={(e) => setAllowIamUser(e.target.checked)}
                className="mt-0.5 h-4 w-4 rounded-sm border-border"
              />
              <span>
                <span className="font-medium">
                  {t('admin_trusted_accounts.allow_iam_user_label')}
                </span>
                <span className="block text-[11px] text-muted-foreground">
                  {t('admin_trusted_accounts.allow_iam_user_desc')}
                </span>
              </span>
            </label>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={allowInstanceProfile}
                onChange={(e) => setAllowInstanceProfile(e.target.checked)}
                className="mt-0.5 h-4 w-4 rounded-sm border-border"
              />
              <span>
                <span className="font-medium text-destructive">
                  {t('admin_trusted_accounts.allow_ip_label')}
                </span>
                <span className="block text-[11px] text-muted-foreground">
                  {t('admin_trusted_accounts.allow_ip_desc')}
                </span>
              </span>
            </label>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="ta-tenant">
                {t('admin_trusted_accounts.default_tenant_label')}
              </Label>
              <select
                id="ta-tenant"
                value={defaultTenantId}
                onChange={(e) => setDefaultTenantId(e.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
              >
                <option value="">
                  {t('admin_trusted_accounts.default_tenant_fallback')}
                </option>
                {(tenantsQuery.data?.tenants ?? []).map((tenant) => (
                  <option key={tenant.tenant_id} value={tenant.tenant_id}>
                    {tenant.name} ({tenant.tenant_id})
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="ta-credit">
                {t('admin_trusted_accounts.default_credit_label')}
              </Label>
              <Input
                id="ta-credit"
                type="number"
                min={0}
                max={10_000_000}
                value={defaultCredit}
                onChange={(e) => setDefaultCredit(e.target.value)}
                placeholder={t('admin_trusted_accounts.default_credit_placeholder')}
              />
            </div>
          </div>
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button disabled={!isValid || mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending
              ? t('admin_trusted_accounts.submitting')
              : t('admin_trusted_accounts.submit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
