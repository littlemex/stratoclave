/**
 * Temp password dialog
 *
 * Shows the Cognito temporary password exactly once after a successful
 * user creation. The UI forces the operator to copy the value before the
 * dialog can be dismissed — overlay click and Escape are intercepted to
 * prevent accidental loss of the credential.
 */

import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
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
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (open) setCopied(false)
  }, [open])

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(temporaryPassword)
    } catch {
      // Clipboard API unavailable: fall back to execCommand('copy').
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
              {t('temp_password.banner')}
            </span>
          </div>
          <DialogTitle>{t('temp_password.title')}</DialogTitle>
          <DialogDescription>{t('temp_password.desc')}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div>
            <Label>{t('temp_password.email_label')}</Label>
            <div className="mt-1 font-mono text-sm">{email}</div>
          </div>
          <div>
            <Label htmlFor="temp-password">{t('temp_password.password_label')}</Label>
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
                    {t('temp_password.copied')}
                  </>
                ) : (
                  <>
                    <Copy className="h-4 w-4" aria-hidden />
                    {t('temp_password.copy')}
                  </>
                )}
              </Button>
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            {t('temp_password.policy_note')}
          </p>
        </div>

        <DialogFooter>
          <Button disabled={!copied} onClick={onAcknowledge}>
            {t('temp_password.acknowledge')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
