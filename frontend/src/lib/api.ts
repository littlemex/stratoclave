/**
 * Backend API client (Phase 2 `/api/mvp/*`)
 *
 * Calls endpoints served on the same origin (= CloudFront / Vite proxy)
 * with a Bearer access_token. All URLs are relative, so the code runs
 * unchanged in both production and development.
 */

import { QueryClient } from '@tanstack/react-query'

import { authFetch } from './authFetch'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failureCount, error) => {
        const status = (error as { status?: number } | null)?.status
        if (status === 401 || status === 403 || status === 404) return false
        return failureCount < 2
      },
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
})

// --- HTTP helpers ---
async function jsonRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await authFetch(path, init)
  if (!res.ok) {
    let detail: string | undefined
    try {
      const body = await res.clone().json()
      detail = typeof body?.detail === 'string' ? body.detail : undefined
    } catch {
      // ignore
    }
    const err = new Error(detail ?? `${res.status} ${res.statusText}`) as Error & {
      status: number
      detail?: string
    }
    err.status = res.status
    err.detail = detail
    throw err
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

const jsonHeaders = { 'Content-Type': 'application/json' }

// --- Domain types (minimal subset the UI uses) ---
export type Role = 'admin' | 'team_lead' | 'user'

// i18n: UI locale. Backend clamps to this set server-side; unknown
// values fall back to "ja". Keep this literal in sync with
// backend/mvp/me.py :: SUPPORTED_LOCALES.
export type Locale = 'en' | 'ja'

export interface MeResponse {
  user_id: string
  email: string
  org_id: string
  roles: Role[]
  total_credit: number
  credit_used: number
  remaining_credit: number
  currency: string
  tenant: { tenant_id: string; name?: string | null } | null
  locale: Locale
}

export interface UpdateMeResponse {
  locale: Locale
}

export interface MePermissionsResponse {
  user_id: string
  auth_kind: string
  roles: Role[]
  key_scopes: string[] | null
  permissions: string[]
}

export interface UsageSummary {
  tenant_id: string
  total_credit: number
  credit_used: number
  remaining_credit: number
  by_model: Record<string, number>
  by_tenant: Record<string, number>
  sample_size: number
  since_days: number
  // P0-11: count of sampled requests served by a fallback model.
  fallback_count?: number
}

export interface UsageHistoryEntry {
  tenant_id: string
  tenant_name?: string | null
  model_id: string // the EFFECTIVE model the request was served by
  input_tokens: number
  output_tokens: number
  total_tokens: number
  recorded_at: string
  // P0-11 fallback visibility. null = legacy row (unknown), never rendered as
  // a fallback.
  requested_model_id?: string | null
  fallback_occurred?: boolean | null
}

export interface UsageHistoryResponse {
  history: UsageHistoryEntry[]
  next_cursor?: string | null
}

export interface UserSummary {
  user_id: string
  email: string
  roles: Role[]
  org_id: string
  total_credit: number
  credit_used: number
  remaining_credit: number
  created_at?: string | null
  // Phase S: SSO / auth metadata
  auth_method?: string | null
  sso_account_id?: string | null
  sso_principal_arn?: string | null
  last_sso_login_at?: string | null
  // i18n: current UI locale (may be null for legacy rows).
  locale?: Locale | null
}

export interface UsersListResponse {
  users: UserSummary[]
  next_cursor?: string | null
}

export interface CreateUserRequest {
  email: string
  role?: Role
  tenant_id?: string
  total_credit?: number
  // i18n: admin can pre-set the new user's UI locale.
  locale?: Locale
}

export interface CreateUserResponse {
  email: string
  user_id: string
  temporary_password: string
  user_pool_id: string
  org_id: string
  role: Role
}

export interface TenantItem {
  tenant_id: string
  name: string
  team_lead_user_id?: string
  default_credit: number
  status: string
  created_at?: string | null
  updated_at?: string | null
  created_by?: string | null
}

export interface TenantsListResponse {
  tenants: TenantItem[]
  next_cursor?: string | null
}

export interface AdminTenantMember {
  user_id: string
  email: string
  role: Role
  total_credit: number
  credit_used: number
  remaining_credit: number
  status: string
}

export interface AdminTenantMembersResponse {
  tenant_id: string
  members: AdminTenantMember[]
}

export interface UsageBucket {
  tenant_id: string
  total_tokens: number
  input_tokens: number
  output_tokens: number
  by_model: Record<string, number>
  by_user?: Record<string, number>
  by_user_email?: Record<string, number>
  sample_size: number
}

// A-1: tenant dollar pool budget. All money is integer micro-USD
// (1 USD = 1_000_000 micro-USD); the *_usd_cents mirrors are integer cents
// the backend derives, so the UI never does float money math.
export interface PoolBudget {
  tenant_id: string
  period: string
  status: string
  pool_limit_microusd: number
  pool_reserved_microusd: number
  pool_settled_microusd: number
  remaining_microusd: number
  pool_limit_usd_cents: number
  remaining_usd_cents: number
}

// P0-11: tenant/user routing config (chain, quotas, allowlist). This is the
// config the per-model-quota + cascading-fallback enforcement reads.
export interface ModelQuota {
  // Only usd_micro is accepted server-side (limit is monthly micro-USD).
  unit?: 'usd_micro'
  limit?: number | null
  period?: 'monthly'
}
export interface TenantRoutingConfig {
  tenant_id: string
  configured: boolean
  allowlist: string[]
  chain: string[]
  quotas: Record<string, ModelQuota>
  fallback_mode: string
  fallback_default: string
  free_tier_model?: string | null
}
export interface UserRoutingConfig {
  tenant_id: string
  user_id: string
  configured: boolean
  preferred_model?: string | null
  chain?: string[] | null
  fallback?: string | null
}

export interface UsageLogEntry {
  tenant_id: string
  user_id: string
  user_email?: string | null
  model_id: string // the EFFECTIVE model the request was served by
  input_tokens: number
  output_tokens: number
  total_tokens: number
  recorded_at: string
  timestamp_log_id: string
  // P0-11 fallback visibility. null = legacy row (unknown), never a fallback.
  requested_model_id?: string | null
  fallback_occurred?: boolean | null
}

export interface UsageLogsResponse {
  logs: UsageLogEntry[]
  next_cursor?: string | null
}

// #66: read-only effective pricing table (built-in defaults <- overrides).
export interface PricingRateEntry {
  pricing_key: string
  input_per_mtok_microusd: number
  output_per_mtok_microusd: number
  cache_read_per_mtok_microusd: number
  cache_write_per_mtok_microusd: number
  source: 'default' | 'override'
  models: string[]
}

export interface PricingConfigResponse {
  version: string | null // null = pure built-in defaults
  rates: PricingRateEntry[]
}

// --- L5-d: per-run billing breakdown (frozen ledger rating) ---
export interface RatingComponentView {
  tokens: number
  rate_microusd_per_mtok: number
  cost_microusd: number
}

// TENANT view: NO provider_cost / margin fields — redaction is enforced by the
// backend (separate response model). The UI type omits them too, and
// `assertNoCostLeak` below is a runtime backstop that fails loudly if a drifted
// API ever returns them to a tenant.
export interface RunEventTenant {
  event_type: string
  settle_reason?: string | null
  model_id?: string | null
  pricing_version?: string | null
  pricing_key?: string | null
  settled_microusd: number
  components: Record<string, RatingComponentView>
  ts_ms: number
}

export interface RunBreakdownTenant {
  tenant_id: string
  run_id: string
  total_settled_microusd: number
  events: RunEventTenant[]
}

// ADMIN view: adds provider cost + margin (may be negative).
export interface RunEventAdmin extends RunEventTenant {
  provider_cost_microusd?: number | null
  margin_microusd?: number | null
}

export interface RunBreakdownAdmin {
  tenant_id: string
  run_id: string
  total_settled_microusd: number
  total_provider_cost_microusd?: number | null
  total_margin_microusd?: number | null
  events: RunEventAdmin[]
}

// Keys that MUST NEVER appear in a tenant-facing billing payload. Runtime
// backstop to the backend's type-level redaction (contract-drift gate).
const COST_MARGIN_KEYS = [
  'provider_cost_microusd',
  'margin_microusd',
  'total_provider_cost_microusd',
  'total_margin_microusd',
]

export function assertNoCostLeak(obj: unknown, path = '$'): void {
  if (Array.isArray(obj)) {
    obj.forEach((v, i) => assertNoCostLeak(v, `${path}[${i}]`))
  } else if (obj && typeof obj === 'object') {
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
      if (COST_MARGIN_KEYS.includes(k)) {
        throw new Error(`billing redaction violated: '${k}' present at ${path}`)
      }
      assertNoCostLeak(v, `${path}.${k}`)
    }
  }
}

