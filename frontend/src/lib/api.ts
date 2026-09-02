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
/**
 * F3 (contract R36/R38/U4): a refusal's `detail` is an OBJECT for every
 * limit-raise/grant/hint endpoint (`{"type": ..., "reason": ..., "message":
 * ..., "raise_hint": {...}, ...}` -- `GrantError.as_detail()` and
 * `_refusal_body()` both shape it this way), never a bare string. The
 * pre-existing check below (`typeof body?.detail === 'string'`) silently
 * discarded exactly that shape -- correct for the plain-string 40x bodies
 * most of this app's existing pages throw, but it left every structured
 * refusal's `raise_hint`/`blocker`/`grantable`/machine `reason` unreachable
 * from a caller, which is what the self-service request view needs to
 * pre-fill from. `err.detailBody` carries the parsed object (whatever shape
 * it is) alongside the pre-existing `err.detail` string, so no existing
 * caller of the string field changes behaviour.
 */
export interface ApiError extends Error {
  status: number
  detail?: string
  detailBody?: unknown
}

async function jsonRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await authFetch(path, init)
  if (!res.ok) {
    let detail: string | undefined
    let detailBody: unknown
    try {
      const body = await res.clone().json()
      detailBody = body?.detail
      detail = typeof body?.detail === 'string' ? body.detail : undefined
    } catch {
      // ignore
    }
    // Every structured refusal this backend throws (`credit_exhausted`,
    // `grant_cap_exceeded`, `figure_includes_active_grant`, ...) sends
    // `detail` as an OBJECT with its own `.message`, not a string -- so
    // `detail` above stays undefined for every one of them, and a caller
    // that only checked `e.detail ?? e.message` used to see the opaque
    // "422 Unprocessable Entity" / "402 Payment Required" instead of the
    // message the backend wrote for exactly this case. `err.detail` keeps
    // its existing string-or-undefined contract for callers that already
    // narrow `detailBody` themselves; only the Error's own `.message` (the
    // fallback every such caller already reads) gains the richer text.
    const structuredMessage =
      typeof detailBody === 'object' && detailBody !== null &&
      typeof (detailBody as { message?: unknown }).message === 'string'
        ? (detailBody as { message: string }).message
        : undefined
    const err = new Error(
      detail ?? structuredMessage ?? `${res.status} ${res.statusText}`,
    ) as ApiError
    err.status = res.status
    err.detail = detail
    err.detailBody = detailBody
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
  // R38 (F3): WHY, not just THAT. See `UsageLogEntry.fallback_reason`.
  fallback_reason?: string | null
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
  // SIGNED and never clamped. A ceiling lowered below committed spend leaves a
  // deficit, and its magnitude is what an operator acts on: floored at zero,
  // "nothing left" and "already $400 over" read identically.
  remaining_microusd: number
  // A DIFFERENT fact, not a restatement: the magnitude of the overshoot, zero
  // whenever there is none, so a surface need not inspect a sign.
  over_ceiling_microusd: number
  pool_limit_usd_cents: number
  remaining_usd_cents: number
  // The ceiling's composition, so the total beside it can be checked. The mode is
  // a SENTENCE and not a state name: a field spelling "per_seat" named a state and
  // told the reader nothing they could act on.
  mode_sentence: string
  seat_tracked: boolean
  seat_count: number
  seat_rate_microusd: number
  seat_entitlement_microusd: number
  // null exactly when the ceiling follows the seats: ABSENCE is the sentinel, and
  // zero is a figure meaning every request refused.
  manual_limit_microusd: number | null
  // Zero until grants exist; rendered anyway so the parts always add up to the
  // total printed beside them.
  pool_granted_microusd: number
  baseline_microusd: number
  entitlement_exceeds_figure: boolean
  resume_action: string | null
  // R21b (F3): three facts, not one, because collapsing them loses the one
  // that matters. `grant_cap_microusd` is the STORED figure, null when
  // nobody set one; `effective_grant_cap_microusd` is the number actually
  // in force either way; `grant_cap_is_derived` says which of those two a
  // surface is looking at, so it can render "derived from baseline" rather
  // than a number an approver might mistake for a fixed cap.
  grant_cap_microusd: number | null
  effective_grant_cap_microusd: number
  grant_cap_is_derived: boolean
  // What an approver still has room to grant right now (R28/R36's B6 half).
  remaining_grant_cap_microusd: number
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
  // Advisory-only: does NOT affect execution, billing, or routing. Controls
  // whether the shadow judge records a potential-saving advisory (for the
  // Savings Certificate). Tri-state: true/false explicit, null = global default.
  shadow_vsr?: boolean | null
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
  // R38 (F3): WHY, not just THAT -- read straight off the row, never derived.
  // null on a legacy row and on a row where no fallback occurred.
  fallback_reason?: string | null
}

