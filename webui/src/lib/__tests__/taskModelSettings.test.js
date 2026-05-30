import { describe, expect, it } from 'vitest';

import {
  TASK_SPEECH_TO_TEXT,
  applyOptionDefaults,
  createTaskModelUpdatePayload,
  normalizeOptionSchema,
  normalizeTargets,
  normalizeTaskModelSettings,
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
});
