import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Info, Lock } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { TempPasswordDialog } from '@/components/admin/TempPasswordDialog'
import { api, type CreateUserResponse, type Role } from '@/lib/api'
import { cn } from '@/lib/utils'

const ROLE_OPTIONS: Array<{
  value: Role
  label: string
  description: string
  lockedReason?: string
}> = [
  {
    value: 'user',
    label: 'User',
    description: 'Messages API と自分のクレジットのみ閲覧可能',
  },
  {
    value: 'team_lead',
    label: 'Team Lead',
    description: '自分が所有する Tenant の管理 + メンバーの使用量閲覧',
  },
  {
    value: 'admin',
    label: 'Administrator',
    description: '全 Tenant・全ユーザー・全 Usage にアクセス可能',
    lockedReason:
      'ALLOW_ADMIN_CREATION=true 環境変数が設定されている場合のみ作成可能 (セキュリティ運用)',
  },
]

export default function AdminUserNew() {
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [email, setEmail] = useState('')
  const [role, setRole] = useState<Role>('user')
  const [tenantId, setTenantId] = useState('')
  const [totalCredit, setTotalCredit] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  const [success, setSuccess] = useState<CreateUserResponse | null>(null)

  const tenantsQuery = useQuery({
    queryKey: ['admin', 'tenants', 'select'],
    queryFn: () => api.admin.listTenants({ limit: 100 }),
  })

  const createMutation = useMutation({
    mutationFn: () => {
      const body = {
        email: email.trim(),
        role,
        tenant_id: tenantId || undefined,
        total_credit: totalCredit ? Number(totalCredit) : undefined,
      }
      return api.admin.createUser(body)
    },
    onSuccess: (resp) => {
      setSuccess(resp)
      void qc.invalidateQueries({ queryKey: ['admin', 'users'] })
    },
    onError: (err: unknown) => {
      const e = err as { status?: number; detail?: string; message?: string } | null
      setFormError(e?.detail ?? e?.message ?? '作成に失敗しました')
    },
  })

  const tenantOptions = useMemo(
    () => tenantsQuery.data?.tenants ?? [],
    [tenantsQuery.data],
  )

  const isValid = email.trim().length > 0 && email.includes('@')

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setFormError(null)
    if (!isValid) {
      setFormError('email が空、または @ を含んでいません。')
      return
    }
    createMutation.mutate()
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/admin/users">
          <ArrowLeft className="h-4 w-4" />
          ユーザー一覧に戻る
        </Link>
      </Button>

      <div>
        <h1 className="font-display text-3xl tracking-tight">新規ユーザー作成</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Cognito AdminCreateUser で一時パスワードを発行します。初回ログイン時に本人がパスワードを更新します。
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-5">
        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">基本情報</CardTitle>
            <CardDescription>
              email は Cognito Username として扱われるため、重複チェックがあります。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="user@example.com"
                autoComplete="off"
                required
              />
            </div>

            <div className="space-y-1.5">
              <Label>ロール</Label>
              <div className="grid gap-2">
                {ROLE_OPTIONS.map((opt) => {
                  const disabled = Boolean(opt.lockedReason)
                  const selected = role === opt.value
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => !disabled && setRole(opt.value)}
                      aria-pressed={selected}
                      aria-disabled={disabled}
                      disabled={disabled}
                      className={cn(
                        'flex items-start gap-3 rounded-md border p-3 text-left transition-colors',
                        selected
                          ? 'border-primary bg-primary/10'
                          : 'border-border hover:border-primary/40',
                        disabled && 'cursor-not-allowed opacity-60 hover:border-border',
                      )}
                    >
                      <span
                        className={cn(
                          'mt-0.5 h-4 w-4 shrink-0 rounded-full border',
                          selected ? 'border-primary bg-primary' : 'border-muted-foreground',
                        )}
                        aria-hidden
                      />
                      <div className="min-w-0 flex-1 space-y-1">
                        <div className="flex items-center gap-2 text-sm font-medium">
                          {opt.label}
                          {disabled ? (
                            <span className="inline-flex items-center gap-1 text-[11px] uppercase tracking-wide text-muted-foreground">
                              <Lock className="h-3 w-3" aria-hidden />
                              作成不可
                            </span>
                          ) : null}
                        </div>
                        <p className="text-xs text-muted-foreground">{opt.description}</p>
                        {disabled && opt.lockedReason ? (
                          <p className="flex items-start gap-1 text-[11px] text-muted-foreground">
                            <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                            {opt.lockedReason}
                          </p>
                        ) : null}
                      </div>
                    </button>
                  )
                })}
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">
              テナントとクレジット
            </CardTitle>
            <CardDescription>
              省略時は default-org、Tenant の default_credit が初期値として適用されます。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="tenant">所属テナント</Label>
              <select
                id="tenant"
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
                className="flex h-10 w-full rounded-md border border-input bg-input px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                <option value="">default-org (省略時)</option>
                {tenantOptions.map((t) => (
                  <option key={t.tenant_id} value={t.tenant_id}>
                    {t.name} ({t.tenant_id})
                  </option>
                ))}
              </select>
              <p className="text-xs text-muted-foreground">
                tenant_id を選択しない場合、default-org に所属しグローバル 100,000 tokens がクレジットとして付与されます。
              </p>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="total-credit">クレジット上書き (任意)</Label>
              <Input
                id="total-credit"
                type="number"
                inputMode="numeric"
                value={totalCredit}
                min={0}
                max={10_000_000}
                onChange={(e) => setTotalCredit(e.target.value)}
                placeholder="未入力なら Tenant の default_credit を使用"
              />
            </div>
          </CardContent>
        </Card>

        {formError ? (
          <p className="text-sm text-destructive">{formError}</p>
        ) : null}

        <div className="flex justify-end gap-3">
          <Button
            type="button"
            variant="ghost"
            onClick={() => navigate('/admin/users')}
            disabled={createMutation.isPending}
          >
            キャンセル
          </Button>
          <Button type="submit" disabled={createMutation.isPending || !isValid}>
            {createMutation.isPending ? '作成中…' : 'ユーザーを作成'}
          </Button>
        </div>
      </form>

      <TempPasswordDialog
        open={success !== null}
        email={success?.email ?? ''}
        temporaryPassword={success?.temporary_password ?? ''}
        onAcknowledge={() => {
          setSuccess(null)
          navigate('/admin/users')
        }}
      />
    </div>
  )
}
