import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { MAX_TOKEN_CREDIT } from './limits'

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
