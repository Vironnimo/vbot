import { t } from '../../../../webui/src/lib/i18n.js';

// How the page words a failed Swarm operation or bridge call. A Swarm
// operation fails with a stable code as its message (`SwarmStoreError.code`),
// which the page replaces with its own text. Other messages, such as the
// host's and the Wiki's, are written for people and show unchanged.

// The codes a user can run into on this page, each with its own text.
const CODE_TEXTS = {
  invalid_arguments: () => t('swarm.error.invalid_arguments'),
  message_not_found: () => t('swarm.error.message_not_found'),
  participant_not_resumable: () => t('swarm.error.participant_not_resumable'),
  profile_not_found: () => t('swarm.error.profile_not_found'),
  revision_conflict: () => t('swarm.error.revision_conflict'),
  slug_unavailable: () => t('swarm.error.slug_unavailable'),
  swarm_closed: () => t('swarm.error.swarm_closed'),
  swarm_not_found: () => t('swarm.error.swarm_not_found'),
  swarm_not_stopped: () => t('swarm.error.swarm_not_stopped'),
};

// The other codes the Swarm Store raises: races, stale paging and checks of
// participants' Tool calls, which the page does not bring about itself. They
// share one text.
const UNEXPECTED_CODES = new Set([
  'before_discussion_mismatch',
  'delivery_unacknowledged',
  'discussion_not_found',
  'execution_epoch_conflict',
  'inapplicable_field',
  'invalid_cursor',
  'invalid_lifecycle_state',
  'invalid_recipient',
  'main_membership_required',
  'participant_binding_conflict',
  'participant_inactive',
  'participant_not_found',
  'participant_unbound',
  'receipt_conflict',
  'reply_discussion_mismatch',
  'request_conflict',
  'scope_mismatch',
  'stale_epoch',
  'stale_run',
  'wake_unavailable',
]);

// The text the page shows for a failed request `cause`.
export function failureText(cause) {
  const message = typeof cause?.message === 'string' ? cause.message : '';
  if (Object.hasOwn(CODE_TEXTS, message)) return CODE_TEXTS[message]();
  if (!message.trim() || UNEXPECTED_CODES.has(message))
    return t('swarm.error.unexpected');
  return message;
}
