import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus } from 'lucide-react'

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
import { api } from '@/lib/api'

function fmt(n: number): string {
  return n.toLocaleString()
}

export default function AdminTenants() {
  const [cursor, setCursor] = useState<string | undefined>()
  const [cursorStack, setCursorStack] = useState<Array<string | undefined>>([])
  const [createOpen, setCreateOpen] = useState(false)

  const tenantsQuery = useQuery({
    queryKey: ['admin', 'tenants', cursor],
    queryFn: () => api.admin.listTenants({ cursor, limit: 50 }),
    placeholderData: (p) => p,
  })

  const nextCursor = tenantsQuery.data?.next_cursor ?? null

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="font-display text-3xl tracking-tight">テナント管理</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            全テナントを一覧します。Team Lead が所有するテナントもここから確認・編集できます。
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          新規テナント
        </Button>
      </header>

      <div className="border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>名前</TableHead>
              <TableHead>tenant_id</TableHead>
              <TableHead>オーナー</TableHead>
              <TableHead className="text-right">default_credit</TableHead>
              <TableHead>状態</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {tenantsQuery.isLoading ? (
              <TableRow>
                <TableCell colSpan={6} className="text-center text-muted-foreground">
                  読み込み中…
                </TableCell>
              </TableRow>
            ) : (tenantsQuery.data?.tenants.length ?? 0) === 0 ? (
              <TableRow>
                <TableCell colSpan={6} className="text-center text-muted-foreground">
                  テナントがありません。
                </TableCell>
              </TableRow>
            ) : (
              tenantsQuery.data!.tenants.map((t) => (
                <TableRow key={t.tenant_id}>
                  <TableCell className="font-medium">{t.name}</TableCell>
                  <TableCell>
                    <code className="font-mono text-xs text-muted-foreground">
                      {t.tenant_id}
                    </code>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {t.team_lead_user_id === 'admin-owned' ? (
                      <Badge variant="muted">admin-owned</Badge>
                    ) : (
                      <code className="font-mono">{t.team_lead_user_id}</code>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm">
                    {fmt(t.default_credit)}
                  </TableCell>
                  <TableCell>
                    <Badge variant={t.status === 'archived' ? 'muted' : 'secondary'}>
                      {t.status}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <Button asChild variant="ghost" size="sm">
                      <Link to={`/admin/tenants/${encodeURIComponent(t.tenant_id)}`}>
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

      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>
          {tenantsQuery.data?.tenants.length ?? 0} 件表示
          {nextCursor ? ' / 次ページあり' : ''}
        </span>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={cursorStack.length === 0}
            onClick={() =>
              setCursorStack((s) => {
                const next = [...s]
                const prev = next.pop()
                setCursor(prev)
                return next
              })
            }
          >
            前へ
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={!nextCursor}
            onClick={() => {
              setCursorStack((s) => [...s, cursor])
              setCursor(nextCursor ?? undefined)
            }}
          >
            次へ
          </Button>
        </div>
      </div>

      <Card className="border-primary/30 bg-primary/5">
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            Tenant の所有者について
          </CardTitle>
          <CardDescription>
            `admin-owned` は組織全体で共有するテナントを意味します。Team Lead が所有するテナントは、その Team Lead 本人の user_id が表示されます。
          </CardDescription>
        </CardHeader>
        <CardContent className="text-xs text-muted-foreground">
          所有者を変更するにはテナント詳細画面の「オーナー再割当」を使用します (Cognito ユーザーが削除されて孤児化したテナントの救済用)。
        </CardContent>
      </Card>

      <CreateTenantDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
      />
    </div>
  )
}

function CreateTenantDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [teamLeadUserId, setTeamLeadUserId] = useState('admin-owned')
  const [defaultCredit, setDefaultCredit] = useState('')
  const [error, setError] = useState<string | null>(null)

  // team_lead ロールのユーザーを提案
  const teamLeadUsersQuery = useQuery({
    queryKey: ['admin', 'users', 'team_lead'],
    queryFn: () => api.admin.listUsers({ role: 'team_lead', limit: 100 }),
    enabled: open,
  })

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.createTenant({
        name: name.trim(),
        team_lead_user_id: teamLeadUserId,
        default_credit: defaultCredit ? Number(defaultCredit) : undefined,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['admin', 'tenants'] })
      onOpenChange(false)
      setName('')
      setTeamLeadUserId('admin-owned')
      setDefaultCredit('')
      setError(null)
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? '作成に失敗しました')
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
          <DialogTitle>新規テナント</DialogTitle>
          <DialogDescription>
            所有者を `admin-owned` にすると組織全体で共有するテナントになります。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="tenant-name">名前</Label>
            <Input
              id="tenant-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Acme Engineering"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="tenant-owner">オーナー (team_lead)</Label>
            <select
              id="tenant-owner"
              value={teamLeadUserId}
              onChange={(e) => setTeamLeadUserId(e.target.value)}
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
          <div className="space-y-1.5">
            <Label htmlFor="tenant-default-credit">default_credit (任意)</Label>
            <Input
              id="tenant-default-credit"
              type="number"
              min={0}
              max={10_000_000}
              value={defaultCredit}
              onChange={(e) => setDefaultCredit(e.target.value)}
              placeholder="未指定なら 100,000 tokens"
            />
          </div>
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button
            disabled={!name.trim() || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? '作成中…' : '作成'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
