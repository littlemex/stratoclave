/**
 * Backend API client (Phase 2 `/api/mvp/*`)
 *
 * 同一 origin (= CloudFront / Vite proxy) に配信されたエンドポイントに
 * Bearer access_token を付けて叩く。すべて相対 URL のため、本番・開発で
 * コードは一切変えずに動く。
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

export interface UsageSummary {
  tenant_id: string
  total_credit: number
  credit_used: number
  remaining_credit: number
  by_model: Record<string, number>
  by_tenant: Record<string, number>
  sample_size: number
  since_days: number
}

export interface UsageHistoryEntry {
  tenant_id: string
  tenant_name?: string | null
  model_id: string
  input_tokens: number
  output_tokens: number
  total_tokens: number
  recorded_at: string
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

export interface UsageLogEntry {
  tenant_id: string
  user_id: string
  user_email?: string | null
  model_id: string
  input_tokens: number
  output_tokens: number
  total_tokens: number
  recorded_at: string
  timestamp_log_id: string
}

export interface UsageLogsResponse {
  logs: UsageLogEntry[]
  next_cursor?: string | null
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
    revoke: (key_hash: string) =>
      jsonRequest<void>(
        `/api/mvp/me/api-keys/${encodeURIComponent(key_hash)}`,
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
