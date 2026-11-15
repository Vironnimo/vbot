// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  bindContributionParams,
  composeContributions,
  contributionContext,
  contributionData,
  contributionMatches,
  normalizeContributionItems,
} from '../contributions.js';
import { createContributions } from '../contributions.svelte.js';

const settingsApi = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
}));
vi.mock('../api.js', () => settingsApi);

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe('public UI contribution composition', () => {
  const surface = 'chat.composer.actions';
  const core = [
    { id: 'core:send', order: 0 },
    { id: 'core:stop', order: 1 },
  ];
  const replacement = {
    id: 'extension:a:send',
    surface,
    replaces: 'core:send',
    when: { has_session: true },
  };
  const addition = { id: 'extension:a:extra', surface, order: 20 };
  const ids = (entries, context, layout) =>
    composeContributions(core, entries, surface, context, layout).map(
      (entry) => entry.id,
    );

  it('selects replacements only with user choice and restores core when a replacement is unavailable', () => {
    const layout = {
      replacements: { 'core:send': replacement.id },
      order: [replacement.id, 'extension:missing:item', 'core:stop'],
    };
    expect(ids([replacement, addition], { has_session: true })).toEqual([
      'core:send',
      'core:stop',
      addition.id,
    ]);
    expect(ids([replacement, addition], { has_session: true }, layout)).toEqual(
      [replacement.id, 'core:stop', addition.id],
    );
    expect(ids([addition], { has_session: true }, layout)).toEqual([
      'core:stop',
      'core:send',
      addition.id,
    ]);
    expect(ids([replacement], { has_session: false }, layout)).toEqual([
      'core:stop',
      'core:send',
    ]);
    expect(
      ids(
        [replacement],
        { has_session: true },
        { ...layout, hidden: [replacement.id] },
      ),
    ).toEqual(['core:stop', 'core:send']);
  });

  it('uses deterministic ordering, known context predicates and bounded public data', () => {
    expect(
      ids([{ ...addition, id: 'extension:b:extra' }, addition], {}),
    ).toEqual(['core:send', 'core:stop', addition.id, 'extension:b:extra']);
    expect(
      ids([{ ...addition, when: { private: true } }], { private: true }),
    ).toEqual(['core:send', 'core:stop']);
    const context = contributionContext({
      session_id: 'one',
      context_tokens: 42,
      draft: 'secret',
      agent: { id: 'private' },
    });
    expect(context).toEqual({ session_id: 'one', context_tokens: 42 });
    expect(
      bindContributionParams(
        { session: { context: 'session_id' }, literal: 3 },
        context,
      ),
    ).toEqual({ session: 'one', literal: 3 });
    expect(() =>
      bindContributionParams({ session: { context: 'draft' } }, context),
    ).toThrow('UI context unavailable');
    expect(
      normalizeContributionItems(
        {
          id: 'extension:a:tokens',
          title: 'Tokens',
          text: { context: 'context_tokens' },
        },
        null,
        context,
      )[0].text,
    ).toBe('42');
    expect(contributionData({ result: 'x'.repeat(256 * 1024) })).toEqual({
      truncated: true,
    });
  });

  it('evaluates bounded structured conditions without coercion or invalid negation', () => {
    const context = {
      message_role: 'assistant',
      context_tokens: 80,
      run_active: false,
    };
    const when = {
      all: [
        { field: 'message_role', op: 'in', value: ['assistant', 'user'] },
        {
          any: [
            { field: 'context_tokens', op: 'gte', value: 80 },
            { run_active: true },
          ],
        },
        { not: { run_active: true } },
        { field: 'message_id', op: 'exists', value: false },
      ],
    };
    expect(contributionMatches({ when }, context)).toBe(true);
    expect(
      contributionMatches({ when }, { ...context, context_tokens: '80' }),
    ).toBe(false);
    for (const invalid of [
      { not: { private: true } },
      { not: null },
      { any: Array(65).fill({}) },
    ]) {
      expect(contributionMatches({ when: invalid }, context)).toBe(false);
    }
    expect(
      contributionMatches(
        { when: { field: 'message_id', op: 'ne', value: null } },
        context,
      ),
    ).toBe(false);
    expect(
      contributionContext({ message_id: 'm1', session_cost: Infinity }),
    ).toEqual({ message_id: 'm1' });
  });

  it('saves shared layouts with a Settings base and preserves unavailable IDs and future fields', async () => {
    const initial = {
      [surface]: {
        hidden: ['extension:removed:item'],
        future: { mode: 'new' },
      },
      'future.surface': { order: ['future:item'] },
    };
    const writeSettings = vi.fn(async (params) => ({
      appearance: params.appearance,
    }));
    const registry = createContributions({
      settings: { appearance: { ui_layout: initial } },
      writeSettings,
    });
    await registry.save(surface, {
      order: ['core:stop', 'extension:removed:item'],
    });
    expect(writeSettings.mock.calls[0][0].base).toEqual({
      appearance: { ui_layout: initial },
    });
    expect(
      writeSettings.mock.calls[0][0].appearance.ui_layout['future.surface'],
    ).toEqual(initial['future.surface']);
    expect(registry.layout(surface)).toEqual({
      ...initial[surface],
      order: ['core:stop', 'extension:removed:item'],
    });
    await registry.reset(surface);
    expect(registry.layout(surface)).toEqual({
      hidden: [],
      order: [],
      replacements: {},
      future: { mode: 'new' },
    });
    expect(registry.pending).toBe(false);
  });

  it('requires an explicit legacy migration, keeps its backup on failure, and honors an existing server layout', async () => {
    const legacy = { [surface]: { hidden: ['core:send'], future: 'retained' } };
    localStorage.setItem('vbot.ui-layout.v1', JSON.stringify(legacy));
    const writeSettings = vi
      .fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockImplementation(async (params) => ({
        appearance: params.appearance,
      }));
    const registry = createContributions({
      settings: { appearance: { ui_layout: null } },
      writeSettings,
    });
    expect(registry.migrationPending).toBe(true);
    expect(await registry.save(surface, { hidden: [] })).toBe(false);
    expect(await registry.migrate(true)).toBe(false);
    expect(registry.pending).toBe(true);
    expect(registry.error).toContain('offline');
    expect(JSON.parse(localStorage.getItem('vbot.ui-layout.v1'))).toEqual(
      legacy,
    );
    expect(await registry.retry()).toBe(true);
    expect(registry.layout(surface).future).toBe('retained');
    expect(localStorage.getItem('vbot.ui-layout.v1')).toBeNull();
    localStorage.setItem('vbot.ui-layout.v1', JSON.stringify(legacy));
    const anotherBrowser = createContributions({
      settings: { appearance: { ui_layout: {} } },
      writeSettings,
    });
    expect(anotherBrowser.migrationPending).toBe(false);
    expect(anotherBrowser.layout(surface)).toEqual({});
  });

  it('serializes newer edits and recovers cold-load failures and remote invalidations', async () => {
    let saved = { appearance: { ui_layout: {} } };
    let listener;
    let finishFirst;
    const readSettings = vi
      .fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockImplementation(async () => saved);
    const writeSettings = vi.fn(async (params) => {
      if (writeSettings.mock.calls.length === 1)
        await new Promise((resolve) => {
          finishFirst = resolve;
        });
      saved = { appearance: params.appearance };
      return saved;
    });
    const unsubscribe = vi.fn();
    const registry = createContributions({
      readSettings,
      writeSettings,
      subscribe: (fn) => {
        listener = fn;
        return unsubscribe;
      },
    });
    expect(await registry.refresh()).toBe(false);
    expect(registry.ready).toBe(false);
    expect(await registry.retry()).toBe(true);
    const disconnect = registry.connect();
    const first = registry.save(surface, { hidden: ['core:send'] });
    const second = registry.save(surface, { order: ['core:stop'] });
    expect(writeSettings).toHaveBeenCalledTimes(1);
    finishFirst();
    expect(await first).toBe(true);
    expect(await second).toBe(true);
    expect(saved.appearance.ui_layout[surface]).toEqual({
      hidden: ['core:send'],
      order: ['core:stop'],
    });
    expect(registry.pending).toBe(false);
    saved = {
      appearance: { ui_layout: { [surface]: { hidden: ['core:stop'] } } },
    };
    listener({ owner: null, change: { resource: 'settings' } });
    await registry.refresh();
    expect(registry.layout(surface)).toEqual({ hidden: ['core:stop'] });
    disconnect();
    expect(unsubscribe).toHaveBeenCalledOnce();
  });

  it('rebases competing browser edits with saved conflicts winning and unrelated draft edits retained', async () => {
    const initial = { [surface]: { hidden: [], order: [] } };
    const remote = {
      [surface]: { hidden: ['core:stop'], order: [] },
      'future.surface': { future: true },
    };
    settingsApi.getSettings.mockResolvedValue({
      appearance: { ui_layout: remote },
    });
    const writeSettings = vi
      .fn()
      .mockRejectedValueOnce(
        Object.assign(new Error('conflict'), { code: 'settings_conflict' }),
      )
      .mockImplementation(async (params) => ({
        appearance: params.appearance,
      }));
    const registry = createContributions({
      settings: { appearance: { ui_layout: initial } },
      writeSettings,
    });
    await registry.save(surface, {
      hidden: ['core:send'],
      order: ['core:send'],
    });
    expect(registry.layout(surface)).toEqual({
      hidden: ['core:stop'],
      order: ['core:send'],
    });
    expect(writeSettings.mock.calls[1][0].base.appearance.ui_layout).toEqual(
      remote,
    );
    expect(
      writeSettings.mock.calls[1][0].appearance.ui_layout['future.surface'],
    ).toEqual({ future: true });
    expect(registry.error).toBeTruthy();
    expect(registry.pending).toBe(false);
  });

  it('only invokes explicit local or global commands with copied parameter objects', async () => {
    const global = vi.fn();
    const local = vi.fn((params) => {
      params.text = 'changed';
      return { applied: true };
    });
    const registry = createContributions({
      commands: { 'navigation.open': global },
    });
    const params = { text: 'hello' };
    await expect(
      registry.command('composer.insert', params, { 'composer.insert': local }),
    ).resolves.toEqual({ applied: true });
    expect(params.text).toBe('hello');
    await registry.command('navigation.open', { view: 'chat' });
    expect(global).toHaveBeenCalledExactlyOnceWith({ view: 'chat' });
    await expect(registry.command('toString')).rejects.toThrow();
    await expect(registry.command('composer.insert', {}, {})).rejects.toThrow();
  });
});
