// Shared builder for "agent target" dropdowns: a single option list that mixes
// identity agents (bare ids, unchanged) with project agents addressed as
// `agent@projekt`. The OPTION VALUE is the address itself, so a caller can hand
// the selected value straight to any RPC that accepts an `agent@projekt`
// address (`cron.create/update`, `prompt.preview`, …) with no extra wiring; an
// identity option's value stays the bare id, byte-identical to an identity-only
// world.
//
// This is the one place that knows how to turn `agent.list` + lazily-scanned
// project teams into dropdown options, so Cron, System Prompt, and any future
// agent-target picker share it instead of each re-deriving the grammar. The
// address grammar itself lives in `agentAddress.js`; this module only arranges
// the options. Group-header labels are passed in already-translated — the lib
// stays i18n-free.

import { formatAgentAddress } from './agentAddress.js';

export const AGENT_TARGET_GROUP_IDENTITY = 'identity';
export const AGENT_TARGET_GROUP_PROJECT = 'project';

// `identityAgents` is the `agent.list` projection (`{ id, name }`); `projectTeams`
// is a list of `{ projectId, displayName, team: [{ agent_id, display_name }] }`
// gathered lazily from `project.list` → `project.show`. The result is a flat
// option list whose `group` discriminates identity vs. project for an optgroup-
// style UI.
export function buildAgentTargetOptions(identityAgents, projectTeams) {
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
      group: AGENT_TARGET_GROUP_IDENTITY,
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
        group: AGENT_TARGET_GROUP_PROJECT,
        projectId,
      });
    }
  }

  return options;
}

// Wrap `buildAgentTargetOptions` for the Dropdown primitive: when BOTH identity
// and project agents are present, insert non-selectable group-header rows
// ("Identity agents" / "Project agents") so the two kinds are visually
// separated. With only identity agents present (the common case, and the
// byte-identical-to-an-identity-only-world case) NO header is inserted, so the
// dropdown looks exactly as it did before projects existed. Header labels are
// passed in already-translated (the lib stays i18n-free).
export function buildAgentTargetDropdownOptions(
  identityAgents,
  projectTeams,
  { identityGroupLabel = '', projectGroupLabel = '' } = {},
) {
  const options = buildAgentTargetOptions(identityAgents, projectTeams);
  const hasProjectOptions = options.some(
    (option) => option.group === AGENT_TARGET_GROUP_PROJECT,
  );
  if (!hasProjectOptions) {
    return options;
  }

  const result = [];
  let lastGroup = null;
  for (const option of options) {
    if (option.group !== lastGroup) {
      const label =
        option.group === AGENT_TARGET_GROUP_PROJECT
          ? projectGroupLabel
          : identityGroupLabel;
      result.push({
        value: `__agent_target_group_${option.group}`,
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
// into the `{ projectId, displayName, team }` entry `buildAgentTargetOptions`
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
// scanning (one `project.show` per id, on demand) avoids an N+1 scan on every
// render.
export function projectIdsFromList(listResponse) {
  const raw = Array.isArray(listResponse?.projects)
    ? listResponse.projects
    : [];
  return raw
    .map((project) => asText(project?.project_id))
    .filter((projectId) => projectId.length > 0);
}

function asText(value) {
  return value === null || value === undefined ? '' : String(value);
}
