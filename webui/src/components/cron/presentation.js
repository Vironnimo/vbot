import { activeLocaleTag, t } from '$lib/i18n.js';
import {
  CRON_DELIVERY_APP,
  CRON_DELIVERY_CHAT,
  CRON_DELIVERY_NONE,
  CRON_FREQUENCY_CUSTOM,
  CRON_FREQUENCY_DAILY,
  CRON_FREQUENCY_HOURLY,
  CRON_FREQUENCY_INTERVAL,
  CRON_FREQUENCY_MONTHLY,
  CRON_FREQUENCY_ONCE,
  CRON_FREQUENCY_WEBHOOK,
  CRON_FREQUENCY_WEEKLY,
  CRON_STATUS_ACTIVE,
  CRON_SCHEDULE_TYPE_EVENT,
  CRON_SCHEDULE_TYPE_INTERVAL,
  CRON_SCHEDULE_TYPE_ONCE,
  CRON_SCHEDULE_TYPE_WEBHOOK,
  CRON_STATUS_COMPLETED,
  CRON_STATUS_MISSED,
  CRON_WATCH_COMMAND,
  CRON_WATCH_NONE,
  CRON_WATCH_URL,
  CRON_WEEKDAYS,
  cronScheduleFields,
  describeCronExpression,
} from '$lib/cronView.js';
import { formatRelativeTime } from '$lib/timeText.js';

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
    { value: CRON_FREQUENCY_WEBHOOK, label: t('cron.frequency.webhook') },
  ];
}

// Who can send requests to a webhook job's URL, as a sentence.
export function webhookReachText(reach) {
  if (reach === 'internet') return t('cron.webhook.reach.internet');
  if (reach === 'network') return t('cron.webhook.reach.network');
  if (reach === 'computer') return t('cron.webhook.reach.computer');
  return '';
}

// A webhook job's latest request and the requests it holds for its next
// Run, such as "Today 10:00 · 2 requests wait for the next Run".
export function webhookRequestsText(job, timezone, now = new Date()) {
  const webhook = job?.webhook;
  if (!webhook?.last_received_at) {
    return t('cron.webhook.noRequest');
  }
  const parts = [compactTimestamp(webhook.last_received_at, timezone, now)];
  if (webhook.waiting === 1) {
    parts.push(t('cron.webhook.waitingOne'));
  } else if (webhook.waiting > 1) {
    parts.push(t('cron.webhook.waitingMany', { count: webhook.waiting }));
  }
  if (webhook.dropped === 1) {
    parts.push(t('cron.webhook.droppedOne'));
  } else if (webhook.dropped > 1) {
    parts.push(t('cron.webhook.droppedMany', { count: webhook.dropped }));
  }
  return parts.filter(Boolean).join(' · ');
}

// Where a job's answers go, vBot first as the form's default.
export function deliveryKindOptions() {
  return [
    { value: CRON_DELIVERY_APP, label: t('cron.deliveryKind.app') },
    { value: CRON_DELIVERY_CHAT, label: t('cron.deliveryKind.chat') },
    { value: CRON_DELIVERY_NONE, label: t('cron.deliveryKind.none') },
  ];
}

// Where a saved job's answers go, in words: "vBot", "Not delivered" or the
// chat with its thread and Channel.
export function deliverySummary(job) {
  const delivery = job?.delivery;
  if (delivery?.kind === CRON_DELIVERY_APP) {
    return t('cron.delivery.app');
  }
  if (delivery?.kind !== CRON_DELIVERY_CHAT) {
    return t('cron.delivery.none');
  }
  if (delivery.thread_id) {
    return t('cron.delivery.chatThread', {
      chat: delivery.platform_target,
      thread: delivery.thread_id,
      channel: delivery.channel_id,
    });
  }
  return t('cron.delivery.chat', {
    chat: delivery.platform_target,
    channel: delivery.channel_id,
  });
}

// What a repeating job checks before each Run; running every time first as
// the form's default.
export function watchKindOptions() {
  return [
    { value: CRON_WATCH_NONE, label: t('cron.watchKind.none') },
    { value: CRON_WATCH_COMMAND, label: t('cron.watchKind.command') },
    { value: CRON_WATCH_URL, label: t('cron.watchKind.url') },
  ];
}

