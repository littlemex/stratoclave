import { Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

export function LoadingScreen({ message }: { message?: string }) {
  const { t } = useTranslation()
  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="flex flex-col items-center gap-4 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin text-primary" aria-hidden />
        <p className="font-mono text-[11px] uppercase tracking-[0.16em]">
          {message ?? t('common.loading')}
        </p>
      </div>
    </div>
  )
}
