import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import { ArrowLeft, ArrowRight, Coins, Trash2 } from 'lucide-react'

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
import { api, type Role, type UserSummary } from '@/lib/api'

function fmt(n: number): string {
  return n.toLocaleString()
}

export default function AdminUserDetail() {
  const { t } = useTranslation()
  const { userId = '' } = useParams<{ userId: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const userQuery = useQuery({
    queryKey: ['admin', 'users', 'detail', userId],
    queryFn: () => api.admin.getUser(userId),
    enabled: !!userId,
  })
  const tenantsQuery = useQuery({
    queryKey: ['admin', 'tenants', 'select'],
    queryFn: () => api.admin.listTenants({ limit: 100 }),
  })

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/admin/users">
          <ArrowLeft className="h-4 w-4" />
          {t('admin_user_detail.back_to_list')}
        </Link>
      </Button>

      {userQuery.isLoading ? (
        <p className="text-sm text-muted-foreground">
          {t('admin_user_detail.loading')}
        </p>
      ) : userQuery.isError || !userQuery.data ? (
        <p className="text-sm text-destructive">
          {t('admin_user_detail.not_found')}
        </p>
      ) : (
        <Content
          user={userQuery.data}
          tenantOptions={tenantsQuery.data?.tenants ?? []}
          onMutated={() => {
            void qc.invalidateQueries({ queryKey: ['admin', 'users'] })
            void userQuery.refetch()
          }}
          onDeleted={() => {
            void qc.invalidateQueries({ queryKey: ['admin', 'users'] })
            navigate('/admin/users')
          }}
        />
      )}
    </div>
  )
}

interface ContentProps {
  user: UserSummary
  tenantOptions: Array<{ tenant_id: string; name: string }>
  onMutated: () => void
  onDeleted: () => void
}

function Content({ user, tenantOptions, onMutated, onDeleted }: ContentProps) {
  const { t } = useTranslation()
  const [assignOpen, setAssignOpen] = useState(false)
  const [creditOpen, setCreditOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)

  return (
    <>
      <section className="space-y-2">
        <h1 className="font-display text-3xl tracking-tight">
          {user.email || t('common.email_unset')}
        </h1>
        <code className="block font-mono text-xs text-muted-foreground">
          {user.user_id}
        </code>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              {t('admin_user_detail.card_role_title')}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="flex flex-wrap gap-1">
              {user.roles.map((r) => (
                <Badge
                  key={r}
                  variant={
                    r === 'admin'
                      ? 'accent'
                      : r === 'team_lead'
                        ? 'default'
                        : 'secondary'
                  }
                >
                  {t(`role.${r}`)}
                </Badge>
              ))}
            </div>
            <Badge
              variant={user.auth_method === 'sso' ? 'outline' : 'muted'}
              className="mt-1"
            >
              auth · {user.auth_method ?? 'cognito'}
            </Badge>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              {t('admin_user_detail.card_tenant_title')}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-display text-lg tracking-tight">{user.org_id}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              {t('admin_user_detail.card_credit_title')}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-display text-lg tracking-tight">
              {fmt(user.remaining_credit)}
              <span className="ml-1 text-xs font-sans font-normal text-muted-foreground">
                {t('admin_user_detail.credit_remaining_unit')}
              </span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {t('admin_user_detail.credit_used_line', {
                used: fmt(user.credit_used),
                total: fmt(user.total_credit),
              })}
            </p>
          </CardContent>
        </Card>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">
              {t('admin_user_detail.action_assign_title')}
            </CardTitle>
            <CardDescription>
              {t('admin_user_detail.action_assign_desc')}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setAssignOpen(true)}
              disabled={tenantOptions.length === 0}
            >
              <ArrowRight className="h-4 w-4" />
              {t('admin_user_detail.action_assign_start')}
            </Button>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">
              {t('admin_user_detail.action_credit_title')}
            </CardTitle>
            <CardDescription>
              {t('admin_user_detail.action_credit_desc')}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="outline" size="sm" onClick={() => setCreditOpen(true)}>
              <Coins className="h-4 w-4" />
              {t('admin_user_detail.action_credit_edit')}
            </Button>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold text-destructive">
              {t('admin_user_detail.action_delete_title')}
            </CardTitle>
            <CardDescription>
              {t('admin_user_detail.action_delete_desc')}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => setDeleteOpen(true)}
            >
              <Trash2 className="h-4 w-4" />
              {t('admin_user_detail.action_delete_button')}
            </Button>
          </CardContent>
        </Card>
      </section>

      {user.auth_method === 'sso' ? (
        <Card className="border-primary/30 bg-primary/5">
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">
              {t('admin_user_detail.sso_card_title')}
            </CardTitle>
            <CardDescription>
              {t('admin_user_detail.sso_card_desc')}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 text-xs">
            <div>
              <span className="font-medium uppercase tracking-[0.12em] text-muted-foreground">
                {t('admin_user_detail.sso_account')}
              </span>
              <div className="mt-0.5 font-mono">{user.sso_account_id ?? '—'}</div>
            </div>
            <div>
              <span className="font-medium uppercase tracking-[0.12em] text-muted-foreground">
                {t('admin_user_detail.sso_principal')}
              </span>
              <div className="mt-0.5 break-all font-mono text-muted-foreground">
                {user.sso_principal_arn ?? '—'}
              </div>
            </div>
            <div>
              <span className="font-medium uppercase tracking-[0.12em] text-muted-foreground">
                {t('admin_user_detail.sso_last_login')}
              </span>
              <div className="mt-0.5 font-mono">
                {user.last_sso_login_at
                  ? new Date(user.last_sso_login_at).toLocaleString()
                  : '—'}
              </div>
            </div>
          </CardContent>
        </Card>
      ) : null}

      <AssignTenantDialog
        open={assignOpen}
        user={user}
        tenantOptions={tenantOptions}
        onOpenChange={setAssignOpen}
        onDone={onMutated}
      />
      <SetCreditDialog
        open={creditOpen}
        user={user}
        onOpenChange={setCreditOpen}
        onDone={onMutated}
      />
      <DeleteUserDialog
        open={deleteOpen}
        user={user}
        onOpenChange={setDeleteOpen}
        onDeleted={onDeleted}
      />
    </>
  )
}

