import { describe, expect, it } from 'vitest';

import {
  SUITABLE_MIN_CONTEXT,
  buildModelSelectOptions,
  filterModelSelectOptions,
  modelFilterFooterLabel,
  modelSelectionValue,
  modelSuitability,
  parseModelSelectionValue,
  selectModelValue,
} from '../modelSelection.js';

function translateWithValues(_key, fallback, values = {}) {
  return fallback.replace(/\{(\w+)\}/g, (match, name) =>
    name in values ? values[name] : match,
  );
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
        isUnavailable: false,
        suitable: true,
        suitabilityReasons: [],
      },
    ]);
  });

  it('keeps one unpinned option when only one account is usable', () => {
    const options = buildModelSelectOptions({
      models: [catalogModel('openai/gpt-5.2', 'openai')],
      connections: [
        usableConnection('openai:api-key', 'openai', 'API Key', [
          account('default'),
          account('work', false),
        ]),
      ],
    });

    expect(options.slice(1)).toEqual([
      {
        value: 'openai/gpt-5.2::api-key',
        label: 'openai/gpt-5.2',
        isUnavailable: false,
        suitable: true,
        suitabilityReasons: [],
      },
    ]);
  });

  it('expands one option per usable account on a multi-account connection', () => {
    const options = buildModelSelectOptions({
      models: [catalogModel('openai/gpt-5.2', 'openai')],
      connections: [
        usableConnection('openai:api-key', 'openai', 'API Key', [
          account('default'),
          account('work'),
        ]),
      ],
    });

    expect(options.slice(1)).toEqual([
      {
        value: 'openai/gpt-5.2::api-key',
        label: 'openai/gpt-5.2 (Default)',
        isUnavailable: false,
        suitable: true,
        suitabilityReasons: [],
      },
      {
        value: 'openai/gpt-5.2::api-key:work',
        label: 'openai/gpt-5.2 (work)',
        isUnavailable: false,
        suitable: true,
        suitabilityReasons: [],
      },
    ]);
  });

  it('labels accounts with the connection when the provider has several connections', () => {
    const options = buildModelSelectOptions({
      models: [catalogModel('openai/gpt-5.2', 'openai')],
      connections: [
        usableConnection('openai:api-key', 'openai', 'API Key', [
          account('default'),
          account('work'),
        ]),
        usableConnection('openai:subscription', 'openai', 'Subscription'),
      ],
    });

    expect(options.slice(1)).toEqual([
      {
        value: 'openai/gpt-5.2::api-key',
        label: 'openai/gpt-5.2 (API Key – Default)',
        isUnavailable: false,
        suitable: true,
        suitabilityReasons: [],
      },
      {
        value: 'openai/gpt-5.2::api-key:work',
        label: 'openai/gpt-5.2 (API Key – work)',
        isUnavailable: false,
        suitable: true,
        suitabilityReasons: [],
      },
      {
        value: 'openai/gpt-5.2::subscription',
        label: 'openai/gpt-5.2 (Subscription)',
        isUnavailable: false,
        suitable: true,
        suitabilityReasons: [],
      },
    ]);
  });

  it('treats an account-pinned selection matching a catalog option as available', () => {
    const options = buildModelSelectOptions({
      models: [catalogModel('openai/gpt-5.2', 'openai')],
      connections: [
        usableConnection('openai:api-key', 'openai', 'API Key', [
          account('default'),
          account('work'),
        ]),
      ],
      selectedModelValue: 'openai/gpt-5.2::api-key:work',
    });

    expect(options.some((option) => option.isUnavailable)).toBe(false);
  });

  it('treats an explicit default-account pin as the unpinned option', () => {
    const options = buildModelSelectOptions({
      models: [catalogModel('openai/gpt-5.2', 'openai')],
      connections: [usableConnection('openai:api-key', 'openai', 'API Key')],
      selectedModelValue: 'openai/gpt-5.2::api-key:default',
    });

    expect(options.some((option) => option.isUnavailable)).toBe(false);
  });

  it('only offers a connection-restricted model on its allowed connection', () => {
    const options = buildModelSelectOptions({
      models: [catalogModel('openai/gpt-5.4', 'openai', ['subscription'])],
      connections: [
        usableConnection('openai:api-key', 'openai', 'API Key'),
        usableConnection('openai:subscription', 'openai', 'Subscription'),
      ],
    });

    expect(options.slice(1)).toEqual([
      {
        value: 'openai/gpt-5.4::subscription',
        label: 'openai/gpt-5.4',
        isUnavailable: false,
        suitable: true,
        suitabilityReasons: [],
      },
    ]);
  });

  it('drops a restricted model entirely when no allowed connection is usable', () => {
    const options = buildModelSelectOptions({
      models: [catalogModel('openai/gpt-5.2', 'openai', ['api-key'])],
      connections: [
        usableConnection('openai:subscription', 'openai', 'Subscription'),
      ],
      emptyLabel: 'None',
    });

    expect(options).toEqual([
      { value: '', label: 'None', isUnavailable: false },
    ]);
  });

  it('marks a saved selection on a now-forbidden connection as unavailable', () => {
    const options = buildModelSelectOptions({
      models: [catalogModel('openai/gpt-5.4', 'openai', ['subscription'])],
      connections: [
        usableConnection('openai:api-key', 'openai', 'API Key'),
        usableConnection('openai:subscription', 'openai', 'Subscription'),
      ],
      selectedModelValue: 'openai/gpt-5.4::api-key',
      translate: translateWithValues,
    });

    expect(options[1]).toEqual({
      value: 'openai/gpt-5.4::api-key',
      label: 'Unavailable / custom: openai/gpt-5.4 (API Key)',
      isUnavailable: true,
    });
    expect(
      options.some((option) => option.value === 'openai/gpt-5.4::subscription'),
    ).toBe(true);
  });

  it('marks a selection pinned to an unknown account as unavailable with the account in the label', () => {
    const options = buildModelSelectOptions({
      models: [catalogModel('openai/gpt-5.2', 'openai')],
      connections: [
        usableConnection('openai:api-key', 'openai', 'API Key', [
          account('default'),
          account('work'),
        ]),
      ],
      selectedModelValue: 'openai/gpt-5.2::api-key:old',
      translate: translateWithValues,
    });

    expect(options[1]).toEqual({
      value: 'openai/gpt-5.2::api-key:old',
      label: 'Unavailable / custom: openai/gpt-5.2 (API Key – old)',
      isUnavailable: true,
    });
  });
});

