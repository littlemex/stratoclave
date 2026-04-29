// Vitest global setup: wires up jest-dom matchers so tests can use
// `expect(element).toBeInTheDocument()` and friends.
import '@testing-library/jest-dom/vitest'

// Initialise i18next once per test process so components that call
// `useTranslation()` or `<Trans>` can resolve keys without spamming
// "pass in an i18next instance" warnings. Tests that need a specific
// locale can call `i18n.changeLanguage('en')` themselves.
import '@/lib/i18n'
