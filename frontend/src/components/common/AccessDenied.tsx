import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ShieldAlert } from 'lucide-react'

import { Button } from '@/components/ui/button'

interface Props {
  title?: string
  description?: string
  homeHref?: string
}

export function AccessDenied({
  title,
  description,
  homeHref = '/',
}: Props) {
  const { t } = useTranslation()
  const resolvedTitle = title ?? t('access_denied.title')
  const resolvedDescription = description ?? t('access_denied.description')
  return (
    <div className="flex min-h-[70vh] flex-col items-center justify-center gap-6 px-6 text-center">
      <div className="grid h-12 w-12 place-items-center border border-border bg-muted/40">
        <ShieldAlert
          className="h-5 w-5 text-muted-foreground"
          aria-hidden
        />
      </div>
      <div className="space-y-2">
        <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-muted-foreground">
          {t('access_denied.eyebrow')}
        </p>
        <h1 className="font-display text-3xl font-semibold tracking-tight">
          {resolvedTitle}
        </h1>
        <p className="mx-auto max-w-md text-sm text-muted-foreground">
          {resolvedDescription}
        </p>
      </div>
      <Button asChild variant="outline">
        <Link to={homeHref}>{t('access_denied.home')}</Link>
      </Button>
    </div>
  )
}
