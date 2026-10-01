import { describe, expect, it } from 'vitest';

import {
  buildEmbeddingModelOptions,
  buildRecallMethodOptions,
  buildRecallSettingsPayload,
  describeEmbeddingModel,
  describeRecallIndexStatus,
  describeRecallMethod,
  getRecallSettings,
  recallSearchesByMeaning,
  recommendedEmbeddingTarget,
} from '../settingsView.js';

const NOW_MS = Date.parse('2026-10-01T12:00:00Z');

function target(
  id,
  { kind = 'provider', usable = true, facts = {}, metadata = {} } = {},
) {
  return {
    id,
    label: id,
    kind,
    usable,
    providerId: kind === 'local' ? '' : id.split('/')[0],
    facts,
    metadata,
  };
}

function status(overrides = {}) {
  return {
    semantic_enabled: true,
    state: 'idle',
    indexed: 0,
    waiting: 0,
    skipped: 0,
    last_error: null,
    next_attempt_at: null,
    spent: { requests: 0, input_tokens: 0, total_tokens: 0, cost: 0 },
    estimate: { characters: 0, tokens: 0, cost: null },
    ...overrides,
  };
}

describe('recall search method', () => {
  it('keeps the recall backend within the offered backends', () => {
    expect(getRecallSettings({})).toEqual({
      backend: 'sqlite_fts',
      available_backends: ['sqlite_fts', 'hybrid', 'vector'],
    });
    expect(
      getRecallSettings({
        recall: { backend: 'hybrid', available_backends: ['sqlite_fts'] },
      }),
    ).toEqual({ backend: 'sqlite_fts', available_backends: ['sqlite_fts'] });
    expect(buildRecallSettingsPayload({ backend: 'sqlite_fts' })).toEqual({
      recall: { backend: 'sqlite_fts' },
    });
  });

  it('lists the methods simplest first, recommends keywords and meaning, and names an Extension method by its id', () => {
    expect(
      buildRecallMethodOptions({
        available_backends: ['custom', 'vector', 'hybrid', 'sqlite_fts'],
      }),
    ).toEqual([
      { value: 'sqlite_fts', label: 'Keywords', secondaryLabel: '' },
      {
        value: 'hybrid',
        label: 'Keywords and meaning',
        secondaryLabel: 'Recommended',
      },
      { value: 'vector', label: 'Meaning only', secondaryLabel: '' },
      { value: 'custom', label: 'custom', secondaryLabel: '' },
    ]);
    expect(describeRecallMethod('hybrid')).toBe(
      'Also finds conversations that say the same thing in other words.',
    );
    expect(describeRecallMethod('custom')).toBe(
      'A search method added by an Extension.',
    );
    expect(
      ['sqlite_fts', 'hybrid', 'vector', 'custom'].map(recallSearchesByMeaning),
    ).toEqual([false, true, true, false]);
  });
});

