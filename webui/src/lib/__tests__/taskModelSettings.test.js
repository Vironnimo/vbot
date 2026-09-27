import { describe, expect, it } from 'vitest';

import {
  TASK_IMAGE_GENERATION,
  TASK_SPEECH_TO_TEXT,
  TASK_TEXT_EMBEDDING,
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

    expect(payload[TASK_SPEECH_TO_TEXT]).toEqual({
      target: 'openrouter/openai/gpt-4o-transcribe::api-key',
      options: { language: 'auto' },
    });
    expect(payload[TASK_TEXT_EMBEDDING]).toEqual({
      target: 'openrouter/google/gemini-embedding-2::api-key',
      options: { dimensions: 768 },
    });
    // Unbound rows are sent as an explicit "not configured".
    expect(payload[TASK_IMAGE_GENERATION]).toEqual({ target: '', options: {} });
    expect(
      taskModelBindingsMatch(bindings, normalizeTaskModelSettings(raw)),
    ).toBe(true);
    expect(taskModelBindingsMatch(bindings, {})).toBe(false);
  });

  it('sends only the rows that changed against the saved bindings', () => {
    const saved = normalizeTaskModelSettings(raw);
    const draft = {
      ...saved,
      [TASK_IMAGE_GENERATION]: { target: 'openai/gpt-image-1', options: {} },
    };

    expect(createTaskModelUpdatePayload(draft, saved)).toEqual({
      [TASK_IMAGE_GENERATION]: { target: 'openai/gpt-image-1', options: {} },
    });
  });

  it('normalizes targets and option schemas', () => {
    expect(
      normalizeTargets({
        targets: [{ id: 'target-1', label: 'Target 1' }, { label: 'No id' }],
      }),
    ).toEqual([
      { id: 'target-1', label: 'Target 1', usable: true, kind: 'provider' },
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
        name: 'backend_model',
        type: 'select',
        default: 'terra',
        options: [
          { value: 'terra', label: 'Terra' },
          { value: 'astra', label: 'Astra' },
          { value: 'luna', label: 'Luna' },
        ],
      },
      {
        name: 'backend_thinking_effort',
        type: 'select',
        default: 'low',
        options: EFFORTS.map((value) => ({
          value,
          label: value || 'Model default',
        })),
        options_by: {
          field: 'backend_model',
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
  const [backendField, effortField] = fields;
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
      field: 'backend_model',
      values: {
        terra: ['', 'none', 'low', 'high'],
        astra: ['', 'none', 'medium', 'max'],
        luna: ['medium', 'max'],
        '': [],
      },
    });
    expect(backendField.optionsBy).toBeNull();
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
    expect(shownValues({ backend_model: 'astra' })).toEqual([
      '',
      'none',
      'medium',
      'max',
    ]);
    expect(shownValues({ backend_model: 'unlisted' })).toEqual(EFFORTS);
    expect(visibleFieldOptions(backendField, fields, {})).toBe(
      backendField.options,
    );
  });

  it('hides a field whose entry for the referenced value is an empty list', () => {
    const none = { backend_model: '', backend_thinking_effort: 'high' };

    expect(isOptionFieldHidden(effortField, fields, none)).toBe(true);
    expect(shownValues(none)).toEqual([]);
    expect(isOptionFieldHidden(effortField, fields, {})).toBe(false);
    expect(
      isOptionFieldHidden(effortField, fields, { backend_model: 'unlisted' }),
    ).toBe(false);
    expect(isOptionFieldHidden(backendField, fields, none)).toBe(false);
    // The hidden field keeps its stored value; the server ignores it.
    expect(reconcileDependentOptions(fields, none, 'backend_model')).toBe(none);
    expect(
      reconcileDependentOptions(
        fields,
        { ...none, backend_model: 'astra' },
        'backend_model',
      ),
    ).toEqual({ backend_model: 'astra', backend_thinking_effort: '' });
  });

  it('keeps a value that stays visible after the referenced option changes', () => {
    const options = { backend_model: 'astra', backend_thinking_effort: 'none' };

    expect(reconcileDependentOptions(fields, options, 'backend_model')).toBe(
      options,
    );
    expect(
      reconcileDependentOptions(fields, options, 'backend_thinking_effort'),
    ).toBe(options);
  });

  it.each([
    ['the default when shown', { backend_model: 'terra' }, 'medium', 'low'],
    [
      'Model default when the default is hidden',
      { backend_model: 'astra' },
      'high',
      '',
    ],
    [
      'the first choice when neither is shown',
      { backend_model: 'luna' },
      'none',
      'medium',
    ],
  ])('moves a hidden value to %s', (_label, backend, current, expected) => {
    const next = reconcileDependentOptions(
      fields,
      { ...backend, backend_thinking_effort: current },
      'backend_model',
    );

    expect(next).toEqual({ ...backend, backend_thinking_effort: expected });
  });

  it('moves an unstored default that the new choice hides', () => {
    expect(
      reconcileDependentOptions(
        fields,
        { backend_model: 'astra' },
        'backend_model',
      ),
    ).toEqual({ backend_model: 'astra', backend_thinking_effort: '' });
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