describe('selectModelValue', () => {
  const options = buildModelSelectOptions({
    models: [catalogModel('openai/gpt-5.2', 'openai')],
    connections: [
      usableConnection('openai:api-key', 'openai', 'API Key', [
        account('default'),
        account('work'),
      ]),
    ],
  });

  it('returns the exact value for an account-pinned option', () => {
    expect(selectModelValue('openai/gpt-5.2::api-key:work', options)).toBe(
      'openai/gpt-5.2::api-key:work',
    );
  });

  it('normalizes an explicit default-account pin to the unpinned option', () => {
    expect(selectModelValue('openai/gpt-5.2::api-key:default', options)).toBe(
      'openai/gpt-5.2::api-key',
    );
  });

  it('keeps an unknown account pin verbatim so the unavailable option matches', () => {
    expect(selectModelValue('openai/gpt-5.2::api-key:old', options)).toBe(
      'openai/gpt-5.2::api-key:old',
    );
  });
});

describe('model selection value round-trip', () => {
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
});

describe('model suitability filter', () => {
  it('marks a tools + big-context model suitable', () => {
    expect(
      modelSuitability({
        capabilities: { tools: true },
        context_window: 200000,
        effective_context_window: 200000,
      }),
    ).toEqual({ suitable: true, reasons: [] });
  });

  it('flags missing tool calling', () => {
    expect(
      modelSuitability({
        capabilities: { tools: false },
        effective_context_window: 200000,
      }),
    ).toEqual({ suitable: false, reasons: ['noTools'] });
  });

  it('flags an effective window below the threshold', () => {
    expect(
      modelSuitability({
        capabilities: { tools: true },
        context_window: 262144,
        effective_context_window: 16384,
      }),
    ).toEqual({ suitable: false, reasons: ['belowMinContext'] });
  });

  it('treats exactly 32k as suitable', () => {
    expect(
      modelSuitability({
        capabilities: { tools: true },
        effective_context_window: SUITABLE_MIN_CONTEXT,
      }),
    ).toEqual({ suitable: true, reasons: [] });
  });

  it('flags an unknown context honestly as unknown, not below-32k', () => {
    expect(
      modelSuitability({
        capabilities: { tools: true },
        context_window: null,
        effective_context_window: null,
      }),
    ).toEqual({ suitable: false, reasons: ['contextUnknown'] });
  });

  it('falls back to the raw window when no effective window is present', () => {
    expect(
      modelSuitability({ capabilities: { tools: true }, context_window: 8192 }),
    ).toEqual({ suitable: false, reasons: ['belowMinContext'] });
  });

  it('annotates unsuitable catalog options with a badge', () => {
    const options = buildModelSelectOptions({
      models: [
        {
          id: 'ollama/tiny',
          provider_id: 'ollama',
          name: 'ollama/tiny',
          capabilities: { tools: false },
          context_window: 8192,
          effective_context_window: 8192,
        },
      ],
      connections: [usableConnection('ollama:local', 'ollama', 'Local')],
      emptyLabel: 'None',
      translate: translateWithValues,
    });

    expect(options[1].suitable).toBe(false);
    expect(options[1].suitabilityReasons).toEqual([
      'noTools',
      'belowMinContext',
    ]);
    expect(options[1].secondaryLabel).toBe(
      'no tool calling · below 32k context',
    );
  });
});

