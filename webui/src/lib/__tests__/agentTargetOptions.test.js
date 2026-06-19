import { describe, expect, it } from 'vitest';

import {
  AGENT_TARGET_GROUP_IDENTITY,
  AGENT_TARGET_GROUP_PROJECT,
  buildAgentTargetDropdownOptions,
  buildAgentTargetOptions,
  projectIdsFromList,
  projectTeamEntry,
} from '../agentTargetOptions.js';

describe('buildAgentTargetOptions', () => {
  const identityAgents = [{ id: 'researcher', name: 'Researcher' }];
  const projectTeams = [
    {
      projectId: 'vbot',
      displayName: 'vBot',
      team: [{ agent_id: 'builder', display_name: 'Builder' }],
    },
  ];

  it('lists identity agents as bare-id options', () => {
    const options = buildAgentTargetOptions(identityAgents, []);
    expect(options).toEqual([
      {
        value: 'researcher',
        label: 'Researcher',
        secondaryLabel: 'researcher',
        group: AGENT_TARGET_GROUP_IDENTITY,
        projectId: null,
      },
    ]);
  });

  it('lists project agents with the address as the option value', () => {
    const [, projectOption] = buildAgentTargetOptions(
      identityAgents,
      projectTeams,
    );
    expect(projectOption.value).toBe('builder@vbot');
    expect(projectOption.group).toBe(AGENT_TARGET_GROUP_PROJECT);
    expect(projectOption.projectId).toBe('vbot');
  });

  it('orders identity agents before project agents', () => {
    const options = buildAgentTargetOptions(identityAgents, projectTeams);
    expect(options.map((option) => option.value)).toEqual([
      'researcher',
      'builder@vbot',
    ]);
  });

  it('tolerates missing/empty inputs', () => {
    expect(buildAgentTargetOptions(null, null)).toEqual([]);
    expect(buildAgentTargetOptions([{ id: '' }], [{ projectId: '' }])).toEqual(
      [],
    );
  });
});

describe('buildAgentTargetDropdownOptions', () => {
  const identityAgents = [{ id: 'researcher', name: 'Researcher' }];
  const projectTeams = [
    {
      projectId: 'vbot',
      displayName: 'vBot',
      team: [{ agent_id: 'builder', display_name: 'Builder' }],
    },
  ];

  it('inserts no group headers when only identity agents exist', () => {
    const options = buildAgentTargetDropdownOptions(identityAgents, [], {
      identityGroupLabel: 'Identity agents',
      projectGroupLabel: 'Project agents',
    });
    expect(options.some((option) => option.isGroupHeader)).toBe(false);
    expect(options.map((option) => option.value)).toEqual(['researcher']);
  });

  it('separates the two kinds with disabled group headers', () => {
    const options = buildAgentTargetDropdownOptions(
      identityAgents,
      projectTeams,
      {
        identityGroupLabel: 'Identity agents',
        projectGroupLabel: 'Project agents',
      },
    );
    expect(options.map((option) => option.label)).toEqual([
      'Identity agents',
      'Researcher',
      'Project agents',
      'builder@vbot',
    ]);
    const headers = options.filter((option) => option.isGroupHeader);
    expect(headers.every((header) => header.disabled)).toBe(true);
  });
});

describe('project team gathering helpers', () => {
  it('extracts non-empty project ids from a project.list response', () => {
    expect(
      projectIdsFromList({
        projects: [{ project_id: 'vbot' }, { project_id: '' }, {}],
      }),
    ).toEqual(['vbot']);
    expect(projectIdsFromList(null)).toEqual([]);
  });

  it('projects a project.show response into a team entry', () => {
    const entry = projectTeamEntry('vbot', {
      project: { display_name: 'vBot' },
      scan: {
        team: [
          { agent_id: 'builder', display_name: 'Builder' },
          { agent_id: '' },
        ],
      },
    });
    expect(entry).toEqual({
      projectId: 'vbot',
      displayName: 'vBot',
      team: [{ agent_id: 'builder', display_name: 'Builder' }],
    });
  });

  it('yields an empty team for a bare/empty project', () => {
    expect(projectTeamEntry('empty', { project: {}, scan: {} })).toEqual({
      projectId: 'empty',
      displayName: 'empty',
      team: [],
    });
  });
});
