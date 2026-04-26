import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Plus, Search } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { api, type Role, type UserSummary } from '@/lib/api'

const ROLE_LABEL: Record<Role, string> = {
  admin: 'Admin',
  team_lead: 'Team Lead',
  user: 'User',
}

function RoleBadge({ role }: { role: Role }) {
  const variant =
    role === 'admin' ? 'accent' : role === 'team_lead' ? 'default' : 'secondary'
  return <Badge variant={variant}>{ROLE_LABEL[role]}</Badge>
}

function fmt(n: number): string {
  return n.toLocaleString()
}

export default function AdminUsers() {
  const [cursor, setCursor] = useState<string | undefined>(undefined)
  const [cursorStack, setCursorStack] = useState<Array<string | undefined>>([])
  const [roleFilter, setRoleFilter] = useState<Role | ''>('')
  const [search, setSearch] = useState('')

  const usersQuery = useQuery({
    queryKey: ['admin', 'users', cursor, roleFilter],
    queryFn: () =>
      api.admin.listUsers({
        cursor,
        limit: 50,
        role: roleFilter || undefined,
      }),
    placeholderData: (prev) => prev,
  })

  const filtered = useMemo<UserSummary[]>(() => {
    const users = usersQuery.data?.users ?? []
    const q = search.trim().toLowerCase()
    if (!q) return users
    return users.filter((u) => u.email.toLowerCase().includes(q) || u.user_id.toLowerCase().includes(q))
  }, [usersQuery.data, search])

  const nextCursor = usersQuery.data?.next_cursor ?? null

  const goNext = () => {
    if (!nextCursor) return
    setCursorStack((s) => [...s, cursor])
    setCursor(nextCursor)
  }
  const goPrev = () => {
    setCursorStack((s) => {
      const next = [...s]
      const prev = next.pop()
      setCursor(prev)
      return next
    })
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="font-display text-3xl tracking-tight">ユーザー管理</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Cognito User Pool + DynamoDB の Users テーブルを横断的に管理します。
          </p>
        </div>
        <Button asChild>
          <Link to="/admin/users/new">
            <Plus className="h-4 w-4" />
            新規ユーザー
          </Link>
        </Button>
      </header>

      <div className="flex flex-col gap-3 md:flex-row md:items-center">
        <div className="flex items-center gap-2 md:w-64">
          <Search className="h-4 w-4 text-muted-foreground" aria-hidden />
          <Input
            placeholder="email または user_id で絞り込み"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="flex gap-2" role="radiogroup" aria-label="ロール絞り込み">
          <Button
            role="radio"
            aria-checked={roleFilter === ''}
            variant={roleFilter === '' ? 'default' : 'outline'}
            size="sm"
            onClick={() => {
              setRoleFilter('')
              setCursor(undefined)
              setCursorStack([])
            }}
          >
            All
          </Button>
          {(['admin', 'team_lead', 'user'] as Role[]).map((r) => (
            <Button
              key={r}
              role="radio"
              aria-checked={roleFilter === r}
              variant={roleFilter === r ? 'default' : 'outline'}
              size="sm"
              onClick={() => {
                setRoleFilter(r)
                setCursor(undefined)
                setCursorStack([])
              }}
            >
              {ROLE_LABEL[r]}
            </Button>
          ))}
        </div>
      </div>

      <div className="border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Email</TableHead>
              <TableHead>ロール</TableHead>
              <TableHead>所属テナント</TableHead>
              <TableHead className="text-right">残クレジット</TableHead>
              <TableHead className="text-right">使用</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {usersQuery.isLoading ? (
              <TableRow>
                <TableCell colSpan={6} className="text-center text-muted-foreground">
                  読み込み中…
                </TableCell>
              </TableRow>
            ) : filtered.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6} className="text-center text-muted-foreground">
                  該当するユーザーがいません。
                </TableCell>
              </TableRow>
            ) : (
              filtered.map((u) => (
                <TableRow key={u.user_id}>
                  <TableCell>
                    <div className="font-medium">{u.email || '(email 未設定)'}</div>
                    <code className="mt-0.5 block truncate font-mono text-xs text-muted-foreground">
                      {u.user_id}
                    </code>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {u.roles.map((r) => (
                        <RoleBadge key={r} role={r} />
                      ))}
                    </div>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{u.org_id}</TableCell>
                  <TableCell className="text-right font-mono text-sm">
                    {fmt(u.remaining_credit)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs text-muted-foreground">
                    {fmt(u.credit_used)} / {fmt(u.total_credit)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button asChild variant="ghost" size="sm">
                      <Link to={`/admin/users/${encodeURIComponent(u.user_id)}`}>詳細</Link>
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
          {(usersQuery.data?.users.length ?? 0)} 件表示
          {nextCursor ? ' / 次ページあり' : ''}
        </span>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={goPrev}
            disabled={cursorStack.length === 0}
          >
            前へ
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={goNext}
            disabled={!nextCursor}
          >
            次へ
          </Button>
        </div>
      </div>
    </div>
  )
}
