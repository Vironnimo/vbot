import { activeLocaleTag, t } from '$lib/i18n.js';
import {
  CRON_FREQUENCY_CUSTOM,
  CRON_FREQUENCY_DAILY,
  CRON_FREQUENCY_HOURLY,
  CRON_FREQUENCY_INTERVAL,
  CRON_FREQUENCY_MONTHLY,
  CRON_FREQUENCY_ONCE,
  CRON_FREQUENCY_WEEKLY,
  CRON_STATUS_ACTIVE,
  CRON_SCHEDULE_TYPE_INTERVAL,
  CRON_SCHEDULE_TYPE_ONCE,
  CRON_STATUS_COMPLETED,
  CRON_STATUS_MISSED,
  CRON_WEEKDAYS,
  cronScheduleFields,
  describeCronExpression,
} from '$lib/cronView.js';

const DAY_MS = 86400000;

export function displayValue(value) {
  return value || t('cron.notAvailable');
}

export function frequencyOptions() {
  return [
    { value: CRON_FREQUENCY_ONCE, label: t('cron.frequency.once') },
    { value: CRON_FREQUENCY_INTERVAL, label: t('cron.frequency.interval') },
    { value: CRON_FREQUENCY_HOURLY, label: t('cron.frequency.hourly') },
    { value: CRON_FREQUENCY_DAILY, label: t('cron.frequency.daily') },
    { value: CRON_FREQUENCY_WEEKLY, label: t('cron.frequency.weekly') },
    { value: CRON_FREQUENCY_MONTHLY, label: t('cron.frequency.monthly') },
    { value: CRON_FREQUENCY_CUSTOM, label: t('cron.frequency.custom') },
  ];
}

export function intervalUnitOptions() {
  return [
    { value: 'minutes', label: t('cron.form.intervalUnit.minutes') },
    { value: 'hours', label: t('cron.form.intervalUnit.hours') },
    { value: 'days', label: t('cron.form.intervalUnit.days') },
  ];
}

// Monday-first weekday toggles: cron day number, short label, full name.
export function weekdayOptions() {
  return CRON_WEEKDAYS.map((day) => ({
    day,
    short: weekdayName(day, 'short'),
    long: weekdayName(day, 'long'),
  }));
}

// A readable sentence for a cron expression. Planner shapes get their own
// wording; anything else falls back to the generic description, then to the
// expression itself.
export function describeCron(expression) {
  const fields = cronScheduleFields(expression);
  if (fields.frequency === CRON_FREQUENCY_HOURLY) {
    return t('cron.schedule.hourly', {
      minute: fields.minute.padStart(2, '0'),
    });
  }
  if (fields.frequency === CRON_FREQUENCY_DAILY) {
    return t('cron.schedule.daily', { time: fields.time });
  }
  if (fields.frequency === CRON_FREQUENCY_WEEKLY) {
    return describeWeekly(fields.weekdays, fields.time);
  }
  if (fields.frequency === CRON_FREQUENCY_MONTHLY) {
    return t('cron.schedule.monthly', {
      day: fields.month_day,
      time: fields.time,
    });
  }
  return describeCronExpression(expression) || String(expression ?? '').trim();
}

export function describeInterval(seconds) {
  if (!Number.isInteger(seconds) || seconds <= 0) {
    return '';
  }
  if (seconds % 86400 === 0) {
    const count = seconds / 86400;
    return count === 1
      ? t('cron.schedule.everyDay')
      : t('cron.schedule.everyDays', { count });
  }
  if (seconds % 3600 === 0) {
    const count = seconds / 3600;
    return count === 1
      ? t('cron.schedule.everyHour')
      : t('cron.schedule.everyHours', { count });
  }
  const count = Math.round(seconds / 60);
  return count === 1
    ? t('cron.schedule.everyMinute')
    : t('cron.schedule.everyMinutes', { count });
}

export function scheduleSummary(job) {
  if (!job) {
    return t('cron.notAvailable');
  }

  if (job.schedule_type === CRON_SCHEDULE_TYPE_ONCE) {
    return t('cron.schedule.once');
  }
  if (job.schedule_type === CRON_SCHEDULE_TYPE_INTERVAL) {
    return displayValue(describeInterval(job.interval_seconds));
  }

  return displayValue(describeCron(job.cron_expression));
}

// The exact value behind the readable cadence: the cron expression, or the
// one-time instant. Intervals have nothing further to show.
export function scheduleTechnicalValue(job) {
  if (job?.schedule_type === CRON_SCHEDULE_TYPE_ONCE) {
    return job.run_at ? job.schedule_description : '';
  }
  if (job?.schedule_type === CRON_SCHEDULE_TYPE_INTERVAL) {
    return '';
  }

  return job?.cron_expression ?? '';
}

