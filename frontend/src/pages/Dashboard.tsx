import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import {
  ArrowRight,
  Building2,
  Coins,
  Fingerprint,
  Key,
  KeyRound,
  ShieldCheck,
  Users,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { usePermissions } from '@/hooks/usePermissions'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { UserRole } from '@/types/auth'

function formatNumber(n: number): string {
  return n.toLocaleString()
}

export default function Dashboard() {
  const { t } = useTranslation()
  const { isAdmin, isTeamLead } = usePermissions()
  const meQuery = useQuery({
    queryKey: ['me'],
    queryFn: () => api.me(),
  })

  const me = meQuery.data
  const remainingPct =
    me && me.total_credit > 0
      ? Math.max(
          0,
          Math.min(100, Math.round((me.remaining_credit / me.total_credit) * 100)),
        )
      : 0

  return (
    <div className="space-y-12">
      <section className="space-y-3">
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          {t('dashboard.label')}
        </p>
        <h1 className="font-display text-4xl font-semibold tracking-tight">
          {me?.email ? (
            <Trans
              i18nKey="dashboard.welcome_with_email"
              values={{ email: me.email }}
              components={{ 1: <span className="text-primary" /> }}
            />
          ) : (
            t('dashboard.welcome')
          )}
        </h1>
        <p className="max-w-2xl text-sm text-muted-foreground">
          {t('dashboard.intro')}
        </p>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <StatCard
          label={t('dashboard.stat_credit')}
          icon={<Coins className="h-3.5 w-3.5" aria-hidden />}
        >
          <div className="flex items-baseline gap-2">
            <span className="strato-stat font-display text-4xl font-semibold tracking-tight">
              {me ? formatNumber(me.remaining_credit) : '—'}
            </span>
            <span className="text-xs text-muted-foreground">
              {t('dashboard.stat_credit_unit')}
            </span>
          </div>
          {me ? (
            <div className="mt-4 space-y-2">
              <div className="h-1 w-full overflow-hidden bg-muted">
                <div
                  className={cn(
                    'h-full transition-all',
                    remainingPct > 20 ? 'bg-primary' : 'bg-destructive',
                  )}
                  style={{ width: `${remainingPct}%` }}
                />
              </div>
              <p className="font-mono text-[11px] tracking-wide text-muted-foreground">
                {t('dashboard.stat_credit_remaining', {
                  used: formatNumber(me.credit_used),
                  total: formatNumber(me.total_credit),
                  pct: remainingPct,
                })}
              </p>
            </div>
          ) : null}
        </StatCard>

        <StatCard
          label={t('dashboard.stat_tenant')}
          icon={<Building2 className="h-3.5 w-3.5" aria-hidden />}
        >
          <div className="font-display text-2xl font-semibold tracking-tight">
            {me?.tenant?.name ?? me?.tenant?.tenant_id ?? '—'}
          </div>
          <p className="mt-3 font-mono text-[11px] text-muted-foreground">
            {t('dashboard.stat_tenant_id', {
              id: me?.tenant?.tenant_id ?? '—',
            })}
          </p>
        </StatCard>

        <StatCard
          label={t('dashboard.stat_role')}
          icon={<ShieldCheck className="h-3.5 w-3.5" aria-hidden />}
        >
          <div className="flex flex-wrap gap-1.5">
            {(me?.roles ?? []).map((r: UserRole) => (
              <Badge
                key={r}
                variant={r === 'admin' ? 'accent' : 'secondary'}
              >
                {t(`role.${r}`)}
              </Badge>
            ))}
            {!me || me.roles.length === 0 ? (
              <Badge variant="muted">{t('dashboard.stat_role_none')}</Badge>
            ) : null}
          </div>
          <p className="mt-3 text-[11px] text-muted-foreground">
            {t('dashboard.stat_role_footer')}
          </p>
        </StatCard>
      </section>

      <section className="space-y-4">
        <div className="flex items-baseline justify-between">
          <h2 className="font-display text-xl font-semibold tracking-tight">
            {t('dashboard.shortcuts')}
          </h2>
          <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
            {t('dashboard.shortcuts_sub')}
          </span>
        </div>
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          <ShortcutCard
            to="/me/usage"
            icon={<Fingerprint className="h-4 w-4" aria-hidden />}
            title={t('dashboard.shortcut_my_usage_title')}
            description={t('dashboard.shortcut_my_usage_desc')}
            openLabel={t('dashboard.open')}
          />
          <ShortcutCard
            to="/me/api-keys"
            icon={<Key className="h-4 w-4" aria-hidden />}
            title={t('dashboard.shortcut_api_keys_title')}
            description={t('dashboard.shortcut_api_keys_desc')}
            openLabel={t('dashboard.open')}
          />
          {isAdmin ? (
            <ShortcutCard
              to="/admin/users"
              icon={<Users className="h-4 w-4" aria-hidden />}
              title={t('dashboard.shortcut_admin_users_title')}
              description={t('dashboard.shortcut_admin_users_desc')}
              openLabel={t('dashboard.open')}
            />
          ) : null}
          {isAdmin ? (
            <ShortcutCard
              to="/admin/tenants"
              icon={<Building2 className="h-4 w-4" aria-hidden />}
              title={t('dashboard.shortcut_admin_tenants_title')}
              description={t('dashboard.shortcut_admin_tenants_desc')}
              openLabel={t('dashboard.open')}
            />
          ) : null}
          {isAdmin ? (
            <ShortcutCard
              to="/admin/trusted-accounts"
              icon={<KeyRound className="h-4 w-4" aria-hidden />}
              title={t('dashboard.shortcut_trusted_accounts_title')}
              description={t('dashboard.shortcut_trusted_accounts_desc')}
              openLabel={t('dashboard.open')}
            />
          ) : null}
          {isTeamLead ? (
            <ShortcutCard
              to="/team-lead/tenants"
              icon={<Building2 className="h-4 w-4" aria-hidden />}
              title={t('dashboard.shortcut_team_lead_title')}
              description={t('dashboard.shortcut_team_lead_desc')}
              openLabel={t('dashboard.open')}
            />
          ) : null}
        </div>
      </section>

      {meQuery.isError ? (
        <p className="border border-destructive/40 bg-destructive/10 px-4 py-2 text-sm text-destructive-foreground">
          {t('dashboard.me_error')}
        </p>
      ) : null}
    </div>
  )
}

function StatCard({
  label,
  icon,
  children,
}: {
  label: string
  icon?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <Card>
      <CardHeader className="space-y-0 pb-3">
        <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
          {icon}
          <span>{label}</span>
        </div>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

function ShortcutCard({
  to,
  icon,
  title,
  description,
  openLabel,
}: {
  to: string
  icon: React.ReactNode
  title: string
  description: string
  openLabel: string
}) {
  return (
    <Card className="group transition-[border-color,background-color] hover:border-primary/40">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2 text-muted-foreground">
          {icon}
          <CardTitle className="font-sans text-base font-semibold text-foreground">
            {title}
          </CardTitle>
        </div>
        <CardDescription className="mt-1 text-xs">
          {description}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Button
          asChild
          variant="ghost"
          size="sm"
          className="px-0 text-primary"
        >
          <Link to={to}>
            {openLabel} <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
          </Link>
        </Button>
      </CardContent>
    </Card>
  )
}
