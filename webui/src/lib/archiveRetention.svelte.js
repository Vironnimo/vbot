// The app-wide archive retention period: whole days, `null` while automatic
// deletion is off, `undefined` until Settings were read. The App seeds it from
// `settings.get` and Settings commits; the Archive view adopts the period each
// `archive.list` answer carries. Delete dialogs read it to say when the
// Archive deletes what they move there.
import { t } from './i18n.js';

export const archiveRetention = $state({ days: undefined });

export function applyArchiveRetention(days) {
  if (days === null || (Number.isInteger(days) && days > 0)) {
    archiveRetention.days = days;
  }
}

export function applyArchiveSettings(archive) {
  if (archive && typeof archive === 'object' && 'retention_days' in archive) {
    applyArchiveRetention(archive.retention_days);
  }
}

// The delete dialogs' line about when the Archive deletes the item.
export function archiveDeletionNotice(retentionDays = archiveRetention.days) {
  if (retentionDays === null) return t('archive.deleteNotice.off');
  if (!Number.isInteger(retentionDays)) return t('archive.deleteNotice.kept');
  return retentionDays === 1
    ? t('archive.deleteNotice.oneDay')
    : t('archive.deleteNotice.days', { days: retentionDays });
}
