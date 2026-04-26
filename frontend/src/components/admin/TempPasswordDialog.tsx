/**
 * Temp password dialog
 *
 * ユーザー作成成功時に一時パスワードを 1 回だけ表示する。
 * - コピー必須: コピーボタンを押すまで「閉じる」ボタンは無効化
 * - 「閉じたら二度と表示されません」を強調
 * - Esc / overlay click での閉じる操作を `onOpenChange` で制御 (強制コピー)
 */

import { useEffect, useState } from 'react'
import { Check, Copy, ShieldAlert } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

interface Props {
  open: boolean
  email: string
  temporaryPassword: string
  onAcknowledge: () => void
}

export function TempPasswordDialog({
  open,
  email,
  temporaryPassword,
  onAcknowledge,
}: Props) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (open) setCopied(false)
  }, [open])

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(temporaryPassword)
    } catch {
      // clipboard 非対応: 最低限コピーしたことを手動で要求
      const ta = document.createElement('textarea')
      ta.value = temporaryPassword
      document.body.appendChild(ta)
      ta.select()
      try {
        document.execCommand('copy')
      } catch {
        // noop
      }
      ta.remove()
    }
    setCopied(true)
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // コピー済みでなければ閉じさせない
        if (!next && !copied) return
        if (!next) onAcknowledge()
      }}
    >
      <DialogContent
        onInteractOutside={(e) => {
          if (!copied) e.preventDefault()
        }}
        onEscapeKeyDown={(e) => {
          if (!copied) e.preventDefault()
        }}
        className="max-w-md"
      >
        <DialogHeader>
          <div className="mb-1 flex items-center gap-2 text-accent">
            <ShieldAlert className="h-4 w-4" aria-hidden />
            <span className="text-xs font-semibold uppercase tracking-wide">
              一度だけ表示されます
            </span>
          </div>
          <DialogTitle>一時パスワードを本人に安全に渡してください</DialogTitle>
          <DialogDescription>
            このダイアログを閉じると再表示できません。本人に手渡したあと、必ず
            新パスワードの設定を促してください。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div>
            <Label>Email</Label>
            <div className="mt-1 font-mono text-sm">{email}</div>
          </div>
          <div>
            <Label htmlFor="temp-password">一時パスワード</Label>
            <div className="mt-1 flex gap-2">
              <Input
                id="temp-password"
                readOnly
                value={temporaryPassword}
                className="font-mono"
                onFocus={(e) => e.currentTarget.select()}
              />
              <Button variant={copied ? 'secondary' : 'default'} onClick={handleCopy}>
                {copied ? (
                  <>
                    <Check className="h-4 w-4" aria-hidden />
                    コピー済み
                  </>
                ) : (
                  <>
                    <Copy className="h-4 w-4" aria-hidden />
                    コピー
                  </>
                )}
              </Button>
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            初回ログイン時に新パスワードの設定が必要です (Cognito NEW_PASSWORD_REQUIRED challenge)。
          </p>
        </div>

        <DialogFooter>
          <Button disabled={!copied} onClick={onAcknowledge}>
            コピーを確認して閉じる
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
