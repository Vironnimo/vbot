import { describe, expect, it } from 'vitest';

import {
  JSON_OPTION_TYPE,
  TASK_IMAGE_GENERATION,
  TASK_SPEECH_TO_TEXT,
  TASK_TEXT_EMBEDDING,
  TASK_TEXT_TO_SPEECH,
  TASK_MODEL_ROWS,
  applyOptionDefaults,
  createTaskModelUpdatePayload,
  normalizeOptionSchema,
  normalizeTargets,
  normalizeTaskModelSettings,
  parseJsonFieldValue,
  stringifyJsonFieldValue,
  taskModelBindingsMatch,
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
        TASK_IMAGE_GENERATION,
        TASK_TEXT_EMBEDDING,
      ]),
    );
    const embeddingRow = TASK_MODEL_ROWS.find(
      (row) => row.taskType === TASK_TEXT_EMBEDDING,
    );
    expect(embeddingRow).toBeTruthy();
    expect(embeddingRow.titleKey).toBe(
      'settings.specializedModels.embeddingModel',
    );
    expect(embeddingRow.titleFallback).toBe('Embedding model');
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
