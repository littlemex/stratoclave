import { Link } from 'react-router-dom'
import { ShieldAlert } from 'lucide-react'

import { Button } from '@/components/ui/button'

interface Props {
  title?: string
  description?: string
  homeHref?: string
}

export function AccessDenied({
  title = 'このページへのアクセス権がありません',
  description = 'ロールが不足している、またはこのテナントの所有者ではない可能性があります。',
  homeHref = '/',
}: Props) {
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
          access denied
        </p>
        <h1 className="font-display text-3xl font-semibold tracking-tight">
          {title}
        </h1>
        <p className="mx-auto max-w-md text-sm text-muted-foreground">
          {description}
        </p>
      </div>
      <Button asChild variant="outline">
        <Link to={homeHref}>ホームに戻る</Link>
      </Button>
    </div>
  )
}
