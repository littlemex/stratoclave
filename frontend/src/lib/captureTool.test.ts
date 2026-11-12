import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { TOKEN_STORAGE_KEY } from './cognito'
import { LOCALE_STORAGE_KEY } from './i18n'

/**
 * The screenshot tool (`docs/screenshots/tool/capture.mjs`) seeds the console's
 * browser storage by hand, so it repeats two facts the app owns: the key the token
 * is stored under and the key the locale is stored under. When the app moved tokens
 * from localStorage to sessionStorage the tool kept writing localStorage and every
 * authenticated shot came out as the sign-in page.
 *
 * These checks are the cheap static half. The general guard is at run time: the tool
 * waits for each page's own heading, in the run's language, before it saves
 * anything, so a token the app does not accept or a wrong locale fails the run
 * instead of producing a plausible picture.
 *
 * The comment stripper below handles today's tool; it does not understand `//` or
 * `/*` inside string or regex literals.
 */
const ROOT = join(__dirname, '..', '..', '..')
const tool = readFileSync(join(ROOT, 'docs', 'screenshots', 'tool', 'capture.mjs'), 'utf8')
const code = (src: string) =>
  src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')

describe('the screenshot tool seeds storage the way the console reads it', () => {
  it('uses the console token and locale keys', () => {
    expect(/const TOKEN_STORAGE_KEY = '([^']+)'/.exec(tool)?.[1]).toBe(TOKEN_STORAGE_KEY)
    expect(/const LOCALE_STORAGE_KEY = '([^']+)'/.exec(tool)?.[1]).toBe(LOCALE_STORAGE_KEY)
  })

  it('keeps the token where the console reads it, sessionStorage', () => {
    expect(code(tool)).not.toMatch(/localStorage/)
    expect(code(tool)).toMatch(/sessionStorage\.setItem\(\s*tokenKey/)
  })

  it('sets the locale in the browser, without writing it to the server', () => {
    expect(code(tool)).toMatch(/route\('\*\*\/api\/mvp\/me'/)
    expect(code(tool)).not.toMatch(/method:\s*'PATCH'|me\('PATCH'/)
  })
})
