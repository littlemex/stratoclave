import { useTranslation } from 'react-i18next'

import { useAuth } from '@/contexts/AuthContext'
import { SUPPORTED_LOCALES, type Locale } from '@/lib/i18n'
import { cn } from '@/lib/utils'

/**
 * Compact segmented EN / JA toggle.
 *
 * Design notes:
 * - Optimistic: `setLocale` flips `i18next` immediately and the UI
 *   re-renders. The PATCH /me network round-trip is fire-and-forget; a
 *   failure keeps the local state so the user is not stuck in a
 *   half-switched UI.
 * - `aria-pressed` toggles let screen readers announce the active
 *   language clearly without a hidden `<select>` dance.
 * - Label uses the *target* locale's own endonym ("日本語", "English")
 *   so a user who lands on the wrong language can still recognise and
 *   correct it — this is a usability requirement for bilingual SaaS.
 */
export function LanguageSwitcher({ className }: { className?: string }) {
  const { t, i18n } = useTranslation()
  const { state, setLocale } = useAuth()
  const active = (state.user?.locale ?? i18n.resolvedLanguage ?? 'ja') as Locale

  return (
    <div
      role="group"
      aria-label={t('nav.language_switch_aria')}
      className={cn(
        'inline-flex items-center overflow-hidden border border-border/60 bg-background/40',
        className,
      )}
    >
      {SUPPORTED_LOCALES.map((lng) => {
        const isActive = active === lng
        return (
          <button
            key={lng}
            type="button"
            aria-pressed={isActive}
            onClick={() => {
              if (!isActive) void setLocale(lng)
            }}
            className={cn(
              'px-2 py-1 text-[11px] font-medium tracking-wide transition-colors',
              isActive
                ? 'bg-primary/15 text-foreground'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {t(`nav.language_${lng}`)}
          </button>
        )
      })}
    </div>
  )
}
