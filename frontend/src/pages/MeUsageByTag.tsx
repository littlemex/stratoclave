import { useTranslation } from 'react-i18next'

import { UsageByTagReport } from '@/components/common/UsageByTagReport'
import { api } from '@/lib/api'

/**
 * The caller's own spend, grouped by the tag they attached to the work.
 *
 * A thin caller. Everything that decides how these numbers should be read lives in
 * `UsageByTagReport`, which every surface showing this report uses — a second rendering would
 * eventually disagree with this one about the disclosures, which is the part that matters.
 *
 * `api.myUsageByTag` takes only a period. There is no tenant and no member to pass, by design on
 * the server side, so this call cannot be steered at somebody else's rows even by a caller who
 * wants to. `memberColumn` is off for the same reason it would be noise: every row is the one
 * reader.
 */
export default function MeUsageByTag() {
  const { t } = useTranslation()

  return (
    <div className="space-y-10">
      <header>
        <p className="text-[11px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          {t('me_usage_by_tag.label')}
        </p>
        <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">
          {t('me_usage_by_tag.title')}
        </h1>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
          {t('me_usage_by_tag.intro')}
        </p>
      </header>

      <UsageByTagReport
        queryScope={['me']}
        fetchReport={({ period }) => api.myUsageByTag(period)}
      />
    </div>
  )
}
