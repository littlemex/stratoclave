import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import { Check, Copy, KeyRound, Plus, ShieldAlert, Trash2 } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  api,
  type ApiKeySummary,
  type CreateApiKeyResponse,
} from '@/lib/api'
import { cn } from '@/lib/utils'
import { usePermissions } from '@/hooks/usePermissions'

interface ExpiryOption {
  labelKey: string
  days: number | null
}

interface ScopePreset {
  labelKey: string
  scopes: string[]
}

const EXPIRY_OPTIONS: ExpiryOption[] = [
  { labelKey: 'me_api_keys.expiry_7d', days: 7 },
  { labelKey: 'me_api_keys.expiry_30d', days: 30 },
  { labelKey: 'me_api_keys.expiry_90d', days: 90 },
  { labelKey: 'me_api_keys.expiry_180d', days: 180 },
  { labelKey: 'me_api_keys.expiry_365d', days: 365 },
  { labelKey: 'me_api_keys.expiry_unlimited', days: null },
]

const DEFAULT_SCOPE_PRESETS: ScopePreset[] = [
  {
    labelKey: 'me_api_keys.preset_default',
    scopes: ['messages:send', 'usage:read-self'],
  },
  {
    labelKey: 'me_api_keys.preset_messages_only',
    scopes: ['messages:send'],
  },
]

async function sha256Hex(text: string): Promise<string> {
  const buf = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(text),
  )
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

function formatDate(v: string | null | undefined, fallback = '—'): string {
  if (!v) return fallback
  try {
    return new Date(v).toLocaleString()
  } catch {
    return v
  }
}

