import { t, activeLocaleTag } from '@vbot/extension-sdk/lib/i18n.js';
import { formatDateTimeInApplicationZone } from '@vbot/extension-sdk/lib/dateTimePrefs.svelte.js';

export const copy = (value) => JSON.parse(JSON.stringify(value));

export function failureText(cause) {
  switch (cause?.code) {
    case 'factory_unavailable':
      return t('factory.error.unavailable');
    case 'project_not_found':
      return t('factory.error.projectMissing');
    case 'invalid_job':
      return cause?.message || t('factory.error.invalidJob');
    case 'invalid_schedule':
      return cause?.message || t('factory.error.invalidSchedule');
    case 'revision_conflict':
    case 'factory_conflict':
    case 'stale_revision':
      return t('factory.error.conflict');
    case 'factory_archived':
      return t('factory.error.factoryArchived');
    case 'member_unavailable':
      return cause?.message || t('factory.error.memberUnavailable');
    case 'trigger_not_found':
      return t('factory.error.triggerMissing');
    case 'triggers_unavailable':
      return t('factory.error.triggersUnavailable');
    case 'missing_field':
    case 'missing_text':
      return t('factory.error.required');
    case 'not_found':
    case 'factory_not_found':
      return t('factory.error.notFound');
    default:
      return cause?.message || t('factory.error.requestFailed');
  }
}

export const projectName = (catalog, id) =>
  catalog?.projects?.find((project) => project.id === id)?.name || id;

export function timestamp(value) {
  return value
    ? formatDateTimeInApplicationZone(value, activeLocaleTag(), {
        dateStyle: 'medium',
        timeStyle: 'short',
      })
    : '';
}

export function runStateLabel(state) {
  switch (state) {
    case 'queued':
      return t('factory.run.status.queued');
    case 'running':
      return t('factory.run.status.running');
    case 'needs_attention':
      return t('factory.run.status.needs_attention');
    case 'done':
      return t('factory.run.status.done');
    case 'failed':
      return t('factory.run.status.failed');
    case 'cancelled':
      return t('factory.run.status.cancelled');
    default:
      return state ?? '';
  }
}

export function runOriginLabel(origin) {
  switch (origin) {
    case 'manual':
      return t('factory.run.origin.manual');
    case 'leader':
      return t('factory.run.origin.leader');
    case 'schedule':
      return t('factory.run.origin.schedule');
    case 'webhook':
      return t('factory.run.origin.webhook');
    case 'public_tool':
      return t('factory.run.origin.public_tool');
    default:
      return origin ?? '';
  }
}

export function memberLabel(members, memberId) {
  if (!memberId) return t('factory.jobs.leaderDefault');
  const member = (members || []).find((item) => item.id === memberId);
  if (!member) return memberId;
  return (
    member.agent_name ||
    member.name ||
    member.role ||
    member.agent_id ||
    memberId
  );
}

export function hasEnabledTrigger(job) {
  return Boolean(
    job?.enabled &&
    (job.triggers || []).some((trigger) => trigger.enabled !== false),
  );
}
