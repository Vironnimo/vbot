// The app-wide archive retention period: whole days, `null` while automatic
// deletion is off, `undefined` until Settings were read. `unknown` is true
// while vBot cannot read the period from its settings file; it then deletes
// nothing automatically. The App seeds the days from `settings.get` and
// Settings commits; the Archive's entries panel adopts what each
// `archive.list` answer carries, the only answer that reports an unknown
// period, and the retention panel above it says when that pauses automatic
// deletion. Delete dialogs read it to say how long the Archive keeps what
// they move there.
import { t } from './i18n.js';

export const archiveRetention = $state({ days: undefined, unknown: false });

function adoptDays(days) {
  if (days === null || (Number.isInteger(days) && days > 0)) {
    archiveRetention.days = days;
    return true;
  }
  return false;
}

// What an `archive.list` answer says: its period, or that it is unknown.
export function applyArchiveRetention(days, { unknown = false } = {}) {
  if (unknown) {
    archiveRetention.unknown = true;
  } else if (adoptDays(days)) {
    archiveRetention.unknown = false;
  }
}

// The period Settings read or saved. Settings cannot tell whether vBot reads
// it, so an unknown period stays unknown until `archive.list` says otherwise.
export function applyArchiveSettings(archive) {
  if (archive && typeof archive === 'object' && 'retention_days' in archive) {
    adoptDays(archive.retention_days);
  }
}

// The delete dialogs' line about when the Archive deletes the item.
export function archiveDeletionNotice({
  days: retentionDays,
  unknown,
} = archiveRetention) {
  if (unknown) return t('archive.deleteNotice.unknown');
  if (retentionDays === null) return t('archive.deleteNotice.off');
  if (!Number.isInteger(retentionDays)) return t('archive.deleteNotice.kept');
  return retentionDays === 1
    ? t('archive.deleteNotice.oneDay')
    : t('archive.deleteNotice.days', { days: retentionDays });
}
