// L5-d contract-drift gate (UI half). Parses the SAME golden fixtures the
// backend emits and the Rust CLI parses (contracts/billing/*.json), so an API
// shape change breaks backend + CLI + UI together. Also proves the runtime
// redaction backstop `assertNoCostLeak` catches a leaked cost/margin key.

import { describe, expect, it } from 'vitest'

import tenantFixture from '../../../contracts/billing/run_tenant.json'
import adminFixture from '../../../contracts/billing/run_admin.json'
import authStatusFixture from '../../../contracts/billing/authorization_status.json'
import {
  assertNoCostLeak,
  type AuthorizationStatus,
  type RunBreakdownAdmin,
  type RunBreakdownTenant,
} from './api'

describe('billing contract fixtures', () => {
  it('tenant fixture matches the RunBreakdownTenant shape and has no cost/margin', () => {
    const b = tenantFixture as RunBreakdownTenant
    expect(b.total_settled_microusd).toBeGreaterThan(0)
    expect(b.events.length).toBeGreaterThan(0)
    // The redaction backstop must NOT throw on a clean tenant payload.
    expect(() => assertNoCostLeak(b)).not.toThrow()
  })

  it('admin fixture carries provider cost + margin', () => {
    const b = adminFixture as RunBreakdownAdmin
    expect(b.total_provider_cost_microusd).not.toBeNull()
    expect(b.total_margin_microusd).not.toBeNull()
    expect(b.events[0].provider_cost_microusd).not.toBeNull()
  })
})

describe('authcap authorization status fixture', () => {
  it('matches the AuthorizationStatus shape (read-only, no cost)', () => {
    const s = authStatusFixture as AuthorizationStatus
    expect(s.authorization_id.startsWith('auth_')).toBe(true)
    expect(s.status).toBe('captured')
    expect(s.terminal).toBe('SETTLE')
    expect(s.captured_microusd).toBe(700000)
    // The cost-leak backstop must not throw on a clean status payload.
    expect(() => assertNoCostLeak(s)).not.toThrow()
  })
})

describe('assertNoCostLeak', () => {
  it('throws when a tenant payload leaks provider_cost_microusd (nested)', () => {
    const leaked = {
      tenant_id: 't',
      run_id: 'r',
      total_settled_microusd: 1,
      events: [
        {
          event_type: 'SETTLE',
          settled_microusd: 1,
          components: {},
          ts_ms: 0,
          provider_cost_microusd: 5,
        },
      ],
    }
    expect(() => assertNoCostLeak(leaked)).toThrow(/redaction violated/)
  })

  it('throws when a top-level total_margin leaks', () => {
    expect(() =>
      assertNoCostLeak({ total_margin_microusd: 9, events: [] }),
    ).toThrow(/redaction violated/)
  })
})
