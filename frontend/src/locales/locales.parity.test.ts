import { describe, expect, it } from 'vitest'

import en from './en.json'
import ja from './ja.json'

/**
 * The two locale bundles must carry the same keys and the same interpolation
 * placeholders.
 *
 * i18next falls back to the fallback language on a missing key, so forgetting a `ja`
 * entry is **silent**: nothing throws, nothing warns, and the result is one English
 * sentence in the middle of a money screen for a Japanese reader. `e2e/i18n.spec.ts`
 * asserts that specific strings render in each language; nothing asserted that the
 * key sets agree, and the bundles matched only because every change so far happened
 * to remember both files.
 *
 * The markup checks are narrower than "no tags", because a tag can be a legitimate
 * `<Trans>` component placeholder. What they ban is a tag carrying attributes, and a
 * key whose two locales disagree about which tags they use.
 *
 * The placeholder half matters as much and fails differently. A key that exists in
 * both but spells `{{amount}}` as `{{ammount}}` in one renders the literal braces
 * next to a figure — visible nonsense on a billing page — and a key-set comparison
 * passes it happily.
 */
type Json = { [k: string]: string | Json }

function flatten(node: Json, prefix = ''): Map<string, string> {
  const out = new Map<string, string>()
  for (const [k, v] of Object.entries(node)) {
    const path = `${prefix}${k}`
    if (typeof v === 'string') out.set(path, v)
    else for (const [ik, iv] of flatten(v, `${path}.`)) out.set(ik, iv)
  }
  return out
}

/** `{{name}}` occurrences, as a sorted set: order and repetition are a translator's
 *  business, the NAMES are the contract with the calling code. */
function placeholders(value: string): string[] {
  return [...new Set(Array.from(value.matchAll(/\{\{\s*([\w.]+)/g), (m) => m[1]))].sort()
}

const flatEn = flatten(en as Json)
const flatJa = flatten(ja as Json)

describe('locale bundles agree', () => {
  it('has a non-trivial number of keys, so an empty read cannot pass', () => {
    // Without this, a bundle that failed to parse into anything (or an import that
    // silently resolved to `{}`) would make every comparison below vacuously true.
    expect(flatEn.size).toBeGreaterThan(100)
  })

  it('en and ja have exactly the same keys', () => {
    const missingInJa = [...flatEn.keys()].filter((k) => !flatJa.has(k)).sort()
    const missingInEn = [...flatJa.keys()].filter((k) => !flatEn.has(k)).sort()
    // Reported as lists rather than counts: the useful output of this failure is
    // which key to go and write.
    expect({ missingInJa, missingInEn }).toEqual({ missingInJa: [], missingInEn: [] })
  })

  it('every shared key uses the same interpolation placeholders', () => {
    const mismatched: Record<string, { en: string[]; ja: string[] }> = {}
    for (const [key, enValue] of flatEn) {
      const jaValue = flatJa.get(key)
      if (jaValue === undefined) continue // reported by the test above
      const a = placeholders(enValue)
      const b = placeholders(jaValue)
      if (a.join(',') !== b.join(',')) mismatched[key] = { en: a, ja: b }
    }
    expect(mismatched).toEqual({})
  })

  it('markup in a translation is a bare tag, never one carrying attributes', () => {
    // A tag in a translation is not automatically wrong: `<Trans>` uses one as a
    // PLACEHOLDER for a mapped React component, and
    // `admin_user_detail.api_keys.revoke_confirm_body` legitimately carries `<b>`
    // with `components={{ b: <strong /> }}`. That never reaches an HTML parser.
    //
    // What must not appear is a tag with ATTRIBUTES, because attributes are where
    // a destination or a handler lives — `<a href=...>` in a bundle puts the link
    // target under the translator's control rather than the code's, and
    // `interpolation: { escapeValue: false }` means the bundles are trusted input.
    // So this bans the dangerous shape and permits the one in use.
    const withAttributes = [
      ...new Set(
        [...flatEn, ...flatJa]
          .filter(([, v]) => /<[a-zA-Z][a-zA-Z0-9]*\s+[^>]*>/.test(v))
          .map(([k]) => k),
      ),
    ].sort()
    expect(withAttributes).toEqual([])
  })

  it('a key uses the same markup tags in both locales', () => {
    // A translator who adds a tag the calling code's `components` map does not
    // cover gets that tag rendered as literal text. The key-set and placeholder
    // checks above both pass such a string.
    const tags = (v: string) =>
      [...new Set(Array.from(v.matchAll(/<\/?([a-zA-Z][a-zA-Z0-9]*)/g), (m) => m[1]))].sort()
    const mismatched: Record<string, { en: string[]; ja: string[] }> = {}
    for (const [key, enValue] of flatEn) {
      const jaValue = flatJa.get(key)
      if (jaValue === undefined) continue
      const a = tags(enValue)
      const b = tags(jaValue)
      if (a.join(',') !== b.join(',')) mismatched[key] = { en: a, ja: b }
    }
    expect(mismatched).toEqual({})
  })
})
