import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { ArrowLeft, ArrowRight, Coins, Trash2 } from 'lucide-react'

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
import { api, type Role, type UserSummary } from '@/lib/api'

const ROLE_LABEL: Record<Role, string> = {
  admin: 'Admin',
  team_lead: 'Team Lead',
  user: 'User',
}

function fmt(n: number): string {
  return n.toLocaleString()
}

export default function AdminUserDetail() {
  const { userId = '' } = useParams<{ userId: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const userQuery = useQuery({
    queryKey: ['admin', 'users', 'detail', userId],
    queryFn: () => api.admin.getUser(userId),
    enabled: !!userId,
  })
  const tenantsQuery = useQuery({
    queryKey: ['admin', 'tenants', 'select'],
    queryFn: () => api.admin.listTenants({ limit: 100 }),
  })

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/admin/users">
          <ArrowLeft className="h-4 w-4" />
          ユーザー一覧に戻る
        </Link>
      </Button>

      {userQuery.isLoading ? (
        <p className="text-sm text-muted-foreground">読み込み中…</p>
      ) : userQuery.isError || !userQuery.data ? (
        <p className="text-sm text-destructive">ユーザーが見つかりませんでした。</p>
      ) : (
        <Content
          user={userQuery.data}
          tenantOptions={tenantsQuery.data?.tenants ?? []}
          onMutated={() => {
            void qc.invalidateQueries({ queryKey: ['admin', 'users'] })
            void userQuery.refetch()
          }}
          onDeleted={() => {
            void qc.invalidateQueries({ queryKey: ['admin', 'users'] })
            navigate('/admin/users')
          }}
        />
      )}
    </div>
  )
}

interface ContentProps {
  user: UserSummary
  tenantOptions: Array<{ tenant_id: string; name: string }>
  onMutated: () => void
  onDeleted: () => void
}

function Content({ user, tenantOptions, onMutated, onDeleted }: ContentProps) {
  const [assignOpen, setAssignOpen] = useState(false)
  const [creditOpen, setCreditOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)

  return (
    <>
      <section className="space-y-2">
        <h1 className="font-display text-3xl tracking-tight">{user.email || '(email 未設定)'}</h1>
        <code className="block font-mono text-xs text-muted-foreground">{user.user_id}</code>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              ロール / 認証
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="flex flex-wrap gap-1">
              {user.roles.map((r) => (
                <Badge
                  key={r}
                  variant={
                    r === 'admin' ? 'accent' : r === 'team_lead' ? 'default' : 'secondary'
                  }
                >
                  {ROLE_LABEL[r] ?? r}
                </Badge>
              ))}
            </div>
            <Badge
              variant={user.auth_method === 'sso' ? 'outline' : 'muted'}
              className="mt-1"
            >
              auth · {user.auth_method ?? 'cognito'}
            </Badge>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              所属テナント
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-display text-lg tracking-tight">{user.org_id}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="font-sans text-sm font-medium text-muted-foreground">
              クレジット
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="font-display text-lg tracking-tight">
              {fmt(user.remaining_credit)}
              <span className="ml-1 text-xs font-sans font-normal text-muted-foreground">
                tokens 残
              </span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {fmt(user.credit_used)} / {fmt(user.total_credit)} tokens 使用
            </p>
          </CardContent>
        </Card>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">
              テナント切替
            </CardTitle>
            <CardDescription>
              別 Tenant に移動します。クレジットはリセットされ、対象ユーザーは強制再ログインになります。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setAssignOpen(true)}
              disabled={tenantOptions.length === 0}
            >
              <ArrowRight className="h-4 w-4" />
              切替を開始
            </Button>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">
              クレジット上書き
            </CardTitle>
            <CardDescription>
              現 Tenant 上のクレジットを直接上書きします (user_override)。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="outline" size="sm" onClick={() => setCreditOpen(true)}>
              <Coins className="h-4 w-4" />
              クレジット編集
            </Button>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold text-destructive">
              ユーザー削除
            </CardTitle>
            <CardDescription>
              Cognito から削除し DynamoDB UserTenants は archived になります (UsageLogs は保持)。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => setDeleteOpen(true)}
            >
              <Trash2 className="h-4 w-4" />
              削除
            </Button>
          </CardContent>
        </Card>
      </section>

      {user.auth_method === 'sso' ? (
        <Card className="border-primary/30 bg-primary/5">
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">
              SSO メタデータ
            </CardTitle>
            <CardDescription>
              このユーザーは AWS SSO / STS 経由で登録されています。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 text-xs">
            <div>
              <span className="font-medium uppercase tracking-[0.12em] text-muted-foreground">
                account
              </span>
              <div className="mt-0.5 font-mono">{user.sso_account_id ?? '—'}</div>
            </div>
            <div>
              <span className="font-medium uppercase tracking-[0.12em] text-muted-foreground">
                principal arn
              </span>
              <div className="mt-0.5 break-all font-mono text-muted-foreground">
                {user.sso_principal_arn ?? '—'}
              </div>
            </div>
            <div>
              <span className="font-medium uppercase tracking-[0.12em] text-muted-foreground">
                last sso login
              </span>
              <div className="mt-0.5 font-mono">
                {user.last_sso_login_at
                  ? new Date(user.last_sso_login_at).toLocaleString()
                  : '—'}
              </div>
            </div>
          </CardContent>
        </Card>
      ) : null}

      <AssignTenantDialog
        open={assignOpen}
        user={user}
        tenantOptions={tenantOptions}
        onOpenChange={setAssignOpen}
        onDone={onMutated}
      />
      <SetCreditDialog
        open={creditOpen}
        user={user}
        onOpenChange={setCreditOpen}
        onDone={onMutated}
      />
      <DeleteUserDialog
        open={deleteOpen}
        user={user}
        onOpenChange={setDeleteOpen}
        onDeleted={onDeleted}
      />
    </>
  )
}

