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
import { asText } from './values.js';

const AGENT_TARGET_GROUP_IDENTITY = 'identity';
export const AGENT_TARGET_GROUP_PROJECT = 'project';

// `identityAgents` is the `agent.list` projection (`{ id, name }`); `projectTeams`
// is a list of `{ projectId, displayName, team: [{ agent_id, display_name }] }`
// from the cached Teams `project.list` carries. The result is a flat option
// list whose `group` discriminates identity vs. project for an optgroup-style
// UI.
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

// Project one `project.list` entry with its cached `scan` into the
// `{ projectId, displayName, team }` entry `buildAgentTargetOptions` consumes;
// only the bare `agent_id`/`display_name` the dropdown needs are kept. An
// empty/bare project yields an empty team, which is normal.
function projectTeamEntry(projectId, project) {
  const rawTeam = Array.isArray(project?.scan?.team) ? project.scan.team : [];
  return {
    projectId,
    displayName: asText(project?.display_name) || projectId,
    team: rawTeam
      .map((member) => ({
        agent_id: asText(member?.agent_id),
        display_name: asText(member?.display_name) || asText(member?.agent_id),
      }))
      .filter((member) => member.agent_id.length > 0),
  };
}

// Own the catalog read as one request generation: `project.list` with each
// Project's cached Team (`includeScan`), so one request covers every Project
// instead of a `project.show` re-scan per Project. A Project whose repository
// could not be read (`scan: null`) is a failed Project; the other Teams and
// the Identity Agents stay, and callers decide how to present errors. A
// retired request returns null and never publishes into a newer selection.
export function createAgentTargetCatalogLoader({
  listProjects,
  listAgents = null,
}) {
  let generation = 0;
  let disposed = false;
  return {
    async load() {
      if (disposed) return null;
      const request = ++generation;
      const current = () => !disposed && request === generation;
      const [projectResult, agentResult] = await Promise.allSettled([
        listProjects({ includeScan: true }),
        listAgents ? listAgents() : Promise.resolve({ agents: [] }),
      ]);
      if (!current()) return null;
      const projects =
        projectResult.status === 'fulfilled' &&
        Array.isArray(projectResult.value?.projects)
          ? projectResult.value.projects
          : [];
      const projectTeams = [];
      const failedProjects = [];
      const seen = new Set();
      for (const project of projects) {
        const projectId = asText(project?.project_id);
        if (!projectId || seen.has(projectId)) continue;
        seen.add(projectId);
        if (project.scan === null) failedProjects.push({ projectId });
        else projectTeams.push(projectTeamEntry(projectId, project));
      }
      return {
        projects,
        agents:
          agentResult.status === 'fulfilled' &&
          Array.isArray(agentResult.value?.agents)
            ? agentResult.value.agents
            : [],
        projectTeams,
        failedProjects,
        projectError:
          projectResult.status === 'rejected' ? projectResult.reason : null,
        agentError:
          agentResult.status === 'rejected' ? agentResult.reason : null,
      };
    },
    dispose() {
      disposed = true;
      generation += 1;
    },
  };
}
