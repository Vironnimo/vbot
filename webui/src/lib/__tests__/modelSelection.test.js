import { describe, expect, it } from 'vitest';

import {
  buildModelSelectOptions,
  modelSelectionValue,
  parseModelSelectionValue,
  selectModelValue,
} from '../modelSelection.js';

function translateWithValues(_key, fallback, values = {}) {
  return fallback.replace(/\{(\w+)\}/g, (match, name) =>
    name in values ? values[name] : match,
  );
}

function catalogModel(id, providerId) {
  return { id, provider_id: providerId, name: id };
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
      },
      {
        value: 'openai/gpt-5.2::api-key:work',
        label: 'openai/gpt-5.2 (work)',
        isUnavailable: false,
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
      },
      {
        value: 'openai/gpt-5.2::api-key:work',
        label: 'openai/gpt-5.2 (API Key – work)',
        isUnavailable: false,
      },
      {
        value: 'openai/gpt-5.2::subscription',
        label: 'openai/gpt-5.2 (Subscription)',
        isUnavailable: false,
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
