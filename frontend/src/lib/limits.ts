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

/**
 * The task-tag grammar, mirroring `backend/mvp/task_tag.py` (whose `GRAMMAR` is
 * `observability.context._ID_GRAMMAR`, the same pattern the `x-sc-*` correlation
 * ids use) and its `MAX_LEN`.
 *
 * Here for the same reason the ceiling above is: a form that rejects what the API
 * accepts, or accepts what it rejects, fails in neither codebase.
 *
 * **This is the grammar and nothing else.** The gateway owns canonicalisation
 * (NFKC, then case-fold) and owns the reserved sentinel, so `Migration-42` is a
 * valid tag here and is stored as `migration-42`, and `UNLABELLED` is valid here
 * and dropped there as reserved. A second canonicaliser in this file, or a copy of
 * the reserved word, would be two places deciding what a tag means.
 */
export const TASK_TAG_PATTERN = /^[A-Za-z0-9._:-]{1,64}$/
export const TASK_TAG_MAX_LEN = 64
