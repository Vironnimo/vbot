import { describe, expect, it, vi } from 'vitest';

import { t } from '../i18n.js';
import {
  buildModelSelectOptions,
  createModelCatalogLoader,
  filterModelSelectOptions,
  modelFilterFooterLabel,
  modelSelectionValue,
  modelShortName,
  parseModelSelectionValue,
  selectModelValue,
} from '../modelSelection.js';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function catalogModel(id, providerId, connections) {
  const model = {
    id,
    provider_id: providerId,
    name: id,
    capabilities: { tools: true },
    context_window: 128000,
    effective_context_window: 128000,
  };
  if (connections) {
    model.connections = connections;
  }
  return model;
}

function usableConnection(id, providerId, label, accounts) {
  const connection = { id, provider_id: providerId, label, usable: true };
  if (accounts) {
    connection.accounts = accounts;
  }
  return connection;
}

function account(id, usable = true) {
  return { id, usable, source: 'data_dir' };
}

const defaultAccount = () => t('settings.providers.accounts.defaultLabel');

function valuesAndLabels(options) {
  return options.slice(1).map(({ value, label }) => [value, label]);
}

describe('createModelCatalogLoader', () => {
  it('applies only the newest overlapping catalog response and drops stale results and failures', async () => {
    const staleModels = deferred();
    const staleConnections = deferred();
    const failingModels = deferred();
    const listModels = vi
      .fn()
      .mockReturnValueOnce(staleModels.promise)
      .mockReturnValueOnce(failingModels.promise)
      .mockResolvedValueOnce({ models: [{ id: 'new/model' }] });
    const listConnections = vi
      .fn()
      .mockReturnValueOnce(staleConnections.promise)
      .mockResolvedValue({ connections: [{ id: 'new:connection' }] });
    const loader = createModelCatalogLoader({ listModels, listConnections });

    const stale = loader.load();
    const failing = loader.load();
    const newest = loader.load();

    await expect(newest).resolves.toEqual({
      models: [{ id: 'new/model' }],
      connections: [{ id: 'new:connection' }],
    });
    staleModels.resolve({ models: [{ id: 'stale/model' }] });
    staleConnections.resolve({ connections: [{ id: 'stale:connection' }] });
    failingModels.reject(new Error('stale failure'));
    await expect(stale).resolves.toBeNull();
    await expect(failing).resolves.toBeNull();
  });
});

