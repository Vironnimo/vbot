import cronstrue from 'cronstrue';

import { formatAgentAddress } from './agentAddress.js';
import { asText } from './values.js';

export const CRON_SCHEDULE_TYPE_CRON = 'cron';
export const CRON_SCHEDULE_TYPE_INTERVAL = 'interval';
export const CRON_SCHEDULE_TYPE_ONCE = 'once';
export const CRON_SCHEDULE_TYPE_EVENT = 'event';

// The schedule planner offers readable frequencies instead of raw cron syntax.
// `once` and `interval` map to their own schedule types; hourly, daily, weekly
// and monthly each generate one five-field cron expression; `custom` edits the
// expression directly. A stored expression that no planner shape can express
// loads as `custom`, so loading never rewrites a schedule.
export const CRON_FREQUENCY_ONCE = 'once';
export const CRON_FREQUENCY_INTERVAL = 'interval';
export const CRON_FREQUENCY_HOURLY = 'hourly';
export const CRON_FREQUENCY_DAILY = 'daily';
export const CRON_FREQUENCY_WEEKLY = 'weekly';
export const CRON_FREQUENCY_MONTHLY = 'monthly';
export const CRON_FREQUENCY_CUSTOM = 'custom';
// A job bound to a calendar event runs at each occurrence; it is not a
// planner frequency and is only ever loaded, never chosen.
export const CRON_FREQUENCY_EVENT = 'event';

// Seconds per interval unit, in display order.
export const CRON_INTERVAL_UNIT_SECONDS = {
  minutes: 60,
  hours: 3600,
  days: 86400,
};

// Cron weekday numbers (0 = Sunday) in Monday-first display order.
export const CRON_WEEKDAYS = [1, 2, 3, 4, 5, 6, 0];

const PLANNER_FREQUENCIES = new Set([
  CRON_FREQUENCY_HOURLY,
  CRON_FREQUENCY_DAILY,
  CRON_FREQUENCY_WEEKLY,
  CRON_FREQUENCY_MONTHLY,
]);

function defaultScheduleFields() {
  return {
    frequency: CRON_FREQUENCY_DAILY,
    time: '09:00',
    minute: '0',
    weekdays: [1, 2, 3, 4, 5],
    month_day: '1',
  };
}

// The planner fields a cron expression corresponds to. Only plain numbers,
// weekday lists and ranges, and `*` are recognized; anything else (steps,
// months, day of month combined with weekdays) is `custom` with default fields.
export function cronScheduleFields(expression) {
  const fields = defaultScheduleFields();
  const custom = { ...fields, frequency: CRON_FREQUENCY_CUSTOM };
  const parts = asText(expression).trim().split(/\s+/);
  if (parts.length !== 5) {
    return custom;
  }

  const [minutePart, hourPart, dayPart, monthPart, weekdayPart] = parts;
  const minute = cronNumber(minutePart, 0, 59);
  if (minute === null || monthPart !== '*') {
    return custom;
  }
  if (hourPart === '*') {
    return dayPart === '*' && weekdayPart === '*'
      ? { ...fields, frequency: CRON_FREQUENCY_HOURLY, minute: String(minute) }
      : custom;
  }

  const hour = cronNumber(hourPart, 0, 23);
  if (hour === null) {
    return custom;
  }
  const time = `${pad2(hour)}:${pad2(minute)}`;
  if (dayPart === '*' && weekdayPart === '*') {
    return { ...fields, frequency: CRON_FREQUENCY_DAILY, time };
  }
  if (dayPart === '*') {
    const weekdays = parseCronWeekdays(weekdayPart);
    return weekdays
      ? { ...fields, frequency: CRON_FREQUENCY_WEEKLY, time, weekdays }
      : custom;
  }
  const monthDay = weekdayPart === '*' ? cronNumber(dayPart, 1, 31) : null;
  return monthDay === null
    ? custom
    : {
        ...fields,
        frequency: CRON_FREQUENCY_MONTHLY,
        time,
        month_day: String(monthDay),
      };
}

