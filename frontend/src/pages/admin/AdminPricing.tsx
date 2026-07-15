import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

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
import { fmtMicroUsdRate } from '@/lib/money'

/**
 * Read-only view of the effective pricing table (#66): the per-pricing-key
 * dollar rates the backend actually charges against (built-in defaults overlaid
 * with any admin overrides), which models map to each key, and whether a key is
 * a default or an override. Editing rates is deferred (P2); this is visibility.
 */
export default function AdminPricing() {
  const { t } = useTranslation()
  const pricing = useQuery({
    queryKey: ['admin-pricing-config'],
    queryFn: () => api.admin.pricingConfig(),
  })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{t('admin_pricing.title')}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{t('admin_pricing.intro')}</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t('admin_pricing.card_title')}</CardTitle>
          <CardDescription>
            {pricing.data
              ? pricing.data.version
                ? t('admin_pricing.version', { version: pricing.data.version })
                : t('admin_pricing.version_defaults')
              : ''}
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {pricing.isLoading ? (
            <p className="p-6 text-sm text-muted-foreground">
              {t('common.loading_ellipsis')}
            </p>
          ) : pricing.error || !pricing.data ? (
            <p className="p-6 text-sm text-destructive">{t('admin_pricing.load_error')}</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('admin_pricing.col_key')}</TableHead>
                  <TableHead>{t('admin_pricing.col_models')}</TableHead>
                  <TableHead className="text-right">{t('admin_pricing.col_input')}</TableHead>
                  <TableHead className="text-right">{t('admin_pricing.col_output')}</TableHead>
                  <TableHead className="text-right">
                    {t('admin_pricing.col_cache_read')}
                  </TableHead>
                  <TableHead className="text-right">
                    {t('admin_pricing.col_cache_write')}
                  </TableHead>
                  <TableHead>{t('admin_pricing.col_source')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pricing.data.rates.map((r) => (
                  <TableRow key={r.pricing_key}>
                    <TableCell className="font-mono text-xs">{r.pricing_key}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {r.models.join(', ') || '—'}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {fmtMicroUsdRate(r.input_per_mtok_microusd)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {fmtMicroUsdRate(r.output_per_mtok_microusd)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {fmtMicroUsdRate(r.cache_read_per_mtok_microusd)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {fmtMicroUsdRate(r.cache_write_per_mtok_microusd)}
                    </TableCell>
                    <TableCell>
                      {r.source === 'override' ? (
                        <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-semibold text-blue-800">
                          {t('admin_pricing.source_override')}
                        </span>
                      ) : (
                        <span className="text-[10px] text-muted-foreground">
                          {t('admin_pricing.source_default')}
                        </span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
