/**
 * Unified Cognito Configuration
 *
 * Single source of truth for Cognito OAuth settings.
 * Values are loaded from runtime config (/config.json) at app initialization.
 * This eliminates the need for .env.production and enables zero-rebuild deployments.
 */

import { getRuntimeConfig } from './runtimeConfig'

/**
 * Get Cognito Client ID from runtime config
 */
export function getClientId(): string {
  return getRuntimeConfig().cognito.client_id
}

/**
 * Get Cognito Domain from runtime config
 */
export function getCognitoDomain(): string {
  return getRuntimeConfig().cognito.domain
}

/**
 * Get API Endpoint from runtime config
 */
export function getApiEndpoint(): string {
  return getRuntimeConfig().api.endpoint
}

/**
 * Get OAuth redirect URI (always based on current origin)
 */
export function getRedirectUri(): string {
  return `${window.location.origin}/callback`
}