// The cron expression the planner fields describe, or '' while they are
// incomplete (no time, no weekday, a day or minute out of range). Only the
// planner frequencies build an expression; the others return ''.
export function buildCronExpression(values) {
  const frequency = values?.frequency;
  if (frequency === CRON_FREQUENCY_HOURLY) {
    const minute = cronNumber(asText(values.minute).trim(), 0, 59);
    return minute === null ? '' : `${minute} * * * *`;
  }
  if (!PLANNER_FREQUENCIES.has(frequency)) {
    return '';
  }

  const time = parseTimeOfDay(values.time);
  if (!time) {
    return '';
  }
  const prefix = `${time.minute} ${time.hour}`;
  if (frequency === CRON_FREQUENCY_DAILY) {
    return `${prefix} * * *`;
  }
  if (frequency === CRON_FREQUENCY_WEEKLY) {
    const weekdays = formatCronWeekdays(values.weekdays);
    return weekdays ? `${prefix} * * ${weekdays}` : '';
  }
  const monthDay = cronNumber(asText(values.month_day).trim(), 1, 31);
  return monthDay === null ? '' : `${prefix} ${monthDay} * *`;
}

// Apply a planner edit to the form. `patch` holds changed planner fields
// (`frequency`, `time`, `minute`, `weekdays`, `month_day`, `interval_value`,
// `interval_unit`) or, for `custom`, `cron_expression`. The persisted fields
// (`schedule_type`, `cron_expression`) follow, so dirty tracking and payloads
// only ever see the effective schedule.
export function updateCronSchedule(formValues, patch) {
  const previousFrequency = formValues.frequency;
  Object.assign(formValues, patch);
  // An event job keeps its event; only its event time changes.
  if (formValues.schedule_type === CRON_SCHEDULE_TYPE_EVENT) {
    return;
  }
  const frequency = formValues.frequency;

  // A one-time schedule runs exactly once: its hidden repeat limit is 1, and
  // it returns to unlimited when the schedule becomes recurring again.
  if (frequency === CRON_FREQUENCY_ONCE && previousFrequency !== frequency) {
    formValues.repeat = '1';
  } else if (
    previousFrequency === CRON_FREQUENCY_ONCE &&
    frequency !== CRON_FREQUENCY_ONCE &&
    formValues.repeat === '1'
  ) {
    formValues.repeat = '';
  }

  if (frequency === CRON_FREQUENCY_ONCE) {
    formValues.schedule_type = CRON_SCHEDULE_TYPE_ONCE;
    return;
  }
  if (frequency === CRON_FREQUENCY_INTERVAL) {
    formValues.schedule_type = CRON_SCHEDULE_TYPE_INTERVAL;
    return;
  }

  formValues.schedule_type = CRON_SCHEDULE_TYPE_CRON;
  if (frequency !== CRON_FREQUENCY_CUSTOM) {
    formValues.cron_expression = buildCronExpression(formValues);
    return;
  }
  // A hand-edited expression keeps the planner fields in step, so switching
  // back to a planner frequency starts from the same time and days.
  const parsed = cronScheduleFields(formValues.cron_expression);
  if (parsed.frequency !== CRON_FREQUENCY_CUSTOM) {
    formValues.time = parsed.time;
    formValues.minute = parsed.minute;
    formValues.weekdays = parsed.weekdays;
    formValues.month_day = parsed.month_day;
  }
}

// The interval the form describes, in seconds; 0 while it is incomplete.
export function cronIntervalSeconds(formValues) {
  const unitSeconds = CRON_INTERVAL_UNIT_SECONDS[formValues?.interval_unit];
  return unitSeconds
    ? positiveInteger(formValues?.interval_value) * unitSeconds
    : 0;
}

function intervalFormFields(seconds) {
  if (!Number.isInteger(seconds) || seconds <= 0) {
    return { interval_value: '1', interval_unit: 'hours' };
  }
  for (const unit of ['days', 'hours']) {
    const unitSeconds = CRON_INTERVAL_UNIT_SECONDS[unit];
    if (seconds % unitSeconds === 0) {
      return {
        interval_value: String(seconds / unitSeconds),
        interval_unit: unit,
      };
    }
  }
  return {
    interval_value: String(Math.round(seconds / 60)),
    interval_unit: 'minutes',
  };
}

// An event job runs at its event's start or end, shifted by a signed offset
// in minutes. Forms edit it as an edge (start, end), a direction (before, at,
// after) and an amount in one unit (m, h, d); `cron.create` and `cron.update`
// take it as event time text such as `start - 30m`.
export const EVENT_TIME_UNIT_MINUTES = { m: 1, h: 60, d: 1440 };
const MAX_EVENT_OFFSET_MINUTES = 31 * 1440;

