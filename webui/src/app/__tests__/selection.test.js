// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createSelectionHarness } from './selection.support.svelte.js';

const { listAgents, listProjects } = vi.hoisted(() => ({
  listAgents: vi.fn(),
  listProjects: vi.fn(),
}));
vi.mock('$lib/api.js', () => ({ listAgents, listProjects }));

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('App selection', () => {
  let selection;
  let dispose;
  beforeEach(() => {
    localStorage.clear();
    listAgents.mockReset();
    listProjects.mockReset();
    ({ selection, dispose } = createSelectionHarness());
  });
  afterEach(() => {
    selection.destroy();
    dispose();
  });

  it('keeps the newest roster and selection when an older server read arrives last', async () => {
    const older = deferred();
    listAgents
      .mockReturnValueOnce(older.promise)
      .mockResolvedValueOnce({ agents: [{ id: 'beta' }] });
    const initial = selection.reloadAgentsFromServer();
    await selection.reloadAgentsFromServer();
    older.resolve({ agents: [{ id: 'deleted' }] });
    await initial;
    expect(selection.agents).toEqual([{ id: 'beta' }]);
    expect(selection.selectedAgentId).toBe('beta');
    expect(selection.agentsRefreshToken).toBe(1);
  });

  it.each(['syncAgents', 'refreshAgents'])(
    'keeps a roster published by %s over a pending server read',
    async (publish) => {
      const response = deferred();
      listAgents.mockReturnValueOnce(response.promise).mockResolvedValueOnce({
        agents: [{ id: 'renamed', name: 'Canonical' }],
      });
      const loading = selection.reloadAgentsFromServer();
      selection[publish]([{ id: 'renamed' }]);
      response.resolve({ agents: [{ id: 'old' }] });
      await loading;
      expect(selection.selectedAgentId).toBe('renamed');
      expect(selection.agents).toEqual([{ id: 'renamed', name: 'Canonical' }]);
      expect(listAgents).toHaveBeenCalledTimes(2);
      expect(selection.agentsRefreshToken).toBe(
        publish === 'refreshAgents' ? 2 : 1,
      );
    },
  );

  it('tells each subscribed surface of an Agent rename before the selection follows', () => {
    listAgents.mockResolvedValue({ agents: [] });
    selection.syncAgents([{ id: 'alpha' }]);
    const seen = [];
    selection.subscribeAgentRenames((oldId, newId) =>
      seen.push([oldId, newId, selection.selectedAgentId]),
    );
    const unsubscribe = selection.subscribeAgentRenames(() =>
      seen.push('unsubscribed'),
    );
    unsubscribe();

    selection.remapIdentityAgentId('alpha', 'gamma');

    expect(seen).toEqual([['alpha', 'gamma', 'alpha']]);
    expect(selection.selectedAgentId).toBe('gamma');
  });

  it('ignores a server read after its App owner is destroyed', async () => {
    const response = deferred();
    listAgents.mockReturnValueOnce(response.promise);
    const loading = selection.reloadAgentsFromServer();
    selection.destroy();
    response.resolve({ agents: [{ id: 'alpha' }] });
    await loading;
    expect(selection.agents).toEqual([]);
    expect(selection.selectedAgentId).toBe('');
    expect(selection.agentsRefreshToken).toBe(0);
  });

  it('keeps the newest valid Project catalog across stale responses and transient errors', async () => {
    const stale = deferred();
    listProjects
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce({ projects: [{ project_id: 'newest-project' }] })
      .mockRejectedValueOnce(new Error('temporary project failure'));
    const projectIds = () =>
      selection.projects.map((project) => project.project_id);

    const initial = selection.loadProjects();
    expect(selection.projectsLoaded).toBe(false);
    await expect(selection.loadProjects()).resolves.toBe(true);
    expect(projectIds()).toEqual(['newest-project']);
    expect(selection.projectsLoaded).toBe(true);
    // Chat's Agent picker lists every Project's Team from the cached scans.
    expect(listProjects).toHaveBeenLastCalledWith({ includeScan: true });

    stale.resolve({ projects: [{ project_id: 'stale-project' }] });
    await expect(initial).resolves.toBe(false);
    expect(projectIds()).toEqual(['newest-project']);

    await expect(selection.loadProjects()).resolves.toBe(false);
    expect(projectIds()).toEqual(['newest-project']);
  });
});