// --- Phase S: Trusted Accounts / SSO Invites ---
export type ProvisioningPolicy = 'invite_only' | 'auto_provision'

export interface TrustedAccountItem {
  account_id: string
  description: string
  provisioning_policy: ProvisioningPolicy
  allowed_role_patterns: string[]
  allow_iam_user: boolean
  allow_instance_profile: boolean
  default_tenant_id?: string | null
  default_credit?: number | null
  created_at?: string | null
  updated_at?: string | null
  created_by?: string | null
}

export interface TrustedAccountsListResponse {
  accounts: TrustedAccountItem[]
  next_cursor?: string | null
}

export interface SsoInviteItem {
  email: string
  account_id: string
  invited_role: 'user' | 'team_lead'
  tenant_id?: string | null
  total_credit?: number | null
  iam_user_name?: string | null
  invited_by: string
  invited_at: string
  consumed_at?: string | null
}

export interface SsoInvitesListResponse {
  invites: SsoInviteItem[]
  next_cursor?: string | null
}

// --- Phase C: Long-lived API Keys (sk-stratoclave-*) ---
export interface ApiKeySummary {
  key_id: string
  name: string
  user_id: string
  scopes: string[]
  created_at?: string | null
  expires_at?: string | null
  revoked_at?: string | null
  last_used_at?: string | null
  created_by?: string | null
}

