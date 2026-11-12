import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { LOCALE_STORAGE_KEY } from './i18n'

/**
 * The screenshot tool (`docs/screenshots/tool/capture.mjs`) seeds the console's
 * browser storage by hand, so it repeats facts the app owns: where the token lives,
 * under which key, and how the locale is chosen. When the app moved tokens from
 * localStorage to sessionStorage the tool kept writing localStorage, the app ignored
 * it, and every authenticated shot came out as the sign-in page. These tests read
 * the tool and the app side by side, so the next move fails here.
 */
const ROOT = join(__dirname, '..', '..', '..')
const tool = readFileSync(join(ROOT, 'docs', 'screenshots', 'tool', 'capture.mjs'), 'utf8')
const cognito = readFileSync(join(__dirname, 'cognito.ts'), 'utf8')
const code = (src: string) => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')

describe('the screenshot tool seeds storage the way the console reads it', () => {
  it('uses the token key the console uses', () => {
    const appKey = /const STORAGE_KEY = '([^']+)'/.exec(cognito)?.[1]
    const toolKey = /const TOKEN_STORAGE_KEY = '([^']+)'/.exec(tool)?.[1]
    expect(appKey).toBeTruthy()
    expect(toolKey).toBe(appKey)
  })

  it('writes the token where the console reads it', () => {
    const appReadsSession = /sessionStorage\.getItem\(STORAGE_KEY\)/.test(code(cognito))
    expect(appReadsSession).toBe(true)
    expect(code(tool)).not.toMatch(/localStorage/)
    expect(code(tool)).toMatch(/sessionStorage\.setItem\(\s*tokenKey/)
  })

  it('uses the console locale key and sets the server-side locale', () => {
    const toolKey = /const LOCALE_STORAGE_KEY = '([^']+)'/.exec(tool)?.[1]
    expect(toolKey).toBe(LOCALE_STORAGE_KEY)
    expect(code(tool)).toMatch(/\/api\/mvp\/me[`'"][\s\S]{0,80}method: 'PATCH'/)
  })
})