export function intervalUnitOptions() {
  return [
    { value: 'minutes', label: t('cron.form.intervalUnit.minutes') },
    { value: 'hours', label: t('cron.form.intervalUnit.hours') },
    { value: 'days', label: t('cron.form.intervalUnit.days') },
  ];
}

// The choices of an event job's time: before, at or after the event's start
// or end, by an amount in minutes, hours or days.
export function eventDirectionOptions() {
  return [
    { value: 'before', label: t('cron.eventTime.before') },
    { value: 'at', label: t('cron.eventTime.at') },
    { value: 'after', label: t('cron.eventTime.after') },
  ];
}

export function eventUnitOptions() {
  return [
    { value: 'm', label: t('cron.form.intervalUnit.minutes') },
    { value: 'h', label: t('cron.form.intervalUnit.hours') },
    { value: 'd', label: t('cron.form.intervalUnit.days') },
  ];
}

export function eventEdgeOptions() {
  return [
    { value: 'start', label: t('cron.eventTime.start') },
    { value: 'end', label: t('cron.eventTime.end') },
  ];
}

// When an event job runs, in words: "At start", "30 minutes before start",
// "1 day after end".
export function eventTimeLabel(edge, offsetMinutes) {
  const end = edge === 'end';
  const offset = Number.isInteger(offsetMinutes) ? offsetMinutes : 0;
  if (offset === 0) {
    return end ? t('cron.eventTime.atEnd') : t('cron.eventTime.atStart');
  }
  const amount = eventAmountText(Math.abs(offset));
  if (offset < 0) {
    return end
      ? t('cron.eventTime.beforeEnd', { amount })
      : t('cron.eventTime.beforeStart', { amount });
  }
  return end
    ? t('cron.eventTime.afterEnd', { amount })
    : t('cron.eventTime.afterStart', { amount });
}

function eventAmountText(minutes) {
  if (minutes % 1440 === 0) {
    const count = minutes / 1440;
    return count === 1
      ? t('cron.eventTime.day')
      : t('cron.eventTime.days', { count });
  }
  if (minutes % 60 === 0) {
    const count = minutes / 60;
    return count === 1
      ? t('cron.eventTime.hour')
      : t('cron.eventTime.hours', { count });
  }
  return minutes === 1
    ? t('cron.eventTime.minute')
    : t('cron.eventTime.minutes', { count: minutes });
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
  if (job.schedule_type === CRON_SCHEDULE_TYPE_WEBHOOK) {
    return t('cron.schedule.webhook');
  }
  if (job.schedule_type === CRON_SCHEDULE_TYPE_EVENT) {
    // A gone or unreadable event is named by its id.
    return t('cron.schedule.event', {
      title: job.event_title || job.event_id || t('cron.notAvailable'),
      timing: eventTimeLabel(job.event_edge, job.event_offset_minutes),
    });
  }

  return displayValue(describeCron(job.cron_expression));
}

// The exact value behind the readable cadence: the cron expression, or the
// one-time instant. Intervals and event jobs have nothing further to show.
export function scheduleTechnicalValue(job) {
  if (job?.schedule_type === CRON_SCHEDULE_TYPE_ONCE) {
    return job.run_at ? job.schedule_description : '';
  }
  if (
    job?.schedule_type === CRON_SCHEDULE_TYPE_INTERVAL ||
    job?.schedule_type === CRON_SCHEDULE_TYPE_EVENT ||
    job?.schedule_type === CRON_SCHEDULE_TYPE_WEBHOOK
  ) {
    return '';
  }

  return job?.cron_expression ?? '';
}

// The second line of a list row. An active schedule shows when it runs next,
// an active event job its event and timing; every other status leads with its
// name, followed by the cadence or, for finished one-time history, when it
// ran.
export function listRowDetail(job, timezone, now = new Date()) {
  if (
    job?.status === CRON_STATUS_ACTIVE &&
    job.schedule_type === CRON_SCHEDULE_TYPE_EVENT
  ) {
    return scheduleSummary(job);
  }
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
    return waitingText(job);
  }

  return job.last_completed_at_display;
}

