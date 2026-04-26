import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
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

const EXPIRY_OPTIONS: Array<{ label: string; days: number | null }> = [
  { label: '7 日', days: 7 },
  { label: '30 日 (推奨)', days: 30 },
  { label: '90 日', days: 90 },
  { label: '180 日', days: 180 },
  { label: '365 日', days: 365 },
  { label: '無期限', days: null },
]

const DEFAULT_SCOPE_PRESETS: Array<{ label: string; scopes: string[] }> = [
  {
    label: '既定 (messages + usage 読み取り)',
    scopes: ['messages:send', 'usage:read-self'],
  },
  { label: 'メッセージ送信のみ', scopes: ['messages:send'] },
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
          API Keys
        </p>
        <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
          <div>
            <h1 className="font-display text-3xl tracking-tight">
              長期 API キー
            </h1>
            <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
              <code className="font-mono text-foreground/80">sk-stratoclave-…</code>{' '}
              形式の長期 API キーを発行します。Claude Desktop cowork や独自のゲートウェイクライアントから Stratoclave 経由で Bedrock を利用できます。
              プレーンテキストは作成時に一度だけ表示されるため、安全に保管してください。
            </p>
          </div>
          <Button onClick={() => setCreateOpen(true)} disabled={listQuery.isLoading}>
            <Plus className="h-4 w-4" aria-hidden />
            新規発行
          </Button>
        </div>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              発行中のキー
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-baseline gap-2">
              <span className="strato-stat font-display text-3xl font-semibold tracking-tight">
                {listQuery.data?.active_count ?? '—'}
              </span>
              <span className="text-xs text-muted-foreground">
                / {listQuery.data?.max_per_user ?? 5} 個まで
              </span>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              保存方式
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-foreground">
              SHA-256 ハッシュのみ
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              プレーンテキストはサーバーに保存されません。
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              漏洩時の影響範囲
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-foreground">テナントのクレジット上限</p>
            <p className="mt-1 text-xs text-muted-foreground">
              それ以外の操作は scope で制限。即時 revoke できます。
            </p>
          </CardContent>
        </Card>
      </section>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            発行済みキー一覧
          </CardTitle>
          <CardDescription>
            キーをクリップボードに貼り付け済のアプリを停止したい場合は、そのキーを revoke してください。即時反映されます。
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {listQuery.isLoading ? (
            <p className="px-6 py-6 text-sm text-muted-foreground">読み込み中…</p>
          ) : keys.length === 0 ? (
            <EmptyState onCreate={() => setCreateOpen(true)} />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Key ID</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Scopes</TableHead>
                  <TableHead>発行 / 失効</TableHead>
                  <TableHead>最終利用</TableHead>
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
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-12 text-center text-sm text-muted-foreground">
      <KeyRound className="h-6 w-6 text-muted-foreground/70" aria-hidden />
      <p>まだ API キーを発行していません。</p>
      <Button size="sm" onClick={onCreate}>
        <Plus className="h-4 w-4" aria-hidden />
        最初のキーを発行
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
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const revokeMutation = useMutation({
    mutationFn: async () => {
      // Frontend 側では plaintext を持たないため、key_id ではなく
      // list レスポンスの key_hash を別途持ちたいところだが、Backend からは
      // key_hash を返しているためここで使用する.
      // list API のレスポンス側に key_hash は含まれない (key_id のみ公開) ので
      // ここでは key_id の前後略記から逆算する API が必要. Phase C2 の簡易版として
      // key_id をそのまま使って revoke するのは不可能。代わりに sha256(plaintext)
      // を呼ばずとも、backend が `/me/api-keys/{key_hash_or_id}` を受け付けるかを
      // 将来的に整える必要がある。現在は **key_hash を持つキーだけ revoke 可能** とし、
      // ここでは list レスポンスに key_hash を含める修正を別途入れる必要あり.
      // 暫定: key_id (先頭 + 末尾 各 4 文字) から逆引きは不可能なので、
      // Backend の /me/api-keys/by-id/{key_id} を追加して呼ぶのが本筋だが、
      // 暫定では admin-list / list API の key_hash 返却を有効化する方針に寄せる.
      // --- フォールバック: エラーを表示 ---
      throw new Error(
        'この UI では key_hash 直指定の revoke のみサポートされています。CLI (`stratoclave api-key revoke <hash>`) から実行してください。',
      )
    },
    onError: (err: unknown) => {
      const e = err as { message?: string } | null
      setError(e?.message ?? '失敗しました')
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
    ? { variant: 'muted' as const, label: 'REVOKED' }
    : isExpired
      ? { variant: 'muted' as const, label: 'EXPIRED' }
      : { variant: 'secondary' as const, label: 'ACTIVE' }

  return (
    <>
      <TableRow>
        <TableCell>
          <div className="flex items-center gap-2">
            <code className="font-mono text-xs">{item.key_id}</code>
            <Badge variant={badge.variant}>{badge.label}</Badge>
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
            作成 {formatDate(item.created_at)}
          </div>
          <div className="text-xs text-muted-foreground">
            {isRevoked
              ? `失効 ${formatDate(item.revoked_at)}`
              : `期限 ${formatDate(item.expires_at, '無期限')}`}
          </div>
        </TableCell>
        <TableCell className="text-xs text-muted-foreground">
          {formatDate(item.last_used_at, '未使用')}
        </TableCell>
        <TableCell className="text-right">
          {!isRevoked ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setConfirmOpen(true)}
              title="Revoke (CLI からも可)"
            >
              <Trash2 className="h-4 w-4" aria-hidden />
            </Button>
          ) : null}
        </TableCell>
      </TableRow>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>キーを revoke</DialogTitle>
            <DialogDescription>
              <strong>{item.key_id}</strong> を revoke すると、このキーを使っているクライアントは直ちに 401 になります。
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-border bg-muted/40 p-3 text-xs text-muted-foreground">
            UI からの revoke は現在未対応です。CLI で以下を実行してください:
            <pre className="mt-2 overflow-x-auto rounded-sm bg-background/60 p-2 font-mono text-[11px]">
{`stratoclave api-key revoke <key_hash>`}
            </pre>
            <p className="mt-2">
              key_hash は plaintext を保存した手元で{' '}
              <code className="font-mono">sha256(plaintext)</code> を計算してください。
              CLI で作成した場合は画面最下部に <code className="font-mono">stratoclave api-key revoke …</code> のヒントが出力されています。
            </p>
          </div>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
              閉じる
            </Button>
            <Button
              variant="destructive"
              disabled={revokeMutation.isPending}
              onClick={() => revokeMutation.mutate()}
            >
              UI からは revoke 不可
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
  const perms = usePermissions()
  const [name, setName] = useState('')
  const [preset, setPreset] = useState<number>(0) // index into DEFAULT_SCOPE_PRESETS
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
      setError(e?.detail ?? e?.message ?? '作成に失敗しました')
    },
  })

  const invalidScopes = useMemo(() => {
    if (!customScopes.trim()) return [] as string[]
    const sc = customScopes.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean)
    return sc.filter((s) => !s.includes(':'))
  }, [customScopes])

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
          <DialogTitle>新しい API キーを発行</DialogTitle>
          <DialogDescription>
            ラベル・scope・有効期限を指定してください。あなたのロール (
            {perms.roles.join(', ') || 'user'}) が持つ permission の subset のみ付与できます。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="ak-name">ラベル (任意)</Label>
            <Input
              id="ak-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="cowork on macbook"
              maxLength={64}
            />
          </div>

          <div className="space-y-1.5">
            <Label>Scope プリセット</Label>
            <div className="grid gap-2">
              {DEFAULT_SCOPE_PRESETS.map((p, idx) => (
                <button
                  key={p.label}
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
                    <div className="text-sm font-medium">{p.label}</div>
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
              Scope を個別指定 (任意、プリセットより優先)
            </Label>
            <Input
              id="ak-scopes"
              value={customScopes}
              onChange={(e) => setCustomScopes(e.target.value)}
              placeholder="messages:send usage:read-self"
            />
            <p className="text-[11px] text-muted-foreground">
              カンマまたはスペース区切り。例:{' '}
              <code className="font-mono">
                messages:send usage:read-self
              </code>
            </p>
            {invalidScopes.length > 0 ? (
              <p className="text-[11px] text-destructive">
                形式が不正: {invalidScopes.join(', ')} (resource:action 必須)
              </p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label>有効期限</Label>
            <div className="flex flex-wrap gap-2">
              {EXPIRY_OPTIONS.map((o) => (
                <Button
                  key={o.label}
                  type="button"
                  size="sm"
                  variant={expires === o.days ? 'default' : 'outline'}
                  onClick={() => setExpires(o.days)}
                >
                  {o.label}
                </Button>
              ))}
            </div>
            <p className="text-[11px] text-muted-foreground">
              セキュリティ上、短いほど望ましいです。必要に応じて無期限も選べます。
            </p>
          </div>

          <div className="flex items-start gap-2 rounded-md border border-accent/40 bg-accent/10 p-3 text-xs text-accent-foreground">
            <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            <p>
              作成後、プレーンテキストは<strong>一度だけ</strong>画面に表示されます。
              表示を閉じると再取得できません。
            </p>
          </div>
        </div>

        {error ? <p className="text-sm text-destructive">{error}</p> : null}

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button
            disabled={mutation.isPending || invalidScopes.length > 0}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? '作成中…' : '発行'}
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
          <DialogTitle>API キー発行完了</DialogTitle>
          <DialogDescription>
            このプレーンテキストは<strong>今だけ</strong>表示されます。閉じる前に必ずコピーして保管してください。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label>Key ID</Label>
            <code className="block rounded-sm bg-muted px-3 py-2 font-mono text-xs">
              {resp.key_id}
            </code>
          </div>
          <div className="space-y-1">
            <Label>プレーンテキスト</Label>
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
                {copied ? 'コピー済' : 'コピー'}
              </Button>
            </div>
          </div>
          <div className="space-y-1">
            <Label>revoke 用 hash (将来の失効手続き)</Label>
            <div className="flex gap-2">
              <code className="flex-1 overflow-x-auto rounded-sm bg-muted px-3 py-2 font-mono text-[11px] text-muted-foreground">
                {hash || '(計算中…)'}
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
              あとで revoke する場合:{' '}
              <code className="font-mono">stratoclave api-key revoke &lt;hash&gt;</code>
            </p>
          </div>
          <div className="space-y-1 text-xs text-muted-foreground">
            <div>Scopes: {resp.scopes.join(', ')}</div>
            <div>期限: {formatDate(resp.expires_at, '無期限')}</div>
            <div>作成: {formatDate(resp.created_at)}</div>
          </div>
        </div>
        <DialogFooter>
          <Button onClick={onClose}>閉じる (以後プレーンテキストは見られません)</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
