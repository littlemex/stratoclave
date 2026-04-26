import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  ArrowRight,
  Building2,
  Coins,
  Fingerprint,
  Key,
  KeyRound,
  ShieldCheck,
  Users,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { usePermissions } from '@/hooks/usePermissions'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { UserRole } from '@/types/auth'

const ROLE_LABEL: Record<UserRole, string> = {
  admin: 'Administrator',
  team_lead: 'Team Lead',
  user: 'User',
}

function formatNumber(n: number): string {
  return n.toLocaleString()
}

export default function Dashboard() {
  const { isAdmin, isTeamLead } = usePermissions()
  const meQuery = useQuery({
    queryKey: ['me'],
    queryFn: () => api.me(),
  })

  const me = meQuery.data
  const remainingPct =
    me && me.total_credit > 0
      ? Math.max(
          0,
          Math.min(100, Math.round((me.remaining_credit / me.total_credit) * 100)),
        )
      : 0

  return (
    <div className="space-y-12">
      <section className="space-y-3">
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          ダッシュボード
        </p>
        <h1 className="font-display text-4xl font-semibold tracking-tight">
          {me?.email ? (
            <>
              ようこそ、
              <span className="text-primary">{me.email}</span>
            </>
          ) : (
            'ようこそ'
          )}
        </h1>
        <p className="max-w-2xl text-sm text-muted-foreground">
          クレジット残高・所属テナント・ロールを俯瞰します。詳細は上部ナビゲーションから各セクションへ進んでください。
        </p>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <StatCard
          label="クレジット残量"
          icon={<Coins className="h-3.5 w-3.5" aria-hidden />}
        >
          <div className="flex items-baseline gap-2">
            <span className="strato-stat font-display text-4xl font-semibold tracking-tight">
              {me ? formatNumber(me.remaining_credit) : '—'}
            </span>
            <span className="text-xs text-muted-foreground">tokens</span>
          </div>
          {me ? (
            <div className="mt-4 space-y-2">
              <div className="h-1 w-full overflow-hidden bg-muted">
                <div
                  className={cn(
                    'h-full transition-all',
                    remainingPct > 20 ? 'bg-primary' : 'bg-destructive',
                  )}
                  style={{ width: `${remainingPct}%` }}
                />
              </div>
              <p className="font-mono text-[11px] tracking-wide text-muted-foreground">
                {formatNumber(me.credit_used)} / {formatNumber(me.total_credit)} ·{' '}
                {remainingPct}% 残
              </p>
            </div>
          ) : null}
        </StatCard>

        <StatCard
          label="所属テナント"
          icon={<Building2 className="h-3.5 w-3.5" aria-hidden />}
        >
          <div className="font-display text-2xl font-semibold tracking-tight">
            {me?.tenant?.name ?? me?.tenant?.tenant_id ?? '—'}
          </div>
          <p className="mt-3 font-mono text-[11px] text-muted-foreground">
            tenant_id ·{' '}
            <span className="text-foreground/70">
              {me?.tenant?.tenant_id ?? '—'}
            </span>
          </p>
        </StatCard>

        <StatCard
          label="ロール"
          icon={<ShieldCheck className="h-3.5 w-3.5" aria-hidden />}
        >
          <div className="flex flex-wrap gap-1.5">
            {(me?.roles ?? []).map((r) => (
              <Badge
                key={r}
                variant={r === 'admin' ? 'accent' : 'secondary'}
              >
                {ROLE_LABEL[r] ?? r}
              </Badge>
            ))}
            {!me || me.roles.length === 0 ? (
              <Badge variant="muted">未設定</Badge>
            ) : null}
          </div>
          <p className="mt-3 text-[11px] text-muted-foreground">
            権限は DynamoDB Users テーブルを真実源に決定されます。
          </p>
        </StatCard>
      </section>

      <section className="space-y-4">
        <div className="flex items-baseline justify-between">
          <h2 className="font-display text-xl font-semibold tracking-tight">
            ショートカット
          </h2>
          <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
            quick actions
          </span>
        </div>
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          <ShortcutCard
            to="/me/usage"
            icon={<Fingerprint className="h-4 w-4" aria-hidden />}
            title="自分の使用履歴"
            description="モデル別・日別のトークン消費を確認します。"
          />
          <ShortcutCard
            to="/me/api-keys"
            icon={<Key className="h-4 w-4" aria-hidden />}
            title="API キー"
            description="cowork など外部ゲートウェイクライアント用の長期 API キーを発行・管理します。"
          />
          {isAdmin ? (
            <ShortcutCard
              to="/admin/users"
              icon={<Users className="h-4 w-4" aria-hidden />}
              title="ユーザー管理"
              description="新規ユーザー発行・テナント紐付け・クレジット調整を行います。"
            />
          ) : null}
          {isAdmin ? (
            <ShortcutCard
              to="/admin/tenants"
              icon={<Building2 className="h-4 w-4" aria-hidden />}
              title="テナント管理"
              description="全テナントの一覧・オーナー再割当・使用量を確認します。"
            />
          ) : null}
          {isAdmin ? (
            <ShortcutCard
              to="/admin/trusted-accounts"
              icon={<KeyRound className="h-4 w-4" aria-hidden />}
              title="SSO 信頼アカウント"
              description="AWS Account 単位の SSO 受入ポリシーと事前招待を管理します。"
            />
          ) : null}
          {isTeamLead ? (
            <ShortcutCard
              to="/team-lead/tenants"
              icon={<Building2 className="h-4 w-4" aria-hidden />}
              title="所有テナント"
              description="自分が所有するテナントの作成・メンバー確認・使用量閲覧を行います。"
            />
          ) : null}
        </div>
      </section>

      {meQuery.isError ? (
        <p className="border border-destructive/40 bg-destructive/10 px-4 py-2 text-sm text-destructive-foreground">
          ユーザー情報の取得に失敗しました。しばらく待ってから再読み込みしてください。
        </p>
      ) : null}
    </div>
  )
}

function StatCard({
  label,
  icon,
  children,
}: {
  label: string
  icon?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <Card>
      <CardHeader className="space-y-0 pb-3">
        <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground">
          {icon}
          <span>{label}</span>
        </div>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

function ShortcutCard({
  to,
  icon,
  title,
  description,
}: {
  to: string
  icon: React.ReactNode
  title: string
  description: string
}) {
  return (
    <Card className="group transition-[border-color,background-color] hover:border-primary/40">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2 text-muted-foreground">
          {icon}
          <CardTitle className="font-sans text-base font-semibold text-foreground">
            {title}
          </CardTitle>
        </div>
        <CardDescription className="mt-1 text-xs">
          {description}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Button
          asChild
          variant="ghost"
          size="sm"
          className="px-0 text-primary"
        >
          <Link to={to}>
            開く <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
          </Link>
        </Button>
      </CardContent>
    </Card>
  )
}
