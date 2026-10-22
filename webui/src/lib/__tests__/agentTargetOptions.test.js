import { describe, expect, it, vi } from 'vitest';

import {
  buildAgentTargetDropdownOptions,
  buildAgentTargetOptions,
  createAgentTargetCatalogLoader,
} from '../agentTargetOptions.js';

const identityAgents = [{ id: 'researcher', name: 'Researcher' }];
const projectTeams = [
  {
    projectId: 'vbot',
    displayName: 'vBot',
    team: [{ agent_id: 'builder', display_name: 'Builder' }],
  },
];

describe('buildAgentTargetOptions', () => {
  it('lists Identity Agents by bare id before Project Agents by address', () => {
    expect(buildAgentTargetOptions(identityAgents, projectTeams)).toEqual([
      {
        value: 'researcher',
        label: 'Researcher',
        secondaryLabel: 'researcher',
        group: 'identity',
        projectId: null,
      },
      {
        value: 'builder@vbot',
        label: 'builder@vbot',
        secondaryLabel: 'Builder',
        group: 'project',
        projectId: 'vbot',
      },
    ]);
    expect(buildAgentTargetOptions(null, null)).toEqual([]);
    expect(buildAgentTargetOptions([{ id: '' }], [{ projectId: '' }])).toEqual(
      [],
    );
  });
});

describe('buildAgentTargetDropdownOptions', () => {
  const labels = {
    identityGroupLabel: 'Identity agents',
    projectGroupLabel: 'Project agents',
  };

  it('inserts no group headers when only Identity Agents exist', () => {
    const options = buildAgentTargetDropdownOptions(identityAgents, [], labels);
    expect(options.some((option) => option.isGroupHeader)).toBe(false);
    expect(options.map((option) => option.value)).toEqual(['researcher']);
  });

  it('separates the two kinds with disabled group headers', () => {
    const options = buildAgentTargetDropdownOptions(
      identityAgents,
      projectTeams,
      labels,
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

describe('Agent target catalog loading', () => {
  it('reads every Team from one Project list and retains healthy Teams when a repository cannot be read', async () => {
    const listProjects = vi.fn(async () => ({
      projects: [
        { project_id: 'bad', scan: null },
        {
          project_id: 'vbot',
          display_name: 'vBot',
          scan: {
            team: [
              { agent_id: 'builder', display_name: 'Builder' },
              { agent_id: 'coder' },
              { agent_id: '' },
            ],
          },
        },
        { project_id: 'vbot', scan: { team: [{ agent_id: 'twin' }] } },
        { project_id: 'empty', scan: {} },
        { project_id: '' },
        {},
      ],
    }));
    const loader = createAgentTargetCatalogLoader({
      listAgents: async () => ({ agents: [{ id: 'main' }] }),
      listProjects,
    });

    const catalog = await loader.load();

    expect(listProjects.mock.calls).toEqual([[{ includeScan: true }]]);
    expect(catalog.projectTeams).toEqual([
      {
        projectId: 'vbot',
        displayName: 'vBot',
        team: [
          { agent_id: 'builder', display_name: 'Builder' },
          { agent_id: 'coder', display_name: 'coder' },
        ],
      },
      { projectId: 'empty', displayName: 'empty', team: [] },
    ]);
    expect(
      buildAgentTargetOptions(catalog.agents, catalog.projectTeams).map(
        (item) => item.value,
      ),
    ).toEqual(['main', 'builder@vbot', 'coder@vbot']);
    expect(catalog.failedProjects).toEqual([{ projectId: 'bad' }]);
  });

  it('retains Identity targets when the Project list fails', async () => {
    const error = new Error('catalog-unavailable');
    const loader = createAgentTargetCatalogLoader({
      listAgents: async () => ({ agents: [{ id: 'main' }] }),
      listProjects: async () => {
        throw error;
      },
    });
    expect(await loader.load()).toMatchObject({
      agents: [{ id: 'main' }],
      projectTeams: [],
      projectError: error,
    });
  });

  it('rejects a superseded catalog and a catalog completed after disposal', async () => {
    let resolveOld;
    const old = new Promise((resolve) => {
      resolveOld = resolve;
    });
    const loader = createAgentTargetCatalogLoader({
      listProjects: vi
        .fn()
        .mockReturnValueOnce(old)
        .mockResolvedValue({ projects: [] }),
    });
    const first = loader.load();
    expect(await loader.load()).toMatchObject({ projects: [] });
    resolveOld({ projects: [{ project_id: 'stale' }] });
    expect(await first).toBeNull();
    const last = loader.load();
    loader.dispose();
    expect(await last).toBeNull();
    expect(await loader.load()).toBeNull();
  });
});