// ------------------------------------------------------------------
// Assign tenant dialog (2-step confirmation)
// ------------------------------------------------------------------
function AssignTenantDialog({
  open,
  user,
  tenantOptions,
  onOpenChange,
  onDone,
}: {
  open: boolean
  user: UserSummary
  tenantOptions: Array<{ tenant_id: string; name: string }>
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const [step, setStep] = useState<1 | 2>(1)
  const [tenantId, setTenantId] = useState('')
  const [newRole, setNewRole] = useState<Role>('user')
  const [totalCredit, setTotalCredit] = useState('')
  const [confirmEmail, setConfirmEmail] = useState('')
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setStep(1)
    setTenantId('')
    setNewRole('user')
    setTotalCredit('')
    setConfirmEmail('')
    setError(null)
  }

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.assignTenant(user.user_id, {
        tenant_id: tenantId,
        total_credit: totalCredit ? Number(totalCredit) : undefined,
        new_role: newRole,
      }),
    onSuccess: () => {
      reset()
      onOpenChange(false)
      onDone()
    },
    onError: (err: unknown) => {
      const e = err as { detail?: string; message?: string } | null
      setError(e?.detail ?? e?.message ?? '切替に失敗しました')
    },
  })

  const step1Valid =
    tenantId.length > 0 && tenantId !== user.org_id

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset()
        onOpenChange(next)
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {step === 1 ? 'ステップ 1: 切替先の指定' : 'ステップ 2: 最終確認'}
          </DialogTitle>
          <DialogDescription>
            {step === 1
              ? '切替先の Tenant と新しいロール・クレジットを入力してください。'
              : `${user.email} を ${user.org_id} → ${tenantId} に切り替えます。確認のために email を入力してください。`}
          </DialogDescription>
        </DialogHeader>

        {step === 1 ? (
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="assign-tenant">切替先 Tenant</Label>
              <select
                id="assign-tenant"
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                <option value="">選択してください</option>
                {tenantOptions.map((t) => (
                  <option key={t.tenant_id} value={t.tenant_id}>
                    {t.name} ({t.tenant_id}){t.tenant_id === user.org_id ? ' — 現在の所属' : ''}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="assign-role">新しいロール</Label>
              <select
                id="assign-role"
                value={newRole}
                onChange={(e) => setNewRole(e.target.value as Role)}
                className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground"
              >
                <option value="user">User</option>
                <option value="team_lead">Team Lead</option>
              </select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="assign-credit">クレジット上書き (任意)</Label>
              <Input
                id="assign-credit"
                type="number"
                min={0}
                max={10_000_000}
                value={totalCredit}
                onChange={(e) => setTotalCredit(e.target.value)}
                placeholder="未入力なら新 Tenant の default_credit"
              />
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive-foreground">
              <p className="font-semibold">この操作は以下を実行します:</p>
              <ul className="mt-1 list-inside list-disc space-y-0.5">
                <li>旧 Tenant ({user.org_id}) の UserTenants を archived に</li>
                <li>新 Tenant ({tenantId}) に new_role={newRole} で active 化</li>
                <li>Cognito の custom:org_id を更新</li>
                <li>対象ユーザーの全セッションを失効 (AdminUserGlobalSignOut)</li>
              </ul>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="assign-confirm">
                確認のために {user.email} を入力してください
              </Label>
              <Input
                id="assign-confirm"
                autoComplete="off"
                value={confirmEmail}
                onChange={(e) => setConfirmEmail(e.target.value)}
                placeholder={user.email}
              />
            </div>
          </div>
        )}

        {error ? <p className="text-sm text-destructive">{error}</p> : null}

        <DialogFooter>
          {step === 1 ? (
            <>
              <Button variant="ghost" onClick={() => onOpenChange(false)}>
                キャンセル
              </Button>
              <Button disabled={!step1Valid} onClick={() => setStep(2)}>
                次へ
              </Button>
            </>
          ) : (
            <>
              <Button variant="ghost" onClick={() => setStep(1)}>
                戻る
              </Button>
              <Button
                variant="destructive"
                disabled={confirmEmail !== user.email || mutation.isPending}
                onClick={() => mutation.mutate()}
              >
                {mutation.isPending ? '切替中…' : '切替を実行'}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------
// Credit overwrite dialog
// ------------------------------------------------------------------
function SetCreditDialog({
  open,
  user,
  onOpenChange,
  onDone,
}: {
  open: boolean
  user: UserSummary
  onOpenChange: (v: boolean) => void
  onDone: () => void
}) {
  const [value, setValue] = useState<string>(String(user.total_credit))
  const [resetUsed, setResetUsed] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () =>
      api.admin.setCredit(user.user_id, {
        total_credit: Number(value),
        reset_used: resetUsed,
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
        if (!v) {
          setError(null)
          setValue(String(user.total_credit))
          setResetUsed(false)
        }
        onOpenChange(v)
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>クレジット上書き</DialogTitle>
          <DialogDescription>
            {user.email} の total_credit を新しい値で上書きします。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="credit-value">新しい total_credit</Label>
            <Input
              id="credit-value"
              type="number"
              min={0}
              max={10_000_000}
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              現在: {fmt(user.total_credit)} tokens / 使用済み: {fmt(user.credit_used)} tokens
            </p>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={resetUsed}
              onChange={(e) => setResetUsed(e.target.checked)}
              className="h-4 w-4 rounded-sm border-border"
            />
            credit_used を 0 にリセットする
          </label>
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button
            disabled={!value || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? '更新中…' : '更新'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ------------------------------------------------------------------
// Delete user dialog (text confirmation)
// ------------------------------------------------------------------
function DeleteUserDialog({
  open,
  user,
  onOpenChange,
  onDeleted,
}: {
  open: boolean
  user: UserSummary
  onOpenChange: (v: boolean) => void
  onDeleted: () => void
}) {
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => api.admin.deleteUser(user.user_id),
    onSuccess: () => {
      onOpenChange(false)
      onDeleted()
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
          <DialogTitle className="text-destructive">ユーザーを削除します</DialogTitle>
          <DialogDescription>
            Cognito からユーザーを削除し、UserTenants は archived に変更されます。UsageLogs は監査のため残ります。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label htmlFor="delete-confirm">
            確認のために <code className="font-mono text-foreground">{user.email}</code> を入力してください
          </Label>
          <Input
            id="delete-confirm"
            autoComplete="off"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder={user.email}
          />
        </div>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            キャンセル
          </Button>
          <Button
            variant="destructive"
            disabled={confirm !== user.email || mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? '削除中…' : '削除を実行'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
