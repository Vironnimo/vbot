import { describe, expect, it } from 'vitest';

import {
  JSON_OPTION_TYPE,
  TASK_IMAGE_GENERATION,
  TASK_IMAGE_UNDERSTANDING,
  TASK_LIVE_VOICE,
  TASK_MUSIC_GENERATION,
  TASK_SPEECH_TO_TEXT,
  TASK_TEXT_EMBEDDING,
  TASK_TEXT_TO_SPEECH,
  TASK_VIDEO_GENERATION,
  TASK_MODEL_ROWS,
  applyOptionDefaults,
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

describe('taskModelSettings helpers', () => {
  it('normalizes bindings and builds sparse update payloads', () => {
    const bindings = normalizeTaskModelSettings({
      model_tasks: {
        speech_to_text: {
          target: ' openrouter/openai/gpt-4o-transcribe::api-key ',
          options: { language: 'auto' },
        },
      },
    });

    expect(bindings.speech_to_text.target).toBe(
      'openrouter/openai/gpt-4o-transcribe::api-key',
    );
    expect(createTaskModelUpdatePayload(bindings).speech_to_text).toEqual({
      target: 'openrouter/openai/gpt-4o-transcribe::api-key',
      options: { language: 'auto' },
    });
  });

  it('normalizes targets and schema defaults', () => {
    const targets = normalizeTargets({
      targets: [{ id: 'target-1', label: 'Target 1' }],
    });
    const fields = normalizeOptionSchema({
      schema: {
        fields: [{ name: 'voice', type: 'select', default: 'alloy' }],
      },
    });

    expect(targets).toEqual([
      { id: 'target-1', label: 'Target 1', usable: true, kind: 'provider' },
    ]);
    expect(
      applyOptionDefaults({ target: 'target-1', options: {} }, fields).options
        .voice,
    ).toBe('alloy');
  });

  it('compares normalized binding payloads', () => {
    expect(
      taskModelBindingsMatch(
        { [TASK_SPEECH_TO_TEXT]: { target: '', options: {} } },
        { [TASK_SPEECH_TO_TEXT]: { target: '', options: {} } },
      ),
    ).toBe(true);
  });

  it('exposes the text_embedding row in TASK_MODEL_ROWS alongside the existing tasks', () => {
    const taskTypes = TASK_MODEL_ROWS.map((row) => row.taskType);
    expect(taskTypes).toEqual(
      expect.arrayContaining([
        TASK_SPEECH_TO_TEXT,
        TASK_TEXT_TO_SPEECH,
        TASK_LIVE_VOICE,
        TASK_IMAGE_UNDERSTANDING,
        TASK_IMAGE_GENERATION,
        TASK_VIDEO_GENERATION,
        TASK_MUSIC_GENERATION,
        TASK_TEXT_EMBEDDING,
      ]),
    );
    expect(TASK_LIVE_VOICE).toBe('live_voice');
    expect(
      TASK_MODEL_ROWS.find((row) => row.taskType === TASK_LIVE_VOICE).titleKey,
    ).toBe('settings.specializedModels.liveVoice');
    const embeddingRow = TASK_MODEL_ROWS.find(
      (row) => row.taskType === TASK_TEXT_EMBEDDING,
    );
    expect(embeddingRow).toBeTruthy();
    expect(embeddingRow.titleKey).toBe(
      'settings.specializedModels.embeddingModel',
    );
    expect(embeddingRow.titleFallback).toBe('Embedding model');
    const imageUnderstandingRow = TASK_MODEL_ROWS.find(
      (row) => row.taskType === TASK_IMAGE_UNDERSTANDING,
    );
    expect(imageUnderstandingRow.titleFallback).toBe('Image understanding');
    expect(imageUnderstandingRow.descriptionKey).toBe(
      'settings.specializedModels.imageUnderstandingDescription',
    );
    expect(
      TASK_MODEL_ROWS.find((row) => row.taskType === TASK_VIDEO_GENERATION)
        .descriptionFallback,
    ).toContain('generate_video');
    expect(
      TASK_MODEL_ROWS.find((row) => row.taskType === TASK_MUSIC_GENERATION)
        .descriptionFallback,
    ).toContain('generate_music');
  });

  it('normalizes an embedding binding and includes it in the update payload', () => {
    const bindings = normalizeTaskModelSettings({
      model_tasks: {
        text_embedding: {
          target: 'openrouter/google/gemini-embedding-2::api-key',
          options: { dimensions: 768 },
        },
      },
    });

    expect(bindings[TASK_TEXT_EMBEDDING].target).toBe(
      'openrouter/google/gemini-embedding-2::api-key',
    );
    const payload = createTaskModelUpdatePayload(bindings);
    expect(payload[TASK_TEXT_EMBEDDING]).toEqual({
      target: 'openrouter/google/gemini-embedding-2::api-key',
      options: { dimensions: 768 },
    });
    // Other rows must still be present in the sparse payload so the
    // server receives an explicit "not configured" for them.
    expect(payload[TASK_SPEECH_TO_TEXT]).toEqual({ target: '', options: {} });
    expect(payload[TASK_IMAGE_UNDERSTANDING]).toEqual({
      target: '',
      options: {},
    });
    expect(payload[TASK_IMAGE_GENERATION]).toEqual({ target: '', options: {} });
  });
});

describe('JSON option field helpers', () => {
  it('exposes a json field type constant for the renderer', () => {
    expect(JSON_OPTION_TYPE).toBe('json');
  });

  it('parses a valid JSON object/array string into a structured value', () => {
    const text = JSON.stringify([
      {
        text: 'hi',
        bbox: [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 1],
        ],
      },
    ]);
    const result = parseJsonFieldValue(text);

    expect(result.error).toBe('');
    expect(result.value).toEqual([
      {
        text: 'hi',
        bbox: [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 1],
        ],
      },
    ]);
  });

  it('parses JSON primitives (numbers, booleans, null) the same way the spec wants', () => {
    expect(parseJsonFieldValue('42').value).toBe(42);
    expect(parseJsonFieldValue('true').value).toBe(true);
    expect(parseJsonFieldValue('null').value).toBeNull();
    expect(parseJsonFieldValue('"hello"').value).toBe('hello');
  });

  it('returns an empty error result for empty input (a binding with no value)', () => {
    const result = parseJsonFieldValue('');

    expect(result.error).toBe('');
    expect(result.value).toBeUndefined();
  });

  it('reports a non-empty error and undefined value for invalid JSON', () => {
    const result = parseJsonFieldValue('[{"text": "hi"');

    expect(result.error.length).toBeGreaterThan(0);
    expect(result.value).toBeUndefined();
  });

  it('reports a non-empty error for non-JSON text', () => {
    const result = parseJsonFieldValue('not json at all');

    expect(result.error.length).toBeGreaterThan(0);
    expect(result.value).toBeUndefined();
  });

  it('stringifies arrays and objects for display with a stable indent', () => {
    const value = [
      {
        text: 'hi',
        bbox: [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 1],
        ],
      },
      { text: 'world' },
    ];

    const rendered = stringifyJsonFieldValue(value);
    expect(rendered).toBe(JSON.stringify(value, null, 2));
  });

  it('returns an empty string for undefined/null inputs', () => {
    expect(stringifyJsonFieldValue(undefined)).toBe('');
    expect(stringifyJsonFieldValue(null)).toBe('');
  });

  it('passes through pre-stringified JSON so the textarea does not double-encode', () => {
    const pre = JSON.stringify([{ a: 1 }]);
    expect(stringifyJsonFieldValue(pre)).toBe(pre);
  });

  it('round-trips complex JSON through parse/stringify without loss', () => {
    const original = {
      items: [
        { key: 'clarity', weight: 0.6, passing_score: 0.5 },
        { key: 'style', weight: 0.4 },
      ],
      background: null,
    };
    const text = stringifyJsonFieldValue(original);
    const reparsed = parseJsonFieldValue(text);

    expect(reparsed.error).toBe('');
    expect(reparsed.value).toEqual(original);
  });

  it('keeps a json field type in the normalized schema', () => {
    const fields = normalizeOptionSchema({
      schema: {
        fields: [
          {
            name: 'text_layout',
            type: JSON_OPTION_TYPE,
            label: 'Text layout',
            default: [],
          },
        ],
      },
    });

    expect(fields).toHaveLength(1);
    expect(fields[0].type).toBe('json');
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
