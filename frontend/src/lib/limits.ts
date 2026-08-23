/**
 * Validation ceilings shared by the admin and team-lead forms.
 *
 * These mirror the backend bounds of the same names in `backend/limits.py`. They
 * live in one place because the values were previously written inline in every
 * form: a backend raise left the inputs silently capped at the old number, so the
 * UI rejected a value the API would have accepted.
 *
 * A mirror across two languages can still drift, so `limits.contract.test.ts`
 * reads the backend module and asserts the numbers match.
 */

/** Upper bound for a token credit budget (per-user balance, tenant default). */
export const MAX_TOKEN_CREDIT = 10_000_000_000