describe('embedding model picker', () => {
  const targets = [
    target('openai/text-embedding-3-large', {
      facts: {
        multilingual: true,
        recommended_rank: 7,
        input_price_per_million: 0.13,
      },
    }),
    target('openai/text-embedding-3-small', {
      facts: {
        multilingual: true,
        recommended_rank: 3,
        input_price_per_million: 0.02,
        note: 'Inexpensive general-purpose OpenAI model.',
      },
    }),
    target('local/harrier-0.6b', {
      kind: 'local',
      usable: false,
      facts: { local: true, multilingual: true, recommended_rank: 2 },
      metadata: { license: 'MIT', download_bytes: 715_629_047 },
    }),
    target('ollama/nomic-embed-text', {
      facts: { local: true, multilingual: false, recommended_rank: null },
    }),
    target('mistral/mistral-embed', {
      usable: false,
      facts: { recommended_rank: null },
    }),
    target('cloud/unpriced', { facts: { recommended_rank: null } }),
  ];

  it('groups the Models by where they run, recommended first, with their facts', () => {
    expect(
      buildEmbeddingModelOptions(targets, 'gone/old-model').map(
        ({ value, secondaryLabel, group, disabled }) => ({
          value,
          secondaryLabel,
          group,
          disabled: disabled === true,
        }),
      ),
    ).toEqual([
      // vBot installs it when it is chosen, so it stays selectable.
      {
        value: 'local/harrier-0.6b',
        secondaryLabel: 'Not installed · 716 MB',
        group: 'On this computer',
        disabled: false,
      },
      {
        value: 'ollama/nomic-embed-text',
        secondaryLabel: 'Free · English only',
        group: 'On this computer',
        disabled: false,
      },
      {
        value: 'openai/text-embedding-3-small',
        secondaryLabel: '$0.02 / 1M tokens',
        group: 'Cloud',
        disabled: false,
      },
      {
        value: 'openai/text-embedding-3-large',
        secondaryLabel: '$0.13 / 1M tokens',
        group: 'Cloud',
        disabled: false,
      },
      {
        value: 'cloud/unpriced',
        secondaryLabel: '',
        group: 'Cloud',
        disabled: false,
      },
      {
        value: 'mistral/mistral-embed',
        secondaryLabel: 'Unavailable',
        group: 'Cloud',
        disabled: true,
      },
      // A saved Model no longer offered stays visible as the selection.
      {
        value: 'gone/old-model',
        secondaryLabel: 'Unavailable',
        group: 'No longer offered',
        disabled: false,
      },
    ]);
    expect(
      buildEmbeddingModelOptions(targets, '').find(
        (option) => option.value === 'openai/text-embedding-3-small',
      ).tooltip,
    ).toBe('Inexpensive general-purpose OpenAI model.');
  });

  it('says where conversation text goes, or which Model to choose', () => {
    const providerName = (id) =>
      ({ openai: 'OpenAI', ollama: 'Ollama' })[id] ?? '';
    const byId = (id) => targets.find((item) => item.id === id);
    const describe = (id) =>
      describeEmbeddingModel(
        byId(id) ?? null,
        id,
        providerName,
        recommendedEmbeddingTarget(targets),
      );

    expect(describe('openai/text-embedding-3-small')).toEqual({
      text: 'Conversation text is sent to OpenAI to build the search index, at $0.02 per 1M tokens.',
      attention: false,
    });
    expect(describe('cloud/unpriced').text).toBe(
      'Conversation text is sent to cloud to build the search index.',
    );
    expect(describe('ollama/nomic-embed-text').text).toBe(
      'Runs in Ollama on this computer. Conversation text stays here.',
    );
    expect(
      describeEmbeddingModel(
        target('local/granite-embedding-r2', { kind: 'local' }),
        'local/granite-embedding-r2',
        providerName,
      ).text,
    ).toBe('Runs on this computer, free. Conversation text stays here.');
    expect(describe('local/harrier-0.6b')).toEqual({
      text: 'This model is not installed yet. Choose it again to install it.',
      attention: true,
    });
    expect(describe('gone/old-model')).toEqual({
      text: 'This model is no longer offered. Choose another one.',
      attention: true,
    });
    // The best recommended Model that can be chosen is suggested.
    expect(describe('')).toEqual({
      text: 'Choose a model to search by meaning. local/harrier-0.6b is a good start.',
      attention: true,
    });
    expect(describeEmbeddingModel(null, '', providerName, null).text).toBe(
      'Choose a model to search by meaning. Connect a Provider that offers embedding models if the list is empty.',
    );
  });
});

