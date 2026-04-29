/**
 * Regression guard for sweep-4: the runtime-config failure splash in
 * main.tsx MUST NOT use innerHTML with any user-influenced value.
 *
 * Attack: an attacker who can influence the value of `/config.json` or
 * any error surface routed into the catch handler can inject
 * `<img onerror=...>` and execute script inside the document before the
 * SPA even mounts. `error.message` is the typical vector because
 * runtime-config errors are frequently "failed to fetch <URL>".
 *
 * Sweep-1 replaced innerHTML with document.createElement +
 * textContent. Sweep-3 squash reintroduced template-string innerHTML.
 * We lock the correct pattern with a static source check — this test
 * intentionally does not mount the real SPA; it scans the main.tsx
 * source to assert the unsafe sink is absent on the error branch.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

describe('main.tsx runtime-config failure splash', () => {
  const src = readFileSync(resolve(__dirname, 'main.tsx'), 'utf8')

  it('does not use ANY HTML-parsing sink in main.tsx', () => {
    // Each of these is a DOM sink that parses its input as HTML and
    // will execute attacker-controlled `<img onerror>` / `<svg onload>`
    // payloads if fed an untrusted string. A future squash could
    // reintroduce the regression in any of these shapes, so we block
    // the entire class, not just the one we've been bitten by.
    const unsafeSinks: { name: string; re: RegExp }[] = [
      { name: 'innerHTML assignment', re: /\.innerHTML\s*[=+]/ },
      { name: 'innerHTML bracket assignment', re: /\[\s*['"]innerHTML['"]\s*\]\s*=/ },
      { name: 'outerHTML assignment', re: /\.outerHTML\s*=/ },
      { name: 'insertAdjacentHTML call', re: /\.insertAdjacentHTML\s*\(/ },
      { name: 'document.write(ln) call', re: /document\.write(ln)?\s*\(/ },
      { name: 'createContextualFragment call', re: /\.createContextualFragment\s*\(/ },
      { name: 'DOMParser instantiation', re: /new\s+DOMParser\s*\(/ },
      { name: 'dangerouslySetInnerHTML', re: /dangerouslySetInnerHTML/ },
    ]
    for (const { name, re } of unsafeSinks) {
      expect(
        re.test(src),
        `main.tsx must not contain unsafe HTML sink: ${name}`,
      ).toBe(false)
    }
  })

  it('uses createElement / textContent for the fallback splash', () => {
    // Positive signal that the safe pattern replaced the unsafe one.
    expect(src).toMatch(/document\.createElement\(/)
    expect(src).toMatch(/textContent\s*=/)
  })
})