describe('buildModelSelectOptions', () => {
  it('keeps one unpinned option per connection without account data', () => {
    const options = buildModelSelectOptions({
      models: [catalogModel('openai/gpt-5.2', 'openai')],
      connections: [usableConnection('openai:api-key', 'openai', 'API Key')],
      emptyLabel: 'None',
    });

    expect(options).toEqual([
      { value: '', label: 'None', isUnavailable: false },
      {
        value: 'openai/gpt-5.2::api-key',
        label: 'openai/gpt-5.2',
        labelLead: 'openai/',
        isUnavailable: false,
        suitable: true,
        suitabilityReasons: [],
      },
    ]);
  });

  it.each([
    [
      'only one usable account',
      [
        usableConnection('openai:api-key', 'openai', 'API Key', [
          account('default'),
          account('work', false),
        ]),
      ],
      () => [['openai/gpt-5.2::api-key', 'openai/gpt-5.2']],
    ],
    [
      'several usable accounts',
      [
        usableConnection('openai:api-key', 'openai', 'API Key', [
          account('default'),
          account('work'),
        ]),
      ],
      () => [
        ['openai/gpt-5.2::api-key', `openai/gpt-5.2 (${defaultAccount()})`],
        ['openai/gpt-5.2::api-key:work', 'openai/gpt-5.2 (work)'],
      ],
    ],
    [
      'several connections of one provider',
      [
        usableConnection('openai:api-key', 'openai', 'API Key', [
          account('default'),
          account('work'),
        ]),
        usableConnection('openai:subscription', 'openai', 'Subscription'),
      ],
      () => [
        [
          'openai/gpt-5.2::api-key',
          `openai/gpt-5.2 (API Key – ${defaultAccount()})`,
        ],
        ['openai/gpt-5.2::api-key:work', 'openai/gpt-5.2 (API Key – work)'],
        ['openai/gpt-5.2::subscription', 'openai/gpt-5.2 (Subscription)'],
      ],
    ],
  ])('labels account options for %s', (_label, connections, expected) => {
    const options = buildModelSelectOptions({
      models: [catalogModel('openai/gpt-5.2', 'openai')],
      connections,
    });

    expect(valuesAndLabels(options)).toEqual(expected());
  });

  it('offers a connection-restricted model only on its allowed usable connections', () => {
    const apiKey = usableConnection('openai:api-key', 'openai', 'API Key');
    const subscription = usableConnection(
      'openai:subscription',
      'openai',
      'Subscription',
    );

    expect(
      valuesAndLabels(
        buildModelSelectOptions({
          models: [catalogModel('openai/gpt-5.4', 'openai', ['subscription'])],
          connections: [apiKey, subscription],
        }),
      ),
    ).toEqual([['openai/gpt-5.4::subscription', 'openai/gpt-5.4']]);
    expect(
      buildModelSelectOptions({
        models: [catalogModel('openai/gpt-5.2', 'openai', ['api-key'])],
        connections: [subscription],
      }),
    ).toHaveLength(1);
  });

  it.each([
    [
      'an account pin',
      'openai/gpt-5.2::api-key:work',
      null,
      'openai/gpt-5.2::api-key:work',
    ],
    [
      'an explicit default-account pin',
      'openai/gpt-5.2::api-key:default',
      null,
      'openai/gpt-5.2::api-key',
    ],
    [
      'an unknown account pin',
      'openai/gpt-5.2::api-key:old',
      { model: 'openai/gpt-5.2', connection: 'API Key – old' },
      'openai/gpt-5.2::api-key:old',
    ],
    [
      'a now-forbidden connection',
      'openai/gpt-5.4::api-key',
      { model: 'openai/gpt-5.4', connection: 'API Key' },
      'openai/gpt-5.4::api-key',
    ],
  ])(
    'resolves a saved selection with %s',
    (_label, selected, unavailable, value) => {
      const catalog = {
        models: [
          catalogModel('openai/gpt-5.2', 'openai'),
          catalogModel('openai/gpt-5.4', 'openai', ['subscription']),
        ],
        connections: [
          usableConnection('openai:api-key', 'openai', 'API Key', [
            account('default'),
            account('work'),
          ]),
          usableConnection('openai:subscription', 'openai', 'Subscription'),
        ],
      };

      const options = buildModelSelectOptions({
        ...catalog,
        selectedModelValue: selected,
      });

      expect(options.filter((option) => option.isUnavailable)).toEqual(
        unavailable
          ? [
              {
                value: selected,
                label: t(
                  'agents.form.modelUnavailableConnectionOption',
                  unavailable,
                ),
                isUnavailable: true,
              },
            ]
          : [],
      );
      expect(selectModelValue(selected, buildModelSelectOptions(catalog))).toBe(
        value,
      );
    },
  );

  it('classifies suitability by tool calling and effective window and badges unreachable models', () => {
    const model = (id, fields) => ({
      id,
      capabilities: { tools: true },
      ...fields,
    });
    const noTools = t('models.filter.noTools');
    const belowMinContext = t('models.filter.belowMinContext');
    const unreachable = t('models.filter.unreachable');

    const options = buildModelSelectOptions({
      modelOnly: true,
      models: [
        model('big', {
          context_window: 200000,
          effective_context_window: 200000,
        }),
        model('no-tools', {
          capabilities: { tools: false },
          effective_context_window: 200000,
        }),
        model('small-effective', {
          context_window: 262144,
          effective_context_window: 16384,
        }),
        model('exactly-32k', { effective_context_window: 32768 }),
        model('unknown', {
          context_window: null,
          effective_context_window: null,
        }),
        model('small-raw', { context_window: 8192 }),
        model('down', { effective_context_window: 32768, reachable: false }),
        model('tiny-down', {
          capabilities: { tools: false },
          effective_context_window: 8192,
          reachable: false,
        }),
      ],
    });

    expect(
      options
        .slice(1)
        .map((option) => [
          option.value,
          option.suitable,
          option.suitabilityReasons,
          option.secondaryLabel,
        ]),
    ).toEqual([
      ['big', true, [], undefined],
      ['no-tools', false, ['noTools'], noTools],
      ['small-effective', false, ['belowMinContext'], belowMinContext],
      ['exactly-32k', true, [], undefined],
      ['unknown', false, ['contextUnknown'], t('models.filter.contextUnknown')],
      ['small-raw', false, ['belowMinContext'], belowMinContext],
      ['down', true, [], unreachable],
      [
        'tiny-down',
        false,
        ['noTools', 'belowMinContext'],
        `${noTools} · ${belowMinContext} · ${unreachable}`,
      ],
    ]);
  });

  it('marks a Model with the latest verification of its usable Connections', () => {
    const withProfiles = (id, profiles) => ({
      ...catalogModel(id, 'demo'),
      wire_profiles: Object.fromEntries(
        Object.entries(profiles).map(([connection, [wireStatus, date]]) => [
          connection,
          { wire_status: wireStatus, verified_at: date },
        ]),
      ),
    });
    const models = [
      withProfiles('demo/verified', {
        'api-key': ['verified', '2026-09-30'],
        oauth: ['verified', '2026-08-15'],
      }),
      withProfiles('demo/undated', {
        'api-key': ['verified', null],
        oauth: ['inferred', null],
      }),
      withProfiles('demo/configured', { 'api-key': ['configured', null] }),
      withProfiles('demo/inferred', {
        'api-key': ['inferred', null],
        oauth: ['inferred', null],
      }),
      catalogModel('demo/unreported', 'demo'),
    ];
    const verified = {
      label: t('models.wire.verifiedOn', { date: '2026-09-30' }),
    };
    const undated = { label: t('models.wire.verified') };
    const markers = (options) =>
      options.slice(1).map((option) => [option.value, option.marker]);

    expect(
      markers(
        buildModelSelectOptions({
          models,
          connections: [
            usableConnection('demo:api-key', 'demo', 'API key'),
            usableConnection('demo:oauth', 'demo', 'Subscription'),
          ],
        }),
      ),
    ).toEqual([
      ['demo/verified::api-key', verified],
      ['demo/verified::oauth', verified],
      ['demo/undated::api-key', undated],
      ['demo/undated::oauth', undated],
      ['demo/configured::api-key', undefined],
      ['demo/configured::oauth', undefined],
      ['demo/inferred::api-key', undefined],
      ['demo/inferred::oauth', undefined],
      ['demo/unreported::api-key', undefined],
      ['demo/unreported::oauth', undefined],
    ]);
    expect(verified.label).toBe('Wire profile verified on 2026-09-30');
    expect(
      markers(buildModelSelectOptions({ models, modelOnly: true })),
    ).toEqual([
      ['demo/verified', verified],
      ['demo/undated', undated],
      ['demo/configured', undefined],
      ['demo/inferred', undefined],
      ['demo/unreported', undefined],
    ]);
  });

  it('reuses suitability and keeps one exact selected value without connection pins', () => {
    const options = buildModelSelectOptions({
      models: [catalogModel('demo/model', 'demo'), { id: 'demo/no-tools' }],
      modelOnly: true,
      selectedModelValue: 'demo/model',
    });
    expect(options.map((option) => option.value)).toEqual([
      '',
      'demo/model',
      'demo/no-tools',
    ]);
    expect(
      filterModelSelectOptions(options).map((option) => option.value),
    ).toEqual(['', 'demo/model']);
    const unavailable = buildModelSelectOptions({
      models: [],
      modelOnly: true,
      selectedModelValue: 'demo/removed',
    });
    expect(unavailable[1]).toMatchObject({
      value: 'demo/removed',
      isUnavailable: true,
    });
  });
});

