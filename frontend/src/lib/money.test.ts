// Unit tests for the integer-only money helpers backing the tenant pool
// budget UI (A-1). Money is never handled as a float; parseUsdToCents mirrors
// the Rust CLI's parse_usd_to_cents so the two admin surfaces agree byte for
// byte, and fmtMicroUsd renders integer micro-USD without rounding drift.

import { describe, expect, it } from 'vitest'

import { fmtMicroUsd, parseUsdToCents } from './money'

describe('parseUsdToCents', () => {
  it('parses a plain integer dollar amount', () => {
    expect(parseUsdToCents('500')).toBe(50_000)
  })

  it('strips $ signs and thousands commas', () => {
    expect(parseUsdToCents('$1,000')).toBe(100_000)
  })

  it('parses two-decimal cents exactly', () => {
    expect(parseUsdToCents('500.50')).toBe(50_050)
    expect(parseUsdToCents('0.01')).toBe(1)
  })

  it('treats one decimal as tenths of a dollar', () => {
    // "500.5" is $500.50 == 50_050 cents, not 505.
    expect(parseUsdToCents('500.5')).toBe(50_050)
  })

  it('treats a leading decimal as zero dollars', () => {
    expect(parseUsdToCents('.50')).toBe(50)
    expect(parseUsdToCents('$.50')).toBe(50)
  })

  it('rejects sub-cent precision', () => {
    expect(parseUsdToCents('1.234')).toBeNull()
  })

  it('rejects negative amounts', () => {
    expect(parseUsdToCents('-5')).toBeNull()
  })

  it('rejects empty and non-numeric input', () => {
    expect(parseUsdToCents('')).toBeNull()
    expect(parseUsdToCents('   ')).toBeNull()
    expect(parseUsdToCents('abc')).toBeNull()
    expect(parseUsdToCents('1.2.3')).toBeNull()
  })

  it('accepts zero', () => {
    expect(parseUsdToCents('0')).toBe(0)
    expect(parseUsdToCents('$0.00')).toBe(0)
  })
})

describe('fmtMicroUsd', () => {
  it('formats whole and fractional dollars from micro-USD', () => {
    expect(fmtMicroUsd(500_000_000)).toBe('$500.00')
    expect(fmtMicroUsd(500_500_000)).toBe('$500.50')
    expect(fmtMicroUsd(10_000)).toBe('$0.01')
    expect(fmtMicroUsd(0)).toBe('$0.00')
  })

  it('groups thousands', () => {
    expect(fmtMicroUsd(1_000_000_000)).toBe('$1,000.00')
  })

  it('rounds to the nearest cent (never sub-cent display)', () => {
    // 4_999 micro-USD == 0.4999 cents -> rounds down to 0 cents.
    expect(fmtMicroUsd(4_999)).toBe('$0.00')
    // 5_000 micro-USD == 0.5 cents -> rounds up to 1 cent.
    expect(fmtMicroUsd(5_000)).toBe('$0.01')
    // 14_999 micro-USD == 1.4999 cents -> 1 cent.
    expect(fmtMicroUsd(14_999)).toBe('$0.01')
  })

  it('handles negatives with a leading sign', () => {
    expect(fmtMicroUsd(-500_000_000)).toBe('-$500.00')
  })
})
