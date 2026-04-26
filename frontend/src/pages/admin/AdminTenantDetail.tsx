import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Archive, Edit3, UserCog } from 'lucide-react'

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
import { api, type TenantItem } from '@/lib/api'
import { cn } from '@/lib/utils'

function fmt(n: number): string {
  return n.toLocaleString()
}

export default function AdminTenantDetail() {
  const { tenantId = '' } = useParams<{ tenantId: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const tenantQuery = useQuery({
    queryKey: ['admin', 'tenants', 'detail', tenantId],
    queryFn: () => api.admin.getTenant(tenantId),
    enabled: !!tenantId,
  })
  const membersQuery = useQuery({
    queryKey: ['admin', 'tenants', 'members', tenantId],
    queryFn: () => api.admin.tenantUsers(tenantId),
    enabled: !!tenantId,
  })
  const usageQuery = useQuery({
    queryKey: ['admin', 'tenants', 'usage', tenantId],
    queryFn: () => api.admin.tenantUsage(tenantId, 30),
    enabled: !!tenantId,
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['admin', 'tenants'] })
  }

  if (tenantQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">読み込み中…</p>
  }
  if (tenantQuery.isError || !tenantQuery.data) {
    return <p className="text-sm text-destructive">テナントが見つかりませんでした。</p>
  }

  const tenant = tenantQuery.data
  const usage = usageQuery.data
  const members = membersQuery.data?.members ?? []
  const usageByModel = Object.entries(usage?.by_model ?? {}).sort(
    (a, b) => b[1] - a[1],
  )
  const totalTokens = usage?.total_tokens ?? 0

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/admin/tenants">
          <ArrowLeft className="h-4 w-4" />
          テナント一覧に戻る
        </Link>
      </Button>

      <div>
        <div className="flex items-center gap-2">
          <h1 className="font-display text-3xl tracking-tight">{tenant.name}</h1>
          <Badge variant={tenant.status === 'archived' ? 'muted' : 'secondary'}>
            {tenant.status}
          </Badge>
        </div>
        <code className="mt-1 block font-mono text-xs text-muted-foreground">{tenant.tenant_id}</code>
      </div>

      <section className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              default_credit
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-display text-2xl tracking-tight">
              {fmt(tenant.default_credit)}
              <span className="ml-1 text-xs font-sans font-normal text-muted-foreground">
                tokens
              </span>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              オーナー
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1">
            {tenant.team_lead_user_id === 'admin-owned' ? (
              <Badge variant="muted">admin-owned</Badge>
            ) : (
              <code className="font-mono text-xs text-muted-foreground">
                {tenant.team_lead_user_id}
              </code>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              この期間の消費 (30日)
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-display text-2xl tracking-tight">
              {fmt(totalTokens)}
              <span className="ml-1 text-xs font-sans font-normal text-muted-foreground">
                tokens
              </span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {usage ? `${fmt(usage.sample_size)} 件のログから集計` : ' '}
            </p>
          </CardContent>
        </Card>
      </section>

      <ActionBar tenant={tenant} onChanged={invalidate} onDeleted={() => navigate('/admin/tenants')} />

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">所属メンバー</CardTitle>
          <CardDescription>
            active な UserTenants レコードのみを表示します。archived の履歴は UsageLogs に残ります。
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>ロール</TableHead>
                <TableHead className="text-right">残クレジット</TableHead>
                <TableHead className="text-right">使用</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {members.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                    このテナントに紐づく active メンバーはいません。
                  </TableCell>
                </TableRow>
              ) : (
                members.map((m) => (
                  <TableRow key={m.user_id}>
                    <TableCell>
                      <div className="font-medium">{m.email || '(email 未設定)'}</div>
                      <code className="mt-0.5 block truncate font-mono text-xs text-muted-foreground">
                        {m.user_id}
                      </code>
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant={
                          m.role === 'admin'
                            ? 'accent'
                            : m.role === 'team_lead'
                              ? 'default'
                              : 'secondary'
                        }
                      >
                        {m.role}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {fmt(m.remaining_credit)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs text-muted-foreground">
                      {fmt(m.credit_used)} / {fmt(m.total_credit)}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button asChild variant="ghost" size="sm">
                        <Link to={`/admin/users/${encodeURIComponent(m.user_id)}`}>詳細</Link>
                      </Button>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">モデル別 消費</CardTitle>
          <CardDescription>直近 30 日 / 最大 1,000 サンプル</CardDescription>
        </CardHeader>
        <CardContent>
          {usageByModel.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              このテナントでの使用履歴はまだありません。
            </p>
          ) : (
            <ul className="space-y-2">
              {usageByModel.map(([model, tokens]) => {
                const pct = totalTokens > 0 ? Math.round((tokens / totalTokens) * 100) : 0
                return (
                  <li key={model} className="space-y-1">
                    <div className="flex items-baseline justify-between gap-3">
                      <code className="truncate font-mono text-xs text-muted-foreground">{model}</code>
                      <span className="text-sm font-medium">
                        {fmt(tokens)} <span className="text-xs text-muted-foreground">tokens ({pct}%)</span>
                      </span>
                    </div>
                    <div className="h-1 w-full overflow-hidden rounded-sm bg-muted">
                      <div className={cn('h-full bg-primary transition-all')} style={{ width: `${pct}%` }} />
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">ユーザー別 消費</CardTitle>
          <CardDescription>Admin 向けビューなので user_email を表示します。</CardDescription>
        </CardHeader>
        <CardContent>
          {usage && Object.keys(usage.by_user ?? {}).length > 0 ? (
            <ul className="space-y-2">
              {Object.entries(usage.by_user ?? {})
                .sort((a, b) => b[1] - a[1])
                .map(([user, tokens]) => {
                  const pct = totalTokens > 0 ? Math.round((tokens / totalTokens) * 100) : 0
                  return (
                    <li key={user} className="space-y-1">
                      <div className="flex items-baseline justify-between gap-3">
                        <span className="truncate text-sm">{user}</span>
                        <span className="text-sm font-medium">
                          {fmt(tokens)}{' '}
                          <span className="text-xs text-muted-foreground">tokens ({pct}%)</span>
                        </span>
                      </div>
                      <div className="h-1 w-full overflow-hidden rounded-sm bg-muted">
                        <div className={cn('h-full bg-accent transition-all')} style={{ width: `${pct}%` }} />
                      </div>
                    </li>
                  )
                })}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">ユーザー別のデータはありません。</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// ------------------------------------------------------------------
// Action bar: edit / owner / archive
// ------------------------------------------------------------------
function ActionBar({
  tenant,
  onChanged,
  onDeleted,
}: {
  tenant: TenantItem
  onChanged: () => void
  onDeleted: () => void
}) {
  const [editOpen, setEditOpen] = useState(false)
  const [ownerOpen, setOwnerOpen] = useState(false)
  const [archiveOpen, setArchiveOpen] = useState(false)

  const isDefaultOrg = tenant.tenant_id === 'default-org'

  return (
    <section className="flex flex-wrap gap-2">
      <Button variant="outline" size="sm" onClick={() => setEditOpen(true)}>
        <Edit3 className="h-4 w-4" />
        編集
      </Button>
      <Button variant="outline" size="sm" onClick={() => setOwnerOpen(true)}>
        <UserCog className="h-4 w-4" />
        オーナー再割当
      </Button>
      <Button
        variant="destructive"
        size="sm"
        disabled={isDefaultOrg || tenant.status === 'archived'}
        onClick={() => setArchiveOpen(true)}
        title={isDefaultOrg ? 'default-org は削除できません' : undefined}
      >
        <Archive className="h-4 w-4" />
        アーカイブ
      </Button>

      <EditDialog
        open={editOpen}
        tenant={tenant}
        onOpenChange={setEditOpen}
        onDone={onChanged}
      />
      <OwnerDialog
        open={ownerOpen}
        tenant={tenant}
        onOpenChange={setOwnerOpen}
        onDone={onChanged}
      />
      <ArchiveDialog
        open={archiveOpen}
        tenant={tenant}
        onOpenChange={setArchiveOpen}
        onDone={onDeleted}
      />
    </section>
  )
}

function EditDialog({
  open,
  tenant,
  onOpenChange,
  onDone,
}: {
  open: boolean
  tenant: TenantItem
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const [name, setName] = useState(tenant.name)
  const [defaultCredit, setDefaultCredit] = useState(String(tenant.default_credit))
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.updateTenant(tenant.tenant_id, {
        name: name !== tenant.name ? name : undefined,
        default_credit:
          Number(defaultCredit) !== tenant.default_credit ? Number(defaultCredit) : undefined,
      }),
    onSuccess: () => {
      onOpenChange(false)
      onDone()
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
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>テナント編集</DialogTitle>
          <DialogDescription>名前と default_credit を変更できます。</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="edit-name">名前</Label>
            <Input id="edit-name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="edit-default">default_credit</Label>
            <Input
              id="edit-default"
              type="number"
              min={0}
              max={10_000_000}
              value={defaultCredit}
              onChange={(e) => setDefaultCredit(e.target.value)}
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

function OwnerDialog({
  open,
  tenant,
  onOpenChange,
  onDone,
}: {
  open: boolean
  tenant: TenantItem
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const [owner, setOwner] = useState(tenant.team_lead_user_id ?? 'admin-owned')
  const [error, setError] = useState<string | null>(null)

  const teamLeadUsersQuery = useQuery({
    queryKey: ['admin', 'users', 'team_lead'],
    queryFn: () => api.admin.listUsers({ role: 'team_lead', limit: 100 }),
    enabled: open,
  })

  const mutation = useMutation({
    mutationFn: () => api.admin.setOwner(tenant.tenant_id, owner),
    onSuccess: () => {
      onOpenChange(false)
      onDone()
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
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>オーナーの再割当</DialogTitle>
          <DialogDescription>
            team_lead ロールを持つユーザーを選択するか、admin-owned (共有) に設定します。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label htmlFor="owner-select">新しい所有者</Label>
          <select
            id="owner-select"
            value={owner}
            onChange={(e) => setOwner(e.target.value)}
            className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
          >
            <option value="admin-owned">admin-owned (共有)</option>
            {(teamLeadUsersQuery.data?.users ?? []).map((u) => (
              <option key={u.user_id} value={u.user_id}>
                {u.email || u.user_id}
              </option>
            ))}
          </select>
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button
            disabled={owner === tenant.team_lead_user_id || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? '更新中…' : '適用'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ArchiveDialog({
  open,
  tenant,
  onOpenChange,
  onDone,
}: {
  open: boolean
  tenant: TenantItem
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => api.admin.archiveTenant(tenant.tenant_id),
    onSuccess: () => {
      onOpenChange(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? 'アーカイブに失敗しました')
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
          <DialogTitle className="text-destructive">
            テナントをアーカイブします
          </DialogTitle>
          <DialogDescription>
            status=archived にしますが、レコード・使用履歴は残ります。同名のテナントをすぐに新規作成しても衝突しません。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label htmlFor="archive-confirm">
            確認のために <code className="font-mono text-foreground">{tenant.tenant_id}</code> を入力してください
          </Label>
          <Input
            id="archive-confirm"
            autoComplete="off"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder={tenant.tenant_id}
          />
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button
            variant="destructive"
            disabled={confirm !== tenant.tenant_id || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? '実行中…' : 'アーカイブ'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
