// What moved with a Skill merged into another one: the server records its shares
// and the automations that triggered it on the merge (`followed`), and both the
// Skill history and a Run's learning changes name them after "Merged into".
import { t } from '$lib/i18n.js';
import { isPlainObject } from '$lib/values.js';

const FOLLOWED_KINDS = new Set(['shared', 'bootstrap', 'cron', 'calendar']);

/** The moved references in words, or '' when nothing moved. */
export function skillFollowedText(followed) {
  const items = (Array.isArray(followed) ? followed : [])
    .filter(
      (reference) =>
        isPlainObject(reference) &&
        FOLLOWED_KINDS.has(reference.kind) &&
        typeof reference.name === 'string',
    )
    .map((reference) =>
      t(`skills.followed.${reference.kind}`, { name: reference.name }),
    );
  return items.length ? t('skills.followed', { items: items.join(', ') }) : '';
}

/** "Merged into <target>", followed by what moved with the Skill. */
export function skillMergedText(text, followed) {
  const moved = skillFollowedText(followed);
  return moved ? `${text} · ${moved}` : text;
}