export interface UsageLogsResponse {
  logs: UsageLogEntry[]
  next_cursor?: string | null
}

// --- F3: limit raises, grants, and the hint a 402 carries -----------------
//
// Mirrors backend/mvp/grants.py's RaiseHint/RaiseHintCandidate exactly (B2:
// F2 ships the model, F3 fills it -- no renames, no removals on either
// side of the wire). `blocker` and `router_mode` are closed enums server-side
// but typed as `string` here deliberately: "both clients render an unknown
// code rather than failing closed" (the F3 contract's own Interface
// section) means a value this build has never seen must still render, not
// throw at a TypeScript-narrowed switch.
export interface RaiseHintCandidate {
  blocker: string
  wall: string
  model_id: string | null
  estimated_cost_microusd: number | null
  shortfall_microusd: number | null
  grantable: boolean
  grant_expired: boolean
}

export interface RaiseHint {
  candidates: RaiseHintCandidate[]
  remaining_cap_microusd: number
  reason_codes: string[]
  minimum_raise_microusd: number
  unattempted_model_ids: string[]
  tenant_id: string | null
  requested_model_id: string | null
  target_shortfall_microusd: number | null
  router_mode: string | null
  pricing_version: string | null
  priced_at: string | null
}

// A 402 refusal's full structured body (`mvp._pipeline._refusal_body`).
// `ApiError.detailBody` is `unknown` at the type level (any endpoint can
// throw one); callers narrow to this shape only after checking `type`.
export interface CreditExhaustedDetail {
  type: 'credit_exhausted'
  reason: string
  message: string
  wall: string
  blocker: string
  grantable: boolean
  raise_hint?: RaiseHint
}

export function isCreditExhaustedDetail(
  detail: unknown,
): detail is CreditExhaustedDetail {
  return (
    typeof detail === 'object' &&
    detail !== null &&
    (detail as { type?: unknown }).type === 'credit_exhausted'
  )
}

export interface LimitRaiseRequest {
  request_id: string
  tenant_id: string
  user_id: string
  status: string
  limit_kind: string
  reason_code: string
  asked_amount_microusd: number
  created_at: string
  decided_at?: string
  grant_id?: string
  decision_comment?: string
  // The requester's OWN free-text justification, R12's "the comment" on the
  // approver's queue -- distinct from `decision_comment` (the approver's
  // reply, addressed back to the requester). Optional: not yet returned by
  // `admin_list_limit_raises` (backend gap, out of this fork's scope --
  // reported upstream); rendered when present so this component is correct
  // the day the field ships.
  comment?: string
  // R30's "at request time" half: not yet captured by `submit_limit_raise`
  // (backend gap, out of this fork's scope -- reported upstream). Optional
  // so the UI degrades honestly ("not recorded") until it exists.
  observed_limit_microusd?: number | null
  observed_remaining_microusd?: number | null
  observed_at?: string | null
  // R24: always present -- null for a pending request, populated for a
  // decided one. `approver_id` is a stable user id (never an address); the
  // console resolves it to a display name on demand.
  approved_amount_microusd: number | null
  expires_at: number | null
  approver_id: string | null
}

export interface LimitRaisesResponse {
  tenant_id?: string
  requests: LimitRaiseRequest[]
  reason_codes: string[]
}

export interface LimitGrant {
  grant_id: string
  tenant_id: string
  request_id: string
  status: string
  approved_amount_microusd: number
  expires_at: number
  period: string
  target_pk: string
  target_sk: string
  approver_user_id: string
  created_at: string
  capacity_bearing: boolean
  revoke_blocked: boolean
  revoke_attempts: number
  revoked_at?: string
  revoked_by?: string
  revoke_reason?: string
  revoke_blocked_reason?: string
  blocked_at?: string
}

// R25/B4: reconciled PER TARGET ROW, never a single tenant-wide sum -- a
// UI that sums across rows reintroduces exactly the defect the amendment
// closes (a stale prior-period row's grants going uncounted or double
// counted). `reconcile_tenant_grants`'s own shape.
export interface GrantReconciliationRow {
  target_pk: string
  target_sk: string
  period: string
  active_only_sum_microusd: number
  capacity_bearing_sum_microusd: number
  blocked_grant_ids: string[]
  pool_granted_microusd: number
  drift_microusd: number
  grant_cap_microusd: number | null
  effective_grant_cap_microusd: number
  cap_is_derived: boolean
  remaining_cap_microusd: number
  cap_exceeded: boolean
}