// The form fields of an event edge and offset, in the largest whole unit.
export function eventTimeFields(edge, offsetMinutes) {
  const offset = Number.isInteger(offsetMinutes) ? offsetMinutes : 0;
  const minutes = Math.abs(offset);
  const unit = minutes % 1440 === 0 ? 'd' : minutes % 60 === 0 ? 'h' : 'm';
  return {
    event_edge: edge === 'end' ? 'end' : 'start',
    event_direction: offset < 0 ? 'before' : offset > 0 ? 'after' : 'at',
    event_amount: minutes
      ? String(minutes / EVENT_TIME_UNIT_MINUTES[unit])
      : '30',
    event_unit: minutes ? unit : 'm',
  };
}

// The event time text the form fields describe, or '' while the amount is
// missing or the offset exceeds 31 days.
export function eventTimeText(fields) {
  const edge = fields?.event_edge === 'end' ? 'end' : 'start';
  const direction = fields?.event_direction;
  if (direction !== 'before' && direction !== 'after') {
    return edge;
  }
  const amount = positiveInteger(fields.event_amount);
  const unitMinutes = EVENT_TIME_UNIT_MINUTES[fields.event_unit];
  if (
    !amount ||
    !unitMinutes ||
    amount * unitMinutes > MAX_EVENT_OFFSET_MINUTES
  ) {
    return '';
  }
  const sign = direction === 'before' ? '-' : '+';
  return `${edge} ${sign} ${amount}${fields.event_unit}`;
}

function cronNumber(value, min, max) {
  if (!/^\d{1,2}$/.test(value)) {
    return null;
  }
  const number = Number(value);
  return number >= min && number <= max ? number : null;
}

function parseCronWeekdays(value) {
  const days = new Set();
  for (const part of value.split(',')) {
    const range = /^(\d)(?:-(\d))?$/.exec(part);
    if (!range) {
      return null;
    }
    const first = Number(range[1]);
    const last = range[2] === undefined ? first : Number(range[2]);
    if (last > 7 || last < first) {
      return null;
    }
    for (let day = first; day <= last; day += 1) {
      days.add(day % 7);
    }
  }
  return CRON_WEEKDAYS.filter((day) => days.has(day));
}

// Ascending cron weekday numbers with runs of three or more as ranges, so
// Monday to Friday reads `1-5`.
function formatCronWeekdays(weekdays) {
  const days = [...new Set(Array.isArray(weekdays) ? weekdays : [])]
    .filter((day) => Number.isInteger(day) && day >= 0 && day <= 6)
    .sort((left, right) => left - right);
  const groups = [];
  for (const day of days) {
    const group = groups.at(-1);
    if (group && day === group[1] + 1) {
      group[1] = day;
    } else {
      groups.push([day, day]);
    }
  }
  return groups
    .flatMap(([first, last]) => {
      if (last - first >= 2) return [`${first}-${last}`];
      return first === last ? [String(first)] : [String(first), String(last)];
    })
    .join(',');
}

function parseTimeOfDay(value) {
  const match = /^(\d{1,2}):(\d{2})$/.exec(asText(value).trim());
  if (!match) {
    return null;
  }
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  return hour <= 23 && minute <= 59 ? { hour, minute } : null;
}

function pad2(value) {
  return String(value).padStart(2, '0');
}

// Human-readable plain-text description of a cron expression, e.g.
// "0 9 * * 1-5" → "At 09:00, Monday through Friday". Returns '' for empty or
// unparseable expressions so callers can hide the preview instead of showing
// a parser error.
export function describeCronExpression(expression) {
  const normalized = asText(expression).trim();
  if (!normalized) {
    return '';
  }

  try {
    return cronstrue.toString(normalized, { use24HourTimeFormat: true });
  } catch {
    return '';
  }
}

export const CRON_STATUS_ACTIVE = 'active';
const CRON_STATUS_PAUSED = 'paused';
export const CRON_STATUS_COMPLETED = 'completed';
const CRON_STATUS_FAILED = 'failed';
export const CRON_STATUS_MISSED = 'missed';

export function createCronViewState() {
  return {
    agents: [],
    jobs: [],
    loadingAgents: false,
    loadingJobs: false,
    agentsError: '',
    jobsError: '',
    systemTimezone: 'UTC',
  };
}

