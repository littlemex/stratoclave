import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Info, Plus } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
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
import { api, type ProvisioningPolicy } from '@/lib/api'
import { cn } from '@/lib/utils'

function fmt(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString()
}

export default function AdminTrustedAccounts() {
  const [createOpen, setCreateOpen] = useState(false)
  const listQuery = useQuery({
    queryKey: ['admin', 'trusted-accounts'],
    queryFn: () => api.admin.listTrustedAccounts({ limit: 100 }),
  })

  const accounts = listQuery.data?.accounts ?? []

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
            SSO Gateway
          </p>
          <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">
            信頼する AWS アカウント
          </h1>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            AWS SSO / STS 経由でログインを受け入れる AWS Account の allowlist。
            各アカウントごとに provisioning policy (invite_only / auto_provision) と
            受け入れる identity type (IAM user / Instance Profile) を制御できます。
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          新規アカウント追加
        </Button>
      </header>

      <Card className="border-accent/30 bg-accent/5">
        <CardHeader>
          <div className="flex items-center gap-2 text-accent">
            <Info className="h-4 w-4" aria-hidden />
            <CardTitle className="font-sans text-base font-semibold text-foreground">
              Instance Profile について
            </CardTitle>
          </div>
          <CardDescription>
            EC2 Instance Profile はインスタンス上の複数ユーザーで共有されるため
            個人特定ができません。原則 OFF のまま運用し、専有 EC2 / CI などで
            identity が一意に特定できる場合のみ有効化してください。
          </CardDescription>
        </CardHeader>
      </Card>

      <div className="border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Account ID</TableHead>
              <TableHead>Description</TableHead>
              <TableHead>Policy</TableHead>
              <TableHead>Role Patterns</TableHead>
              <TableHead>IAM User</TableHead>
              <TableHead>Instance Profile</TableHead>
              <TableHead className="text-right">Default Credit</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {listQuery.isLoading ? (
              <TableRow>
                <TableCell colSpan={8} className="text-center text-muted-foreground">
                  読み込み中…
                </TableCell>
              </TableRow>
            ) : accounts.length === 0 ? (
              <TableRow>
                <TableCell colSpan={8} className="py-10 text-center text-muted-foreground">
                  信頼アカウントがまだ登録されていません。
                  <br />
                  「新規アカウント追加」から AWS Account を allowlist に登録してください。
                </TableCell>
              </TableRow>
            ) : (
              accounts.map((a) => (
                <TableRow key={a.account_id}>
                  <TableCell>
                    <code className="font-mono text-xs">{a.account_id}</code>
                  </TableCell>
                  <TableCell className="max-w-xs truncate">{a.description || '—'}</TableCell>
                  <TableCell>
                    <Badge
                      variant={
                        a.provisioning_policy === 'auto_provision' ? 'default' : 'secondary'
                      }
                    >
                      {a.provisioning_policy}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {a.allowed_role_patterns.length === 0 ? (
                      <span className="text-xs text-muted-foreground">全 role</span>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {a.allowed_role_patterns.slice(0, 2).map((p) => (
                          <code
                            key={p}
                            className="rounded-sm border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
                          >
                            {p}
                          </code>
                        ))}
                        {a.allowed_role_patterns.length > 2 ? (
                          <span className="text-[10px] text-muted-foreground">
                            +{a.allowed_role_patterns.length - 2}
                          </span>
                        ) : null}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <Badge variant={a.allow_iam_user ? 'destructive' : 'muted'}>
                      {a.allow_iam_user ? 'ALLOW' : 'DENY'}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Badge variant={a.allow_instance_profile ? 'destructive' : 'muted'}>
                      {a.allow_instance_profile ? 'ALLOW' : 'DENY'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm">
                    {fmt(a.default_credit)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button asChild variant="ghost" size="sm">
                      <Link to={`/admin/trusted-accounts/${encodeURIComponent(a.account_id)}`}>
                        詳細
                      </Link>
                    </Button>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <CreateAccountDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  )
}

function CreateAccountDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const qc = useQueryClient()
  const [accountId, setAccountId] = useState('')
  const [description, setDescription] = useState('')
  const [policy, setPolicy] = useState<ProvisioningPolicy>('invite_only')
  const [rolePatterns, setRolePatterns] = useState('')
  const [allowIamUser, setAllowIamUser] = useState(false)
  const [allowInstanceProfile, setAllowInstanceProfile] = useState(false)
  const [defaultTenantId, setDefaultTenantId] = useState('')
  const [defaultCredit, setDefaultCredit] = useState('')
  const [error, setError] = useState<string | null>(null)

  const tenantsQuery = useQuery({
    queryKey: ['admin', 'tenants', 'select'],
    queryFn: () => api.admin.listTenants({ limit: 100 }),
    enabled: open,
  })

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.createTrustedAccount({
        account_id: accountId.trim(),
        description: description.trim() || undefined,
        provisioning_policy: policy,
        allowed_role_patterns: rolePatterns
          .split(/[,\n]/)
          .map((s) => s.trim())
          .filter(Boolean),
        allow_iam_user: allowIamUser,
        allow_instance_profile: allowInstanceProfile,
        default_tenant_id: defaultTenantId || undefined,
        default_credit: defaultCredit ? Number(defaultCredit) : undefined,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['admin', 'trusted-accounts'] })
      reset()
      onOpenChange(false)
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? '作成に失敗しました')
    },
  })

  const reset = () => {
    setAccountId('')
    setDescription('')
    setPolicy('invite_only')
    setRolePatterns('')
    setAllowIamUser(false)
    setAllowInstanceProfile(false)
    setDefaultTenantId('')
    setDefaultCredit('')
    setError(null)
  }

  const isValid = /^\d{12}$/.test(accountId.trim())

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) reset()
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>信頼する AWS アカウントを追加</DialogTitle>
          <DialogDescription>
            このアカウントの IAM Identity Center / SAML / IAM user からの SSO ログインを受け入れます。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="ta-account">AWS Account ID (12 桁)</Label>
            <Input
              id="ta-account"
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              placeholder="123456789012"
              inputMode="numeric"
              maxLength={12}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ta-desc">説明 (任意)</Label>
            <Input
              id="ta-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Production us-east-1"
            />
          </div>
          <div className="space-y-1.5">
            <Label>Provisioning Policy</Label>
            <div className="grid gap-2">
              {(['invite_only', 'auto_provision'] as ProvisioningPolicy[]).map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => setPolicy(p)}
                  aria-pressed={policy === p}
                  className={cn(
                    'rounded-md border p-3 text-left text-sm transition-colors',
                    policy === p
                      ? 'border-primary bg-primary/10'
                      : 'border-border hover:border-primary/40',
                  )}
                >
                  <div className="font-medium">{p}</div>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {p === 'invite_only'
                      ? '招待が無いユーザーは拒否 (厳格)。IAM user / Isengard 等も招待で個別 map 可能。'
                      : '招待が無くても session_name を email として自動 provision。招待を追加すれば併用も可能。'}
                  </p>
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ta-roles">
              Allowed Role Patterns (glob、空なら全 role 許可)
            </Label>
            <textarea
              id="ta-roles"
              value={rolePatterns}
              onChange={(e) => setRolePatterns(e.target.value)}
              placeholder={'AWSReservedSSO_Developer_*\ndata-engineer-*'}
              rows={3}
              className="flex w-full rounded-md border border-input bg-input px-3 py-2 font-mono text-xs text-foreground placeholder:text-muted-foreground/70 focus-visible:outline-none focus-visible:border-primary/70 focus-visible:ring-2 focus-visible:ring-ring/60"
            />
            <p className="text-[11px] text-muted-foreground">
              改行 or カンマ区切り。例: <code className="font-mono">AWSReservedSSO_Admin_*</code>
            </p>
          </div>
          <div className="grid gap-2 rounded-md border border-border bg-muted/30 p-3">
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={allowIamUser}
                onChange={(e) => setAllowIamUser(e.target.checked)}
                className="mt-0.5 h-4 w-4 rounded-sm border-border"
              />
              <span>
                <span className="font-medium">IAM user を許可</span>
                <span className="block text-[11px] text-muted-foreground">
                  長期 access key の IAM user。invite_only との併用で Admin 事前登録が必須。
                </span>
              </span>
            </label>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={allowInstanceProfile}
                onChange={(e) => setAllowInstanceProfile(e.target.checked)}
                className="mt-0.5 h-4 w-4 rounded-sm border-border"
              />
              <span>
                <span className="font-medium text-destructive">
                  Instance Profile を許可 (非推奨)
                </span>
                <span className="block text-[11px] text-muted-foreground">
                  複数ユーザー共有 EC2 では個人特定不能。専有インスタンスの場合のみ有効化。
                </span>
              </span>
            </label>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="ta-tenant">Default Tenant (任意)</Label>
              <select
                id="ta-tenant"
                value={defaultTenantId}
                onChange={(e) => setDefaultTenantId(e.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
              >
                <option value="">default-org</option>
                {(tenantsQuery.data?.tenants ?? []).map((t) => (
                  <option key={t.tenant_id} value={t.tenant_id}>
                    {t.name} ({t.tenant_id})
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="ta-credit">Default Credit (任意)</Label>
              <Input
                id="ta-credit"
                type="number"
                min={0}
                max={10_000_000}
                value={defaultCredit}
                onChange={(e) => setDefaultCredit(e.target.value)}
                placeholder="未入力なら tenant default"
              />
            </div>
          </div>
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button disabled={!isValid || mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? '作成中…' : '追加'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