export interface GrantReconciliation {
  reconciler: 'tenant_grants'
  tenant_id: string
  period: string | null
  rows: GrantReconciliationRow[]
  orphans: unknown[]
  clean: boolean
}

export interface LimitGrantsResponse {
  tenant_id: string
  grants: LimitGrant[]
  reconciliation: GrantReconciliation
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
  // Whether the PROVIDER reported this leg. `tokens: 0` cannot say it: some models
  // never report prompt-cache counts, and "reported as none" is a measurement while
  // "not reported" is not. Absent/null on an event written before the gateway
  // recorded the distinction — unknown, which is also not zero.
  reported?: boolean | null
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

// External authorize/capture (P0 authcap) — READ-ONLY in the UI. The UI never
// issues authorize/capture (money typo risk); it only surfaces the status of an
// external authorization the tenant created via API/CLI.
export interface AuthorizationStatus {
  authorization_id: string
  tenant_id: string
  amount_microusd: number
  status: string // authorized | captured | voided | expired
  terminal?: string | null
  captured_microusd?: number | null
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

  // R12/R36: file a raise against the caller's own tenant's money ceiling.
  // `tenant_id` is deliberately absent from the body -- the backend reads it
  // from the session, never from a client-supplied field (see the F3
  // contract's own Interface note, mirrored in U4's navigation-state design:
  // the tenant a submission targets comes from the hint that sent the
  // caller here, never from ambient client state).
  submitLimitRaise: (body: {
    asked_amount_microusd: number
    reason_code: string
    client_token: string
    limit_kind?: string
    comment?: string
  }) =>
    jsonRequest<LimitRaiseRequest>('/api/mvp/me/limit-raises', {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify(body),
    }),

  // R24: the join is the whole point -- a decided request carries its own
  // approved amount, expiry and approver; the caller never has to
  // cross-reference the grant inventory to learn what it was granted.
  listMyLimitRaises: (limit?: number) => {
    const q = limit ? `?limit=${limit}` : ''
    return jsonRequest<LimitRaisesResponse>(`/api/mvp/me/limit-raises${q}`)
  },

  withdrawLimitRaise: (request_id: string) =>
    jsonRequest<LimitRaiseRequest>(
      `/api/mvp/me/limit-raises/${encodeURIComponent(request_id)}/withdraw`,
      { method: 'POST' },
    ),

  // R12: the walls that apply to the caller, reachable before any refusal.
  myWallStatus: () =>
    jsonRequest<{
      tenant_id: string
      period: string
      pool: {
        status: string
        pool_limit_microusd: number
        remaining_microusd: number
        remaining_grant_cap_microusd: number
      } | null
    }>('/api/mvp/me/limit-raises/wall-status'),

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