export default function MeApiKeys() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [createdKey, setCreatedKey] = useState<CreateApiKeyResponse | null>(
    null,
  )

  const listQuery = useQuery({
    queryKey: ['me', 'api-keys'],
    queryFn: () => api.apiKeys.list(),
  })

  const keys = listQuery.data?.keys ?? []

  return (
    <div className="space-y-10">
      <section className="space-y-3">
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          {t('me_api_keys.label')}
        </p>
        <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
          <div>
            <h1 className="font-display text-3xl tracking-tight">
              {t('me_api_keys.title')}
            </h1>
            <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
              <Trans
                i18nKey="me_api_keys.intro_lead"
                components={{
                  1: <code className="font-mono text-foreground/80" />,
                }}
              />
            </p>
          </div>
          <Button onClick={() => setCreateOpen(true)} disabled={listQuery.isLoading}>
            <Plus className="h-4 w-4" aria-hidden />
            {t('me_api_keys.new_button')}
          </Button>
        </div>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              {t('me_api_keys.stat_active')}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-baseline gap-2">
              <span className="strato-stat font-display text-3xl font-semibold tracking-tight">
                {listQuery.data?.active_count ?? '—'}
              </span>
              <span className="text-xs text-muted-foreground">
                {t('me_api_keys.stat_active_unit', {
                  max: listQuery.data?.max_per_user ?? 5,
                })}
              </span>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              {t('me_api_keys.stat_storage')}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-foreground">
              {t('me_api_keys.stat_storage_value')}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {t('me_api_keys.stat_storage_desc')}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              {t('me_api_keys.stat_blast')}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-foreground">
              {t('me_api_keys.stat_blast_value')}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {t('me_api_keys.stat_blast_desc')}
            </p>
          </CardContent>
        </Card>
      </section>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            {t('me_api_keys.list_title')}
          </CardTitle>
          <CardDescription>
            {t('me_api_keys.list_desc')}
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {listQuery.isLoading ? (
            <p className="px-6 py-6 text-sm text-muted-foreground">
              {t('me_api_keys.loading')}
            </p>
          ) : keys.length === 0 ? (
            <EmptyState onCreate={() => setCreateOpen(true)} />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('me_api_keys.col_key_id')}</TableHead>
                  <TableHead>{t('me_api_keys.col_name')}</TableHead>
                  <TableHead>{t('me_api_keys.col_scopes')}</TableHead>
                  <TableHead>{t('me_api_keys.col_lifecycle')}</TableHead>
                  <TableHead>{t('me_api_keys.col_last_used')}</TableHead>
                  <TableHead className="text-right" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {keys.map((k) => (
                  <KeyRow
                    key={k.key_id}
                    item={k}
                    onRevoked={() => {
                      void qc.invalidateQueries({ queryKey: ['me', 'api-keys'] })
                    }}
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <CreateDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={(resp) => {
          setCreatedKey(resp)
          void qc.invalidateQueries({ queryKey: ['me', 'api-keys'] })
        }}
      />
      <CreatedKeyDialog
        resp={createdKey}
        onClose={() => setCreatedKey(null)}
      />
    </div>
  )
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-12 text-center text-sm text-muted-foreground">
      <KeyRound className="h-6 w-6 text-muted-foreground/70" aria-hidden />
      <p>{t('me_api_keys.empty_message')}</p>
      <Button size="sm" onClick={onCreate}>
        <Plus className="h-4 w-4" aria-hidden />
        {t('me_api_keys.empty_cta')}
      </Button>
    </div>
  )
}

function KeyRow({
  item,
  onRevoked,
}: {
  item: ApiKeySummary
  onRevoked: () => void
}) {
  const { t } = useTranslation()
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const revokeMutation = useMutation({
    mutationFn: async () => {
      // The list API exposes key_id only, not key_hash. Revocation
      // currently requires the hash, so UI-initiated revokes are not
      // wired up yet and we surface a CLI hint instead.
      throw new Error(t('me_api_keys.revoke_unsupported_error'))
    },
    onError: (err: unknown) => {
      const e = err as { message?: string } | null
      setError(e?.message ?? t('me_api_keys.revoke_error_fallback'))
    },
    onSuccess: () => {
      setConfirmOpen(false)
      setError(null)
      onRevoked()
    },
  })

  const isRevoked = !!item.revoked_at
  const isExpired =
    item.expires_at && new Date(item.expires_at).getTime() < Date.now()
  const badge = isRevoked
    ? { variant: 'muted' as const, labelKey: 'me_api_keys.badge_revoked' }
    : isExpired
      ? { variant: 'muted' as const, labelKey: 'me_api_keys.badge_expired' }
      : { variant: 'secondary' as const, labelKey: 'me_api_keys.badge_active' }

  return (
    <>
      <TableRow>
        <TableCell>
          <div className="flex items-center gap-2">
            <code className="font-mono text-xs">{item.key_id}</code>
            <Badge variant={badge.variant}>{t(badge.labelKey)}</Badge>
          </div>
        </TableCell>
        <TableCell className="max-w-[200px] truncate text-sm">
          {item.name || '—'}
        </TableCell>
        <TableCell className="space-x-1">
          {item.scopes.map((s) => (
            <Badge key={s} variant="outline" className="font-mono text-[10px]">
              {s}
            </Badge>
          ))}
        </TableCell>
        <TableCell>
          <div className="text-xs text-muted-foreground">
            {t('me_api_keys.created_prefix', { when: formatDate(item.created_at) })}
          </div>
          <div className="text-xs text-muted-foreground">
            {isRevoked
              ? t('me_api_keys.revoked_prefix', { when: formatDate(item.revoked_at) })
              : t('me_api_keys.expires_prefix', {
                  when: formatDate(item.expires_at, t('me_api_keys.never_expires')),
                })}
          </div>
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">
          {formatDate(item.last_used_at, t('me_api_keys.never_used'))}
        </TableCell>
        <TableCell className="text-right">
          {!isRevoked ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setConfirmOpen(true)}
              title={t('me_api_keys.revoke_tooltip')}
            >
              <Trash2 className="h-4 w-4" aria-hidden />
            </Button>
          ) : null}
        </TableCell>
      </TableRow>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('me_api_keys.revoke_title')}</DialogTitle>
            <DialogDescription>
              <Trans
                i18nKey="me_api_keys.revoke_desc"
                values={{ id: item.key_id }}
                components={{ 1: <strong /> }}
              />
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-border bg-muted/40 p-3 text-xs text-muted-foreground">
            {t('me_api_keys.revoke_ui_unsupported')}
            <pre className="mt-2 overflow-x-auto rounded-sm bg-background/60 p-2 font-mono text-[11px]">
{`stratoclave api-key revoke <key_hash>`}
            </pre>
            <p className="mt-2">
              <Trans
                i18nKey="me_api_keys.revoke_hash_hint"
                components={{ 1: <code className="font-mono" /> }}
              />
            </p>
          </div>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
              {t('me_api_keys.revoke_close')}
            </Button>
            <Button
              variant="destructive"
              disabled={revokeMutation.isPending}
              onClick={() => revokeMutation.mutate()}
            >
              {t('me_api_keys.revoke_ui_button')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

function CreateDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onCreated: (resp: CreateApiKeyResponse) => void
}) {
  const { t } = useTranslation()
  const perms = usePermissions()
  const [name, setName] = useState('')
  const [preset, setPreset] = useState<number>(0)
  const [customScopes, setCustomScopes] = useState('')
  const [expires, setExpires] = useState<number | null>(30)
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: async () => {
      let scopes: string[] = DEFAULT_SCOPE_PRESETS[preset].scopes
      if (customScopes.trim()) {
        scopes = customScopes
          .split(/[,\s]+/)
          .map((s) => s.trim())
          .filter(Boolean)
      }
      return api.apiKeys.create({
        name: name.trim() || undefined,
        scopes,
        expires_in_days: expires === null ? 0 : expires,
      })
    },
    onSuccess: (resp) => {
      onCreated(resp)
      onOpenChange(false)
      setName('')
      setPreset(0)
      setCustomScopes('')
      setExpires(30)
      setError(null)
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? t('me_api_keys.create_error_fallback'))
    },
  })

  const invalidScopes = useMemo(() => {
    if (!customScopes.trim()) return [] as string[]
    const sc = customScopes.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean)
    return sc.filter((s) => !s.includes(':'))
  }, [customScopes])

  const roles = perms.roles.join(', ') || t('me_api_keys.default_user_role')

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) setError(null)
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('me_api_keys.create_title')}</DialogTitle>
          <DialogDescription>
            {t('me_api_keys.create_desc', { roles })}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="ak-name">{t('me_api_keys.create_name_label')}</Label>
            <Input
              id="ak-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('me_api_keys.create_name_placeholder')}
              maxLength={64}
            />
          </div>

          <div className="space-y-1.5">
            <Label>{t('me_api_keys.create_preset_label')}</Label>
            <div className="grid gap-2">
              {DEFAULT_SCOPE_PRESETS.map((p, idx) => (
                <button
                  key={p.labelKey}
                  type="button"
                  onClick={() => setPreset(idx)}
                  className={cn(
                    'flex items-start gap-3 rounded-md border p-3 text-left transition-colors',
                    preset === idx
                      ? 'border-primary bg-primary/10'
                      : 'border-border hover:border-primary/40',
                  )}
                >
                  <div className="flex-1">
                    <div className="text-sm font-medium">{t(p.labelKey)}</div>
                    <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">
                      {p.scopes.join('  ·  ')}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="ak-scopes">
              {t('me_api_keys.create_scopes_custom_label')}
            </Label>
            <Input
              id="ak-scopes"
              value={customScopes}
              onChange={(e) => setCustomScopes(e.target.value)}
              placeholder={t('me_api_keys.create_scopes_placeholder')}
            />
            <p className="text-[11px] text-muted-foreground">
              <Trans
                i18nKey="me_api_keys.create_scopes_help"
                components={{ 1: <code className="font-mono" /> }}
              />
            </p>
            {invalidScopes.length > 0 ? (
              <p className="text-[11px] text-destructive">
                {t('me_api_keys.create_scopes_invalid', {
                  bad: invalidScopes.join(', '),
                })}
              </p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label>{t('me_api_keys.create_expires_label')}</Label>
            <div className="flex flex-wrap gap-2">
              {EXPIRY_OPTIONS.map((o) => (
                <Button
                  key={o.labelKey}
                  type="button"
                  size="sm"
                  variant={expires === o.days ? 'default' : 'outline'}
                  onClick={() => setExpires(o.days)}
                >
                  {t(o.labelKey)}
                </Button>
              ))}
            </div>
            <p className="text-[11px] text-muted-foreground">
              {t('me_api_keys.create_expires_help')}
            </p>
          </div>

          <div className="flex items-start gap-2 rounded-md border border-accent/40 bg-accent/10 p-3 text-xs text-accent-foreground">
            <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            <p>
              <Trans
                i18nKey="me_api_keys.create_warning"
                components={{ 1: <strong /> }}
              />
            </p>
          </div>
        </div>

        {error ? <p className="text-sm text-destructive">{error}</p> : null}

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button
            disabled={mutation.isPending || invalidScopes.length > 0}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending
              ? t('me_api_keys.create_submitting')
              : t('me_api_keys.create_submit')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function CreatedKeyDialog({
  resp,
  onClose,
}: {
  resp: CreateApiKeyResponse | null
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  const [hashCopied, setHashCopied] = useState(false)
  const [hash, setHash] = useState('')

  useMemo(() => {
    if (!resp) return
    void sha256Hex(resp.plaintext_key).then(setHash)
  }, [resp])

  if (!resp) return null

  const copy = async (val: string, which: 'plain' | 'hash') => {
    try {
      await navigator.clipboard.writeText(val)
    } catch {
      // ignore
    }
    if (which === 'plain') {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } else {
      setHashCopied(true)
      window.setTimeout(() => setHashCopied(false), 2000)
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(v) => {
        if (!v) {
          setCopied(false)
          onClose()
        }
      }}
    >
      <DialogContent className="max-w-lg" onEscapeKeyDown={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>{t('me_api_keys.created_title')}</DialogTitle>
          <DialogDescription>
            <Trans
              i18nKey="me_api_keys.created_desc"
              components={{ 1: <strong /> }}
            />
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label>{t('me_api_keys.created_key_id')}</Label>
            <code className="block rounded-sm bg-muted px-3 py-2 font-mono text-xs">
              {resp.key_id}
            </code>
          </div>
          <div className="space-y-1">
            <Label>{t('me_api_keys.created_plaintext_label')}</Label>
            <div className="flex gap-2">
              <code className="flex-1 overflow-x-auto rounded-sm border border-primary/40 bg-primary/5 px-3 py-2 font-mono text-xs">
                {resp.plaintext_key}
              </code>
              <Button
                size="sm"
                variant={copied ? 'secondary' : 'outline'}
                onClick={() => copy(resp.plaintext_key, 'plain')}
              >
                {copied ? (
                  <Check className="h-4 w-4" aria-hidden />
                ) : (
                  <Copy className="h-4 w-4" aria-hidden />
                )}
                {copied
                  ? t('me_api_keys.created_copied')
                  : t('me_api_keys.created_copy')}
              </Button>
            </div>
          </div>
          <div className="space-y-1">
            <Label>{t('me_api_keys.created_hash_label')}</Label>
            <div className="flex gap-2">
              <code className="flex-1 overflow-x-auto rounded-sm bg-muted px-3 py-2 font-mono text-[11px] text-muted-foreground">
                {hash || t('me_api_keys.created_hash_computing')}
              </code>
              <Button
                size="sm"
                variant={hashCopied ? 'secondary' : 'outline'}
                disabled={!hash}
                onClick={() => copy(hash, 'hash')}
              >
                {hashCopied ? (
                  <Check className="h-4 w-4" aria-hidden />
                ) : (
                  <Copy className="h-4 w-4" aria-hidden />
                )}
              </Button>
            </div>
            <p className="text-[11px] text-muted-foreground">
              <Trans
                i18nKey="me_api_keys.created_hash_hint"
                components={{ 1: <code className="font-mono" /> }}
              />
            </p>
          </div>
          <div className="space-y-1 text-xs text-muted-foreground">
            <div>
              {t('me_api_keys.created_scopes', { scopes: resp.scopes.join(', ') })}
            </div>
            <div>
              {t('me_api_keys.created_expires', {
                when: formatDate(resp.expires_at, t('me_api_keys.never_expires')),
              })}
            </div>
            <div>
              {t('me_api_keys.created_created', { when: formatDate(resp.created_at) })}
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button onClick={onClose}>{t('me_api_keys.created_close')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
