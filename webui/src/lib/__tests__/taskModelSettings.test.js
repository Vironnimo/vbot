import { describe, expect, it } from 'vitest';

import {
  createTaskModelUpdatePayload,
  isOptionFieldHidden,
  normalizeOptionSchema,
  normalizeTargets,
  normalizeTaskModelSettings,
  parseJsonFieldValue,
  reconcileDependentOptions,
  stringifyJsonFieldValue,
  taskModelBindingsMatch,
  visibleFieldOptions,
} from '../taskModelSettings.js';

describe('task model bindings', () => {
  const raw = {
    model_tasks: {
      speech_to_text: {
        target: ' openrouter/openai/gpt-4o-transcribe::api-key ',
        options: { language: 'auto' },
      },
      text_embedding: {
        target: 'openrouter/google/gemini-embedding-2::api-key',
        options: { dimensions: 768, unset: undefined },
      },
    },
  };

  it('normalizes bindings and sends every task row in the update payload', () => {
    const bindings = normalizeTaskModelSettings(raw);
    const payload = createTaskModelUpdatePayload(bindings);

    expect(payload.speech_to_text).toEqual({
      target: 'openrouter/openai/gpt-4o-transcribe::api-key',
      options: { language: 'auto' },
    });
    expect(payload.text_embedding).toEqual({
      target: 'openrouter/google/gemini-embedding-2::api-key',
      options: { dimensions: 768 },
    });
    // Unbound rows are sent as an explicit "not configured".
    expect(payload.image_generation).toEqual({ target: '', options: {} });
    expect(
      taskModelBindingsMatch(bindings, normalizeTaskModelSettings(raw)),
    ).toBe(true);
    expect(taskModelBindingsMatch(bindings, {})).toBe(false);
  });

  it('sends only the rows that changed against the saved bindings', () => {
    const saved = normalizeTaskModelSettings(raw);
    const draft = {
      ...saved,
      image_generation: { target: 'openai/gpt-image-1', options: {} },
    };

    expect(createTaskModelUpdatePayload(draft, saved)).toEqual({
      image_generation: { target: 'openai/gpt-image-1', options: {} },
    });
  });

  it('normalizes targets and option schemas', () => {
    expect(
      normalizeTargets({
        targets: [
          { id: 'target-1', label: 'Target 1' },
          {
            id: 'openai/text-embedding-3-small',
            label: 'OpenAI / text-embedding-3-small',
            kind: 'provider',
            provider_id: 'openai',
            facts: { local: false, recommended_rank: 3 },
          },
          {
            id: 'local/granite-embedding-r2',
            label: 'Granite',
            kind: 'local',
            usable: false,
            metadata: { license: 'Apache-2.0', download_bytes: 346806730 },
          },
          { label: 'No id' },
        ],
      }),
    ).toEqual([
      {
        id: 'target-1',
        label: 'Target 1',
        usable: true,
        kind: 'provider',
        providerId: '',
        facts: {},
        metadata: {},
      },
      {
        id: 'openai/text-embedding-3-small',
        label: 'OpenAI / text-embedding-3-small',
        usable: true,
        kind: 'provider',
        providerId: 'openai',
        facts: { local: false, recommended_rank: 3 },
        metadata: {},
      },
      {
        id: 'local/granite-embedding-r2',
        label: 'Granite',
        usable: false,
        kind: 'local',
        providerId: '',
        facts: {},
        metadata: { license: 'Apache-2.0', download_bytes: 346806730 },
      },
    ]);

    const fields = normalizeOptionSchema({
      schema: {
        fields: [
          { name: 'voice', type: 'select', default: 'alloy' },
          { name: 'text_layout', type: 'json', label: 'Text layout' },
          { type: 'text' },
        ],
      },
    });
    expect(
      fields.map((field) => [field.name, field.type, field.default]),
    ).toEqual([
      ['voice', 'select', 'alloy'],
      ['text_layout', 'json', ''],
    ]);
  });
});

describe('JSON option fields', () => {
  it('parses JSON text into a value or an inline error', () => {
    const layout = [
      {
        text: 'hi',
        bbox: [
          [0, 0],
          [1, 1],
        ],
      },
    ];

    expect(parseJsonFieldValue(JSON.stringify(layout))).toEqual({
      value: layout,
      error: '',
    });
    expect(parseJsonFieldValue('42').value).toBe(42);
    expect(parseJsonFieldValue('null')).toEqual({ value: null, error: '' });
    // Empty input is a binding without a value, not an error.
    expect(parseJsonFieldValue('')).toEqual({ value: undefined, error: '' });

    for (const invalid of ['[{"text": "hi"', 'not json at all']) {
      const result = parseJsonFieldValue(invalid);
      expect(result.value, invalid).toBeUndefined();
      expect(result.error.length, invalid).toBeGreaterThan(0);
    }
  });

  it('renders stored values for the textarea and round-trips them', () => {
    const value = {
      items: [{ key: 'clarity', weight: 0.6 }, { key: 'style' }],
      background: null,
    };
    const text = stringifyJsonFieldValue(value);

    expect(text).toBe(JSON.stringify(value, null, 2));
    expect(parseJsonFieldValue(text)).toEqual({ value, error: '' });
    expect(stringifyJsonFieldValue(undefined)).toBe('');
    expect(stringifyJsonFieldValue(null)).toBe('');
    // Pre-stringified JSON passes through without double encoding.
    expect(stringifyJsonFieldValue('[{"a":1}]')).toBe('[{"a":1}]');
  });
});