// ------------------------------------------------------------------
// Assign tenant dialog (2-step confirmation)
// ------------------------------------------------------------------
function AssignTenantDialog({
  open,
  user,
  tenantOptions,
  onOpenChange,
  onDone,
}: {
  open: boolean
  user: UserSummary
  tenantOptions: Array<{ tenant_id: string; name: string }>
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const { t } = useTranslation()
  const [step, setStep] = useState<1 | 2>(1)
  const [tenantId, setTenantId] = useState('')
  const [newRole, setNewRole] = useState<Role>('user')
  const [totalCredit, setTotalCredit] = useState('')
  const [confirmEmail, setConfirmEmail] = useState('')
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setStep(1)
    setTenantId('')
    setNewRole('user')
    setTotalCredit('')
    setConfirmEmail('')
    setError(null)
  }

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.assignTenant(user.user_id, {
        tenant_id: tenantId,
        total_credit: totalCredit ? Number(totalCredit) : undefined,
        new_role: newRole,
      }),
    onSuccess: () => {
      reset()
      onOpenChange(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('admin_user_detail.assign_error_fallback'))
    },
  })

  const step1Valid = tenantId.length > 0 && tenantId !== user.org_id

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset()
        onOpenChange(next)
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {step === 1
              ? t('admin_user_detail.assign_step1_title')
              : t('admin_user_detail.assign_step2_title')}
          </DialogTitle>
          <DialogDescription>
            {step === 1
              ? t('admin_user_detail.assign_step1_desc')
              : t('admin_user_detail.assign_step2_desc', {
                  email: user.email,
                  from: user.org_id,
                  to: tenantId,
                })}
          </DialogDescription>
        </DialogHeader>

        {step === 1 ? (
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="assign-tenant">
                {t('admin_user_detail.assign_tenant_label')}
              </Label>
              <select
                id="assign-tenant"
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                <option value="">
                  {t('admin_user_detail.assign_tenant_placeholder')}
                </option>
                {tenantOptions.map((tenant) => (
                  <option key={tenant.tenant_id} value={tenant.tenant_id}>
                    {tenant.name} ({tenant.tenant_id})
                    {tenant.tenant_id === user.org_id
                      ? t('admin_user_detail.assign_tenant_current_suffix')
                      : ''}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="assign-role">
                {t('admin_user_detail.assign_role_label')}
              </Label>
              <select
                id="assign-role"
                value={newRole}
                onChange={(e) => setNewRole(e.target.value as Role)}
                className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
              >
                <option value="user">{t('role.user')}</option>
                <option value="team_lead">{t('role.team_lead')}</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="assign-credit">
                {t('admin_user_detail.assign_credit_label')}
              </Label>
              <Input
                id="assign-credit"
                type="number"
                min={0}
                max={10_000_000}
                value={totalCredit}
                onChange={(e) => setTotalCredit(e.target.value)}
                placeholder={t('admin_user_detail.assign_credit_placeholder')}
              />
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive-foreground">
              <p className="font-semibold">
                {t('admin_user_detail.assign_confirm_tasks_title')}
              </p>
              <ul className="mt-1 list-inside list-disc space-y-0.5">
                <li>
                  {t('admin_user_detail.assign_task_archive', { from: user.org_id })}
                </li>
                <li>
                  {t('admin_user_detail.assign_task_activate', {
                    to: tenantId,
                    role: newRole,
                  })}
                </li>
                <li>{t('admin_user_detail.assign_task_cognito')}</li>
                <li>{t('admin_user_detail.assign_task_signout')}</li>
              </ul>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="assign-confirm">
                {t('admin_user_detail.assign_confirm_label', { email: user.email })}
              </Label>
              <Input
                id="assign-confirm"
                autoComplete="off"
                value={confirmEmail}
                onChange={(e) => setConfirmEmail(e.target.value)}
                placeholder={user.email}
              />
            </div>
          </div>
        )}

        {error ? <p className="text-sm text-destructive">{error}</p> : null}

        <DialogFooter>
          {step === 1 ? (
            <>
              <Button variant="ghost" onClick={() => onOpenChange(false)}>
                {t('common.cancel')}
              </Button>
              <Button disabled={!step1Valid} onClick={() => setStep(2)}>
                {t('admin_user_detail.assign_next')}
              </Button>
            </>
          ) : (
            <>
              <Button variant="ghost" onClick={() => setStep(1)}>
                {t('admin_user_detail.assign_back')}
              </Button>
              <Button
                variant="destructive"
                disabled={confirmEmail !== user.email || mutation.isPending}
                onClick={() => mutation.mutate()}
              >
                {mutation.isPending
                  ? t('admin_user_detail.assign_submitting')
                  : t('admin_user_detail.assign_submit')}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------
// Credit overwrite dialog
// ------------------------------------------------------------------
function SetCreditDialog({
  open,
  user,
  onOpenChange,
  onDone,
}: {
  open: boolean
  user: UserSummary
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const { t } = useTranslation()
  const [value, setValue] = useState<string>(String(user.total_credit))
  const [resetUsed, setResetUsed] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.setCredit(user.user_id, {
        total_credit: Number(value),
        reset_used: resetUsed,
      }),
    onSuccess: () => {
      onOpenChange(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('admin_user_detail.credit_error_fallback'))
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) {
          setError(null)
          setValue(String(user.total_credit))
          setResetUsed(false)
        }
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t('admin_user_detail.credit_title')}</DialogTitle>
          <DialogDescription>
            {t('admin_user_detail.credit_desc', { email: user.email })}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="credit-value">
              {t('admin_user_detail.credit_new_label')}
            </Label>
            <Input
              id="credit-value"
              type="number"
              min={0}
              max={10_000_000}
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              {t('admin_user_detail.credit_current_line', {
                total: fmt(user.total_credit),
                used: fmt(user.credit_used),
              })}
            </p>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={resetUsed}
              onChange={(e) => setResetUsed(e.target.checked)}
              className="h-4 w-4 rounded-sm border-border"
            />
            {t('admin_user_detail.credit_reset_used')}
          </label>
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button
            disabled={!value || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending
              ? t('admin_user_detail.credit_submitting')
              : t('admin_user_detail.credit_submit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------
// Delete user dialog (text confirmation)
// ------------------------------------------------------------------
function DeleteUserDialog({
  open,
  user,
  onOpenChange,
  onDeleted,
}: {
  open: boolean
  user: UserSummary
  onOpenChange: (v: boolean) => void
  onDeleted: () => void
}) {
  const { t } = useTranslation()
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => api.admin.deleteUser(user.user_id),
    onSuccess: () => {
      onOpenChange(false)
      onDeleted()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('admin_user_detail.delete_error_fallback'))
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) {
          setConfirm('')
          setError(null)
        }
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="text-destructive">
            {t('admin_user_detail.delete_title')}
          </DialogTitle>
          <DialogDescription>
            {t('admin_user_detail.delete_desc')}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label htmlFor="delete-confirm">
            <Trans
              i18nKey="admin_user_detail.delete_confirm_label"
              values={{ email: user.email }}
              components={{
                1: <code className="font-mono text-foreground" />,
              }}
            />
          </Label>
          <Input
            id="delete-confirm"
            autoComplete="off"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder={user.email}
          />
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button
            variant="destructive"
            disabled={confirm !== user.email || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending
              ? t('admin_user_detail.delete_submitting')
              : t('admin_user_detail.delete_submit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
