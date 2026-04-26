export interface TeamMember {
  id: number
  email: string
  role: string
  created_at: string
  last_active?: string
}

export interface TeamUsageSummary {
  org_id: string
  total_tokens: number
  active_users: number
  period_start: string
  period_end: string
  by_user: Array<{
    email: string
    total_tokens: number
    input_tokens: number
    output_tokens: number
  }>
  by_model: Array<{
    model_id: string
    total_tokens: number
  }>
}

export interface UsageLog {
  id: number
  tenant_id: string
  user_email: string
  model_id: string
  input_tokens: number
  output_tokens: number
  total_tokens: number
  created_at: string
}

export interface UsageLogQuery {
  tenant_id?: string
  user_email?: string
  model_id?: string
  start_date?: string
  end_date?: string
  limit?: number
  offset?: number
}

export interface User {
  id: number
  email: string
  auth_provider: string
  auth_provider_user_id: string
  created_at: string
  tenants: Array<{
    tenant_id: string
    role: string
  }>
}

export interface CreateUserRequest {
  email: string
  auth_provider?: string
  temporary_password?: string
  send_email?: boolean
}

export interface Tenant {
  tenant_id: string
  parent_tenant_id?: string
  metadata: Record<string, unknown>
}

export interface CreateTenantRequest {
  tenant_id: string
  parent_tenant_id?: string
  metadata?: Record<string, unknown>
}