describe('select choices narrowed by another option', () => {
  const EFFORTS = ['', 'none', 'low', 'medium', 'high', 'max'];
  const fields = normalizeOptionSchema({
    fields: [
      {
        name: 'variant',
        type: 'select',
        default: 'terra',
        options: [
          { value: 'terra', label: 'Terra' },
          { value: 'astra', label: 'Astra' },
          { value: 'luna', label: 'Luna' },
        ],
      },
      {
        name: 'effort',
        type: 'select',
        default: 'low',
        options: EFFORTS.map((value) => ({
          value,
          label: value || 'Model default',
        })),
        options_by: {
          field: 'variant',
          values: {
            terra: ['', 'none', 'low', 'high'],
            astra: ['', 'none', 'medium', 'max'],
            luna: ['medium', 'max'],
            '': [],
          },
        },
      },
    ],
  });
  const [variantField, effortField] = fields;
  const shownValues = (options) =>
    visibleFieldOptions(effortField, fields, options).map(
      (choice) => choice.value,
    );

  it('keeps empty-value choices and normalizes the narrowing', () => {
    expect(effortField.options[0]).toEqual({
      value: '',
      label: 'Model default',
    });
    expect(effortField.optionsBy).toEqual({
      field: 'variant',
      values: {
        terra: ['', 'none', 'low', 'high'],
        astra: ['', 'none', 'medium', 'max'],
        luna: ['medium', 'max'],
        '': [],
      },
    });
    expect(variantField.optionsBy).toBeNull();
    const [malformed] = normalizeOptionSchema({
      fields: [
        {
          name: 'tier',
          type: 'select',
          options: [{ value: null, label: 'Broken' }, { value: 'fast' }],
          options_by: { field: '', values: {} },
        },
      ],
    });
    expect(malformed.options).toEqual([{ value: 'fast', label: 'fast' }]);
    expect(malformed.optionsBy).toBeNull();
  });

  it('filters by the stored or default value of the referenced option', () => {
    expect(shownValues({})).toEqual(['', 'none', 'low', 'high']);
    expect(shownValues({ variant: 'astra' })).toEqual([
      '',
      'none',
      'medium',
      'max',
    ]);
    expect(shownValues({ variant: 'unlisted' })).toEqual(EFFORTS);
    expect(visibleFieldOptions(variantField, fields, {})).toBe(
      variantField.options,
    );
  });

  it('hides a field whose entry for the referenced value is an empty list', () => {
    const hiding = { variant: '', effort: 'high' };

    expect(isOptionFieldHidden(effortField, fields, hiding)).toBe(true);
    expect(shownValues(hiding)).toEqual([]);
    expect(isOptionFieldHidden(effortField, fields, {})).toBe(false);
    expect(
      isOptionFieldHidden(effortField, fields, { variant: 'unlisted' }),
    ).toBe(false);
    expect(isOptionFieldHidden(variantField, fields, hiding)).toBe(false);
    // The hidden field keeps its stored value; the server ignores it.
    expect(reconcileDependentOptions(fields, hiding, 'variant')).toBe(hiding);
    expect(
      reconcileDependentOptions(
        fields,
        { ...hiding, variant: 'astra' },
        'variant',
      ),
    ).toEqual({ variant: 'astra', effort: '' });
  });

  it('keeps a value that stays visible after the referenced option changes', () => {
    const options = { variant: 'astra', effort: 'none' };

    expect(reconcileDependentOptions(fields, options, 'variant')).toBe(options);
    expect(reconcileDependentOptions(fields, options, 'effort')).toBe(options);
  });

  it.each([
    ['the default when shown', { variant: 'terra' }, 'medium', 'low'],
    [
      'Model default when the default is hidden',
      { variant: 'astra' },
      'high',
      '',
    ],
    [
      'the first choice when neither is shown',
      { variant: 'luna' },
      'none',
      'medium',
    ],
  ])('moves a hidden value to %s', (_label, chosen, current, expected) => {
    const next = reconcileDependentOptions(
      fields,
      { ...chosen, effort: current },
      'variant',
    );

    expect(next).toEqual({ ...chosen, effort: expected });
  });

  it('moves an unstored default that the new choice hides', () => {
    expect(
      reconcileDependentOptions(fields, { variant: 'astra' }, 'variant'),
    ).toEqual({ variant: 'astra', effort: '' });
  });

  it('follows chains of narrowed options', () => {
    const chained = normalizeOptionSchema({
      fields: [
        {
          name: 'engine',
          type: 'select',
          default: 'big',
          options: [{ value: 'big' }, { value: 'small' }],
        },
        {
          name: 'mode',
          type: 'select',
          default: 'deep',
          options: [{ value: 'deep' }, { value: 'fast' }],
          options_by: { field: 'engine', values: { small: ['fast'] } },
        },
        {
          name: 'depth',
          type: 'select',
          default: '3',
          options: [{ value: '3' }, { value: '1' }],
          options_by: { field: 'mode', values: { fast: ['1'] } },
        },
      ],
    });

    expect(
      reconcileDependentOptions(chained, { engine: 'small' }, 'engine'),
    ).toEqual({ engine: 'small', mode: 'fast', depth: '1' });
  });
});
