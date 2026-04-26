export type ConnectionMode = 'production' | 'development' | 'local'

export interface AWSProfile {
  name: string
  region?: string
  is_default: boolean
}

export interface ProxyGatewaySettings {
  url: string
  verify_ssl: boolean
}

export interface ConnectionSettings {
  mode: ConnectionMode
  aws_profile: string | null
  aws_region: string | null
  bedrock_model_id: string
  proxy_gateway: ProxyGatewaySettings | null
}

export interface UpdateConnectionSettingsRequest {
  mode?: ConnectionMode
  aws_profile?: string
  aws_region?: string
  bedrock_model_id?: string
  proxy_gateway_url?: string
  proxy_gateway_verify_ssl?: boolean
}

export interface ConnectionTestResult {
  success: boolean
  message: string
  details?: Record<string, unknown>
}
