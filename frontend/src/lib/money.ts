// Integer-only money helpers for the tenant dollar pool budget (A-1).
//
// All money crossing the wire is integer micro-USD (1 USD = 1_000_000
// micro-USD); the backend also returns integer USD-cent mirrors. The UI must
// never do floating-point arithmetic on money, so these helpers convert and
// format using integer math only. They live in their own module (not the page
// component) so they can be unit-tested and so React Fast Refresh is not
// disturbed by a component file exporting non-components.

// Render integer micro-USD as a "$X.YY" string, truncated to whole cents.
// TRUNCATE TOWARD ZERO ON MAGNITUDE, matching the backend's cent convention
// (`micro // 10_000`, floor of the magnitude — see admin_tenants
// `_MICRO_USD_PER_CENT`): the UI must never display a cent MORE than the
// backend recorded (Fable review round-2: a half-up UI vs a floor backend
// disagrees by a cent on sub-cent spend like pool_settled_microusd). Symmetric
// across sign (truncation on the magnitude, sign reapplied); integer-only; the
// /10_000,/100 constants are exact for |micro| < 2^53 (~$9.007e9), beyond which
// JSON numbers lose integer precision anyway.
export function fmtMicroUsd(micro: number): string {
  const neg = micro < 0
  const absMicro = Math.abs(Math.trunc(micro))
  const cents = Math.floor(absMicro / 10_000) // truncate toward zero (backend parity)
  const sign = neg && cents !== 0 ? '-' : ''
  const dollars = Math.floor(cents / 100).toLocaleString('en-US')
  return `${sign}$${dollars}.${String(cents % 100).padStart(2, '0')}`
}

// Render integer micro-USD as a full-precision dollar rate, e.g. a per-MTok
// price. Unlike fmtMicroUsd (which rounds to whole cents and would show a real
// sub-cent rate as $0.00), this keeps up to 6 decimals — 1 micro-USD = $0.000001
// — trimming trailing zeros so $5.00/MTok reads "$5", $0.075 reads "$0.075".
// Integer math only (no float on the money value): split micro into whole
// dollars and a 6-digit fractional micro remainder.
export function fmtMicroUsdRate(micro: number): string {
  const neg = micro < 0
  const abs = Math.abs(Math.trunc(micro))
  const dollars = Math.floor(abs / 1_000_000)
  const frac6 = String(abs % 1_000_000).padStart(6, '0').replace(/0+$/, '')
  const sign = neg ? '-' : ''
  // Pin en-US grouping so the thousands separator is always ',' and never
  // collides with the fixed '.' decimal point on a non-US runtime locale.
  const whole = dollars.toLocaleString('en-US')
  return frac6 ? `${sign}$${whole}.${frac6}` : `${sign}$${whole}`
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
  const total = dollars * 100 + cents
  // Guard the PRODUCT too (Fable review M5): a dollars value just under the
  // safe-integer limit passes the check above but dollars*100 overflows.
  if (!Number.isSafeInteger(total)) return null
  return total
}

// Current billing period as YYYY-MM in UTC, matching the backend's
// current_period(). Used for the "no pool" empty-state message.
export function currentPeriodUtc(): string {
  const now = new Date()
  const y = now.getUTCFullYear()
  const mo = String(now.getUTCMonth() + 1).padStart(2, '0')
  return `${y}-${mo}`
}
