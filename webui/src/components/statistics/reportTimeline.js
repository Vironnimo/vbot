import { t } from '$lib/i18n.js';
import { formatActivityDate } from '$lib/statisticsView.js';

export function rangeLabel(range) {
  return t(`statistics.range.${range}`);
}

export function activityWindowLabel(reportRange, granularity) {
  if (reportRange !== 'all') return rangeLabel(reportRange);
  return t(`statistics.overview.activityWindow.${granularity}`);
}

export function activityPeriodLabel(
  dateKey,
  granularity,
  locale,
  long = false,
) {
  const formatted = formatActivityDate(dateKey, granularity, locale, {
    long,
  });
  return granularity === 'week' && long
    ? t('statistics.overview.weekOf', { date: formatted })
    : formatted;
}

export function activityHeight(value, total) {
  return total > 0 ? `${(Math.max(0, value) / total) * 100}%` : '0%';
}