export function createCronFormValues(job = null, systemTimezone = 'UTC') {
  if (!job) {
    const schedule = defaultScheduleFields();
    return {
      id: '',
      agent_id: '',
      name: '',
      prompt: '',
      schedule_type: CRON_SCHEDULE_TYPE_CRON,
      cron_expression: buildCronExpression(schedule),
      ...schedule,
      ...intervalFormFields(null),
      run_at: '',
      event_id: '',
      event_title: '',
      ...eventTimeFields('start', 0),
      repeat: '',
      session_id: '',
      original_run_at: '',
      system_timezone: systemTimezone,
    };
  }

  const normalized = normalizeCronJob(job, systemTimezone);
  const schedule = cronScheduleFields(normalized.cron_expression);
  if (normalized.schedule_type === CRON_SCHEDULE_TYPE_ONCE) {
    schedule.frequency = CRON_FREQUENCY_ONCE;
  } else if (normalized.schedule_type === CRON_SCHEDULE_TYPE_INTERVAL) {
    schedule.frequency = CRON_FREQUENCY_INTERVAL;
  } else if (normalized.schedule_type === CRON_SCHEDULE_TYPE_EVENT) {
    schedule.frequency = CRON_FREQUENCY_EVENT;
  }

  return {
    id: normalized.id,
    agent_id: normalized.agent_id,
    name: normalized.name,
    prompt: normalized.prompt,
    schedule_type: normalized.schedule_type,
    cron_expression: normalized.cron_expression ?? '',
    ...schedule,
    ...intervalFormFields(normalized.interval_seconds),
    run_at: toDateTimeLocalInput(normalized.run_at, systemTimezone),
    event_id: normalized.event_id ?? '',
    event_title: normalized.event_title ?? '',
    ...eventTimeFields(normalized.event_edge, normalized.event_offset_minutes),
    repeat:
      normalized.remaining_runs === null
        ? ''
        : String(normalized.remaining_runs),
    session_id: normalized.session_id ?? '',
    original_run_at: normalized.run_at ?? '',
    system_timezone: systemTimezone,
  };
}

export function applyAgentListResponse(state, result) {
  const rawAgents = Array.isArray(result?.agents) ? result.agents : [];
  state.agents = rawAgents
    .map((agent) => ({
      id: asText(agent?.id),
      name: asText(agent?.name) || asText(agent?.id),
    }))
    .filter((agent) => agent.id.length > 0);
  return state.agents;
}

export function applyCronListResponse(state, result) {
  state.systemTimezone = optionalText(result?.system_timezone) ?? 'UTC';
  state.jobs = normalizeCronJobs(result?.jobs, state.systemTimezone);
  return state.jobs;
}

function normalizeCronJobs(jobs, systemTimezone = 'UTC') {
  const rawJobs = Array.isArray(jobs) ? jobs : [];
  return rawJobs.map((job) => normalizeCronJob(job, systemTimezone));
}

export function visibleCronJobs(jobs, systemTimezone = 'UTC') {
  return normalizeCronJobs(jobs, systemTimezone);
}

export function cronFormFingerprint(formValues) {
  const values = formValues ?? {};
  return JSON.stringify({
    agent_id: asText(values.agent_id),
    name: asText(values.name),
    prompt: asText(values.prompt),
    schedule_type: normalizeScheduleType(values.schedule_type),
    cron_expression: asText(values.cron_expression),
    interval_seconds: cronIntervalSeconds(values),
    run_at: asText(values.run_at),
    event_id: asText(values.event_id),
    event_time: eventTimeText(values),
    repeat: asText(values.repeat),
    session_id: asText(values.session_id),
  });
}

export function buildCreateCronPayload(formValues) {
  const scheduleType = normalizeScheduleType(formValues?.schedule_type);

  const payload = {
    agent_id: requiredText(formValues?.agent_id),
    prompt: requiredText(formValues?.prompt),
    schedule_type: scheduleType,
  };
  const name = optionalText(formValues?.name);
  if (name !== null) {
    payload.name = name;
  }

  if (scheduleType === CRON_SCHEDULE_TYPE_CRON) {
    payload.cron_expression = requiredText(formValues?.cron_expression);
  } else if (scheduleType === CRON_SCHEDULE_TYPE_INTERVAL) {
    payload.interval_seconds = cronIntervalSeconds(formValues);
  } else if (scheduleType === CRON_SCHEDULE_TYPE_EVENT) {
    payload.event_id = requiredText(formValues?.event_id);
    payload.event_time = eventTimeText(formValues);
  } else {
    payload.run_at = requiredText(formValues?.run_at);
  }
  // The event's occurrences drive an event job's repetition.
  const repeat = optionalPositiveInteger(formValues?.repeat);
  if (repeat !== null && scheduleType !== CRON_SCHEDULE_TYPE_EVENT) {
    payload.repeat = repeat;
  }

  const sessionId = optionalText(formValues?.session_id);
  if (sessionId !== null) {
    payload.session_id = sessionId;
  }

  return payload;
}