// The second line of a list row. An active schedule shows when it runs next;
// every other status leads with its name, followed by the cadence or, for
// finished one-time history, when it ran.
export function listRowDetail(job, timezone, now = new Date()) {
  if (job?.status === CRON_STATUS_ACTIVE) {
    return job.next_fire_at
      ? t('cron.list.next', {
          time: compactTimestamp(job.next_fire_at, timezone, now),
        })
      : scheduleSummary(job);
  }

  const status = statusLabel(job?.status);
  let detail = scheduleSummary(job);
  if (isTerminalJob(job)) {
    const at =
      job.status === CRON_STATUS_COMPLETED
        ? job.last_completed_at || job.last_fired_at
        : job.run_at;
    detail = at ? compactTimestamp(at, timezone, now) : '';
  }
  return detail ? t('cron.list.statusDetail', { status, detail }) : status;
}

// A short, list-friendly time in the schedule timezone: Today/Tomorrow/
// Yesterday with the time, a weekday within the coming week, otherwise the
// date (with the time only for upcoming instants of the current year).
export function compactTimestamp(value, timezone, now = new Date()) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  const zone = timezone || 'UTC';
  const time = formatInZone(date, zone, {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  });
  const dayOffset = Math.round(
    (zonedDayStart(date, zone) - zonedDayStart(now, zone)) / DAY_MS,
  );
  if (dayOffset === 0) return t('cron.time.today', { time });
  if (dayOffset === 1) return t('cron.time.tomorrow', { time });
  if (dayOffset === -1) return t('cron.time.yesterday', { time });

  const sameYear =
    formatInZone(date, zone, { year: 'numeric' }) ===
    formatInZone(now, zone, { year: 'numeric' });
  if (dayOffset > 1 && dayOffset < 7) {
    return `${formatInZone(date, zone, { weekday: 'short' })} ${time}`;
  }
  const day = formatInZone(date, zone, {
    day: 'numeric',
    month: 'short',
    ...(sameYear ? {} : { year: 'numeric' }),
  });
  return dayOffset > 0 && sameYear ? `${day}, ${time}` : day;
}

export function headerStatusVisible(job) {
  return isTerminalJob(job) || job?.status === 'failed';
}

function describeWeekly(weekdays, time) {
  const days = new Set(weekdays);
  if (days.size === 5 && [1, 2, 3, 4, 5].every((day) => days.has(day))) {
    return t('cron.schedule.weekdays', { time });
  }
  if (days.size === 2 && days.has(0) && days.has(6)) {
    return t('cron.schedule.weekends', { time });
  }
  if (days.size === 7) {
    return t('cron.schedule.daily', { time });
  }
  const style = weekdays.length > 2 ? 'short' : 'long';
  const names = weekdays.map((day) => weekdayName(day, style));
  return t('cron.schedule.weekly', { days: formatList(names), time });
}

function weekdayName(cronDay, style) {
  // 2023-01-01 was a Sunday, so cron day N is January 1 + N.
  return new Intl.DateTimeFormat(activeLocaleTag(), {
    weekday: style,
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(2023, 0, 1 + cronDay)));
}

function formatList(items) {
  try {
    return new Intl.ListFormat(activeLocaleTag(), {
      style: 'long',
      type: 'conjunction',
    }).format(items);
  } catch {
    return items.join(', ');
  }
}

function formatInZone(date, timeZone, options) {
  try {
    return new Intl.DateTimeFormat(activeLocaleTag(), {
      ...options,
      timeZone,
    }).format(date);
  } catch {
    return '';
  }
}

// UTC milliseconds of the calendar day `date` falls on in `timeZone`, so two
// instants compare by their local dates.
function zonedDayStart(date, timeZone) {
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(date);
    const part = (type) =>
      Number(parts.find((entry) => entry.type === type)?.value);
    return Date.UTC(part('year'), part('month') - 1, part('day'));
  } catch {
    return Date.UTC(
      date.getUTCFullYear(),
      date.getUTCMonth(),
      date.getUTCDate(),
    );
  }
}

export function sessionSummary(job) {
  return job?.session_id ? job.session_id : t('cron.detail.newSessionEachRun');
}

export function lastResultSupport(job) {
  if (!job?.last_completed_at_display) {
    return t('cron.detail.waitingForFirstRun');
  }

  return job.last_completed_at_display;
}

export function remainingRunsLabel(job) {
  return job?.remaining_runs === null
    ? t('cron.detail.unlimited')
    : String(job?.remaining_runs ?? 0);
}

export function statusLabel(status) {
  if (status === CRON_STATUS_ACTIVE) {
    return t('cron.status.active');
  }

  if (status === 'paused') {
    return t('cron.status.paused');
  }

  if (status === 'failed') {
    return t('cron.status.failed');
  }

  if (status === CRON_STATUS_MISSED) {
    return t('cron.status.missed');
  }

  return t('cron.status.completed');
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
    return t('cron.outcome.success');
  }
  if (outcome === 'failed') {
    return t('cron.outcome.failed');
  }
  if (outcome === 'cancelled') {
    return t('cron.outcome.cancelled');
  }
  if (outcome === 'missed') {
    return t('cron.outcome.missed');
  }
  if (outcome === 'unknown') {
    return t('cron.outcome.unknown');
  }
  return t('cron.notAvailable');
}

export function isTerminalJob(job) {
  return (
    job?.status === CRON_STATUS_COMPLETED || job?.status === CRON_STATUS_MISSED
  );
}
