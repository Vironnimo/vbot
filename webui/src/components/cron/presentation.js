import { t } from '$lib/i18n.js';
import {
  CRON_STATUS_ACTIVE,
  CRON_SCHEDULE_TYPE_INTERVAL,
  CRON_SCHEDULE_TYPE_ONCE,
  CRON_STATUS_COMPLETED,
  CRON_STATUS_MISSED,
  describeCronExpression,
} from '$lib/cronView.js';

export function displayValue(value) {
  return value || t('cron.notAvailable', '—');
}

export function listNextRun(job) {
  if (!job?.next_fire_at_display) {
    return t('cron.list.noNextRun', 'No next Run');
  }

  return t('cron.list.nextRun', 'Next · {time}', {
    time: job.next_fire_at_display,
  });
}

export function scheduleKindLabel(job) {
  if (job?.schedule_type === CRON_SCHEDULE_TYPE_ONCE) {
    return t('cron.detail.kind.once', 'One-time schedule');
  }

  return t('cron.detail.kind.recurring', 'Recurring schedule');
}

export function scheduleSummary(job) {
  if (!job) {
    return t('cron.notAvailable', '—');
  }

  if (job.schedule_type === CRON_SCHEDULE_TYPE_ONCE) {
    return t('cron.form.scheduleType.once', 'Once');
  }
  if (job.schedule_type === CRON_SCHEDULE_TYPE_INTERVAL) {
    return displayValue(job.schedule_description);
  }

  return (
    describeCronExpression(job.cron_expression) ||
    displayValue(job.schedule_description)
  );
}

export function scheduleTechnicalValue(job) {
  if (job?.schedule_type === CRON_SCHEDULE_TYPE_ONCE) {
    return displayValue(job.run_at ? job.schedule_description : '');
  }
  if (job?.schedule_type === CRON_SCHEDULE_TYPE_INTERVAL) {
    return displayValue(job.schedule_description);
  }

  return displayValue(job?.cron_expression);
}

export function sessionSummary(job) {
  return job?.session_id
    ? job.session_id
    : t('cron.detail.newSessionEachRun', 'New Session each Run');
}

export function lastResultSupport(job) {
  if (!job?.last_completed_at_display) {
    return t('cron.detail.waitingForFirstRun', 'Waiting for first execution');
  }

  return job.last_completed_at_display;
}

export function remainingRunsLabel(job) {
  return job?.remaining_runs === null
    ? t('cron.detail.unlimited', 'Unlimited')
    : String(job?.remaining_runs ?? 0);
}

export function statusLabel(status) {
  if (status === CRON_STATUS_ACTIVE) {
    return t('cron.status.active', 'Active');
  }

  if (status === 'paused') {
    return t('cron.status.paused', 'Paused');
  }

  if (status === 'failed') {
    return t('cron.status.failed', 'Failed');
  }

  if (status === CRON_STATUS_MISSED) {
    return t('cron.status.missed', 'Missed');
  }

  return t('cron.status.completed', 'Completed');
}

export function statusChipVariant(job) {
  if (
    job.status === CRON_STATUS_ACTIVE &&
    job.last_outcome !== 'failed' &&
    job.last_outcome !== 'cancelled'
  ) {
    return 'success';
  }

  if (job.status === 'paused' || job.status === CRON_STATUS_MISSED) {
    return 'warn';
  }

  if (
    job.status === 'failed' ||
    job.last_outcome === 'failed' ||
    job.last_outcome === 'cancelled'
  ) {
    return 'error';
  }

  return 'neutral';
}

export function outcomeLabel(outcome) {
  if (outcome === 'success') {
    return t('cron.outcome.success', 'Succeeded');
  }
  if (outcome === 'failed') {
    return t('cron.outcome.failed', 'Failed');
  }
  if (outcome === 'cancelled') {
    return t('cron.outcome.cancelled', 'Cancelled');
  }
  if (outcome === 'missed') {
    return t('cron.outcome.missed', 'Missed');
  }
  if (outcome === 'unknown') {
    return t('cron.outcome.unknown', 'Outcome unknown after restart');
  }
  return t('cron.notAvailable', '—');
}

export function isTerminalJob(job) {
  return (
    job?.status === CRON_STATUS_COMPLETED || job?.status === CRON_STATUS_MISSED
  );
}
