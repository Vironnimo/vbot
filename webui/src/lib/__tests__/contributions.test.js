// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  bindContributionParams,
  composeContributions,
  contributionContext,
  contributionData,
  normalizeContributionItems,
} from '../contributions.js';
import { createContributions } from '../contributions.svelte.js';

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

  it('retains unknown saved IDs and keeps core recovery available after customization', () => {
    const registry = createContributions({});
    registry.save(surface, {
      hidden: ['extension:removed:item'],
      order: ['extension:removed:item'],
      replacements: { 'core:send': replacement.id },
    });
    registry.save(surface, { order: ['core:stop', 'extension:removed:item'] });
    const restored = createContributions({});
    expect(restored.layout(surface)).toEqual({
      hidden: ['extension:removed:item'],
      order: ['core:stop', 'extension:removed:item'],
      replacements: { 'core:send': replacement.id },
    });
    expect(restored.customized).toBe(true);
    restored.reset(surface);
    expect(restored.customized).toBe(false);
    expect(restored.layout(surface)).toEqual({
      hidden: [],
      order: [],
      replacements: {},
    });
  });

  it('handles malformed stored preferences and storage failure without pretending to save', () => {
    localStorage.setItem(
      'vbot.ui-layout.v1',
      JSON.stringify({
        broken: null,
        [surface]: { hidden: 'bad', order: {}, replacements: [] },
      }),
    );
    const registry = createContributions({});
    expect(registry.layout(surface)).toEqual({
      hidden: [],
      order: [],
      replacements: {},
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('full');
    });
    expect(registry.save(surface, { hidden: ['core:send'] })).toBe(false);
    expect(registry.storageError).toBe(true);
    expect(registry.customized).toBe(false);
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
    await expect(registry.command('toString')).rejects.toThrow(
      'UI action unavailable',
    );
    await expect(registry.command('composer.insert', {}, {})).rejects.toThrow(
      'UI action unavailable',
    );
  });
});
