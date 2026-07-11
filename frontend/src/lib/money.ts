// Integer-only money helpers for the tenant dollar pool budget (A-1).
//
// All money crossing the wire is integer micro-USD (1 USD = 1_000_000
// micro-USD); the backend also returns integer USD-cent mirrors. The UI must
// never do floating-point arithmetic on money, so these helpers convert and
// format using integer math only. They live in their own module (not the page
// component) so they can be unit-tested and so React Fast Refresh is not
// disturbed by a component file exporting non-components.

// Render integer micro-USD as a "$X.YY" string. cents = round(micro / 10_000).
export function fmtMicroUsd(micro: number): string {
  const cents = Math.round(micro / 10_000)
  const neg = cents < 0
  const abs = Math.abs(cents)
  const sign = neg ? '-' : ''
  return `${sign}$${Math.floor(abs / 100).toLocaleString()}.${String(abs % 100).padStart(2, '0')}`
}

// Parse a user-typed dollar string ("500", "$500", "1,000", "500.50") into
// integer USD cents, mirroring the Rust CLI's parse_usd_to_cents. Returns null
// on empty, negative, non-numeric, or sub-cent (>2 decimals) input so callers
// can block submission.
export function parseUsdToCents(input: string): number | null {
  const cleaned = input.trim().replace(/[$,\s]/g, '')
  if (cleaned === '' || cleaned.startsWith('-')) return null
  const m = /^(\d*)(?:\.(\d+))?$/.exec(cleaned)
  if (!m) return null
  const dollarsStr = m[1] ?? ''
  const centsStr = m[2] ?? ''
  if (dollarsStr === '' && centsStr === '') return null
  if (centsStr.length > 2) return null
  const dollars = dollarsStr === '' ? 0 : Number(dollarsStr)
  const cents = centsStr === '' ? 0 : Number(centsStr.padEnd(2, '0'))
  if (!Number.isSafeInteger(dollars) || !Number.isFinite(cents)) return null
  return dollars * 100 + cents
}

// Current billing period as YYYY-MM in UTC, matching the backend's
// current_period(). Used for the "no pool" empty-state message.
export function currentPeriodUtc(): string {
  const now = new Date()
  const y = now.getUTCFullYear()
  const mo = String(now.getUTCMonth() + 1).padStart(2, '0')
  return `${y}-${mo}`
}
