import cronstrue from 'cronstrue';

import { formatAgentAddress } from './agentAddress.js';

export const CRON_SCHEDULE_TYPE_CRON = 'cron';
export const CRON_SCHEDULE_TYPE_ONCE = 'once';

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

// A successfully fired once job (`completed`) drops off the list, but a job
// that gave up after repeated failures (`failed`) stays visible so the user
// sees that it never ran instead of it silently disappearing.
const VISIBLE_JOB_STATUSES = new Set([
  CRON_STATUS_ACTIVE,
  CRON_STATUS_PAUSED,
  CRON_STATUS_FAILED,
]);

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
    // The form pre-fill and save round-trip key on the full outside address so a
    // project job preselects its `agent@projekt` dropdown option and writes the
    // address back to `cron.create/update` (not the bare id, which would silently
    // strip the project). `cron.list` formats `target` server-side; we fall back
    // to building it from `agent_id` + `project_id` if `target` is ever absent.
    agent_id: cronJobTarget(job),
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

// Build the combined cron agent dropdown: identity agents (bare ids, unchanged)
// followed by project agents addressed as `agent@projekt`. The OPTION VALUE is
// the address itself, so selecting a project member and saving sends that
// address as the `agent_id` param of `cron.create/update` with no extra wiring;
// an identity option's value stays the bare id, byte-identical to today.
//
// `identityAgents` is the `agent.list` projection (`{ id, name }`); `projectTeams`
// is a list of `{ projectId, displayName, team: [{ agent_id, display_name }] }`
// gathered lazily from `project.list` → `project.show`. The result is a flat
// option list whose `group` discriminates identity vs. project for an optgroup-
// style UI.
export const CRON_AGENT_GROUP_IDENTITY = 'identity';
export const CRON_AGENT_GROUP_PROJECT = 'project';

export function buildCronAgentOptions(identityAgents, projectTeams) {
  const options = [];

  const identities = Array.isArray(identityAgents) ? identityAgents : [];
  for (const agent of identities) {
    const id = asText(agent?.id);
    if (!id) {
      continue;
    }
    options.push({
      value: id,
      label: asText(agent?.name) || id,
      secondaryLabel: id,
      group: CRON_AGENT_GROUP_IDENTITY,
      projectId: null,
    });
  }

  const teams = Array.isArray(projectTeams) ? projectTeams : [];
  for (const project of teams) {
    const projectId = asText(project?.projectId);
    if (!projectId) {
      continue;
    }
    const members = Array.isArray(project?.team) ? project.team : [];
    for (const member of members) {
      const bareId = asText(member?.agent_id);
      if (!bareId) {
        continue;
      }
      const address = formatAgentAddress(bareId, projectId);
      options.push({
        value: address,
        label: address,
        secondaryLabel:
          asText(member?.display_name) ||
          asText(project?.displayName) ||
          bareId,
        group: CRON_AGENT_GROUP_PROJECT,
        projectId,
      });
    }
  }

  return options;
}

// Wrap `buildCronAgentOptions` for the Dropdown primitive: when BOTH identity and
// project agents are present, insert non-selectable group-header rows ("Identity
// agents" / "Project agents") so the two kinds are visually separated. With only
// identity agents present (the common case, and the byte-identical-to-today
// case) NO header is inserted, so the dropdown looks exactly as it did before
// projects existed. Header labels are passed in already-translated (the lib
// stays i18n-free).
export function buildCronAgentDropdownOptions(
  identityAgents,
  projectTeams,
  { identityGroupLabel = '', projectGroupLabel = '' } = {},
) {
  const options = buildCronAgentOptions(identityAgents, projectTeams);
  const hasProjectOptions = options.some(
    (option) => option.group === CRON_AGENT_GROUP_PROJECT,
  );
  if (!hasProjectOptions) {
    return options;
  }

  const result = [];
  let lastGroup = null;
  for (const option of options) {
    if (option.group !== lastGroup) {
      const label =
        option.group === CRON_AGENT_GROUP_PROJECT
          ? projectGroupLabel
          : identityGroupLabel;
      result.push({
        value: `__cron_group_${option.group}`,
        label,
        disabled: true,
        isGroupHeader: true,
      });
      lastGroup = option.group;
    }
    result.push(option);
  }
  return result;
}

// Project a `project.show` response (`{ project, scan }`) plus the project id
// into the `{ projectId, displayName, team }` entry `buildCronAgentOptions`
// consumes. The team is read straight from `scan.team` (the repo is the source
// of truth); only the bare `agent_id`/`display_name` the dropdown needs are
// kept. An empty/bare project yields an empty team, which is normal.
export function projectTeamEntry(projectId, showResponse) {
  const id = asText(projectId);
  const rawTeam = Array.isArray(showResponse?.scan?.team)
    ? showResponse.scan.team
    : [];
  return {
    projectId: id,
    displayName: asText(showResponse?.project?.display_name) || id,
    team: rawTeam
      .map((member) => ({
        agent_id: asText(member?.agent_id),
        display_name: asText(member?.display_name) || asText(member?.agent_id),
      }))
      .filter((member) => member.agent_id.length > 0),
  };
}

// The project ids to scan for teams, read from a `project.list` response. Lazy
// scanning (one `project.show` per id on cron-modal open) avoids the N+1 scan on
// every render flagged as a plan risk.
export function projectIdsFromList(listResponse) {
  const raw = Array.isArray(listResponse?.projects)
    ? listResponse.projects
    : [];
  return raw
    .map((project) => asText(project?.project_id))
    .filter((projectId) => projectId.length > 0);
}

function normalizeScheduleType(value) {
  return value === CRON_SCHEDULE_TYPE_ONCE
    ? CRON_SCHEDULE_TYPE_ONCE
    : CRON_SCHEDULE_TYPE_CRON;
}

function normalizeStatus(value) {
  if (
    value === CRON_STATUS_PAUSED ||
    value === CRON_STATUS_COMPLETED ||
    value === CRON_STATUS_FAILED
  ) {
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
