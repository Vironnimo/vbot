export const CRON_SCHEDULE_TYPE_CRON = 'cron';
export const CRON_SCHEDULE_TYPE_ONCE = 'once';

export const CRON_STATUS_ACTIVE = 'active';
export const CRON_STATUS_PAUSED = 'paused';
export const CRON_STATUS_COMPLETED = 'completed';

const VISIBLE_JOB_STATUSES = new Set([CRON_STATUS_ACTIVE, CRON_STATUS_PAUSED]);

export function createCronViewState() {
  return {
    agents: [],
    jobs: [],
    loadingAgents: false,
    loadingJobs: false,
    errorMessage: '',
    statusMessage: '',
  };
}

export function createCronFormValues(job = null) {
  if (!job) {
    return {
      id: '',
      agent_id: '',
      prompt: '',
      schedule_type: CRON_SCHEDULE_TYPE_CRON,
      cron_expression: '',
      run_at: '',
      timezone: '',
      session_id: '',
      original_run_at: '',
      original_timezone: '',
    };
  }

  const normalized = normalizeCronJob(job);

  return {
    id: normalized.id,
    agent_id: normalized.agent_id,
    prompt: normalized.prompt,
    schedule_type: normalized.schedule_type,
    cron_expression: normalized.cron_expression ?? '',
    run_at: toDateTimeLocalInput(normalized.run_at),
    timezone: normalized.timezone ?? '',
    session_id: normalized.session_id ?? '',
    original_run_at: normalized.run_at ?? '',
    original_timezone: normalized.timezone ?? '',
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
  state.jobs = normalizeCronJobs(result?.jobs);
  return state.jobs;
}

export function normalizeCronJobs(jobs) {
  const rawJobs = Array.isArray(jobs) ? jobs : [];
  return rawJobs.map((job) => normalizeCronJob(job));
}

export function visibleCronJobs(jobs) {
  return normalizeCronJobs(jobs).filter((job) =>
    VISIBLE_JOB_STATUSES.has(job.status),
  );
}

export function buildCreateCronPayload(formValues) {
  const scheduleType = normalizeScheduleType(formValues?.schedule_type);

  const payload = {
    agent_id: requiredText(formValues?.agent_id),
    prompt: requiredText(formValues?.prompt),
    schedule_type: scheduleType,
  };

  if (scheduleType === CRON_SCHEDULE_TYPE_CRON) {
    payload.cron_expression = requiredText(formValues?.cron_expression);
  } else {
    payload.run_at = requiredText(formValues?.run_at);
  }

  const timezone = optionalText(formValues?.timezone);
  if (timezone !== null) {
    payload.timezone = timezone;
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
    prompt: requiredText(formValues?.prompt),
    schedule_type: scheduleType,
    timezone: optionalText(formValues?.timezone),
    session_id: optionalText(formValues?.session_id),
  };

  if (scheduleType === CRON_SCHEDULE_TYPE_CRON) {
    payload.cron_expression = requiredText(formValues?.cron_expression);
  } else {
    payload.run_at = resolveOnceRunAtValue(formValues);
  }

  return payload;
}

function resolveOnceRunAtValue(formValues) {
  const runAt = requiredText(formValues?.run_at);
  const originalRunAt = optionalText(formValues?.original_run_at);
  const timezone = optionalText(formValues?.timezone);
  const originalTimezone = optionalText(formValues?.original_timezone);

  if (
    originalRunAt !== null &&
    runAt === toDateTimeLocalInput(originalRunAt) &&
    timezone === originalTimezone
  ) {
    return originalRunAt;
  }

  return runAt;
}

function normalizeCronJob(job) {
  const scheduleType = normalizeScheduleType(job?.schedule_type);
  const cronExpression = optionalText(job?.cron_expression);
  const runAt = optionalText(job?.run_at);
  const lastFiredAt = optionalText(job?.last_fired_at);
  const nextFireAt = optionalText(job?.next_fire_at);

  return {
    id: asText(job?.id),
    agent_id: asText(job?.agent_id),
    prompt: asText(job?.prompt),
    schedule_type: scheduleType,
    cron_expression: cronExpression,
    run_at: runAt,
    timezone: optionalText(job?.timezone),
    session_id: optionalText(job?.session_id),
    status: normalizeStatus(job?.status),
    last_fired_at: lastFiredAt,
    next_fire_at: nextFireAt,
    created_at: optionalText(job?.created_at),
    schedule_description: deriveScheduleDescription(
      scheduleType,
      cronExpression,
      runAt,
    ),
    last_fired_at_display: formatTimestamp(lastFiredAt),
    next_fire_at_display: formatTimestamp(nextFireAt),
  };
}

function deriveScheduleDescription(scheduleType, cronExpression, runAt) {
  if (scheduleType === CRON_SCHEDULE_TYPE_CRON) {
    return cronExpression ?? '';
  }

  return formatTimestamp(runAt);
}

function toDateTimeLocalInput(value) {
  if (!value) {
    return '';
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return '';
  }

  const year = String(parsed.getFullYear()).padStart(4, '0');
  const month = String(parsed.getMonth() + 1).padStart(2, '0');
  const day = String(parsed.getDate()).padStart(2, '0');
  const hours = String(parsed.getHours()).padStart(2, '0');
  const minutes = String(parsed.getMinutes()).padStart(2, '0');

  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function formatTimestamp(value) {
  if (!value) {
    return '';
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return '';
  }

  const iso = parsed.toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

function normalizeScheduleType(value) {
  return value === CRON_SCHEDULE_TYPE_ONCE
    ? CRON_SCHEDULE_TYPE_ONCE
    : CRON_SCHEDULE_TYPE_CRON;
}

function normalizeStatus(value) {
  if (value === CRON_STATUS_PAUSED || value === CRON_STATUS_COMPLETED) {
    return value;
  }

  return CRON_STATUS_ACTIVE;
}

function requiredText(value) {
  return asText(value).trim();
}

function optionalText(value) {
  const normalized = asText(value).trim();
  return normalized ? normalized : null;
}

function asText(value) {
  return value === null || value === undefined ? '' : String(value);
}
