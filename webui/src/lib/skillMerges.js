// What was planned to move with a merged Skill: the server records its shares
// and the automations that triggered it on the merge (`followed`), and both the
// Skill history and a Run's learning changes name them after "Merged into".
import { t } from '$lib/i18n.js';
import { isPlainObject } from '$lib/values.js';

// One planned reference move in words, or '' for an unknown kind.
function followedReferenceText(reference) {
  const { name } = reference;
  switch (reference.kind) {
    case 'shared':
      return t('skills.followed.shared', { name });
    case 'bootstrap':
      return t('skills.followed.bootstrap', { name });
    case 'cron':
      return t('skills.followed.cron', { name });
    case 'calendar':
      return t('skills.followed.calendar', { name });
    default:
      return '';
  }
}

/** Planned reference moves in words, or '' when none were recorded. */
export function skillFollowedText(followed) {
  const items = (Array.isArray(followed) ? followed : [])
    .filter(
      (reference) =>
        isPlainObject(reference) && typeof reference.name === 'string',
    )
    .map(followedReferenceText)
    .filter(Boolean);
  return items.length ? t('skills.followed', { items: items.join(', ') }) : '';
}

/** "Merged into <target>", followed by the planned reference moves. */
export function skillMergedText(text, followed) {
  const planned = skillFollowedText(followed);
  return planned ? `${text} · ${planned}` : text;
}
