import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'

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
import { api } from '@/lib/api'

export default function TeamLeadTenantNew() {
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [name, setName] = useState('')
  const [defaultCredit, setDefaultCredit] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () =>
      api.teamLead.createTenant({
        name: name.trim(),
        default_credit: defaultCredit ? Number(defaultCredit) : undefined,
      }),
    onSuccess: (tenant) => {
      void qc.invalidateQueries({ queryKey: ['team-lead', 'tenants'] })
      navigate(`/team-lead/tenants/${encodeURIComponent(tenant.tenant_id)}`)
    },
    onError: (err: unknown) => {
      const e = err as { status?: number; detail?: string; message?: string } | null
      if (e?.status === 403 && e.detail?.includes('tenant_limit_exceeded')) {
        setError(
          'Team Lead が所有できるテナント数の上限に達しました (50 件)。不要なテナントをアーカイブするか Administrator に相談してください。',
        )
      } else {
        setError(e?.detail ?? e?.message ?? '作成に失敗しました')
      }
    },
  })

  const isValid = name.trim().length > 0

  return (
    <div className="mx-auto max-w-xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="px-0">
        <Link to="/team-lead/tenants">
          <ArrowLeft className="h-4 w-4" />
          所有テナントに戻る
        </Link>
      </Button>

      <div>
        <h1 className="font-display text-3xl tracking-tight">新規テナント</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          あなたが所有者として作成されます。ユーザーの所属紐付けは Administrator に依頼してください。
        </p>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault()
          setError(null)
          if (!isValid) {
            setError('名前が空です。')
            return
          }
          mutation.mutate()
        }}
        className="space-y-5"
      >
        <Card>
          <CardHeader>
            <CardTitle className="font-sans text-base font-semibold">基本情報</CardTitle>
            <CardDescription>
              default_credit はこのテナントに紐づく新規ユーザーの初期クレジットとして適用されます。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="tl-name">名前</Label>
              <Input
                id="tl-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Platform Team"
                required
                autoComplete="off"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="tl-default-credit">default_credit (任意)</Label>
              <Input
                id="tl-default-credit"
                type="number"
                min={0}
                max={10_000_000}
                value={defaultCredit}
                onChange={(e) => setDefaultCredit(e.target.value)}
                placeholder="未入力なら 100,000 tokens"
              />
            </div>
          </CardContent>
        </Card>

        {error ? <p className="text-sm text-destructive">{error}</p> : null}

        <div className="flex justify-end gap-3">
          <Button
            type="button"
            variant="ghost"
            onClick={() => navigate('/team-lead/tenants')}
            disabled={mutation.isPending}
          >
            キャンセル
          </Button>
          <Button type="submit" disabled={!isValid || mutation.isPending}>
            {mutation.isPending ? '作成中…' : 'テナントを作成'}
          </Button>
        </div>
      </form>
    </div>
  )
}
