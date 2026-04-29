import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ArrowLeft } from 'lucide-react'

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
import { api } from '@/lib/api'

export default function TeamLeadTenantNew() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [name, setName] = useState('')
  const [defaultCredit, setDefaultCredit] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () =>
      api.teamLead.createTenant({
        name: name.trim(),
        default_credit: defaultCredit ? Number(defaultCredit) : undefined,
      }),
    onSuccess: (tenant) => {
      void qc.invalidateQueries({ queryKey: ['team-lead', 'tenants'] })
      navigate(`/team-lead/tenants/${encodeURIComponent(tenant.tenant_id)}`)
    },
    onError: (err: unknown) => {
      const e = err as { status?: number; detail?: string; message?: string } | null
      if (e?.status === 403 && e.detail?.includes('tenant_limit_exceeded')) {
        setError(t('team_lead_tenant_new.error_limit'))
      } else {
        setError(e?.detail ?? e?.message ?? t('team_lead_tenant_new.error_fallback'))
      }
    },
  })

  const isValid = name.trim().length > 0

  return (
    <div className="mx-auto max-w-xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/team-lead/tenants">
          <ArrowLeft className="h-4 w-4" />
          {t('team_lead_tenant_new.back_to_list')}
        </Link>
      </Button>

      <div>
        <h1 className="font-display text-3xl tracking-tight">
          {t('team_lead_tenant_new.title')}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {t('team_lead_tenant_new.intro')}
        </p>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault()
          setError(null)
          if (!isValid) {
            setError(t('team_lead_tenant_new.error_name_empty'))
            return
          }
          mutation.mutate()
        }}
        className="space-y-5"
      >
        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">
              {t('team_lead_tenant_new.basic')}
            </CardTitle>
            <CardDescription>
              {t('team_lead_tenant_new.basic_desc')}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="tl-name">{t('team_lead_tenant_new.name_label')}</Label>
              <Input
                id="tl-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t('team_lead_tenant_new.name_placeholder')}
                required
                autoComplete="off"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="tl-default-credit">
                {t('team_lead_tenant_new.default_credit_label')}
              </Label>
              <Input
                id="tl-default-credit"
                type="number"
                min={0}
                max={10_000_000}
                value={defaultCredit}
                onChange={(e) => setDefaultCredit(e.target.value)}
                placeholder={t('team_lead_tenant_new.default_credit_placeholder')}
              />
            </div>
          </CardContent>
        </Card>

        {error ? <p className="text-sm text-destructive">{error}</p> : null}

        <div className="flex justify-end gap-3">
          <Button
            type="button"
            variant="ghost"
            onClick={() => navigate('/team-lead/tenants')}
            disabled={mutation.isPending}
          >
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={!isValid || mutation.isPending}>
            {mutation.isPending ? t('common.creating') : t('team_lead_tenant_new.submit')}
          </Button>
        </div>
      </form>
    </div>
  )
}