export interface ApiKeyList {
  keys: ApiKeySummary[]
  active_count: number
  max_per_user: number
}

export interface CreateApiKeyRequest {
  name?: string
  scopes?: string[]
  expires_in_days?: number | null
}

export interface CreateApiKeyResponse {
  key_id: string
  plaintext_key: string
  name: string
  scopes: string[]
  expires_at?: string | null
  created_at: string
}

export interface TeamLeadMember {
  email: string
  role: Role
  total_credit: number
  credit_used: number
  remaining_credit: number
}

export interface TeamLeadMembersResponse {
  tenant_id: string
  members: TeamLeadMember[]
}

// --- API surface ---
export const api = {
  me: () => jsonRequest<MeResponse>('/api/mvp/me'),

  // The caller's own effective capabilities — server-computed via the same
  // evaluation the request path enforces (no client-side re-derivation).
  myPermissions: () => jsonRequest<MePermissionsResponse>('/api/mvp/me/permissions'),

  updateMe: (body: { locale: Locale }) =>
    jsonRequest<UpdateMeResponse>('/api/mvp/me', {
      method: 'PATCH',
      headers: jsonHeaders,
      body: JSON.stringify(body),
    }),

  usageSummary: (sinceDays?: number) => {
    const q = sinceDays ? `?since_days=${sinceDays}` : ''
    return jsonRequest<UsageSummary>(`/api/mvp/me/usage-summary${q}`)
  },

  usageHistory: (opts?: { since_days?: number; limit?: number; cursor?: string }) => {
    const params = new URLSearchParams()
    if (opts?.since_days) params.set('since_days', String(opts.since_days))
    if (opts?.limit) params.set('limit', String(opts.limit))
    if (opts?.cursor) params.set('cursor', opts.cursor)
    const q = params.toString()
    return jsonRequest<UsageHistoryResponse>(
      `/api/mvp/me/usage-history${q ? `?${q}` : ''}`,
    )
  },

  // L5-d: the caller's own per-run charge breakdown (redacted — no cost/margin).
  // The runtime `assertNoCostLeak` backstop turns a redaction regression into a
  // loud client error instead of a silent leak into the DOM.
  runBilling: async (runId: string): Promise<RunBreakdownTenant> => {
    const body = await jsonRequest<RunBreakdownTenant>(
      `/api/mvp/me/billing/runs/${encodeURIComponent(runId)}`,
    )
    assertNoCostLeak(body)
    return body
  },

  admin: {
    listUsers: (opts?: { cursor?: string; limit?: number; role?: Role; tenant_id?: string }) => {
      const params = new URLSearchParams()
      if (opts?.cursor) params.set('cursor', opts.cursor)
      if (opts?.limit) params.set('limit', String(opts.limit))
      if (opts?.role) params.set('role', opts.role)
      if (opts?.tenant_id) params.set('tenant_id', opts.tenant_id)
      const q = params.toString()
      return jsonRequest<UsersListResponse>(`/api/mvp/admin/users${q ? `?${q}` : ''}`)
    },
    getUser: (user_id: string) =>
      jsonRequest<UserSummary>(`/api/mvp/admin/users/${encodeURIComponent(user_id)}`),
    createUser: (body: CreateUserRequest) =>
      jsonRequest<CreateUserResponse>('/api/mvp/admin/users', {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify(body),
      }),
    deleteUser: (user_id: string) =>
      jsonRequest<void>(`/api/mvp/admin/users/${encodeURIComponent(user_id)}`, {
        method: 'DELETE',
      }),
    updateUser: (user_id: string, body: { locale: Locale }) =>
      jsonRequest<UserSummary>(
        `/api/mvp/admin/users/${encodeURIComponent(user_id)}`,
        {
          method: 'PATCH',
          headers: jsonHeaders,
          body: JSON.stringify(body),
        },
      ),
    assignTenant: (
      user_id: string,
      body: { tenant_id: string; total_credit?: number; new_role?: Role },
    ) =>
      jsonRequest<UserSummary>(
        `/api/mvp/admin/users/${encodeURIComponent(user_id)}/tenant`,
        {
          method: 'PUT',
          headers: jsonHeaders,
          body: JSON.stringify(body),
        },
      ),
    setCredit: (
      user_id: string,
      body: { total_credit: number; reset_used?: boolean },
    ) =>
      jsonRequest<UserSummary>(
        `/api/mvp/admin/users/${encodeURIComponent(user_id)}/credit`,
        {
          method: 'PATCH',
          headers: jsonHeaders,
          body: JSON.stringify(body),
        },
      ),
    setRole: (user_id: string, role: Role) =>
      jsonRequest<UserSummary>(
        `/api/mvp/admin/users/${encodeURIComponent(user_id)}/role`,
        {
          method: 'PATCH',
          headers: jsonHeaders,
          body: JSON.stringify({ role }),
        },
      ),

    listTenants: (opts?: { cursor?: string; limit?: number }) => {
      const params = new URLSearchParams()
      if (opts?.cursor) params.set('cursor', opts.cursor)
      if (opts?.limit) params.set('limit', String(opts.limit))
      const q = params.toString()
      return jsonRequest<TenantsListResponse>(`/api/mvp/admin/tenants${q ? `?${q}` : ''}`)
    },
    getTenant: (tenant_id: string) =>
      jsonRequest<TenantItem>(`/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}`),
    createTenant: (body: { name: string; team_lead_user_id: string; default_credit?: number }) =>
      jsonRequest<TenantItem>('/api/mvp/admin/tenants', {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify(body),
      }),
    updateTenant: (
      tenant_id: string,
      body: { name?: string; default_credit?: number },
    ) =>
      jsonRequest<TenantItem>(
        `/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}`,
        {
          method: 'PATCH',
          headers: jsonHeaders,
          body: JSON.stringify(body),
        },
      ),
    archiveTenant: (tenant_id: string) =>
      jsonRequest<void>(
        `/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}`,
        { method: 'DELETE' },
      ),
    setOwner: (tenant_id: string, team_lead_user_id: string) =>
      jsonRequest<TenantItem>(
        `/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}/owner`,
        {
          method: 'PUT',
          headers: jsonHeaders,
          body: JSON.stringify({ team_lead_user_id }),
        },
      ),
    tenantUsers: (tenant_id: string) =>
      jsonRequest<AdminTenantMembersResponse>(
        `/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}/users`,
      ),
    tenantUsage: (tenant_id: string, sinceDays?: number) => {
      const q = sinceDays ? `?since_days=${sinceDays}` : ''
      return jsonRequest<UsageBucket>(
        `/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}/usage${q}`,
      )
    },
    // L5-d: admin per-run billing incl. provider cost + margin.
    runBilling: (tenant_id: string, runId: string) =>
      jsonRequest<RunBreakdownAdmin>(
        `/api/mvp/admin/billing/runs/${encodeURIComponent(runId)}?tenant_id=${encodeURIComponent(tenant_id)}`,
      ),
    // A-1: get the tenant's dollar pool budget for a period. Throws a 404
    // (err.status === 404) when the tenant has no pool for the period — the
    // caller treats that as "no pool set" rather than an error.
    getPoolBudget: (tenant_id: string, period?: string) => {
      const q = period ? `?period=${encodeURIComponent(period)}` : ''
      return jsonRequest<PoolBudget>(
        `/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}/pool-budget${q}`,
      )
    },
    setPoolBudget: (
      tenant_id: string,
      body: { limit_usd_cents: number; period?: string; status?: 'active' | 'suspended' },
    ) =>
      jsonRequest<PoolBudget>(
        `/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}/pool-budget`,
        {
          method: 'PUT',
          headers: jsonHeaders,
          body: JSON.stringify(body),
        },
      ),
    // P0-11: tenant/user routing config (chain, quotas, allowlist). GET returns
    // defaults (configured=false) when unset. PUT is a full replace; the backend
    // validates model ids, quota limits, and user-chain subsequence (400 names
    // the offending field).
    getRoutingConfig: (tenant_id: string) =>
      jsonRequest<TenantRoutingConfig>(
        `/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}/routing-config`,
      ),
    setRoutingConfig: (
      tenant_id: string,
      body: {
        allowlist?: string[]
        chain?: string[]
        quotas?: Record<string, ModelQuota>
        fallback_mode?: string
        fallback_default?: 'on' | 'off'
        free_tier_model?: string | null
      },
    ) =>
      jsonRequest<TenantRoutingConfig>(
        `/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}/routing-config`,
        { method: 'PUT', headers: jsonHeaders, body: JSON.stringify(body) },
      ),
    getUserRoutingConfig: (tenant_id: string, user_id: string) =>
      jsonRequest<UserRoutingConfig>(
        `/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}/users/${encodeURIComponent(user_id)}/routing-config`,
      ),
    setUserRoutingConfig: (
      tenant_id: string,
      user_id: string,
      body: { preferred_model?: string | null; chain?: string[] | null; fallback?: 'on' | 'off' | null },
    ) =>
      jsonRequest<UserRoutingConfig>(
        `/api/mvp/admin/tenants/${encodeURIComponent(tenant_id)}/users/${encodeURIComponent(user_id)}/routing-config`,
        { method: 'PUT', headers: jsonHeaders, body: JSON.stringify(body) },
      ),
    usageLogs: (opts?: {
      tenant_id?: string
      user_id?: string
      since?: string
      until?: string
      cursor?: string
      limit?: number
    }) => {
      const params = new URLSearchParams()
      if (opts?.tenant_id) params.set('tenant_id', opts.tenant_id)
      if (opts?.user_id) params.set('user_id', opts.user_id)
      if (opts?.since) params.set('since', opts.since)
      if (opts?.until) params.set('until', opts.until)
      if (opts?.cursor) params.set('cursor', opts.cursor)
      if (opts?.limit) params.set('limit', String(opts.limit))
      const q = params.toString()
      return jsonRequest<UsageLogsResponse>(
        `/api/mvp/admin/usage-logs${q ? `?${q}` : ''}`,
      )
    },

    // Read-only effective pricing table (#66).
    pricingConfig: () =>
      jsonRequest<PricingConfigResponse>('/api/mvp/admin/pricing-config'),

    // --- Phase S: Trusted Accounts ---
    listTrustedAccounts: (opts?: { cursor?: string; limit?: number }) => {
      const params = new URLSearchParams()
      if (opts?.cursor) params.set('cursor', opts.cursor)
      if (opts?.limit) params.set('limit', String(opts.limit))
      const q = params.toString()
      return jsonRequest<TrustedAccountsListResponse>(
        `/api/mvp/admin/trusted-accounts${q ? `?${q}` : ''}`,
      )
    },
    getTrustedAccount: (account_id: string) =>
      jsonRequest<TrustedAccountItem>(
        `/api/mvp/admin/trusted-accounts/${encodeURIComponent(account_id)}`,
      ),
    createTrustedAccount: (body: {
      account_id: string
      description?: string
      provisioning_policy?: ProvisioningPolicy
      allowed_role_patterns?: string[]
      allow_iam_user?: boolean
      allow_instance_profile?: boolean
      default_tenant_id?: string
      default_credit?: number
    }) =>
      jsonRequest<TrustedAccountItem>('/api/mvp/admin/trusted-accounts', {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify(body),
      }),
    updateTrustedAccount: (
      account_id: string,
      body: Partial<{
        description: string
        provisioning_policy: ProvisioningPolicy
        allowed_role_patterns: string[]
        allow_iam_user: boolean
        allow_instance_profile: boolean
        default_tenant_id: string
        default_credit: number
      }>,
    ) =>
      jsonRequest<TrustedAccountItem>(
        `/api/mvp/admin/trusted-accounts/${encodeURIComponent(account_id)}`,
        {
          method: 'PATCH',
          headers: jsonHeaders,
          body: JSON.stringify(body),
        },
      ),
    deleteTrustedAccount: (account_id: string) =>
      jsonRequest<void>(
        `/api/mvp/admin/trusted-accounts/${encodeURIComponent(account_id)}`,
        { method: 'DELETE' },
      ),

    // --- Phase S: SSO Invites ---
    listSsoInvites: (opts?: { account_id?: string; cursor?: string; limit?: number }) => {
      const params = new URLSearchParams()
      if (opts?.account_id) params.set('account_id', opts.account_id)
      if (opts?.cursor) params.set('cursor', opts.cursor)
      if (opts?.limit) params.set('limit', String(opts.limit))
      const q = params.toString()
      return jsonRequest<SsoInvitesListResponse>(
        `/api/mvp/admin/sso-invites${q ? `?${q}` : ''}`,
      )
    },
    createSsoInvite: (body: {
      email: string
      account_id: string
      invited_role?: 'user' | 'team_lead'
      tenant_id?: string
      total_credit?: number
      iam_user_name?: string
    }) =>
      jsonRequest<SsoInviteItem>('/api/mvp/admin/sso-invites', {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify(body),
      }),
    deleteSsoInvite: (email: string) =>
      jsonRequest<void>(
        `/api/mvp/admin/sso-invites/${encodeURIComponent(email)}`,
        { method: 'DELETE' },
      ),

    // Admin: list a user's API keys. NOTE the admin per-user endpoint returns a
    // BARE array (list[ApiKeySummary]), not the {keys,...} envelope the
    // self-service `apiKeys.list` uses. include_revoked defaults true so an
    // admin auditing a user sees revocation history (and the row stays visible
    // after revoke).
    userApiKeys: (user_id: string, includeRevoked = true) => {
      const q = includeRevoked ? '?include_revoked=true' : ''
      return jsonRequest<ApiKeySummary[]>(
        `/api/mvp/admin/users/${encodeURIComponent(user_id)}/api-keys${q}`,
      )
    },
    // Admin: revoke ANY key by its key_id. The bare /{key_hash} route is 410
    // Gone; this by-key-id path is the live one.
    revokeApiKey: (key_id: string) =>
      jsonRequest<void>(
        `/api/mvp/admin/api-keys/by-key-id/${encodeURIComponent(key_id)}`,
        { method: 'DELETE' },
      ),
  },

  apiKeys: {
    list: (includeRevoked = false) => {
      const q = includeRevoked ? '?include_revoked=true' : ''
      return jsonRequest<ApiKeyList>(`/api/mvp/me/api-keys${q}`)
    },
    create: (body: CreateApiKeyRequest) =>
      jsonRequest<CreateApiKeyResponse>('/api/mvp/me/api-keys', {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify(body),
      }),
    // Revoke by the user-facing key_id (e.g. "sk-stratoclave-AbCd...XYz9").
    // The list API returns key_id but not key_hash, so this is the path the
    // UI uses. The legacy /api/mvp/me/api-keys/{key_hash} route returns 410.
    revokeByKeyId: (key_id: string) =>
      jsonRequest<void>(
        `/api/mvp/me/api-keys/by-key-id/${encodeURIComponent(key_id)}`,
        { method: 'DELETE' },
      ),
  },

  teamLead: {
    listTenants: () =>
      jsonRequest<{ tenants: TenantItem[] }>('/api/mvp/team-lead/tenants'),
    getTenant: (tenant_id: string) =>
      jsonRequest<TenantItem>(
        `/api/mvp/team-lead/tenants/${encodeURIComponent(tenant_id)}`,
      ),
    createTenant: (body: { name: string; default_credit?: number }) =>
      jsonRequest<TenantItem>('/api/mvp/team-lead/tenants', {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify(body),
      }),
    updateTenant: (
      tenant_id: string,
      body: { name?: string; default_credit?: number },
    ) =>
      jsonRequest<TenantItem>(
        `/api/mvp/team-lead/tenants/${encodeURIComponent(tenant_id)}`,
        {
          method: 'PATCH',
          headers: jsonHeaders,
          body: JSON.stringify(body),
        },
      ),
    members: (tenant_id: string) =>
      jsonRequest<TeamLeadMembersResponse>(
        `/api/mvp/team-lead/tenants/${encodeURIComponent(tenant_id)}/members`,
      ),
    usage: (tenant_id: string, sinceDays?: number) => {
      const q = sinceDays ? `?since_days=${sinceDays}` : ''
      return jsonRequest<UsageBucket>(
        `/api/mvp/team-lead/tenants/${encodeURIComponent(tenant_id)}/usage${q}`,
      )
    },
  },
}

export type ApiClient = typeof api
