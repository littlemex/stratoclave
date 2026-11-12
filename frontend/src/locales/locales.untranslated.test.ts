import { describe, expect, it } from 'vitest'

import en from './en.json'
import ja from './ja.json'

/**
 * A Japanese string has Japanese in it.
 *
 * `locales.parity.test.ts` checks that the two bundles agree in shape (keys,
 * placeholders, markup). It never reads a value, so an English value left in
 * ja.json passed it, and the Japanese console showed "Allowed Role Patterns",
 * "Default Credit" and "Administrator". These checks read the content.
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

const flatEn = flatten(en as Json)
const flatJa = flatten(ja as Json)

describe('the Japanese bundle is Japanese', () => {
  // Values in ja.json that stay as they are, each for a stated reason. Listing them
  // by key makes leaving one more in English a decision someone writes down.
  const KEPT_AS_IS: Record<string, string> = {
    'app.name': 'product name',
    'login.title': 'product name',
    'app.footer.brand': 'brand lockup (name and tagline), shown in English in both locales',
    'nav.language_en': 'a language shown in its own name',
    'locale.en': 'a language shown in its own name',
    'me_api_keys.default_user_role': 'role identifier',
    'admin_trusted_account_detail.invite_tenant_default': 'tenant identifier',
    'admin_discovery_candidate_new.wire_protocol_messages': 'API name',
    'admin_discovery_candidate_new.wire_protocol_responses': 'API name',
    'admin_trusted_accounts.desc_placeholder': 'example input',
    'admin_trusted_accounts.roles_placeholder': 'example input',
    'admin_tenants.create_name_placeholder': 'example input',
    'team_lead_tenant_new.name_placeholder': 'example input',
    'me_api_keys.create_name_placeholder': 'example input',
    'me_api_keys.create_scopes_placeholder': 'example input',
  }
  const japanese = /[\u3040-\u30ff\u4e00-\u9fff]/
  // Placeholders, nested references, parenthetical notes and identifier-shaped tokens
  // are not the label, so "Default Tenant (任意)" is judged by "Default Tenant" and
  // "default_credit (任意)" by nothing at all.
  const visible = (v: string) =>
    v
      .replace(/\{\{[^}]*\}\}/g, '')
      .replace(/\$t\([^)]*\)/g, '')
      // Only a note written in Japanese is set aside; an English one is judged.
      .replace(/\s*[(（][^)）]*[)）]/g, (m) => (japanese.test(m) ? '' : m))
      // snake_case tokens (default_credit) are identifiers, not prose.
      .replace(/\b[a-z0-9]+(?:_[a-z0-9]+)+\b/g, '')
      // Hyphenated identifiers the console shows as such (tenant ids and an owner value).
      .replace(/\b(?:default-org|admin-owned)\b/g, '')
  const isEnglish = (v: string) => !japanese.test(visible(v)) && /[A-Za-z]{3,}/.test(visible(v))

  it('a Japanese string has Japanese in it, unless it is kept as is for a stated reason', () => {
    // i18next renders whatever the ja bundle holds, so an English value left there is
    // as silent as a missing key: the trusted-account screens showed "Default
    // Credit" and "IAM User" to a Japanese reader, and the key-set check above passed.
    // Sentences that mix Japanese with English terms ("IAM user を許可") are left to
    // review; only values that are English once their notes are set aside are judged.
    const english = [...flatJa]
      .filter(([k, v]) => !Object.prototype.hasOwnProperty.call(KEPT_AS_IS, k) && isEnglish(v))
      .map(([k, v]) => `${k}: ${v}`)
    expect(english).toEqual([])
  })

  it('every key kept as is exists and would be flagged without its entry', () => {
    const stale = Object.keys(KEPT_AS_IS).filter((k) => !isEnglish(flatJa.get(k) ?? ''))
    expect(stale).toEqual([])
    // "Kept as is" means the same text in both locales; a ja value that differs from
    // en is a stale copy, not a decision.
    const differs = Object.keys(KEPT_AS_IS).filter((k) => flatJa.get(k) !== flatEn.get(k))
    expect(differs).toEqual([])
  })

  it.each([
    ['Default Tenant (任意)', true],
    ['Revoke (CLI からも可)', true],
    ['(optional)', true],
    ['default_credit (任意)', false],
    ['tenant_id · {{id}}', false],
    ['IAM user を許可', false],
    ['既定のテナント (任意)', false],
  ])('the English check reads %s as English: %s', (value, english) => {
    expect(isEnglish(value)).toBe(english)
  })
})
