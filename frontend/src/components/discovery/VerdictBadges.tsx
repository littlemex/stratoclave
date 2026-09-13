import { Badge } from '@/components/ui/badge'

/**
 * One badge per invocation this candidate carries a verdict entry for
 * ("unverified" is itself a state, never an absent key). Pure and
 * read-only -- shared between the admin candidates list and the
 * team_lead read-only view.
 */
export function VerdictBadges({
  verdicts,
}: {
  verdicts: Record<string, { invocation: string; state: string }>
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {Object.values(verdicts).map((v) => (
        <Badge
          key={v.invocation}
          variant={
            v.state === 'verified' ? 'accent' : v.state === 'invalidated' ? 'destructive' : 'muted'
          }
        >
          {v.invocation}: {v.state}
        </Badge>
      ))}
    </div>
  )
}