describe('recall index status line', () => {
  it.each([
    [
      'progress with a waiting estimate',
      status({
        state: 'indexing',
        indexed: 812,
        waiting: 183,
        estimate: { characters: 1_600_000, tokens: 400_000, cost: 0.004 },
      }),
      {
        state: 'indexing',
        summary:
          'Indexing: 812 of 995 passages · about 400K tokens waiting (~$0.004)',
        problem: '',
      },
    ],
    [
      'the time left while indexing',
      status({
        state: 'indexing',
        indexed: 300,
        waiting: 700,
        estimate: { tokens: 70_000 },
        eta_seconds: 754,
      }),
      {
        state: 'indexing',
        summary:
          'Indexing: 300 of 1,000 passages · about 13 min left · about 70K tokens waiting',
        problem: '',
      },
    ],
    [
      'hours left while indexing',
      status({ state: 'indexing', waiting: 9000, eta_seconds: 9000 }),
      expect.objectContaining({
        summary: expect.stringContaining('about 2.5 hr left'),
      }),
    ],
    [
      'less than a minute left while indexing',
      status({ state: 'indexing', indexed: 990, waiting: 10, eta_seconds: 12 }),
      expect.objectContaining({
        summary: expect.stringContaining(' · less than a minute left · '),
      }),
    ],
    [
      'a local Model that is not installed',
      status({
        state: 'error',
        waiting: 2,
        estimate: { tokens: 10 },
        last_error: { code: 'local_model_missing', message: 'English' },
      }),
      {
        state: 'error',
        summary: 'Indexed 0 of 2 passages · about 10 tokens waiting',
        problem:
          'The local embedding model is not installed. Choose it under Embedding model to install it, or choose another model.',
      },
    ],
    [
      'a free local Model: no cost while waiting or spent',
      status({
        state: 'indexing',
        indexed: 56,
        waiting: 938,
        estimate: { tokens: 264_300, cost: 0 },
        spent: { requests: 7, input_tokens: 18_000, cost: 0 },
        eta_seconds: 170,
      }),
      {
        state: 'indexing',
        summary:
          'Indexing: 56 of 994 passages · about 3 min left · about 264.3K tokens waiting',
        problem: '',
      },
    ],
    [
      'waiting without a known price',
      status({ indexed: 10, waiting: 5, estimate: { tokens: 1200 } }),
      {
        state: 'idle',
        summary: 'Indexed 10 of 15 passages · about 1.2K tokens waiting',
        problem: '',
      },
    ],
    [
      'a complete index with skipped texts and spent cost',
      status({
        indexed: 1234,
        skipped: 2,
        spent: { requests: 3, input_tokens: 9000, cost: 0.0123 },
      }),
      {
        state: 'idle',
        summary: 'All passages indexed (1,234) · 2 skipped · $0.0123 spent',
        problem: '',
      },
    ],
    [
      'spent tokens when the cost is unknown',
      status({
        indexed: 4,
        spent: { requests: 1, input_tokens: 2500, cost: null },
      }),
      {
        state: 'idle',
        summary: 'All passages indexed (4) · 2.5K tokens spent',
        problem: '',
      },
    ],
    [
      'an empty index',
      status(),
      { state: 'idle', summary: 'No passages to index yet', problem: '' },
    ],
    [
      'a retry with its next attempt',
      status({
        state: 'retrying',
        indexed: 3,
        waiting: 1,
        estimate: { tokens: 500 },
        last_error: { code: 'provider_rate_limited', message: 'English' },
        next_attempt_at: '2026-10-01T12:05:00Z',
      }),
      {
        state: 'retrying',
        summary: 'Indexed 3 of 4 passages · about 500 tokens waiting',
        problem:
          'The embedding Provider is limiting requests. Retrying in 5 minutes.',
      },
    ],
    [
      'an unknown failure code',
      status({
        state: 'error',
        last_error: { code: 'something_new', message: 'English' },
        next_attempt_at: '2026-10-01T14:00:00Z',
      }),
      {
        state: 'error',
        summary: 'No passages to index yet',
        problem: 'Indexing failed. Next attempt in 2 hours.',
      },
    ],
    [
      'a failure without a next attempt',
      status({
        state: 'error',
        last_error: { code: 'provider_auth', message: 'English' },
      }),
      {
        state: 'error',
        summary: 'No passages to index yet',
        problem:
          'The embedding Provider rejected the credentials. Check its connection under Providers.',
      },
    ],
  ])('describes %s', (_name, value, expected) => {
    expect(describeRecallIndexStatus(value, NOW_MS)).toEqual(expected);
  });

  it.each([
    ['no status', null],
    ['search by meaning off', status({ state: 'disabled' })],
    ['no embedding model', status({ state: 'unconfigured' })],
  ])('shows no status line for %s', (_name, value) => {
    expect(describeRecallIndexStatus(value, NOW_MS)).toBeNull();
  });
});
