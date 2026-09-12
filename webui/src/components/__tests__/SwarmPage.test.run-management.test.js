// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  tick,
  SwarmPage,
  profile,
  swarm,
  button,
  createBridge,
  fill,
  choose,
  render,
  fixtureState,
} from './SwarmPage.support.js';

describe('SwarmPage', () => {
  it('shows a connection failure if the host never initializes the page', async () => {
    vi.useFakeTimers();
    const { bridge } = createBridge();
    fixtureState.mounted = mount(SwarmPage, {
      target: document.body,
      props: { bridgeClient: bridge },
    });
    flushSync();
    await vi.advanceTimersByTimeAsync(10_000);
    flushSync();
    expect(document.querySelector('[role="alert"]')).not.toBeNull();
    expect(button('Refresh').disabled).toBe(false);
    bridge.show();
    await vi.advanceTimersByTimeAsync(0);
    flushSync();
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(button('New Swarm')).toBeDefined();
  });

  it('loads retained lists without requesting the profile catalog', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    expect(button('New Swarm')).toBeDefined();
    expect(
      document.querySelector('nav[aria-label="Inactive runs"]'),
    ).not.toBeNull();
    expect(operation).toHaveBeenCalledWith('profiles.list', { limit: 100 });
    expect(operation.mock.calls.some(([name]) => name === 'catalog')).toBe(
      false,
    );
    bridge.show();
    await tick();
    expect(
      operation.mock.calls.filter(([name]) => name === 'profiles.list'),
    ).toHaveLength(1);
    button('New Swarm').click();
    await tick();
    flushSync();
    expect(
      operation.mock.calls.filter(([name]) => name === 'catalog'),
    ).toHaveLength(1);
    expect(document.querySelector('input')).not.toBeNull();
  });

  it('groups Run execution states and moves a Run to inactive after invalidation', async () => {
    const { bridge, operation } = createBridge();
    const base = operation.getMockImplementation();
    const states = [
      'preparing',
      'running',
      'stopping',
      'idle',
      'needs_attention',
      'interrupted',
      'stopped',
      'failed',
    ];
    let entries = states.map((state) => ({
      id: state,
      title: `goal-${state}`,
      state,
    }));
    operation.mockImplementation((name, args) =>
      name === 'swarms.list' ? Promise.resolve({ entries }) : base(name, args),
    );
    await render(bridge);
    const active = () =>
      document.querySelector('nav[aria-label="Active runs"]');
    const inactive = () =>
      document.querySelector('nav[aria-label="Inactive runs"]');
    expect(
      [...active().querySelectorAll('button')].map((row) =>
        row.textContent.trim(),
      ),
    ).toEqual(['goal-preparing', 'goal-running', 'goal-stopping']);
    expect(inactive().querySelectorAll('button')).toHaveLength(5);
    const profileRow = document.querySelector(
      'nav[aria-label="Swarms"] button',
    );
    expect(profileRow.textContent.trim()).toBe('Research (2)');
    expect(profileRow.children).toHaveLength(1);
    expect(document.querySelector('.run-group strong')).toBeNull();
    entries = entries.map((entry) =>
      entry.id === 'running' ? { ...entry, state: 'idle' } : entry,
    );
    bridge.invalidate();
    await vi.waitFor(() =>
      expect(active().querySelectorAll('button')).toHaveLength(2),
    );
    expect(inactive().textContent).toContain('goal-running');
  });

  it('prefills the Run directory, preserves edits during invalidation and submits only the override', async () => {
    const { bridge, operation } = createBridge();
    const base = operation.getMockImplementation();
    operation.mockImplementation((name, args) =>
      name === 'swarms.start'
        ? Promise.resolve({ swarm_id: swarm.id })
        : base(name, args),
    );
    await render(bridge);
    const directory = document.getElementById('swarm-start-directory');
    expect(directory.value).toBe('C:/work');
    const form = document.querySelector('.start');
    expect(
      [...form.querySelectorAll('[id]')]
        .map((el) => el.id)
        .filter((id) =>
          [
            'swarm-start-profile',
            'swarm-start-directory',
            'swarm-goal',
          ].includes(id),
        ),
    ).toEqual(['swarm-start-profile', 'swarm-start-directory', 'swarm-goal']);
    fill('swarm-start-directory', 'D:/run-only');
    fill('swarm-goal', 'goal-sentinel');
    await tick();
    bridge.invalidate();
    await new Promise((resolve) => setTimeout(resolve));
    expect(directory.value).toBe('D:/run-only');
    button('Start Run').click();
    await vi.waitFor(() =>
      expect(operation).toHaveBeenCalledWith(
        'swarms.start',
        expect.objectContaining({
          profile_id: profile.id,
          prompt: 'goal-sentinel',
          working_directory: 'D:/run-only',
        }),
      ),
    );
    expect(
      operation.mock.calls.some(([name]) => name === 'profiles.save'),
    ).toBe(false);
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-head')).not.toBeNull(),
    );
    button('New run').click();
    await tick();
    expect(document.getElementById('swarm-start-directory').value).toBe(
      'C:/work',
    );
  });

  it('resolves Project defaults, preserves their Project selection and replaces the default on Swarm selection', async () => {
    const projectProfile = {
      ...profile,
      working_directory: { kind: 'project', project_id: 'project-a' },
    };
    const directoryProfile = { ...profile, id: 'prf-b', name: 'Other' };
    const { bridge, operation } = createBridge(projectProfile);
    const base = operation.getMockImplementation();
    operation.mockImplementation((name, args) => {
      if (name === 'profiles.list')
        return Promise.resolve({ entries: [projectProfile, directoryProfile] });
      if (name === 'swarms.start')
        return Promise.resolve({ swarm_id: swarm.id });
      return base(name, args);
    });
    await render(bridge);
    await vi.waitFor(() =>
      expect(document.getElementById('swarm-start-directory').value).toBe(
        'C:/project',
      ),
    );
    fill('swarm-goal', 'project-goal');
    await tick();
    button('Start Run').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-head')).not.toBeNull(),
    );
    const start = operation.mock.calls.find(
      ([name]) => name === 'swarms.start',
    )[1];
    expect(start).not.toHaveProperty('working_directory');
    button('New run').click();
    await tick();
    await choose('swarm-start-profile', 'Other');
    expect(document.getElementById('swarm-start-directory').value).toBe(
      'C:/work',
    );
  });

  it('ignores stale Project lookups and prevents starting with a blank directory', async () => {
    const projectProfile = {
      ...profile,
      working_directory: { kind: 'project', project_id: 'project-a' },
    };
    const directoryProfile = { ...profile, id: 'prf-b', name: 'Other' };
    const { bridge, operation } = createBridge(projectProfile);
    const base = operation.getMockImplementation();
    let resolveCatalog;
    operation.mockImplementation((name, args) => {
      if (name === 'profiles.list')
        return Promise.resolve({ entries: [projectProfile, directoryProfile] });
      if (name === 'catalog')
        return new Promise((resolve) => {
          resolveCatalog = resolve;
        });
      return base(name, args);
    });
    await render(bridge);
    expect(button('Start Run').disabled).toBe(true);
    await choose('swarm-start-profile', 'Other');
    resolveCatalog({
      catalog: { projects: [{ id: 'project-a', cwd: 'C:/late' }] },
    });
    await tick();
    expect(document.getElementById('swarm-start-directory').value).toBe(
      'C:/work',
    );
    fill('swarm-start-directory', '  ');
    await tick();
    expect(button('Start Run').disabled).toBe(true);
  });

  it('keeps the overview usable when the profile catalog fails and retries on request', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    operation.mockRejectedValueOnce(new Error('catalog-unavailable-test'));
    button('New Swarm').click();
    await tick();
    flushSync();
    expect(document.querySelector('[role="alert"]')?.textContent).toContain(
      'catalog-unavailable-test',
    );
    expect(button('New Swarm').disabled).toBe(false);
    button('New Swarm').click();
    await tick();
    flushSync();
    expect(document.querySelector('input')).not.toBeNull();
  });

  it('requires Stop before a Swarm can be deleted', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    expect(button('Delete Run').disabled).toBe(true);
    button('Delete Run').click();
    expect(
      operation.mock.calls.some(([name]) => name === 'swarms.delete'),
    ).toBe(false);
  });

  it('confirms deletion, keeps the profile and clears the selected Swarm', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    let deleted = false;
    operation.mockImplementation((name, args) => {
      if (name === 'swarms.get')
        return Promise.resolve({
          swarm: { ...structuredClone(swarm), state: 'cancelled' },
        });
      if (name === 'swarms.delete') {
        deleted = true;
        return Promise.resolve({ deleted: true });
      }
      if (name === 'swarms.list' && deleted)
        return Promise.resolve({ entries: [], has_more: false });
      return original(name, args);
    });
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    button('Delete Run').click();
    await tick();
    expect(document.querySelector('[role="dialog"]').textContent).toContain(
      'participant Sessions',
    );
    button('Cancel').click();
    await tick();
    expect(
      operation.mock.calls.some(([name]) => name === 'swarms.delete'),
    ).toBe(false);
    button('Delete Run').click();
    await tick();
    [...document.querySelectorAll('[role="dialog"] button')]
      .find((node) => node.textContent.trim() === 'Delete')
      .click();
    await new Promise((resolve) => setTimeout(resolve));
    expect(operation).toHaveBeenCalledWith('swarms.delete', {
      swarm_id: 'swr-a',
    });
    expect(
      operation.mock.calls.some(([name]) => name === 'profiles.delete'),
    ).toBe(false);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(button('Investigate')).toBeUndefined();
    expect(bridge.replaceRoute).toHaveBeenLastCalledWith('');
    expect(button('Research')).toBeDefined();
  });

  it('retains the confirmation and allows retry after a failed deletion', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    let attempts = 0;
    operation.mockImplementation((name, args) => {
      if (name === 'swarms.get')
        return Promise.resolve({
          swarm: { ...structuredClone(swarm), state: 'deleting' },
        });
      if (name === 'swarms.delete') {
        attempts += 1;
        return Promise.reject(new Error('Storage unavailable'));
      }
      return original(name, args);
    });
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    expect(button('Resume')).toBeUndefined();
    button('Delete Run').click();
    await tick();
    const confirm = () =>
      [...document.querySelectorAll('[role="dialog"] button')].find(
        (node) => node.textContent.trim() === 'Delete',
      );
    confirm().click();
    await new Promise((resolve) => setTimeout(resolve));
    expect(document.querySelector('[role="dialog"]').textContent).toContain(
      'Storage unavailable',
    );
    expect(confirm().disabled).toBe(false);
    confirm().click();
    await new Promise((resolve) => setTimeout(resolve));
    expect(attempts).toBe(2);
  });

  it('offers Swarm Resume beside Stop when a participant is idle', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await new Promise((resolve) => setTimeout(resolve));
    expect(button('Stop')).toBeDefined();
    button('Resume').click();
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'swarms.resume',
      expect.objectContaining({ swarm_id: 'swr-a' }),
    );
  });

  it.each([
    ['cancelled', 'cancelled'],
    ['interrupted', 'interrupted'],
  ])(
    'offers Resume for an unfinished %s Swarm participant',
    async (swarmState, participantState) => {
      const { bridge, operation } = createBridge();
      const originalGet = operation.getMockImplementation();
      operation.mockImplementation((name, arguments_) => {
        if (name === 'swarms.get') {
          return Promise.resolve({
            swarm: {
              ...structuredClone(swarm),
              state: swarmState,
              participants: [
                {
                  ...structuredClone(swarm.participants[0]),
                  state: participantState,
                },
              ],
            },
          });
        }
        return originalGet(name, arguments_);
      });
      await render(bridge);
      button('Investigate').click();
      await new Promise((resolve) => setTimeout(resolve));
      expect(button('Resume')).toBeDefined();
      button('Resume').click();
      await tick();
      expect(operation).toHaveBeenCalledWith(
        'swarms.resume',
        expect.objectContaining({ swarm_id: 'swr-a' }),
      );
    },
  );

  it('does not autosave delivery changes and stops only on an explicit click', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await tick();
    await tick();
    flushSync();
    button('Change communication settings').click();
    flushSync();
    const select = document.querySelector('.communication select');
    select.value = 'pull';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await tick();
    expect(operation).not.toHaveBeenCalledWith(
      'swarms.settings',
      expect.anything(),
    );
    expect(document.body.textContent).toContain('Proposed changes');
    button('Apply changes').click();
    await new Promise((resolve) => setTimeout(resolve));
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'swarms.settings',
      expect.objectContaining({
        swarm_id: 'swr-a',
        expected_revision: 3,
        delivery: expect.objectContaining({
          main: expect.objectContaining({ mode: 'pull' }),
        }),
      }),
    );
    button('Stop').click();
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'swarms.stop',
      expect.objectContaining({
        swarm_id: 'swr-a',
        request_id: expect.any(String),
      }),
    );
  });
});
