import { AlertTriangle, Clock, Network, ShieldX, X } from 'lucide-react'

import { useError, type ErrorType } from '@/contexts/ErrorContext'
import { cn } from '@/lib/utils'

function variantFor(type: ErrorType) {
  switch (type) {
    case 'server':
    case 'unauthorized':
    case 'forbidden':
    case 'parse':
      return 'border-destructive/60 bg-destructive/10 text-destructive-foreground'
    case 'rate_limit':
    case 'timeout':
      return 'border-accent/50 bg-accent/15 text-accent-foreground'
    case 'network':
      return 'border-primary/40 bg-primary/10 text-primary-foreground'
    default:
      return 'border-border bg-card text-foreground'
  }
}

function iconFor(type: ErrorType) {
  switch (type) {
    case 'unauthorized':
    case 'forbidden':
      return ShieldX
    case 'timeout':
    case 'rate_limit':
      return Clock
    case 'network':
      return Network
    default:
      return AlertTriangle
  }
}

function labelFor(type: ErrorType): string {
  switch (type) {
    case 'server': return 'サーバーエラー'
    case 'rate_limit': return 'レート制限'
    case 'unauthorized': return '認証切れ'
    case 'forbidden': return '権限なし'
    case 'timeout': return 'タイムアウト'
    case 'network': return 'ネットワーク'
    case 'parse': return '解析エラー'
    default: return 'エラー'
  }
}

export function ErrorToast() {
  const { errors, dismissError } = useError()

  if (errors.length === 0) return null

  return (
    <div
      className="pointer-events-none fixed right-6 top-6 z-[100] flex w-[min(420px,90vw)] flex-col gap-3"
      role="alert"
      aria-live="assertive"
    >
      {errors.map((error) => {
        const Icon = iconFor(error.type)
        return (
          <div
            key={error.id}
            data-testid="error-toast"
            className={cn(
              'pointer-events-auto flex items-start gap-3 border px-4 py-3 shadow-lg backdrop-blur-sm',
              variantFor(error.type),
            )}
          >
            <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold uppercase tracking-wide opacity-80">
                {labelFor(error.type)}
              </p>
              <p className="mt-0.5 break-words text-sm">{error.message}</p>
            </div>
            <button
              className="shrink-0 rounded-sm p-0.5 text-foreground/70 transition-colors hover:text-foreground"
              onClick={() => dismissError(error.id)}
              aria-label="通知を閉じる"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )
      })}
    </div>
  )
}

export default ErrorToast
