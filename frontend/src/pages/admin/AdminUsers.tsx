import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
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

function RoleBadge({ role }: { role: Role }) {
  const { t } = useTranslation()
  const variant =
    role === 'admin' ? 'accent' : role === 'team_lead' ? 'default' : 'secondary'
  return <Badge variant={variant}>{t(`role.${role}`)}</Badge>
}

function fmt(n: number): string {
  return n.toLocaleString()
}

export default function AdminUsers() {
  const { t } = useTranslation()
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
    return users.filter(
      (u) =>
        u.email.toLowerCase().includes(q) ||
        u.user_id.toLowerCase().includes(q),
    )
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
          <h1 className="font-display text-3xl tracking-tight">
            {t('admin_users.title')}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {t('admin_users.intro')}
          </p>
        </div>
        <Button asChild>
          <Link to="/admin/users/new">
            <Plus className="h-4 w-4" />
            {t('admin_users.new')}
          </Link>
        </Button>
      </header>

      <div className="flex flex-col gap-3 md:flex-row md:items-center">
        <div className="flex items-center gap-2 md:w-64">
          <Search className="h-4 w-4 text-muted-foreground" aria-hidden />
          <Input
            placeholder={t('admin_users.search_placeholder')}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div
          className="flex gap-2"
          role="radiogroup"
          aria-label={t('admin_users.role_filter_aria')}
        >
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
            {t('common.all')}
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
              {t(`role.${r}`)}
            </Button>
          ))}
        </div>
      </div>

      <div className="border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t('admin_users.col_email')}</TableHead>
              <TableHead>{t('admin_users.col_role')}</TableHead>
              <TableHead>{t('admin_users.col_tenant')}</TableHead>
              <TableHead className="text-right">{t('admin_users.col_remaining')}</TableHead>
              <TableHead className="text-right">{t('admin_users.col_usage')}</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {usersQuery.isLoading ? (
              <TableRow>
                <TableCell
                  colSpan={6}
                  className="text-center text-muted-foreground"
                >
                  {t('common.loading_ellipsis')}
                </TableCell>
              </TableRow>
            ) : filtered.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={6}
                  className="text-center text-muted-foreground"
                >
                  {t('admin_users.row_empty')}
                </TableCell>
              </TableRow>
            ) : (
              filtered.map((u) => (
                <TableRow key={u.user_id}>
                  <TableCell>
                    <div className="font-medium">
                      {u.email || t('common.email_unset')}
                    </div>
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
                  <TableCell className="text-xs text-muted-foreground">
                    {u.org_id}
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm">
                    {fmt(u.remaining_credit)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs text-muted-foreground">
                    {fmt(u.credit_used)} / {fmt(u.total_credit)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button asChild variant="ghost" size="sm">
                      <Link
                        to={`/admin/users/${encodeURIComponent(u.user_id)}`}
                      >
                        {t('common.details')}
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
          {t('admin_users.count_showing', {
            shown: usersQuery.data?.users.length ?? 0,
          })}
          {nextCursor ? t('admin_users.count_more') : ''}
        </span>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={goPrev}
            disabled={cursorStack.length === 0}
          >
            {t('common.prev')}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={goNext}
            disabled={!nextCursor}
          >
            {t('common.next')}
          </Button>
        </div>
      </div>
    </div>
  )
}
