import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ArrowLeft, Info, Lock } from 'lucide-react'

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
import { TempPasswordDialog } from '@/components/admin/TempPasswordDialog'
import { api, type CreateUserResponse, type Locale, type Role } from '@/lib/api'
import { SUPPORTED_LOCALES } from '@/lib/i18n'
import { cn } from '@/lib/utils'

interface RoleOption {
  value: Role
  labelKey: string
  descKey: string
  lockedReasonKey?: string
}

const ROLE_OPTIONS: RoleOption[] = [
  {
    value: 'user',
    labelKey: 'role.user',
    descKey: 'admin_user_new.role_user_desc',
  },
  {
    value: 'team_lead',
    labelKey: 'role.team_lead',
    descKey: 'admin_user_new.role_team_lead_desc',
  },
  {
    value: 'admin',
    labelKey: 'role.admin',
    descKey: 'admin_user_new.role_admin_desc',
    lockedReasonKey: 'admin_user_new.role_admin_locked',
  },
]

export default function AdminUserNew() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { t } = useTranslation()

  const [email, setEmail] = useState('')
  const [role, setRole] = useState<Role>('user')
  const [tenantId, setTenantId] = useState('')
  const [totalCredit, setTotalCredit] = useState('')
  // i18n: empty string = "let server pick default". We do not pre-fill
  // with "ja" because "ja" is the server default already; keeping the
  // field unset preserves existing behavior when this form is submitted
  // unchanged.
  const [locale, setLocale] = useState<'' | Locale>('')
  const [formError, setFormError] = useState<string | null>(null)
  const [success, setSuccess] = useState<CreateUserResponse | null>(null)

  const tenantsQuery = useQuery({
    queryKey: ['admin', 'tenants', 'select'],
    queryFn: () => api.admin.listTenants({ limit: 100 }),
  })

  const createMutation = useMutation({
    mutationFn: () => {
      const body = {
        email: email.trim(),
        role,
        tenant_id: tenantId || undefined,
        total_credit: totalCredit ? Number(totalCredit) : undefined,
        locale: locale || undefined,
      }
      return api.admin.createUser(body)
    },
    onSuccess: (resp) => {
      setSuccess(resp)
      void qc.invalidateQueries({ queryKey: ['admin', 'users'] })
    },
    onError: (err: unknown) => {
      const e = err as { status?: number; detail?: string; message?: string } | null
      setFormError(e?.detail ?? e?.message ?? t('admin_user_new.error_fallback'))
    },
  })

  const tenantOptions = useMemo(
    () => tenantsQuery.data?.tenants ?? [],
    [tenantsQuery.data],
  )

  const isValid = email.trim().length > 0 && email.includes('@')

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setFormError(null)
    if (!isValid) {
      setFormError(t('admin_user_new.error_email'))
      return
    }
    createMutation.mutate()
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/admin/users">
          <ArrowLeft className="h-4 w-4" />
          {t('admin_user_new.back_to_list')}
        </Link>
      </Button>

      <div>
        <h1 className="font-display text-3xl tracking-tight">{t('admin_user_new.title')}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {t('admin_user_new.intro')}
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-5">
        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">
              {t('admin_user_new.basic')}
            </CardTitle>
            <CardDescription>{t('admin_user_new.basic_desc')}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="email">{t('admin_user_new.email_label')}</Label>
              <Input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="user@example.com"
                autoComplete="off"
                required
              />
            </div>

            <div className="space-y-1.5">
              <Label>{t('admin_user_new.role_label')}</Label>
              <div className="grid gap-2">
                {ROLE_OPTIONS.map((opt) => {
                  const disabled = Boolean(opt.lockedReasonKey)
                  const selected = role === opt.value
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => !disabled && setRole(opt.value)}
                      aria-pressed={selected}
                      aria-disabled={disabled}
                      disabled={disabled}
                      className={cn(
                        'flex items-start gap-3 rounded-md border p-3 text-left transition-colors',
                        selected
                          ? 'border-primary bg-primary/10'
                          : 'border-border hover:border-primary/40',
                        disabled && 'cursor-not-allowed opacity-60 hover:border-border',
                      )}
                    >
                      <span
                        className={cn(
                          'mt-0.5 h-4 w-4 shrink-0 rounded-full border',
                          selected ? 'border-primary bg-primary' : 'border-muted-foreground',
                        )}
                        aria-hidden
                      />
                      <div className="min-w-0 flex-1 space-y-1">
                        <div className="flex items-center gap-2 text-sm font-medium">
                          {t(opt.labelKey)}
                          {disabled ? (
                            <span className="inline-flex items-center gap-1 text-[11px] uppercase tracking-wide text-muted-foreground">
                              <Lock className="h-3 w-3" aria-hidden />
                              {t('admin_user_new.role_locked_badge')}
                            </span>
                          ) : null}
                        </div>
                        <p className="text-xs text-muted-foreground">
                          {t(opt.descKey)}
                        </p>
                        {disabled && opt.lockedReasonKey ? (
                          <p className="flex items-start gap-1 text-[11px] text-muted-foreground">
                            <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                            {t(opt.lockedReasonKey)}
                          </p>
                        ) : null}
                      </div>
                    </button>
                  )
                })}
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">
              {t('admin_user_new.tenant_card')}
            </CardTitle>
            <CardDescription>
              {t('admin_user_new.tenant_card_desc')}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="tenant">{t('admin_user_new.tenant_label')}</Label>
              <select
                id="tenant"
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                <option value="">{t('admin_user_new.tenant_default_option')}</option>
                {tenantOptions.map((t) => (
                  <option key={t.tenant_id} value={t.tenant_id}>
                    {t.name} ({t.tenant_id})
                  </option>
                ))}
              </select>
              <p className="text-xs text-muted-foreground">
                {t('admin_user_new.tenant_help')}
              </p>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="total-credit">{t('admin_user_new.credit_label')}</Label>
              <Input
                id="total-credit"
                type="number"
                inputMode="numeric"
                value={totalCredit}
                min={0}
                max={10_000_000}
                onChange={(e) => setTotalCredit(e.target.value)}
                placeholder={t('admin_user_new.credit_placeholder')}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="locale">{t('admin_user_new.locale_label')}</Label>
              <select
                id="locale"
                value={locale}
                onChange={(e) => setLocale(e.target.value as '' | Locale)}
                className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                <option value="">—</option>
                {SUPPORTED_LOCALES.map((loc) => (
                  <option key={loc} value={loc}>
                    {t(`locale.${loc}`)}
                  </option>
                ))}
              </select>
              <p className="text-xs text-muted-foreground">
                {t('admin_user_new.locale_help')}
              </p>
            </div>
          </CardContent>
        </Card>

        {formError ? (
          <p className="text-sm text-destructive">{formError}</p>
        ) : null}

        <div className="flex justify-end gap-3">
          <Button
            type="button"
            variant="ghost"
            onClick={() => navigate('/admin/users')}
            disabled={createMutation.isPending}
          >
            {t('admin_user_new.cancel')}
          </Button>
          <Button type="submit" disabled={createMutation.isPending || !isValid}>
            {createMutation.isPending
              ? t('admin_user_new.submit_pending')
              : t('admin_user_new.submit')}
          </Button>
        </div>
      </form>

      <TempPasswordDialog
        open={success !== null}
        email={success?.email ?? ''}
        temporaryPassword={success?.temporary_password ?? ''}
        onAcknowledge={() => {
          setSuccess(null)
          navigate('/admin/users')
        }}
      />
    </div>
  )
}
