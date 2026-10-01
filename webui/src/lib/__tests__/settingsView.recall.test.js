import { describe, expect, it } from 'vitest';

import {
  buildEmbeddingModelChoices,
  buildRecallBackendOptions,
  buildRecallSettingsPayload,
  describeEmbeddingPrivacy,
  describeRecallIndexStatus,
  getRecallSettings,
  recallBackendForMeaning,
  recallBackendNeedsAdvanced,
  recallMeaningAvailable,
  recallSearchesByMeaning,
} from '../settingsView.js';

const NOW_MS = Date.parse('2026-10-01T12:00:00Z');

function target(id, { kind = 'provider', usable = true, facts = {} } = {}) {
  return {
    id,
    label: id,
    kind,
    usable,
    providerId: kind === 'local' ? '' : id.split('/')[0],
    facts,
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

describe('recall backend', () => {
  it('keeps the recall backend within the offered backends', () => {
    expect(getRecallSettings({})).toEqual({
      backend: 'sqlite_fts',
      available_backends: ['sqlite_fts', 'vector', 'hybrid'],
    });
    expect(
      getRecallSettings({
        recall: { backend: 'hybrid', available_backends: ['sqlite_fts'] },
      }),
    ).toEqual({ backend: 'sqlite_fts', available_backends: ['sqlite_fts'] });
    expect(buildRecallSettingsPayload({ backend: 'sqlite_fts' })).toEqual({
      recall: { backend: 'sqlite_fts' },
    });
    expect(
      buildRecallBackendOptions({ available_backends: ['vector', 'custom'] }),
    ).toEqual([
      { value: 'vector', label: 'Meaning only' },
      { value: 'custom', label: 'custom' },
    ]);
  });

  it.each([
    ['sqlite_fts', true, 'hybrid'],
    ['custom', true, 'hybrid'],
    ['hybrid', true, 'hybrid'],
    ['vector', true, 'vector'],
    ['hybrid', false, 'sqlite_fts'],
    ['vector', false, 'sqlite_fts'],
  ])(
    'maps the meaning switch from %s turned %s to %s',
    (backend, on, expected) => {
      expect(recallBackendForMeaning(backend, on)).toBe(expected);
      expect(recallSearchesByMeaning(expected)).toBe(on);
    },
  );

  it('opens Advanced only for a backend the switch does not select', () => {
    expect(
      ['sqlite_fts', 'hybrid', 'vector', 'custom'].map(
        recallBackendNeedsAdvanced,
      ),
    ).toEqual([false, false, true, true]);
    expect(recallMeaningAvailable({})).toBe(true);
    expect(recallMeaningAvailable({ available_backends: ['sqlite_fts'] })).toBe(
      false,
    );
  });
});

describe('embedding model choices', () => {
  it('groups recommended targets by where they run, best first', () => {
    const targets = [
      target('openai/text-embedding-3-large', {
        facts: {
          local: false,
          multilingual: true,
          recommended_rank: 7,
          input_price_per_million: 0.13,
          note: 'Higher-quality OpenAI model at a higher price.',
        },
      }),
      target('openai/text-embedding-3-small', {
        facts: {
          local: false,
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
      }),
      target('ollama/nomic-embed-text', {
        facts: {
          local: true,
          multilingual: false,
          recommended_rank: null,
          input_price_per_million: null,
        },
      }),
      target('mistral/mistral-embed', {
        facts: { local: false, recommended_rank: 9 },
      }),
      ...[4, 5, 6, 8].map((rank) =>
        target(`cloud/rank-${rank}`, {
          facts: { local: false, recommended_rank: rank },
        }),
      ),
    ];

    const choices = buildEmbeddingModelChoices(
      targets,
      'ollama/nomic-embed-text',
    );

    expect(choices.local).toEqual([
      {
        id: 'local/harrier-0.6b',
        label: 'local/harrier-0.6b',
        providerId: '',
        local: true,
        usable: false,
        facts: ['Multilingual', 'Free, runs locally'],
        note: '',
      },
      // Unranked, but selected: it stays visible.
      expect.objectContaining({
        id: 'ollama/nomic-embed-text',
        facts: ['English only', 'Free, runs locally'],
      }),
    ]);
    // Four per group: ranks 3-6; ranks 7-9 are left to All models.
    expect(choices.cloud.map((choice) => choice.id)).toEqual([
      'openai/text-embedding-3-small',
      'cloud/rank-4',
      'cloud/rank-5',
      'cloud/rank-6',
    ]);
    expect(choices.cloud[0]).toMatchObject({
      facts: ['Multilingual', '$0.02 per 1M tokens'],
      note: 'Inexpensive general-purpose OpenAI model.',
    });
    expect(choices.cloud[1].facts).toEqual(['Price unknown']);
    expect(buildEmbeddingModelChoices([], '')).toEqual({
      local: [],
      cloud: [],
    });
  });

  it('says where conversation text goes for the chosen target', () => {
    const providerName = (id) => ({ openai: 'OpenAI' })[id] ?? '';

    expect(
      describeEmbeddingPrivacy(
        target('openai/text-embedding-3-small'),
        providerName,
      ),
    ).toBe('Conversation text is sent to OpenAI to build the search index.');
    expect(
      describeEmbeddingPrivacy(target('ollama/bge-m3'), providerName),
    ).toBe('Conversation text is sent to ollama to build the search index.');
    expect(
      describeEmbeddingPrivacy(
        target('local/granite-embedding-r2', { kind: 'local' }),
        providerName,
      ),
    ).toBe('Conversation text stays on this computer.');
    expect(describeEmbeddingPrivacy(null, providerName)).toBe(
      'Conversation text is sent to the chosen model’s Provider to build the search index.',
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
          'The local embedding model is not installed yet. Install it under On this computer, or choose another model.',
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
