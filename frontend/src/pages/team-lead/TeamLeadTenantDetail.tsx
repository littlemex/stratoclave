import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Edit3 } from 'lucide-react'

import { AccessDenied } from '@/components/common/AccessDenied'
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

const RANGES: Array<{ label: string; days: number }> = [
  { label: '7 日', days: 7 },
  { label: '30 日', days: 30 },
  { label: '90 日', days: 90 },
]

export default function TeamLeadTenantDetail() {
  const { tenantId = '' } = useParams<{ tenantId: string }>()
  const qc = useQueryClient()
  const [days, setDays] = useState(30)

  const tenantQuery = useQuery({
    queryKey: ['team-lead', 'tenants', 'detail', tenantId],
    queryFn: () => api.teamLead.getTenant(tenantId),
    enabled: !!tenantId,
    retry: false,
  })

  const membersQuery = useQuery({
    queryKey: ['team-lead', 'tenants', 'members', tenantId],
    queryFn: () => api.teamLead.members(tenantId),
    enabled: !!tenantId && tenantQuery.isSuccess,
  })

  const usageQuery = useQuery({
    queryKey: ['team-lead', 'tenants', 'usage', tenantId, days],
    queryFn: () => api.teamLead.usage(tenantId, days),
    enabled: !!tenantId && tenantQuery.isSuccess,
  })

  const invalidateTenant = () => {
    void qc.invalidateQueries({ queryKey: ['team-lead', 'tenants', 'detail', tenantId] })
    void qc.invalidateQueries({ queryKey: ['team-lead', 'tenants'] })
  }

  if (tenantQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">読み込み中…</p>
  }

  // Backend が 404 を返すケース:
  // 1. 非所有者が他人のテナントを叩いた場合 (enumeration 防御)
  // 2. 存在しない tenant_id
  if (tenantQuery.isError || !tenantQuery.data) {
    return (
      <AccessDenied
        title="このテナントは表示できません"
        description="あなたが所有していないか、存在しない tenant_id です。所有するテナントは「所有テナント」一覧から選択してください。"
        homeHref="/team-lead/tenants"
      />
    )
  }

  const tenant = tenantQuery.data
  const usage = usageQuery.data
  const totalTokens = usage?.total_tokens ?? 0
  const byModel = Object.entries(usage?.by_model ?? {}).sort((a, b) => b[1] - a[1])
  const byUserEmail = Object.entries(usage?.by_user_email ?? {}).sort(
    (a, b) => b[1] - a[1],
  )
  const members = membersQuery.data?.members ?? []

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/team-lead/tenants">
          <ArrowLeft className="h-4 w-4" />
          所有テナントに戻る
        </Link>
      </Button>

      <div>
        <div className="flex items-center gap-2">
          <h1 className="font-display text-3xl tracking-tight">{tenant.name}</h1>
          <Badge variant={tenant.status === 'archived' ? 'muted' : 'secondary'}>
            {tenant.status}
          </Badge>
        </div>
        <code className="mt-1 block font-mono text-xs text-muted-foreground">
          {tenant.tenant_id}
        </code>
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
              所属メンバー
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-display text-2xl tracking-tight">
              {members.length}
              <span className="ml-1 text-xs font-sans font-normal text-muted-foreground">
                名
              </span>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              この期間の消費
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

      <EditButton tenant={tenant} onDone={invalidateTenant} />

      <Card>
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div>
            <CardTitle className="font-sans text-base font-semibold">
              所属メンバー
            </CardTitle>
            <CardDescription>
              あなたの閲覧権限では email とクレジット情報のみが表示されます。
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>ロール</TableHead>
                <TableHead className="text-right">残クレジット</TableHead>
                <TableHead className="text-right">使用</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {members.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                    このテナントに紐づく active メンバーはいません。Administrator にユーザーの紐付けを依頼してください。
                  </TableCell>
                </TableRow>
              ) : (
                members.map((m) => (
                  <TableRow key={m.email}>
                    <TableCell className="font-medium">
                      {m.email || '(email 未設定)'}
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
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div>
            <CardTitle className="font-sans text-base font-semibold">使用量</CardTitle>
            <CardDescription>
              モデル別・メンバー別 (email) の集計です。Team Lead の視点では user_id は非公開。
            </CardDescription>
          </div>
          <div className="flex gap-2" role="radiogroup" aria-label="期間選択">
            {RANGES.map((r) => (
              <Button
                key={r.days}
                role="radio"
                aria-checked={days === r.days}
                variant={days === r.days ? 'default' : 'outline'}
                size="sm"
                onClick={() => setDays(r.days)}
              >
                {r.label}
              </Button>
            ))}
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          <section>
            <h3 className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
              モデル別
            </h3>
            {byModel.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                この期間の使用履歴はまだありません。
              </p>
            ) : (
              <ul className="space-y-2">
                {byModel.map(([model, tokens]) => {
                  const pct = totalTokens > 0 ? Math.round((tokens / totalTokens) * 100) : 0
                  return (
                    <li key={model} className="space-y-1">
                      <div className="flex items-baseline justify-between gap-3">
                        <code className="truncate font-mono text-xs text-muted-foreground">
                          {model}
                        </code>
                        <span className="text-sm font-medium">
                          {fmt(tokens)}{' '}
                          <span className="text-xs text-muted-foreground">tokens ({pct}%)</span>
                        </span>
                      </div>
                      <div className="h-1 w-full overflow-hidden rounded-sm bg-muted">
                        <div
                          className={cn('h-full bg-primary transition-all')}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>

          <section>
            <h3 className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
              メンバー別 (email)
            </h3>
            {byUserEmail.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                メンバー別のデータはまだありません。
              </p>
            ) : (
              <ul className="space-y-2">
                {byUserEmail.map(([email, tokens]) => {
                  const pct = totalTokens > 0 ? Math.round((tokens / totalTokens) * 100) : 0
                  return (
                    <li key={email} className="space-y-1">
                      <div className="flex items-baseline justify-between gap-3">
                        <span className="truncate text-sm">{email}</span>
                        <span className="text-sm font-medium">
                          {fmt(tokens)}{' '}
                          <span className="text-xs text-muted-foreground">tokens ({pct}%)</span>
                        </span>
                      </div>
                      <div className="h-1 w-full overflow-hidden rounded-sm bg-muted">
                        <div
                          className={cn('h-full bg-accent transition-all')}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </CardContent>
      </Card>
    </div>
  )
}

function EditButton({
  tenant,
  onDone,
}: {
  tenant: TenantItem
  onDone: () => void
}) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState(tenant.name)
  const [defaultCredit, setDefaultCredit] = useState(String(tenant.default_credit))
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () =>
      api.teamLead.updateTenant(tenant.tenant_id, {
        name: name !== tenant.name ? name : undefined,
        default_credit:
          Number(defaultCredit) !== tenant.default_credit ? Number(defaultCredit) : undefined,
      }),
    onSuccess: () => {
      setOpen(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? '更新に失敗しました')
    },
  })

  return (
    <>
      <div>
        <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
          <Edit3 className="h-4 w-4" />
          編集
        </Button>
      </div>
      <Dialog
        open={open}
        onOpenChange={(v) => {
          if (!v) {
            setError(null)
            setName(tenant.name)
            setDefaultCredit(String(tenant.default_credit))
          }
          setOpen(v)
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>テナント編集</DialogTitle>
            <DialogDescription>名前と default_credit を変更できます。</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="tl-edit-name">名前</Label>
              <Input id="tl-edit-name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="tl-edit-default">default_credit</Label>
              <Input
                id="tl-edit-default"
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
            <Button variant="ghost" onClick={() => setOpen(false)}>
              キャンセル
            </Button>
            <Button disabled={mutation.isPending} onClick={() => mutation.mutate()}>
              {mutation.isPending ? '更新中…' : '更新'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
