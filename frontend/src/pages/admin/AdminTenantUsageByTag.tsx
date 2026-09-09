import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'

import { UsageByTagReport } from '@/components/common/UsageByTagReport'
import { api } from '@/lib/api'

/**
 * One tenant's spend, grouped by task tag, for an administrator.
 *
 * The question this answers that the self report cannot: *what did this month's migration cost
 * us*. Nobody is one person's worth of a tenant's spend, so the self view could never answer it.
 *
 * **Admin only, and the team-lead mirror is deliberately absent.** The team-lead by-tag route
 * returns the shared row shape, which carries a stable `user_id` — and `team_lead.py`'s
 * `TenantMemberPublic` states the opposite policy for that role in the same file: it omits
 * `user_id` to prevent cross-tenant tracking of a person by a lead who owns two tenants. A
 * test also pins the admin and team-lead bodies as byte-identical, deliberately, so the two
 * cannot grow separate aggregations. Two well-motivated rules collide, and choosing between an
 * opaque per-tenant alias, an email projection, or no member dimension for leads is a backend
 * decision. Hiding the column here would not help: the JSON still carries the field.
 *
 * An admin is cross-tenant by permission (`usage:read-all`), so the identifier is not a leak to
 * them, which is why this half ships and the other waits.
 */
export default function AdminTenantUsageByTag() {
  const { t } = useTranslation()
  const { tenantId = '' } = useParams()

  return (
    <div className="space-y-10">
      <header>
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          {t('admin_tenant_usage_by_tag.label')}
        </p>
        <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">
          {t('admin_tenant_usage_by_tag.title')}
        </h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          {t('admin_tenant_usage_by_tag.intro', { tenant: tenantId })}
        </p>
      </header>

      <UsageByTagReport
        // The tenant is IN the key: an admin reads many tenants in one session, and two
        // tenants' reports under one key is a cross-tenant read served from cache, which
        // needs no bug in the backend at all.
        queryScope={['admin', 'tenants', tenantId]}
        fetchReport={(query) => api.admin.tenantUsageByTag(tenantId, query)}
        memberColumn
      />
    </div>
  )
}
