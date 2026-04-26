import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Filter } from 'lucide-react'

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

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

export default function AdminUsageLogs() {
  const [tenantId, setTenantId] = useState('')
  const [userId, setUserId] = useState('')
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')
  const [applied, setApplied] = useState<{
    tenant_id?: string
    user_id?: string
    since?: string
    until?: string
  }>({})
  const [cursor, setCursor] = useState<string | undefined>()
  const [cursorStack, setCursorStack] = useState<Array<string | undefined>>([])

  const logsQuery = useQuery({
    queryKey: ['admin', 'usage-logs', applied, cursor],
    queryFn: () =>
      api.admin.usageLogs({
        ...applied,
        cursor,
        limit: 100,
      }),
    placeholderData: (p) => p,
  })

  const apply = () => {
    setApplied({
      tenant_id: tenantId || undefined,
      user_id: userId || undefined,
      since: since || undefined,
      until: until || undefined,
    })
    setCursor(undefined)
    setCursorStack([])
  }

  const nextCursor = logsQuery.data?.next_cursor ?? null

  return (
    <div className="space-y-6">
      <header>
        <h1 className="font-display text-3xl tracking-tight">全体使用量</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          UsageLogs テーブルの全件をフィルタ付きで確認します。tenant_id 指定時は PK Query、user_id 指定時は GSI Query、いずれも無ければ Scan です (100 件で truncate)。
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="font-sans text-base font-semibold">絞り込み</CardTitle>
          <CardDescription>
            ISO 8601 (<code className="font-mono text-xs">2026-04-01T00:00:00Z</code> 形式) を since / until に入力できます。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3 md:grid-cols-4">
            <div className="space-y-1.5">
              <Label htmlFor="filter-tenant">tenant_id</Label>
              <Input
                id="filter-tenant"
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
                placeholder="default-org"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="filter-user">user_id</Label>
              <Input
                id="filter-user"
                value={userId}
                onChange={(e) => setUserId(e.target.value)}
                placeholder="a4f8..."
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="filter-since">since</Label>
              <Input
                id="filter-since"
                value={since}
                onChange={(e) => setSince(e.target.value)}
                placeholder="2026-04-01T00:00:00Z"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="filter-until">until</Label>
              <Input
                id="filter-until"
                value={until}
                onChange={(e) => setUntil(e.target.value)}
                placeholder="2026-04-30T23:59:59Z"
              />
            </div>
          </div>
          <div className="mt-4 flex gap-2">
            <Button size="sm" onClick={apply}>
              <Filter className="h-4 w-4" />
              絞り込み適用
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setTenantId('')
                setUserId('')
                setSince('')
                setUntil('')
                setApplied({})
                setCursor(undefined)
                setCursorStack([])
              }}
            >
              クリア
            </Button>
          </div>
        </CardContent>
      </Card>

      <div className="border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>日時</TableHead>
              <TableHead>User</TableHead>
              <TableHead>Tenant</TableHead>
              <TableHead>Model</TableHead>
              <TableHead className="text-right">Input</TableHead>
              <TableHead className="text-right">Output</TableHead>
              <TableHead className="text-right">Total</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {logsQuery.isLoading ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center text-muted-foreground">
                  読み込み中…
                </TableCell>
              </TableRow>
            ) : (logsQuery.data?.logs.length ?? 0) === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center text-muted-foreground">
                  該当するログがありません。
                </TableCell>
              </TableRow>
            ) : (
              logsQuery.data!.logs.map((log) => (
                <TableRow key={log.timestamp_log_id}>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                    {formatDate(log.recorded_at)}
                  </TableCell>
                  <TableCell>
                    <div className="text-xs">{log.user_email ?? '(email 無)'}</div>
                    <code className="font-mono text-[10px] text-muted-foreground">
                      {log.user_id}
                    </code>
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {log.tenant_id}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{log.model_id}</TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {fmt(log.input_tokens)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {fmt(log.output_tokens)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs font-semibold">
                    {fmt(log.total_tokens)}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>
          {logsQuery.data?.logs.length ?? 0} 件表示{nextCursor ? ' / 次ページあり' : ''}
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
    </div>
  )
}