describe('filterModelSelectOptions', () => {
  const suitableOption = {
    value: 'openai/gpt-5.2::api-key',
    label: 'openai/gpt-5.2',
    suitable: true,
    suitabilityReasons: [],
  };
  const unsuitableOption = {
    value: 'ollama/tiny::local',
    label: 'ollama/tiny',
    suitable: false,
    suitabilityReasons: ['belowMinContext'],
  };
  const emptyOption = { value: '', label: 'None', isUnavailable: false };
  const options = [emptyOption, suitableOption, unsuitableOption];

  it('hides unsuitable options by default', () => {
    expect(filterModelSelectOptions(options)).toEqual([
      emptyOption,
      suitableOption,
    ]);
  });

  it('reveals everything with showAll', () => {
    expect(filterModelSelectOptions(options, { showAll: true })).toEqual(
      options,
    );
  });

  it('keeps the currently selected unsuitable option visible', () => {
    expect(
      filterModelSelectOptions(options, {
        selectedModelValue: 'ollama/tiny::local',
      }),
    ).toEqual(options);
  });

  it('keeps a default-account pinned selection visible via its canonical value', () => {
    expect(
      filterModelSelectOptions(options, {
        selectedModelValue: 'ollama/tiny::local:default',
      }),
    ).toEqual(options);
  });
});

describe('modelFilterFooterLabel', () => {
  it('offers to reveal with the hidden count', () => {
    expect(
      modelFilterFooterLabel({
        showAll: false,
        hiddenCount: 3,
        translate: translateWithValues,
      }),
    ).toBe('Show all models (3 hidden)');
  });

  it('offers to restore the filter when everything is shown', () => {
    expect(
      modelFilterFooterLabel({ showAll: true, translate: translateWithValues }),
    ).toBe('Show only suitable models');
  });

  it('shows nothing when the filter hides nothing', () => {
    expect(
      modelFilterFooterLabel({
        showAll: false,
        hiddenCount: 0,
        translate: translateWithValues,
      }),
    ).toBe('');
  });
});