export function buildUpdateCronPayload(formValues) {
  const scheduleType = normalizeScheduleType(formValues?.schedule_type);

  const payload = {
    id: requiredText(formValues?.id),
    agent_id: requiredText(formValues?.agent_id),
    name: requiredText(formValues?.name),
    prompt: requiredText(formValues?.prompt),
    schedule_type: scheduleType,
    session_id: optionalText(formValues?.session_id),
  };

  if (scheduleType === CRON_SCHEDULE_TYPE_CRON) {
    payload.cron_expression = requiredText(formValues?.cron_expression);
  } else if (scheduleType === CRON_SCHEDULE_TYPE_INTERVAL) {
    payload.interval_seconds = cronIntervalSeconds(formValues);
  } else if (scheduleType === CRON_SCHEDULE_TYPE_EVENT) {
    payload.event_id = requiredText(formValues?.event_id);
    payload.event_time = eventTimeText(formValues);
    // The event's occurrences drive the repetition; a job has no repeat limit.
    return payload;
  } else {
    payload.run_at = resolveOnceRunAtValue(formValues);
  }
  const repeat = optionalPositiveInteger(formValues?.repeat);
  payload.repeat = repeat;

  return payload;
}

function resolveOnceRunAtValue(formValues) {
  const runAt = requiredText(formValues?.run_at);
  const originalRunAt = optionalText(formValues?.original_run_at);
  const systemTimezone = optionalText(formValues?.system_timezone) ?? 'UTC';

  if (
    originalRunAt !== null &&
    runAt === toDateTimeLocalInput(originalRunAt, systemTimezone)
  ) {
    return originalRunAt;
  }

  return runAt;
}

function normalizeCronJob(job, systemTimezone = 'UTC') {
  const scheduleType = normalizeScheduleType(job?.schedule_type);
  const cronExpression = optionalText(job?.cron_expression);
  const intervalSeconds =
    Number.isInteger(job?.interval_seconds) && job.interval_seconds > 0
      ? job.interval_seconds
      : null;
  const runAt = optionalText(job?.run_at);
  const remainingRuns =
    Number.isInteger(job?.remaining_runs) && job.remaining_runs >= 0
      ? job.remaining_runs
      : null;
  const lastFiredAt = optionalText(job?.last_fired_at);
  const lastAttemptAt = optionalText(job?.last_attempt_at);
  const lastCompletedAt = optionalText(job?.last_completed_at);
  const nextFireAt = optionalText(job?.next_fire_at);
  return {
    id: asText(job?.id),
    // The form pre-fill and save round-trip key on the full outside address so a
    // project job preselects its `agent@projekt` dropdown option and writes the
    // address back to `cron.create/update` (not the bare id, which would silently
    // strip the project). `cron.list` formats `target` server-side; we fall back
    // to building it from `agent_id` + `project_id` if `target` is ever absent.
    agent_id: cronJobTarget(job),
    name: asText(job?.name) || asText(job?.prompt),
    prompt: asText(job?.prompt),
    schedule_type: scheduleType,
    cron_expression: cronExpression,
    interval_seconds: intervalSeconds,
    run_at: runAt,
    event_id: optionalText(job?.event_id),
    event_edge: job?.event_edge === 'end' ? 'end' : 'start',
    event_offset_minutes: Number.isInteger(job?.event_offset_minutes)
      ? job.event_offset_minutes
      : 0,
    // Null while the event is gone or unreadable.
    event_title: optionalText(job?.event_title),
    remaining_runs: remainingRuns,
    session_id: optionalText(job?.session_id),
    status: normalizeStatus(job?.status),
    last_fired_at: lastFiredAt,
    last_attempt_at: lastAttemptAt,
    last_completed_at: lastCompletedAt,
    last_run_id: optionalText(job?.last_run_id),
    last_outcome: optionalText(job?.last_outcome),
    last_error: optionalText(job?.last_error),
    consecutive_failures: Number.isInteger(job?.consecutive_failures)
      ? job.consecutive_failures
      : 0,
    next_fire_at: nextFireAt,
    created_at: optionalText(job?.created_at),
    schedule_description: deriveScheduleDescription(
      scheduleType,
      cronExpression,
      intervalSeconds,
      runAt,
      systemTimezone,
    ),
    last_attempt_at_display: formatTimestamp(lastAttemptAt, systemTimezone),
    last_fired_at_display: formatTimestamp(lastFiredAt, systemTimezone),
    last_completed_at_display: formatTimestamp(lastCompletedAt, systemTimezone),
    next_fire_at_display: formatTimestamp(nextFireAt, systemTimezone),
  };
}

