// Guards on `docs/index.html`, the bilingual landing page.
//
// The page is a single hand-written HTML file with a language toggle that swaps
// `textContent` from `data-ja`/`data-en` on every element carrying BOTH. Three
// things about it are silent when broken and mechanical to check, so they are
// checked here rather than left to whoever notices:
//
//   - An element with only one of the two attributes is skipped by the toggle
//     entirely, so it keeps rendering Japanese to an English reader with no
//     error anywhere.
//   - The step chips are a reading order. Inserting a section without
//     renumbering leaves two chips with the same number, which looks like a
//     mistake and is invisible to any other test.
//   - The repository forbids emoji in Markdown and code; this page is neither,
//     and the rule's reasons (searchability, terminal rendering) apply to it, so
//     the same bar is asserted rather than assumed.
//
// Loaded over `file://` from a path relative to this spec, because the page is a
// static artifact rather than something the dev server serves.
import { expect, test } from '@playwright/test'
import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'

// Playwright runs with the frontend package as its working directory.
const PAGE = pathToFileURL(resolve(process.cwd(), '..', 'docs', 'index.html')).href

test.describe('docs landing page', () => {
  test('every translatable element carries both languages', async ({ page }) => {
    await page.goto(PAGE)

    const lonely = await page.evaluate(() => {
      const out: string[] = []
      for (const el of Array.from(document.querySelectorAll('[data-ja], [data-en]'))) {
        const ja = el.hasAttribute('data-ja')
        const en = el.hasAttribute('data-en')
        if (ja !== en) {
          out.push(
            `<${el.tagName.toLowerCase()}> has only data-${ja ? 'ja' : 'en'}: ` +
              `${(el.textContent ?? '').trim().slice(0, 60)}`,
          )
        }
      }
      return out
    })

    expect(
      lonely,
      'the toggle only touches elements with BOTH attributes, so these would ' +
        'keep rendering one language to a reader who asked for the other',
    ).toEqual([])
  })

  test('the step chips are a gapless reading order', async ({ page }) => {
    await page.goto(PAGE)
    const chips = await page.locator('.step-chip').allTextContents()
    const numbers = chips.map((c) => Number(c.trim()))
    expect(
      numbers,
      'inserting or removing a section without renumbering leaves a gap or a duplicate',
    ).toEqual(numbers.map((_, i) => i + 1))
  })

  test('no emoji, the same bar the repository holds elsewhere', async ({ page }) => {
    await page.goto(PAGE)
    const found = await page.evaluate(() => {
      // Pictographs and emoji presentation, not arrows or box drawing: the page
      // uses typographic characters deliberately and those are not the target.
      // U+FE0F is a variation selector, a COMBINING mark: putting it in the same
      // class as the pictograph ranges makes the class match half of a
      // two-codepoint grapheme, which is why the linter refuses it. It belongs
      // as its own alternative, optional after a pictograph.
      const re = /[\u{1F300}-\u{1FAFF}\u{1F004}-\u{1F0CF}\u{2700}-\u{27BF}]\u{FE0F}?|\u{FE0F}/gu
      const hits = new Set<string>()
      const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
      let n = walk.nextNode()
      while (n) {
        for (const m of (n.textContent ?? '').matchAll(re)) hits.add(m[0])
        n = walk.nextNode()
      }
      return Array.from(hits)
    })
    expect(found).toEqual([])
  })

  test('the catalogue demo runs, and the language toggle follows its generated text', async ({
    page,
  }) => {
    await page.goto(PAGE)
    const sec = page.locator('#catalogue')
    await expect(sec).toBeVisible()

    // Nothing is claimed before the demo runs.
    await expect(sec.locator('#catFilesReadout')).toHaveText('')

    await sec.locator('#catNewModelBtn').click()

    // The fast lane reaches serving; the slow lane is still short of it. That gap
    // IS the claim, so it is asserted rather than left to the eye.
    await expect(sec.locator('#catFastNote')).toContainText('提供中', { timeout: 5000 })
    await expect(sec.locator('#catFilesReadout')).toContainText('0')
    const left = (sel: string) =>
      sec.locator(sel).evaluate((el) => parseFloat((el as HTMLElement).style.left))
    expect(await left('#catPuckFast')).toBeGreaterThan(await left('#catPuckSlow'))

    // A price cut reaches the billed amount, and the amount is computed rather
    // than a string someone typed. The DIRECTION is asserted, not just the change:
    // provider prices come down far more often than up, and a demo that showed a
    // rise would be showing the rarer case.
    const money = (s: string | null) => Number((s ?? '').replace(/[^0-9.]/g, ''))
    const billedBefore = money(await sec.locator('#catBillCell').textContent())
    await sec.locator('#catPriceBtn').click()
    await expect(sec.locator('#catRateCell')).toHaveText('$2.40')
    const billedAfter = money(await sec.locator('#catBillCell').textContent())
    expect(billedAfter).toBeLessThan(billedBefore)

    // The recorded breakdown must NOT follow the cut -- that is the whole point of
    // the row beside it.
    await expect(sec.locator('#catLedgerBody')).toContainText('$3.00')

    // The ledger breakdown names the items in plain words, not internal jargon.
    const ledger = sec.locator('#catLedgerBody')
    await expect(ledger).toContainText('入力')
    await expect(ledger).toContainText('キャッシュ読み込み')
    await expect(sec).not.toContainText('脚')

    // Switching language must rewrite the text the script generated, not only the
    // markup the toggle walks -- the failure mode a JS-driven section adds.
    await page.locator('#btn-en').click()
    await expect(ledger).toContainText('cache read')
    await expect(sec.locator('#catLedgerNote')).toContainText('recomputed at the new rate')
    await expect(ledger).not.toContainText('キャッシュ')

    await page.locator('#btn-ja').click()
    await expect(ledger).toContainText('キャッシュ読み込み')
  })

  test('the section stays short: the demo carries it, not the prose', async ({ page }) => {
    // A guard on the thing that actually went wrong the first time round. The
    // section was rewritten because it argued in paragraphs; this keeps it from
    // growing back one sentence at a time.
    await page.goto(PAGE)
    const chars = await page.locator('#catalogue').evaluate((sec) => {
      let n = 0
      for (const el of Array.from(sec.querySelectorAll('[data-ja]'))) {
        // Only prose: headings, intros and labels. Demo readouts are generated.
        if (el.closest('.demo-shell')) continue
        n += (el.getAttribute('data-ja') ?? '').length
      }
      return n
    })
    expect(chars, 'the prose outside the demo has grown past a screen').toBeLessThan(400)
  })
})
