import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { init, t } from '../i18n.js';
import { buildModelSelectOptions } from '../modelSelection.js';
import { buildEmbeddingModelOptions } from '../settingsView/recall.js';
import {
  configureModelFavorites,
  holdModelFavoriteOrder,
  loadModelFavorites,
  modelFavoriteFields,
} from '../modelFavorites.svelte.js';

const model = (id) => ({
  id,
  provider_id: id.split('/')[0],
  name: id,
  capabilities: { tools: true },
  context_window: 128000,
  effective_context_window: 128000,
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const favorite = (id) => modelFavoriteFields(id).favorite;

describe('model favorites', () => {
  let saved;
  let transport;

  beforeEach(() => {
    init('en');
    saved = [];
    transport = {
      load: vi.fn(async () => ({ models: saved })),
      save: vi.fn(async (id, active) => {
        saved = active ? [...saved, id] : saved.filter((entry) => entry !== id);
        return { models: saved };
      }),
      onError: vi.fn(),
    };
    configureModelFavorites(transport);
  });

  afterEach(() => configureModelFavorites(null));

  it('offers no star on a page without favorites', () => {
    configureModelFavorites(null);
    expect(modelFavoriteFields('openai/gpt-5.2')).toEqual({});
  });

  it('lists favorites first, after the empty choice, in catalog order', async () => {
    saved = ['openai/gpt-5.2', 'anthropic/claude'];
    await loadModelFavorites();

    const options = buildModelSelectOptions({
      models: ['mistral/large', 'openai/gpt-5.2', 'anthropic/claude'].map(
        model,
      ),
      emptyLabel: 'None',
      connections: ['mistral', 'openai', 'anthropic'].map((provider) => ({
        id: `${provider}:api-key`,
        provider_id: provider,
        label: 'API Key',
        usable: true,
      })),
    });

    expect(options.map((option) => option.label)).toEqual([
      'None',
      'openai/gpt-5.2',
      'anthropic/claude',
      'mistral/large',
    ]);
  });

  it('lists favorite embedding Models first within their group', async () => {
    saved = ['openai/large'];
    await loadModelFavorites();

    const options = buildEmbeddingModelOptions(
      ['local/bge', 'openai/small', 'openai/large'].map((id) => ({
        id,
        label: id,
        kind: id.startsWith('local/') ? 'local' : 'cloud',
        usable: true,
      })),
    );

    expect(options.map((option) => option.value)).toEqual([
      'local/bge',
      'openai/large',
      'openai/small',
    ]);
  });

  it('marks the star at once and keeps the order while a picker is open', async () => {
    const release = holdModelFavoriteOrder();

    await favorite('openai/gpt-5.2').toggle();

    expect(favorite('openai/gpt-5.2').active).toBe(true);
    expect(favorite('openai/gpt-5.2').listed).toBe(false);
    release();
    release();
    expect(favorite('openai/gpt-5.2').listed).toBe(true);
    expect(saved).toEqual(['openai/gpt-5.2']);

    await favorite('openai/gpt-5.2').toggle();
    expect(transport.save).toHaveBeenLastCalledWith('openai/gpt-5.2', false);
    expect(favorite('openai/gpt-5.2').active).toBe(false);
  });

  it('takes the change of another window, but not over a running save', async () => {
    const save = deferred();
    transport.save.mockReturnValueOnce(save.promise);
    const toggled = favorite('openai/gpt-5.2').toggle();

    await loadModelFavorites();
    expect(favorite('openai/gpt-5.2').active).toBe(true);

    save.resolve({ models: ['openai/gpt-5.2', 'other/model'] });
    await toggled;
    expect(favorite('other/model').active).toBe(true);

    saved = [];
    await loadModelFavorites();
    expect(favorite('openai/gpt-5.2').active).toBe(false);
  });

  it('tells the user about a failed save and returns to the stored favorites', async () => {
    transport.save.mockRejectedValueOnce(new Error('offline'));

    await favorite('openai/gpt-5.2').toggle();

    expect(transport.onError).toHaveBeenCalledWith(
      t('dropdown.favoriteFailed'),
    );
    expect(favorite('openai/gpt-5.2').active).toBe(false);
  });
});
