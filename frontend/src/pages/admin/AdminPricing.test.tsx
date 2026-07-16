// AdminPricing page rendering test (#66): the read-only effective pricing
// table. Mocks api.admin.pricingConfig and asserts a row renders with the
// pricing key, mapped models, $-formatted rates, and the source label.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { PricingConfigResponse } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  api: {
    admin: {
      pricingConfig: (...args: unknown[]) =>
        (globalThis as any).__pricingConfig(...args),
    },
  },
}))

const mockPricingConfig = vi.fn<() => Promise<PricingConfigResponse>>()
;(globalThis as any).__pricingConfig = () => mockPricingConfig()

import AdminPricing from './AdminPricing'

function withClient(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

beforeEach(() => {
  mockPricingConfig.mockReset()
})

describe('AdminPricing', () => {
  it('renders effective rates with $ formatting and source (defaults)', async () => {
    mockPricingConfig.mockResolvedValue({
      version: null,
      rates: [
        {
          pricing_key: 'opus',
          input_per_mtok_microusd: 5_000_000,
          output_per_mtok_microusd: 25_000_000,
          cache_read_per_mtok_microusd: 500_000,
          cache_write_per_mtok_microusd: 6_250_000,
          source: 'default',
          models: ['claude-opus-4-7'],
        },
      ],
    })

    render(withClient(<AdminPricing />))

    await waitFor(() =>
      expect(screen.getAllByText(/opus/).length).toBeGreaterThan(0),
    )
    // model mapped to the key
    expect(screen.getByText(/claude-opus-4-7/)).toBeInTheDocument()
    // $5 input rate (5_000_000 micro-USD / MTok; trailing zeros trimmed)
    expect(screen.getAllByText(/\$5\b/).length).toBeGreaterThan(0)
    // built-in defaults version line (also appears in the intro copy)
    expect(screen.getAllByText(/Built-in defaults/i).length).toBeGreaterThan(0)
  })

  it('shows the override badge + version when a rate is customized', async () => {
    mockPricingConfig.mockResolvedValue({
      version: 'v1',
      rates: [
        {
          pricing_key: 'haiku',
          input_per_mtok_microusd: 2_000_000,
          output_per_mtok_microusd: 6_000_000,
          cache_read_per_mtok_microusd: 200_000,
          cache_write_per_mtok_microusd: 2_500_000,
          source: 'override',
          models: ['claude-haiku-4-5'],
        },
      ],
    })

    const { container } = render(withClient(<AdminPricing />))

    // Wait for the row data (not just any "override" substring — the intro copy
    // contains the word "overrides", which would resolve the wait prematurely).
    await waitFor(() =>
      expect(screen.getByText(/claude-haiku-4-5/)).toBeInTheDocument(),
    )
    // The override badge + version are rendered once data loads.
    expect(screen.getAllByText(/^override$/i).length).toBeGreaterThan(0)
    expect(container.textContent).toContain('v1')
    expect(container.textContent).toMatch(/Override version/i)
  })
})
