/**
 * Runtime Configuration Loader
 *
 * Loads and caches config.json once at startup.
 * Production: scripts/generate-config-json.sh generates it from SSM and places it in S3 (snake_case)
 * Development: Vite serves frontend/public/config.json (same schema)
 */

export interface RuntimeConfig {
  cognito: {
    user_pool_id: string
    client_id: string
    domain: string
    region: string
  }
  api: {
    endpoint: string
  }
  app?: {
    cloudfront_domain?: string
  }
}

let cachedConfig: RuntimeConfig | null = null

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  if (cachedConfig) {
    return cachedConfig
  }

  const response = await fetch('/config.json', { cache: 'no-store' })
  if (!response.ok) {
    throw new Error(
      `Failed to fetch config.json: ${response.status} ${response.statusText}`,
    )
  }
  const config = (await response.json()) as RuntimeConfig

  if (!config.cognito?.client_id) throw new Error('config.json missing: cognito.client_id')
  if (!config.cognito?.domain) throw new Error('config.json missing: cognito.domain')
  if (!config.cognito?.user_pool_id) throw new Error('config.json missing: cognito.user_pool_id')
  if (!config.cognito?.region) throw new Error('config.json missing: cognito.region')
  if (config.api?.endpoint === undefined) throw new Error('config.json missing: api.endpoint')

  if (config.api.endpoint === '') {
    config.api.endpoint = window.location.origin
  }

  cachedConfig = config
  return config
}

export function getRuntimeConfig(): RuntimeConfig {
  if (!cachedConfig) {
    throw new Error('Runtime config not loaded. Call loadRuntimeConfig() first.')
  }
  return cachedConfig
}

/** @internal testing only */
export function clearCachedConfig(): void {
  cachedConfig = null
}