  // Authcap: READ-ONLY status of an external authorization (created via API/CLI).
  // The UI deliberately has NO authorize/capture/void methods — issuing money
  // actions from a human form is a typo risk; those stay programmatic. The
  // cost-leak backstop runs here too (the status shape carries no cost, but the
  // guard is cheap insurance against a drifted API).
  getAuthorization: async (authorizationId: string): Promise<AuthorizationStatus> => {
    const body = await jsonRequest<AuthorizationStatus>(
      `/api/mvp/billing/authorizations/${encodeURIComponent(authorizationId)}`,
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
    // Exactly one of `limit_usd_cents` (a figure; zero means every request
    // refused) and `follow_seats: true` (the reversal). The backend refuses a body
    // carrying both or neither, rather than picking one.
    setPoolBudget: (
      tenant_id: string,
      body:
        | { limit_usd_cents: number; period?: string; status?: 'active' | 'suspended' }
        | { follow_seats: true; period?: string },
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
        shadow_vsr?: boolean | null
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

    // R12 (tenant approval view), global approver (`limits:approve`).
    listLimitRaises: (tenant_id: string, status?: string) => {
      const params = new URLSearchParams({ tenant_id })
      if (status) params.set('status', status)
      return jsonRequest<LimitRaisesResponse>(
        `/api/mvp/admin/limit-raises?${params.toString()}`,
      )
    },
    approveLimitRaise: (
      request_id: string,
      body: { approved_amount_microusd: number; expires_at: number; decision_comment?: string },
    ) =>
      jsonRequest<{ request: LimitRaiseRequest; grant: LimitGrant }>(
        `/api/mvp/admin/limit-raises/${encodeURIComponent(request_id)}/approve`,
        { method: 'POST', headers: jsonHeaders, body: JSON.stringify(body) },
      ),
    rejectLimitRaise: (request_id: string, decision_comment: string) =>
      jsonRequest<LimitRaiseRequest>(
        `/api/mvp/admin/limit-raises/${encodeURIComponent(request_id)}/reject`,
        {
          method: 'POST',
          headers: jsonHeaders,
          body: JSON.stringify({ decision_comment }),
        },
      ),
    // R25: the inventory, reconciled per target row (never a single sum).
    listLimitGrants: (tenant_id: string) =>
      jsonRequest<LimitGrantsResponse>(
        `/api/mvp/admin/limit-grants?tenant_id=${encodeURIComponent(tenant_id)}`,
      ),
    revokeLimitGrant: (tenant_id: string, grant_id: string, reason?: string) =>
      jsonRequest<LimitGrant>(
        `/api/mvp/admin/limit-grants/${encodeURIComponent(grant_id)}/revoke?tenant_id=${encodeURIComponent(tenant_id)}`,
        { method: 'POST', headers: jsonHeaders, body: JSON.stringify({ reason }) },
      ),
    // R28: shown before it is typed, so the approver sees the bound rather
    // than discovering it from a refusal.
    latestPermissibleExpiry: (period?: string) => {
      const q = period ? `?period=${encodeURIComponent(period)}` : ''
      return jsonRequest<{ period: string; latest_permissible_expiry: number }>(
        `/api/mvp/admin/limit-raises/latest-permissible-expiry${q}`,
      )
    },
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
    // A team lead can SET this ceiling, and setting it is the write that ends seat
    // tracking -- so without the read, the one role that can silently leave seat
    // tracking is the one role that cannot see it happened.
    getPoolBudget: (tenant_id: string, period?: string) => {
      const q = period ? `?period=${encodeURIComponent(period)}` : ''
      return jsonRequest<PoolBudget>(
        `/api/mvp/team-lead/tenants/${encodeURIComponent(tenant_id)}/pool-budget${q}`,
      )
    },
    setPoolBudget: (
      tenant_id: string,
      body:
        | { limit_usd_cents: number; period?: string; status?: 'active' | 'suspended' }
        | { follow_seats: true; period?: string },
    ) =>
      jsonRequest<PoolBudget>(
        `/api/mvp/team-lead/tenants/${encodeURIComponent(tenant_id)}/pool-budget`,
        {
          method: 'PUT',
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

    // R12, for a tenant the caller OWNS (`limits:approve-own`) rather than any
    // tenant (`limits:approve`) -- a distinct authority, reached at a distinct
    // route, per the backend's own separation.
    listLimitRaises: (tenant_id: string, status?: string) => {
      const params = new URLSearchParams({ tenant_id })
      if (status) params.set('status', status)
      return jsonRequest<LimitRaisesResponse>(
        `/api/mvp/team-lead/limit-raises?${params.toString()}`,
      )
    },
    approveLimitRaise: (
      request_id: string,
      body: { approved_amount_microusd: number; expires_at: number; decision_comment?: string },
    ) =>
      jsonRequest<{ request: LimitRaiseRequest; grant: LimitGrant }>(
        `/api/mvp/team-lead/limit-raises/${encodeURIComponent(request_id)}/approve`,
        { method: 'POST', headers: jsonHeaders, body: JSON.stringify(body) },
      ),
    rejectLimitRaise: (request_id: string, decision_comment: string) =>
      jsonRequest<LimitRaiseRequest>(
        `/api/mvp/team-lead/limit-raises/${encodeURIComponent(request_id)}/reject`,
        {
          method: 'POST',
          headers: jsonHeaders,
          body: JSON.stringify({ decision_comment }),
        },
      ),
    listLimitGrants: (tenant_id: string) =>
      jsonRequest<LimitGrantsResponse>(
        `/api/mvp/team-lead/limit-grants?tenant_id=${encodeURIComponent(tenant_id)}`,
      ),
    revokeLimitGrant: (tenant_id: string, grant_id: string, reason?: string) =>
      jsonRequest<LimitGrant>(
        `/api/mvp/team-lead/limit-grants/${encodeURIComponent(grant_id)}/revoke?tenant_id=${encodeURIComponent(tenant_id)}`,
        { method: 'POST', headers: jsonHeaders, body: JSON.stringify({ reason }) },
      ),
    latestPermissibleExpiry: (period?: string) => {
      const q = period ? `?period=${encodeURIComponent(period)}` : ''
      return jsonRequest<{ period: string; latest_permissible_expiry: number }>(
        `/api/mvp/team-lead/limit-raises/latest-permissible-expiry${q}`,
      )
    },
  },
}

export type ApiClient = typeof api
