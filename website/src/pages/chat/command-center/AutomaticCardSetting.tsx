import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../../api/client'
import { SettingsToggle } from '../../../components/settings'
import ErrorNotice from '../../../components/ErrorNotice'
import { fmtNumber } from '../../../i18n/format'

/** One explicit gateway-wide cost opt-in; viewing cards never starts model work. */
export default function AutomaticCardSetting({ active }: { active: boolean }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const query = useQuery<{ dashboard?: { dynamic_dashboard_cards?: boolean } }>({
    queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig(), enabled: active,
    staleTime: 30_000, refetchOnWindowFocus: false, retry: false,
  })
  const mutation = useMutation({
    mutationFn: (enabled: boolean) => api.patchConfig('dashboard.dynamic_dashboard_cards', enabled),
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
        qc.invalidateQueries({ queryKey: ['dashboard-card'] }),
      ])
    },
  })
  return <div className="space-y-2">
    <SettingsToggle configKey="dashboard.dynamic_dashboard_cards" label={t('commandCenter.automatic_cards')}
      description={t('commandCenter.card_limits', { minutes: fmtNumber(2), hourly: fmtNumber(60) })}
      checked={query.data?.dashboard?.dynamic_dashboard_cards === true} onChange={value => mutation.mutate(value)}
      disabled={!active || !query.isSuccess || mutation.isPending} />
    {/* No hand-off: native decision cards on this surface may contain unsent answers. */}
    <ErrorNotice message={query.isError || mutation.isError ? t('commandCenter.card_settings_error') : undefined} />
  </div>
}
