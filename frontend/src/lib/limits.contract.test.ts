import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { MAX_TOKEN_CREDIT, TASK_TAG_MAX_LEN, TASK_TAG_PATTERN } from './limits'

/**
 * The forms and the API must agree on the same ceiling. Keeping the number in one
 * TypeScript module fixed the drift *within* the frontend, but a hand-written
 * mirror of a Python constant can still fall behind — and the symptom is a form
 * that rejects a value the API accepts, with nothing failing in either codebase.
 * So read the backend module and compare.
 */
const backendLimits = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../../../backend/limits.py'),
  'utf8',
)

function backendConstant(name: string): number {
  const match = backendLimits.match(new RegExp(`^${name}\\s*=\\s*([0-9_]+)`, 'm'))
  if (!match) throw new Error(`${name} not found in backend/limits.py`)
  return Number(match[1].replace(/_/g, ''))
}

describe('credit ceilings match the backend', () => {
  it('MAX_TOKEN_CREDIT is the same on both sides', () => {
    expect(MAX_TOKEN_CREDIT).toBe(backendConstant('MAX_TOKEN_CREDIT'))
  })

  it('stays inside the range a browser can represent exactly', () => {
    expect(MAX_TOKEN_CREDIT).toBeLessThan(Number.MAX_SAFE_INTEGER)
  })
})

/**
 * The tag grammar is a mirror of a Python regex, so it drifts the same way a
 * mirrored number does — and worse, because the failure is asymmetric: a UI
 * pattern that is too strict rejects a tag the gateway would have recorded, and
 * one that is too loose lets a request through to be dropped server-side, filed
 * as unlabelled with the requester believing they labelled it.
 */
const backendTaskTag = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../../../backend/mvp/task_tag.py'),
  'utf8',
)
// `task_tag.py` imports its GRAMMAR from `observability.context._ID_GRAMMAR`
// rather than declaring one, so the pattern is read from that module -- following
// the import instead of assuming the two agree.
const backendIdGrammar = readFileSync(
  resolve(
    dirname(fileURLToPath(import.meta.url)),
    '../../../backend/mvp/observability/context.py',
  ),
  'utf8',
)

describe('task tag grammar matches the backend', () => {
  it('imports its grammar from the id grammar, so that is the one to compare', () => {
    // If this stops being true, the pattern below is being compared against the
    // wrong source and the rest of this block proves nothing.
    expect(backendTaskTag).toMatch(
      /from \.observability\.context import _ID_GRAMMAR as GRAMMAR/,
    )
  })

  it('is the same character class and the same bound', () => {
    const m = backendIdGrammar.match(/_ID_GRAMMAR = re\.compile\(r"(.+?)"\)/)
    if (!m) throw new Error('_ID_GRAMMAR not found in observability/context.py')
    // Python's \A / \Z anchor the whole string; JavaScript spells that ^ / $ on a
    // non-multiline regex. Compare the pattern with the anchors translated, so a
    // change to the character class or the bound fails here.
    const asJs = m[1].replace(/^\\A/, '^').replace(/\\Z$/, '$')
    expect(TASK_TAG_PATTERN.source).toBe(asJs)
  })

  it('MAX_LEN is the same on both sides', () => {
    const m = backendTaskTag.match(/^MAX_LEN:\s*int\s*=\s*([0-9_]+)/m)
    if (!m) throw new Error('MAX_LEN not found in backend/mvp/task_tag.py')
    expect(TASK_TAG_MAX_LEN).toBe(Number(m[1].replace(/_/g, '')))
  })

  it('accepts the reserved word and mixed case, because neither is this side\'s business', () => {
    // Pinned as behaviour, not left as a comment: a future author "helpfully"
    // rejecting UNLABELLED here would refuse a tag the gateway accepts-and-reports
    // on, and would put a copy of the reserved list in the UI.
    expect(TASK_TAG_PATTERN.test('UNLABELLED')).toBe(true)
    expect(TASK_TAG_PATTERN.test('Migration-42')).toBe(true)
  })
})
