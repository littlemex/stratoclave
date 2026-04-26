import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Building2, Plus } from 'lucide-react'

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

export default function TeamLeadTenants() {
  const tenantsQuery = useQuery({
    queryKey: ['team-lead', 'tenants'],
    queryFn: () => api.teamLead.listTenants(),
  })

  const tenants = tenantsQuery.data?.tenants ?? []

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="font-display text-3xl tracking-tight">所有テナント</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            あなたが所有するテナントのみ表示されます。他のメンバーのテナントは一覧・参照ともにできません。
          </p>
        </div>
        <Button asChild>
          <Link to="/team-lead/tenants/new">
            <Plus className="h-4 w-4" />
            新規テナント
          </Link>
        </Button>
      </header>

      {tenantsQuery.isLoading ? (
        <p className="text-sm text-muted-foreground">読み込み中…</p>
      ) : tenants.length === 0 ? (
        <Card className="border-dashed">
          <CardHeader>
            <div className="flex items-center gap-2 text-muted-foreground">
              <Building2 className="h-5 w-5" aria-hidden />
              <CardTitle className="font-sans text-base font-semibold">
                まだテナントを作成していません
              </CardTitle>
            </div>
            <CardDescription>
              「新規テナント」ボタンから、あなたが所有する最初のテナントを作成してください。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button asChild>
              <Link to="/team-lead/tenants/new">
                <Plus className="h-4 w-4" />
                新規テナント
              </Link>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名前</TableHead>
                <TableHead>tenant_id</TableHead>
                <TableHead className="text-right">default_credit</TableHead>
                <TableHead>状態</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {tenants.map((t) => (
                <TableRow key={t.tenant_id}>
                  <TableCell className="font-medium">{t.name}</TableCell>
                  <TableCell>
                    <code className="font-mono text-xs text-muted-foreground">
                      {t.tenant_id}
                    </code>
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
                      <Link to={`/team-lead/tenants/${encodeURIComponent(t.tenant_id)}`}>
                        詳細
                      </Link>
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Card className="border-primary/30 bg-primary/5">
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">
            Team Lead の権限
          </CardTitle>
          <CardDescription>
            所有するテナントの作成・名前変更・default_credit 変更・メンバーと使用量の閲覧が可能です。
            ユーザーをテナントに紐づける作業は Administrator に依頼する必要があります。
          </CardDescription>
        </CardHeader>
      </Card>
    </div>
  )
}
