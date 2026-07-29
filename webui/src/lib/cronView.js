import cronstrue from 'cronstrue';

import { formatAgentAddress } from './agentAddress.js';

export const CRON_SCHEDULE_TYPE_CRON = 'cron';
export const CRON_SCHEDULE_TYPE_INTERVAL = 'interval';
export const CRON_SCHEDULE_TYPE_ONCE = 'once';

// The "Custom" preset key: the selection state when the cron expression matches
// none of the named presets (or the field is being hand-edited). It carries no
// expression of its own — selecting it never rewrites the field.
export const CRON_PRESET_CUSTOM = 'custom';

// The named schedule presets, in display order. Each maps a stable key (its i18n
// label lives in the catalog under `cron.presets.<key>`) to the cron expression
// it fills in. `custom` is intentionally absent here — it is the no-expression
// fallback, prepended by the option builder. The component only orchestrates;
// filling, matching, and deriving are the pure helpers below.
const CRON_PRESETS = [
  { key: 'every15Minutes', expression: '*/15 * * * *' },
  { key: 'hourly', expression: '0 * * * *' },
  { key: 'dailyMorning', expression: '0 9 * * *' },
  { key: 'weekdayMornings', expression: '0 9 * * 1-5' },
  { key: 'mondayMornings', expression: '0 9 * * 1' },
  { key: 'monthlyFirst', expression: '0 9 1 * *' },
];

// Dropdown options for the schedule-preset picker: the "Custom" fallback first,
// then every named preset. Labels are passed in already-translated (this module
// stays i18n-free), keyed by preset key via `translateLabel(key)`.
export function buildCronPresetOptions(translateLabel) {
  const label =
    typeof translateLabel === 'function' ? translateLabel : () => '';
  return [
    { value: CRON_PRESET_CUSTOM, label: label(CRON_PRESET_CUSTOM) },
    ...CRON_PRESETS.map((preset) => ({
      value: preset.key,
      label: label(preset.key),
    })),
  ];
}

// The expression a preset fills into the cron field. `custom` (or any unknown
// key) fills nothing — the caller keeps the current expression.
export function cronPresetExpression(presetKey) {
  const preset = CRON_PRESETS.find((entry) => entry.key === presetKey);
  return preset ? preset.expression : '';
}

// The preset a raw cron expression corresponds to, by EXACT (trimmed) match.
// No match — including an empty expression — derives `custom`, so a hand-edited
// field that drifts from its preset flips the selection back to Custom.
export function cronPresetForExpression(expression) {
  const normalized = asText(expression).trim();
  if (!normalized) {
    return CRON_PRESET_CUSTOM;
  }

  const preset = CRON_PRESETS.find((entry) => entry.expression === normalized);
  return preset ? preset.key : CRON_PRESET_CUSTOM;
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
export const CRON_STATUS_PAUSED = 'paused';
export const CRON_STATUS_COMPLETED = 'completed';
export const CRON_STATUS_FAILED = 'failed';
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
    return {
      id: '',
      agent_id: '',
      name: '',
      prompt: '',
      schedule_type: CRON_SCHEDULE_TYPE_CRON,
      cron_expression: '',
      interval_minutes: '',
      run_at: '',
      repeat: '',
      session_id: '',
      original_run_at: '',
      system_timezone: systemTimezone,
    };
  }

  const normalized = normalizeCronJob(job, systemTimezone);

  return {
    id: normalized.id,
    agent_id: normalized.agent_id,
    name: normalized.name,
    prompt: normalized.prompt,
    schedule_type: normalized.schedule_type,
    cron_expression: normalized.cron_expression ?? '',
    interval_minutes:
      normalized.interval_seconds === null
        ? ''
        : String(normalized.interval_seconds / 60),
    run_at: toDateTimeLocalInput(normalized.run_at, systemTimezone),
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
    interval_minutes: asText(values.interval_minutes),
    run_at: asText(values.run_at),
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
    payload.interval_seconds =
      positiveInteger(formValues?.interval_minutes) * 60;
  } else {
    payload.run_at = requiredText(formValues?.run_at);
  }
  const repeat = optionalPositiveInteger(formValues?.repeat);
  if (repeat !== null) {
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
    payload.interval_seconds =
      positiveInteger(formValues?.interval_minutes) * 60;
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
    return cronExpression ?? '';
  }
  if (scheduleType === CRON_SCHEDULE_TYPE_INTERVAL) {
    return formatInterval(intervalSeconds);
  }

  return formatTimestamp(runAt, systemTimezone);
}

export function toDateTimeLocalInput(value, timezone = 'UTC') {
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

export function formatTimestamp(value, timezone = 'UTC', locale = 'en-GB') {
  if (!value) {
    return '';
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return '';
  }

  try {
    return new Intl.DateTimeFormat(locale, {
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
// keeps its historical export names so callers and tests are unchanged: the
// option VALUE is still the `agent@projekt` address (bare id for identity), so
// saving sends it straight through as the `cron.create/update` `agent_id`.
export {
  buildAgentTargetOptions as buildCronAgentOptions,
  buildAgentTargetDropdownOptions as buildCronAgentDropdownOptions,
  projectTeamEntry,
  projectIdsFromList,
  AGENT_TARGET_GROUP_IDENTITY as CRON_AGENT_GROUP_IDENTITY,
  AGENT_TARGET_GROUP_PROJECT as CRON_AGENT_GROUP_PROJECT,
} from './agentTargetOptions.js';

function normalizeScheduleType(value) {
  if (value === CRON_SCHEDULE_TYPE_INTERVAL) {
    return CRON_SCHEDULE_TYPE_INTERVAL;
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

function asText(value) {
  return value === null || value === undefined ? '' : String(value);
}
