import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

/**
 * The task tag and the requester's comment are safe as React text nodes, and only
 * as React text nodes.
 *
 * JSX interpolation escapes them, including a value written by an older client
 * under looser rules — this build never re-validates what is already stored. But
 * text-node safety is a property of that ONE sink, not of the value. The same string
 * in an `href`, a `style`, a `download` filename, a `window.open` target or a
 * spreadsheet cell is a different question with a different answer, and React's
 * escaping does not make a `javascript:` URL safe or a leading `=` inert in Excel.
 *
 * This repo already has the right idiom for that: `main-splash-xss.test.ts` scans
 * source for a CLASS of sinks rather than the one instance that bit someone, with a
 * comment recording that a squash reintroduced the regression twice. Prose in a
 * design document does not survive a refactor; a scan does.
 *
 * So: on the files that handle a tag or a comment, none of these sinks may appear at
 * all. That is deliberately blunter than "must not receive a tag" — dataflow
 * analysis is not available here, and a file that has no `href={` cannot put a tag
 * in one. If one of these files ever legitimately needs a link, the reviewer is
 * forced to look, which is the outcome worth having.
 */
const HERE = dirname(fileURLToPath(import.meta.url))

/**
 * Source with comments removed.
 *
 * Necessary, not cosmetic: the first version of this scan failed on
 * `MeLimitRaises.tsx` and `LimitRaiseApproval.tsx` because both carry comments
 * saying `dangerouslySetInnerHTML` is never used. A check that fails on a comment
 * warning against the thing punishes the behaviour it is trying to encourage, and
 * the obvious "fix" is to delete the warning.
 */
function stripComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/^\s*\/\/.*$/gm, ' ')
}

/** Files that read or write a tag / comment value. */
const FILES = ['MeLimitRaises.tsx', 'MeUsageByTag.tsx', 'LimitRaiseApproval.tsx'] as const

const FORBIDDEN: { name: string; re: RegExp; why: string }[] = [
  {
    name: 'href from an expression',
    re: /href=\{/,
    why: 'a tag or comment in a URL can carry a javascript: or data: scheme',
  },
  {
    name: 'src from an expression',
    re: /\bsrc=\{/,
    why: 'same as href, and it fetches',
  },
  {
    name: 'window.open',
    re: /window\.open\s*\(/,
    why: 'the URL and the window name are both interpreted, not displayed',
  },
  {
    name: 'download attribute',
    re: /\bdownload=/,
    why: 'a filename derived from a tag escapes into the filesystem',
  },
  {
    name: 'createObjectURL',
    re: /createObjectURL\s*\(/,
    why: 'the entry point for an export, which needs spreadsheet-formula defences',
  },
  {
    name: 'style from an expression',
    re: /\bstyle=\{\{?[^}]*\$\{/,
    why: 'a CSS value is parsed as CSS, and url() in it fetches',
  },
  {
    name: 'HTML-parsing sink',
    re: /dangerouslySetInnerHTML|\.innerHTML\s*[=+]|insertAdjacentHTML\s*\(/,
    why: 'parses its input as HTML',
  },
  {
    name: 'template-literal URL',
    re: /(fetch|navigate|to)\s*\(\s*`[^`]*\$\{/,
    why: 'string-concatenated URLs skip encoding; use URLSearchParams or encodeURIComponent',
  },
]

describe('a task tag cannot reach a sink other than a React text node', () => {
  for (const file of FILES) {
    const raw = readFileSync(resolve(HERE, file), 'utf8')
    const src = stripComments(raw)

    it(`${file} contains no forbidden sink`, () => {
      const hits = FORBIDDEN.filter(({ re }) => re.test(src)).map(
        ({ name, why }) => `${name}: ${why}`,
      )
      expect(hits).toEqual([])
    })

    it(`${file} actually mentions a tag or a comment, so this scan is not vacuous`, () => {
      // Without this, renaming a file or moving the tag elsewhere would leave a
      // green test scanning source that no longer handles the value. Checked on the
      // RAW source: a file whose only mention is in a comment is still a file this
      // scan should be watching.
      expect(raw).toMatch(/task_tag|taskTag|decision_comment|comment/)
    })
  }

  it('no untrusted value is used as an i18next KEY', () => {
    // `t('fixed.key', { value: taskTag })` is fine: React renders the value as a
    // text node. `t(taskTag)` is not — it lets user input select which string is
    // shown, and with `saveMissing` enabled it would ship that input to a
    // translation backend.
    //
    // The rule is stated over the UNTRUSTED NAMES rather than over the shape of the
    // argument, because the shape rule has legitimate exceptions this codebase
    // already uses: `t(cond ? 'a.key' : 'b.key')` picks between two literals and is
    // correct. A first version banning any non-literal argument failed on exactly
    // that and would have pushed a reviewer to loosen the check instead of the code.
    const untrusted = /\b(taskTag|task_tag|decisionComment|decision_comment)\b/
    for (const file of FILES) {
      const src = stripComments(readFileSync(resolve(HERE, file), 'utf8'))
      const firstArgs = Array.from(src.matchAll(/\bt\(([^,)]*)/g), (m) => m[1])
      const offenders = firstArgs.filter((a) => untrusted.test(a))
      expect(offenders, `${file}: an untrusted value is being used as a t() key`).toEqual([])
    }
  })
})