// What a job without a Run waits for: its first due time, for a watching
// job the first change its check finds, for a webhook job its first request.
function waitingText(job) {
  if (job?.schedule_type === CRON_SCHEDULE_TYPE_WEBHOOK) {
    return t('cron.detail.waitingForRequest');
  }
  return job?.watch
    ? t('cron.detail.waitingForChange')
    : t('cron.detail.waitingForFirstRun');
}

// When a job runs next: its next due time, or for an active webhook job the
// next request.
export function nextRunText(job) {
  if (
    job?.schedule_type === CRON_SCHEDULE_TYPE_WEBHOOK &&
    job.status === CRON_STATUS_ACTIVE
  ) {
    return t('cron.detail.nextRequest');
  }
  return displayValue(job?.next_fire_at_display);
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

// `silent`: the successful Run had nothing to report and delivered nothing.
export function outcomeLabel(outcome, silent = false) {
  if (outcome === 'success') {
    return silent ? t('cron.outcome.silent') : t('cron.outcome.success');
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

const VARIANT_TONES = {
  success: 'success',
  warn: 'warning',
  error: 'danger',
  neutral: 'muted',
};

const OUTCOME_TONES = {
  success: 'success',
  failed: 'danger',
  cancelled: 'warning',
  missed: 'warning',
  unknown: 'muted',
};

// Details card beside a Schedule row: the name, the last error as lead text,
// then what the status dot means, the readable cadence with its exact
// expression, the next and last Run as absolute and relative moments in the
// schedule timezone, what a watching job checks and its last check, where the
// Run goes and where its answer goes.
// `agentLabel` names the target.
export function scheduleRowDetails(
  job,
  { agentLabel = (target) => target, nowMs = Date.now() } = {},
) {
  const failures = job.consecutive_failures ?? 0;
  return {
    title: job.name,
    text: job.last_error || '',
    rows: [
      {
        label: t('cron.card.status'),
        value:
          failures > 1
            ? t('cron.card.statusFailures', {
                status: statusLabel(job.status),
                count: failures,
              })
            : statusLabel(job.status),
        tone: VARIANT_TONES[statusChipVariant(job)],
      },
      { label: t('cron.detail.cadence'), value: scheduleSummary(job) },
      {
        label: t('cron.card.expression'),
        value: job.schedule_type === 'cron' ? job.cron_expression : '',
        mono: true,
      },
      {
        label: t('cron.detail.nextFire'),
        value: momentText(job.next_fire_at_display, job.next_fire_at, nowMs),
      },
      {
        label: t('cron.detail.lastResult'),
        value: job.last_outcome
          ? [
              outcomeLabel(job.last_outcome, job.last_run_silent),
              momentText(
                job.last_completed_at_display,
                job.last_completed_at,
                nowMs,
              ),
            ]
              .filter(Boolean)
              .join(' · ')
          : waitingText(job),
        tone: OUTCOME_TONES[job.last_outcome],
      },
      {
        label: t('cron.webhook.lastRequest'),
        value: job.webhook?.last_received_at
          ? momentText(
              job.webhook.last_received_at_display,
              job.webhook.last_received_at,
              nowMs,
            )
          : '',
      },
      {
        label: t('cron.card.watch'),
        value: job.watch?.target ?? '',
        mono: true,
      },
      {
        label: t('cron.detail.lastCheck'),
        value: job.watch
          ? [
              momentText(
                job.last_checked_at_display,
                job.last_checked_at,
                nowMs,
              ),
              job.last_check_error ? t('cron.card.checkFailed') : '',
            ]
              .filter(Boolean)
              .join(' · ')
          : '',
        tone: job.last_check_error ? 'danger' : undefined,
      },
      { label: t('cron.detail.target'), value: agentLabel(job.agent_id) },
      {
        label: t('cron.card.session'),
        value: job.session_id || t('cron.detail.newSessionEachRun'),
        mono: Boolean(job.session_id),
      },
      { label: t('cron.card.delivery'), value: deliverySummary(job) },
    ],
    placement: 'right',
  };
}

// A moment shown in the schedule timezone, then its distance from now.
function momentText(display, value, nowMs) {
  if (!display) return '';
  const relative = formatRelativeTime(value, nowMs);
  return relative ? `${display} · ${relative}` : display;
}

export function isTerminalJob(job) {
  return (
    job?.status === CRON_STATUS_COMPLETED || job?.status === CRON_STATUS_MISSED
  );
}
