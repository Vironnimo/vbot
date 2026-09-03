import { describe, expect, it } from 'vitest';

import {
  buildCompactionPolicyPayload,
  compactionPoliciesEqual,
  normalizeCompactionPolicy,
} from '../compactionPolicy.js';

describe('Compaction Policy helpers', () => {
  it('normalizes both Trigger and Strategy variants', () => {
    expect(
      normalizeCompactionPolicy({
        enabled: false,
        trigger: { type: 'input_tokens', tokens: '120000' },
        strategy: { type: 'continuation' },
      }),
    ).toEqual({
      enabled: false,
      trigger: { type: 'input_tokens', tokens: 120000 },
      strategy: { type: 'continuation' },
    });
  });

  it('normalizes comma ratios and trims Summary Model bindings', () => {
    expect(
      buildCompactionPolicyPayload({
        enabled: true,
        trigger: { type: 'context_ratio', threshold: '0,65' },
        strategy: {
          type: 'summary_tail',
          tail_tokens: '9000',
          summary_model: ' openai/gpt-5 ',
        },
      }),
    ).toEqual({
      enabled: true,
      trigger: { type: 'context_ratio', threshold: 0.65 },
      strategy: {
        type: 'summary_tail',
        tail_tokens: 9000,
        summary_model: 'openai/gpt-5',
      },
    });
  });

  it('combines a context ratio with an optional absolute token cap', () => {
    expect(
      buildCompactionPolicyPayload({
        enabled: true,
        trigger: {
          type: 'context_ratio',
          threshold: '0,8',
          tokens: '200000',
        },
        strategy: { type: 'continuation' },
      }),
    ).toEqual({
      enabled: true,
      trigger: {
        type: 'context_ratio',
        threshold: 0.8,
        tokens: 200000,
      },
      strategy: { type: 'continuation' },
    });
  });

  it('compares normalized Policies rather than draft input types', () => {
    expect(
      compactionPoliciesEqual(
        {
          enabled: true,
          trigger: { type: 'context_ratio', threshold: '0.8' },
          strategy: {
            type: 'summary_tail',
            tail_tokens: '15000',
            summary_model: '',
          },
        },
        undefined,
      ),
    ).toBe(true);
  });
});
