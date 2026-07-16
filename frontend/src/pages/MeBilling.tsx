import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Receipt } from 'lucide-react'

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
import { fmtMicroUsd } from '@/lib/money'

/**
 * Per-run billing detail (L5-d). The caller looks up one workflow run id and
 * sees the charge breakdown frozen on the credit ledger. This is the TENANT
 * view: `api.runBilling` runs the `assertNoCostLeak` backstop, so provider cost
 * / margin can never reach this component even if the API regressed.
 */
export default function MeBilling() {
  const [runId, setRunId] = useState('')
  const [submitted, setSubmitted] = useState('')

  const q = useQuery({
    queryKey: ['me', 'billing', 'run', submitted],
    queryFn: () => api.runBilling(submitted),
    enabled: submitted.length > 0,
    retry: false,
  })

  return (
    <div className="space-y-8">
      <header>
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          Billing
        </p>
        <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">
          Run charge breakdown
        </h1>
        <p className="mt-2 max-w-xl text-sm text-muted-foreground">
          Look up the frozen charge for one workflow run id
          (<code>x-sc-workflow-run-id</code>).
        </p>
      </header>

      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          setSubmitted(runId.trim())
        }}
      >
        <input
          aria-label="workflow run id"
          className="flex-1 border border-border bg-card px-3 py-2 text-sm"
          placeholder="run id"
          value={runId}
          onChange={(e) => setRunId(e.target.value)}
        />
        <button
          type="submit"
          className="border border-border bg-primary px-4 py-2 text-sm text-primary-foreground"
        >
          Look up
        </button>
      </form>

      {q.isError && (
        <p className="text-sm text-destructive" data-testid="billing-error">
          Run not found.
        </p>
      )}

      {q.data && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Receipt className="h-4 w-4" /> Run {q.data.run_id}
            </CardTitle>
            <CardDescription>
              Total charged:{' '}
              <span data-testid="total-charged">
                {fmtMicroUsd(q.data.total_settled_microusd)}
              </span>
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Event</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead>Pricing version</TableHead>
                  <TableHead className="text-right">Charged</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {q.data.events.map((ev, i) => (
                  <TableRow key={i}>
                    <TableCell>{ev.event_type}</TableCell>
                    <TableCell>{ev.model_id ?? '—'}</TableCell>
                    <TableCell>{ev.pricing_version ?? '—'}</TableCell>
                    <TableCell className="text-right">
                      {fmtMicroUsd(ev.settled_microusd)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
