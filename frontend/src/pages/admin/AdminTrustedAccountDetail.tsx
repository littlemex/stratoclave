import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, MailPlus, Trash2 } from 'lucide-react'

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
  type ProvisioningPolicy,
  type TrustedAccountItem,
} from '@/lib/api'

function fmt(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString()
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

export default function AdminTrustedAccountDetail() {
  const { accountId = '' } = useParams<{ accountId: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const accountQuery = useQuery({
    queryKey: ['admin', 'trusted-accounts', accountId],
    queryFn: () => api.admin.getTrustedAccount(accountId),
    enabled: !!accountId,
  })

  const invitesQuery = useQuery({
    queryKey: ['admin', 'sso-invites', accountId],
    queryFn: () => api.admin.listSsoInvites({ account_id: accountId, limit: 100 }),
    enabled: !!accountId,
  })

  if (accountQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">読み込み中…</p>
  }
  if (accountQuery.isError || !accountQuery.data) {
    return (
      <p className="text-sm text-destructive">
        このアカウントは登録されていません。
      </p>
    )
  }

  const account = accountQuery.data
  const invites = invitesQuery.data?.invites ?? []

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/admin/trusted-accounts">
          <ArrowLeft className="h-4 w-4" />
          信頼アカウント一覧に戻る
        </Link>
      </Button>

      <div>
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          AWS Account
        </p>
        <div className="flex items-center gap-2">
          <h1 className="font-display text-3xl font-semibold tracking-tight">
            {account.description || 'No description'}
          </h1>
          <Badge variant="secondary">{account.provisioning_policy}</Badge>
        </div>
        <code className="mt-1 block font-mono text-xs text-muted-foreground">
          {account.account_id}
        </code>
      </div>

      <section className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
              Default Credit
            </p>
          </CardHeader>
          <CardContent>
            <div className="font-display text-2xl font-semibold tracking-tight">
              {fmt(account.default_credit)}
              <span className="ml-1 text-xs font-sans font-normal text-muted-foreground">
                tokens
              </span>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
              IAM User Login
            </p>
          </CardHeader>
          <CardContent>
            <Badge variant={account.allow_iam_user ? 'destructive' : 'muted'}>
              {account.allow_iam_user ? 'ALLOWED' : 'DENIED'}
            </Badge>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
              Instance Profile
            </p>
          </CardHeader>
          <CardContent>
            <Badge variant={account.allow_instance_profile ? 'destructive' : 'muted'}>
              {account.allow_instance_profile ? 'ALLOWED' : 'DENIED'}
            </Badge>
          </CardContent>
        </Card>
      </section>

      <ActionBar
        account={account}
        onChanged={() => {
          void qc.invalidateQueries({ queryKey: ['admin', 'trusted-accounts'] })
        }}
        onDeleted={() => {
          void qc.invalidateQueries({ queryKey: ['admin', 'trusted-accounts'] })
          navigate('/admin/trusted-accounts')
        }}
      />

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            Allowed Role Patterns
          </CardTitle>
          <CardDescription>
            空の場合は全 role を許可します。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {account.allowed_role_patterns.length === 0 ? (
            <p className="text-sm text-muted-foreground">（制限なし）</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {account.allowed_role_patterns.map((p) => (
                <code
                  key={p}
                  className="rounded-sm border border-border bg-muted/40 px-2 py-1 font-mono text-xs"
                >
                  {p}
                </code>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div>
            <CardTitle className="font-sans text-base font-semibold">
              SSO 事前招待 (Pre-registrations)
            </CardTitle>
            <CardDescription>
              招待は常に最優先で適用されます。auto_provision の既定動作に加えて、
              session_name が email でない Isengard / IAM user / 個別ロール昇格 を
              email にマップするために併用できます。
              {account.provisioning_policy === 'invite_only'
                ? ' (現在: invite_only なので、招待が無いユーザーは拒否されます)'
                : ' (現在: auto_provision なので、招待がないユーザーは session_name から email を自動抽出します)'}
            </CardDescription>
          </div>
          <InviteButton accountId={account.account_id} />
        </CardHeader>
        <CardContent className="p-0">
          {invitesQuery.isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">読み込み中…</p>
          ) : invites.length === 0 ? (
            <p className="px-6 py-10 text-center text-sm text-muted-foreground">
              このアカウントの招待はまだありません。
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Email</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>IAM User</TableHead>
                  <TableHead>招待日</TableHead>
                  <TableHead>利用</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {invites.map((inv) => (
                  <TableRow key={inv.email}>
                    <TableCell>
                      <div className="font-medium">{inv.email}</div>
                      {inv.tenant_id ? (
                        <span className="text-[11px] text-muted-foreground">
                          tenant: {inv.tenant_id}
                        </span>
                      ) : null}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={inv.invited_role === 'team_lead' ? 'default' : 'secondary'}
                      >
                        {inv.invited_role}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {inv.iam_user_name || '—'}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {formatDate(inv.invited_at)}
                    </TableCell>
                    <TableCell>
                      {inv.consumed_at ? (
                        <Badge variant="muted">used</Badge>
                      ) : (
                        <Badge variant="secondary">pending</Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      <DeleteInviteButton email={inv.email} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// ------------------------------------------------------------------
function ActionBar({
  account,
  onChanged,
  onDeleted,
}: {
  account: TrustedAccountItem
  onChanged: () => void
  onDeleted: () => void
}) {
  const [editOpen, setEditOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  return (
    <section className="flex flex-wrap gap-2">
      <Button variant="outline" size="sm" onClick={() => setEditOpen(true)}>
        編集
      </Button>
      <Button variant="destructive" size="sm" onClick={() => setDeleteOpen(true)}>
        <Trash2 className="h-4 w-4" />
        削除
      </Button>
      <EditDialog
        account={account}
        open={editOpen}
        onOpenChange={setEditOpen}
        onDone={onChanged}
      />
      <DeleteDialog
        account={account}
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        onDone={onDeleted}
      />
    </section>
  )
}

function EditDialog({
  account,
  open,
  onOpenChange,
  onDone,
}: {
  account: TrustedAccountItem
  open: boolean
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const [description, setDescription] = useState(account.description)
  const [policy, setPolicy] = useState<ProvisioningPolicy>(account.provisioning_policy)
  const [rolePatterns, setRolePatterns] = useState(
    account.allowed_role_patterns.join('\n'),
  )
  const [allowIamUser, setAllowIamUser] = useState(account.allow_iam_user)
  const [allowInstanceProfile, setAllowInstanceProfile] = useState(
    account.allow_instance_profile,
  )
  const [defaultCredit, setDefaultCredit] = useState(
    account.default_credit == null ? '' : String(account.default_credit),
  )
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.updateTrustedAccount(account.account_id, {
        description,
        provisioning_policy: policy,
        allowed_role_patterns: rolePatterns
          .split(/[,\n]/)
          .map((s) => s.trim())
          .filter(Boolean),
        allow_iam_user: allowIamUser,
        allow_instance_profile: allowInstanceProfile,
        default_credit: defaultCredit ? Number(defaultCredit) : undefined,
      }),
    onSuccess: () => {
      onDone()
      onOpenChange(false)
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? '更新に失敗しました')
    },
  })

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
          <DialogTitle>信頼アカウントの編集</DialogTitle>
          <DialogDescription>
            account_id は変更できません。その他のパラメータを調整します。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label>Description</Label>
            <Input value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label>Provisioning Policy</Label>
            <select
              value={policy}
              onChange={(e) => setPolicy(e.target.value as ProvisioningPolicy)}
              className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
            >
              <option value="invite_only">invite_only</option>
              <option value="auto_provision">auto_provision</option>
            </select>
          </div>
          <div className="space-y-1.5">
            <Label>Allowed Role Patterns</Label>
            <textarea
              value={rolePatterns}
              onChange={(e) => setRolePatterns(e.target.value)}
              rows={3}
              className="flex w-full rounded-md border border-input bg-input px-3 py-2 font-mono text-xs text-foreground"
            />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={allowIamUser}
              onChange={(e) => setAllowIamUser(e.target.checked)}
              className="h-4 w-4 rounded-sm"
            />
            IAM user を許可
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={allowInstanceProfile}
              onChange={(e) => setAllowInstanceProfile(e.target.checked)}
              className="h-4 w-4 rounded-sm"
            />
            <span className="text-destructive">Instance Profile を許可 (非推奨)</span>
          </label>
          <div className="space-y-1.5">
            <Label>Default Credit</Label>
            <Input
              type="number"
              value={defaultCredit}
              onChange={(e) => setDefaultCredit(e.target.value)}
              min={0}
              max={10_000_000}
            />
          </div>
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? '更新中…' : '更新'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DeleteDialog({
  account,
  open,
  onOpenChange,
  onDone,
}: {
  account: TrustedAccountItem
  open: boolean
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => api.admin.deleteTrustedAccount(account.account_id),
    onSuccess: () => {
      onOpenChange(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? '削除に失敗しました')
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) {
          setConfirm('')
          setError(null)
        }
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="text-destructive">アカウントを削除</DialogTitle>
          <DialogDescription>
            このアカウント経由で作成されたユーザーの Cognito エントリは残ります。
            今後このアカウントからの SSO ログインは拒否されます。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label>
            確認のために <code className="font-mono">{account.account_id}</code> を入力
          </Label>
          <Input value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button
            variant="destructive"
            disabled={confirm !== account.account_id || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? '削除中…' : '削除を実行'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------
function InviteButton({ accountId }: { accountId: string }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
        <MailPlus className="h-4 w-4" />
        招待を追加
      </Button>
      <InviteDialog accountId={accountId} open={open} onOpenChange={setOpen} />
    </>
  )
}

function InviteDialog({
  accountId,
  open,
  onOpenChange,
}: {
  accountId: string
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const qc = useQueryClient()
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<'user' | 'team_lead'>('user')
  const [tenantId, setTenantId] = useState('')
  const [totalCredit, setTotalCredit] = useState('')
  const [iamUserName, setIamUserName] = useState('')
  const [error, setError] = useState<string | null>(null)

  const tenantsQuery = useQuery({
    queryKey: ['admin', 'tenants', 'select'],
    queryFn: () => api.admin.listTenants({ limit: 100 }),
    enabled: open,
  })

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.createSsoInvite({
        email: email.trim(),
        account_id: accountId,
        invited_role: role,
        tenant_id: tenantId || undefined,
        total_credit: totalCredit ? Number(totalCredit) : undefined,
        iam_user_name: iamUserName.trim() || undefined,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['admin', 'sso-invites', accountId] })
      setEmail('')
      setRole('user')
      setTenantId('')
      setTotalCredit('')
      setIamUserName('')
      setError(null)
      onOpenChange(false)
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? '招待の作成に失敗しました')
    },
  })

  const isValid = email.includes('@')

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>SSO 招待を追加</DialogTitle>
          <DialogDescription>
            account <code className="font-mono">{accountId}</code> 経由で初回ログインした
            際にこの email が自動で Stratoclave user として作成されます。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label>Email</Label>
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="user@example.com"
            />
          </div>
          <div className="space-y-1.5">
            <Label>Role</Label>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as 'user' | 'team_lead')}
              className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
            >
              <option value="user">user</option>
              <option value="team_lead">team_lead</option>
            </select>
          </div>
          <div className="space-y-1.5">
            <Label>IAM User Name (任意、IAM user 招待のみ)</Label>
            <Input
              value={iamUserName}
              onChange={(e) => setIamUserName(e.target.value)}
              placeholder="alice"
            />
            <p className="text-[11px] text-muted-foreground">
              IAM user は Arn から email を導出できないため、この name でマップします。
            </p>
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label>Tenant (任意)</Label>
              <select
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
              >
                <option value="">default</option>
                {(tenantsQuery.data?.tenants ?? []).map((t) => (
                  <option key={t.tenant_id} value={t.tenant_id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <Label>Total Credit (任意)</Label>
              <Input
                type="number"
                value={totalCredit}
                onChange={(e) => setTotalCredit(e.target.value)}
                min={0}
                max={10_000_000}
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
            {mutation.isPending ? '作成中…' : '招待を作成'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DeleteInviteButton({ email }: { email: string }) {
  const qc = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => api.admin.deleteSsoInvite(email),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['admin', 'sso-invites'] })
    },
  })
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => {
        if (confirm(`Delete invite for ${email}?`)) mutation.mutate()
      }}
      disabled={mutation.isPending}
    >
      <Trash2 className="h-4 w-4" />
    </Button>
  )
}
