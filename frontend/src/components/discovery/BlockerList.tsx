import { useTranslation } from 'react-i18next'

import { Badge } from '@/components/ui/badge'
import type { DiscoveryBlocker } from '@/lib/api'

/**
 * Every blocker on a discovered record, evidence included -- rendering only
 * `type` tells a reader THAT something blocks the record but not WHY. Pure
 * and read-only: shared verbatim between the admin discovery screens and
 * the team_lead read-only view, since neither carries any action here to
 * keep structurally apart from the other.
 */
export function BlockerList({ blockers }: { blockers: DiscoveryBlocker[] }) {
  const { t } = useTranslation()
  if (blockers.length === 0) {
    return (
      <span className="text-xs text-muted-foreground">{t('discovery_common.no_blockers')}</span>
    )
  }
  return (
    <ul className="space-y-1.5">
      {blockers.map((b, i) => (
        <li key={`${b.type}-${b.subtype}-${i}`} className="text-xs">
          <Badge variant="outline" className="mr-1.5">
            {b.type}/{b.subtype}
          </Badge>
          <span className="text-muted-foreground">{b.evidence}</span>
        </li>
      ))}
    </ul>
  )
}