function deriveScheduleDescription(
  scheduleType,
  cronExpression,
  intervalSeconds,
  runAt,
  systemTimezone,
) {
  if (scheduleType === CRON_SCHEDULE_TYPE_CRON) {
    return cronExpression;
  }
  if (scheduleType === CRON_SCHEDULE_TYPE_INTERVAL) {
    return formatInterval(intervalSeconds);
  }
  if (scheduleType === CRON_SCHEDULE_TYPE_EVENT) {
    return '';
  }

  return formatTimestamp(runAt, systemTimezone);
}

function toDateTimeLocalInput(value, timezone = 'UTC') {
  if (!value) {
    return '';
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return '';
  }

  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(parsed);
    const part = (type) =>
      parts.find((entry) => entry.type === type)?.value ?? '';
    return `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}`;
  } catch {
    return '';
  }
}

function formatTimestamp(value, timezone) {
  if (!value) {
    return '';
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return '';
  }

  try {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: timezone,
      year: 'numeric',
      month: 'short',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
      timeZoneName: 'short',
    }).format(parsed);
  } catch {
    return '';
  }
}

// The readable + savable target of a cron job. `cron.list` returns `target`
// already formatted as `agent@projekt` (bare `agent` for identity); we use it
// verbatim and only synthesize it from `agent_id`/`project_id` as a fallback so
// the view never has to format the address itself.
function cronJobTarget(job) {
  const target = optionalText(job?.target);
  if (target !== null) {
    return target;
  }

  const agentId = asText(job?.agent_id);
  const projectId = optionalText(job?.project_id);
  return formatAgentAddress(agentId, projectId);
}

// The combined identity + project agent dropdown lives in the shared
// `agentTargetOptions` module now that System Prompt previews reuse it. Cron
// keeps its historical builder names for its callers: the option VALUE is
// still the `agent@projekt` address (bare id for identity), so saving sends it
// straight through as the `cron.create/update` `agent_id`.
export {
  buildAgentTargetOptions as buildCronAgentOptions,
  buildAgentTargetDropdownOptions as buildCronAgentDropdownOptions,
} from './agentTargetOptions.js';

function normalizeScheduleType(value) {
  if (
    value === CRON_SCHEDULE_TYPE_INTERVAL ||
    value === CRON_SCHEDULE_TYPE_EVENT
  ) {
    return value;
  }
  return value === CRON_SCHEDULE_TYPE_ONCE
    ? CRON_SCHEDULE_TYPE_ONCE
    : CRON_SCHEDULE_TYPE_CRON;
}

function normalizeStatus(value) {
  if (
    value === CRON_STATUS_PAUSED ||
    value === CRON_STATUS_COMPLETED ||
    value === CRON_STATUS_FAILED ||
    value === CRON_STATUS_MISSED
  ) {
    return value;
  }

  return CRON_STATUS_ACTIVE;
}

function requiredText(value) {
  return asText(value).trim();
}

function positiveInteger(value) {
  const normalized = Number(asText(value).trim());
  return Number.isInteger(normalized) && normalized > 0 ? normalized : 0;
}

function optionalPositiveInteger(value) {
  const normalized = asText(value).trim();
  return normalized ? positiveInteger(normalized) : null;
}

function formatInterval(seconds) {
  if (!Number.isInteger(seconds) || seconds < 60) {
    return '';
  }
  if (seconds % 86400 === 0) {
    return `every ${seconds / 86400}d`;
  }
  if (seconds % 3600 === 0) {
    return `every ${seconds / 3600}h`;
  }
  return `every ${seconds / 60}m`;
}

function optionalText(value) {
  const normalized = asText(value).trim();
  return normalized ? normalized : null;
}