describe('model suitability filter', () => {
  it('hides unsuitable options unless everything is shown or one is the current selection', () => {
    const suitableOption = {
      value: 'openai/gpt-5.2::api-key',
      suitable: true,
      suitabilityReasons: [],
    };
    const unsuitableOption = {
      value: 'ollama/tiny::local',
      suitable: false,
      suitabilityReasons: ['belowMinContext'],
    };
    const emptyOption = { value: '', label: 'None', isUnavailable: false };
    const options = [emptyOption, suitableOption, unsuitableOption];

    expect(filterModelSelectOptions(options)).toEqual([
      emptyOption,
      suitableOption,
    ]);
    expect(filterModelSelectOptions(options, { showAll: true })).toEqual(
      options,
    );
    for (const selectedModelValue of [
      'ollama/tiny::local',
      'ollama/tiny::local:default',
    ]) {
      expect(filterModelSelectOptions(options, { selectedModelValue })).toEqual(
        options,
      );
    }
  });

  it('labels the footer toggle with the hidden count or the way back', () => {
    expect(modelFilterFooterLabel({ showAll: false, hiddenCount: 3 })).toBe(
      t('models.filter.showAll', {
        count: 3,
      }),
    );
    expect(modelFilterFooterLabel({ showAll: true })).toBe(
      t('models.filter.showSuitable'),
    );
    expect(modelFilterFooterLabel({ showAll: false, hiddenCount: 0 })).toBe('');
  });
});

describe('model selection value', () => {
  it('keeps the account part inside the connection suffix', () => {
    const selection = parseModelSelectionValue('openai/gpt-5.2::api-key:work');

    expect(selection).toEqual({
      model: 'openai/gpt-5.2',
      connectionLocalId: 'api-key:work',
    });
    expect(
      modelSelectionValue(selection.model, selection.connectionLocalId),
    ).toBe('openai/gpt-5.2::api-key:work');
  });

  it('reduces a stored model address to its final model segment', () => {
    expect(modelShortName('openai/gpt-5.5::subscription')).toBe('gpt-5.5');
    expect(
      modelShortName('openrouter/poolside/laguna-xs.2:free::api-key:work'),
    ).toBe('laguna-xs.2:free');
    expect(modelShortName('custom-model')).toBe('custom-model');
    expect(modelShortName('')).toBe('');
  });
});
