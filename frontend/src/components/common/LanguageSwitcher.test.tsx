// LanguageSwitcher behavior tests.
//
// Verifies:
//   1. The switcher renders the target locale's endonym (English / 日本語).
//   2. aria-pressed marks the active locale.
//   3. Clicking a non-active locale flips i18next + calls `setLocale`.
//
// Auth state is stubbed through a mock of `useAuth` rather than by
// mounting AuthProvider, because the switcher's contract is narrow:
// "give me the current locale and a setter". Wiring through
// AuthContext + AuthProvider would force us to stand up the whole
// auth/cognito/api mock surface that the AuthContext test already
// covers.

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const setLocaleMock = vi.fn()

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    state: {
      status: 'authenticated',
      user: {
        user_id: 'u1',
        email: 'alice@example.com',
        org_id: 'default-org',
        roles: ['user'],
        locale: 'ja',
      },
      tokens: null,
      error: null,
    },
    setLocale: setLocaleMock,
  }),
}))

import { LanguageSwitcher } from './LanguageSwitcher'
import i18n from '@/lib/i18n'

beforeEach(async () => {
  setLocaleMock.mockReset()
  await i18n.changeLanguage('ja')
})

afterEach(async () => {
  await i18n.changeLanguage('ja')
})

describe('LanguageSwitcher', () => {
  it('renders both locales by their endonym', () => {
    render(<LanguageSwitcher />)
    expect(screen.getByRole('button', { name: 'English' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '日本語' })).toBeInTheDocument()
  })

  it('marks the active locale with aria-pressed=true', () => {
    render(<LanguageSwitcher />)
    const ja = screen.getByRole('button', { name: '日本語' })
    const en = screen.getByRole('button', { name: 'English' })
    expect(ja).toHaveAttribute('aria-pressed', 'true')
    expect(en).toHaveAttribute('aria-pressed', 'false')
  })

  it('calls setLocale when the user picks a different locale', () => {
    render(<LanguageSwitcher />)
    fireEvent.click(screen.getByRole('button', { name: 'English' }))
    expect(setLocaleMock).toHaveBeenCalledWith('en')
  })

  it('does not call setLocale when clicking the already-active locale', () => {
    render(<LanguageSwitcher />)
    fireEvent.click(screen.getByRole('button', { name: '日本語' }))
    expect(setLocaleMock).not.toHaveBeenCalled()
  })
})
