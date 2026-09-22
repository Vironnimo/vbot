// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createSelectionHarness } from './selectionHarness.svelte.js';

const { listAgents } = vi.hoisted(() => ({ listAgents: vi.fn() }));
vi.mock('$lib/api.js', () => ({ listAgents, listProjects: vi.fn() }));

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('App Agent selection', () => {
  let selection;
  let dispose;
  beforeEach(() => {
    localStorage.clear();
    listAgents.mockReset();
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
});
