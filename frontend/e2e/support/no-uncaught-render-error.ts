// A `test` that fails when the page under test threw an uncaught error.
//
// Written because three separate fixture defects in the discovery specs each
// produced a WHITE SCREEN, and not one of them failed as "the page crashed".
// They failed as "waiting for element to be visible", "waiting for
// getByLabel(/invocation/i)" and a timed-out `waitForRequest` -- three
// different-looking symptoms of one cause, each of which reads like a missing
// affordance or a broken button. Two of the three sent me looking at the
// component that was fine.
//
// The cause each time was a mocked response missing a field the real response
// model declares required, so a screen did `x.identifiers.map(...)` on
// `undefined`. Checking fixtures against the models would need the TypeScript
// literals parsed and kept in step; asserting on the CONSEQUENCE needs one
// hook and cannot drift, because it does not encode any shape at all.
//
// MEASURED, because the first version of this comment claimed more than the
// check delivers: with this assertion removed and a required field deleted from
// a fixture, the specs still fail (2 of 5). So the crash was never UNDETECTED
// -- it was misattributed, and one of those failures took a 30-second timeout
// to arrive. The value here is naming the cause on the first read instead of
// three different misleading symptoms, not finding something otherwise missed.
//
// It does reach one case the fixtures cannot: a real response, from a real
// gateway, that crashes a screen.
import { test as base, expect } from '@playwright/test'

export const test = base.extend<Record<string, never>>({
  page: async ({ page }, use) => {
    const uncaught: string[] = []
    page.on('pageerror', (error) => uncaught.push(error.message))

    await use(page)

    // Reported after the body has run, so the test's own assertion failure --
    // usually the more specific message -- is what the reader sees first, and
    // this only speaks when nothing else did.
    expect(
      uncaught,
      'the page threw an uncaught error; a screen that crashes renders nothing, ' +
        'so every later assertion in this test failed for the wrong reason',
    ).toEqual([])
  },
})

export { expect }
