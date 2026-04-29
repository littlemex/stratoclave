import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { LoadingScreen } from '@/components/common/LoadingScreen'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/contexts/AuthContext'
import { handleCallback } from '@/lib/cognito'

export default function Callback() {
  const navigate = useNavigate()
  const { reloadUser } = useAuth()
  const { t } = useTranslation()
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      try {
        await handleCallback()
        await reloadUser()
        if (!cancelled) navigate('/', { replace: true })
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err))
        }
      }
    }
    void run()
    return () => {
      cancelled = true
    }
    // reloadUser は安定参照 (useCallback)、navigate も react-router 由来
  }, [navigate, reloadUser])

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center px-6">
        <div className="w-full max-w-md border border-destructive/40 bg-card p-8 shadow-sm">
          <h1 className="font-display text-2xl tracking-tight text-destructive">
            {t('callback.failed_title')}
          </h1>
          <p className="mt-3 text-sm text-muted-foreground">{error}</p>
          <Button
            className="mt-6"
            onClick={() => navigate('/', { replace: true })}
          >
            {t('callback.back_home')}
          </Button>
        </div>
      </div>
    )
  }

  return <LoadingScreen message={t('callback.processing')} />
}
