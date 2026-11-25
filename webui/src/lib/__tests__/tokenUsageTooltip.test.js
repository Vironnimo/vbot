import { describe, expect, it } from 'vitest';

import { t } from '../i18n.js';
import {
  automaticCompactionRatio,
  contextLimitWarning,
  contextUsageCardModel,
} from '../tokenUsageTooltip.js';

const label = (key, params) => t(`chat.contextCard.${key}`, params);

describe('contextUsageCardModel', () => {
  it('compares cache shares for each scope and keeps reasoning within output', () => {
    const card = contextUsageCardModel(
      { tokens: 155489 },
      {
        session: {
          input_tokens: 1_500_000,
          output_tokens: 9000,
          cache_read_tokens: 300000,
          reasoning_tokens: 1000,
        },
        run: {
          input_tokens: 50000,
          output_tokens: 4000,
          cache_read_tokens: 30000,
          reasoning_tokens: 600,
        },
        iteration: {
          input_tokens: 20000,
          output_tokens: 1000,
          cache_read_tokens: 18000,
        },
      },
      262144,
    );
    expect(card.summary).toBe('155,489 / 262,144');
    expect(
      card.rows.map(({ label, session, run, iteration }) => [
        label,
        session,
        run,
        iteration,
      ]),
    ).toEqual([
      [label('cacheHit'), '20.0%', '60.0%', '90.0%'],
      [label('input'), '1.5M', '50,000', '20,000'],
      [label('output'), '9,000', '4,000', '1,000'],
      [label('reasoning'), '1,000', '600', '0'],
    ]);
  });

  it('distinguishes a measured zero share from a scope without input', () => {
    const card = contextUsageCardModel(
      { tokens: 900 },
      {
        session: { input_tokens: 500 },
        run: null,
        iteration: { output_tokens: 15 },
      },
      null,
    );
    expect(card.summary).toBe('900');
    expect(card.rows).toHaveLength(3);
    expect(card.rows[0]).toMatchObject({
      session: '0.0%',
      run: '—',
      iteration: '—',
    });
  });

  it('has no headline or rows without any usage', () => {
    expect(contextUsageCardModel(null, {}, 262144)).toEqual({
      summary: null,
      rows: [],
    });
  });
});

describe('automaticCompactionRatio', () => {
  const policy = (trigger, enabled = true) => ({
    enabled,
    trigger,
    strategy: { type: 'summary_tail' },
  });

  it('uses the threshold, or an earlier token cap, of a context-ratio trigger', () => {
    expect(
      automaticCompactionRatio(
        policy({ type: 'context_ratio', threshold: 0.8 }),
        100_000,
      ),
    ).toBe(0.8);
    expect(
      automaticCompactionRatio(
        policy({ type: 'context_ratio', threshold: 0.8, tokens: 50_000 }),
        100_000,
      ),
    ).toBe(0.5);
    expect(
      automaticCompactionRatio(
        policy({ type: 'context_ratio', threshold: 0.6, tokens: 90_000 }),
        100_000,
      ),
    ).toBe(0.6);
  });

  it('relates an input-token trigger to the context window', () => {
    expect(
      automaticCompactionRatio(
        policy({ type: 'input_tokens', tokens: 25_000 }),
        100_000,
      ),
    ).toBe(0.25);
    // A trigger beyond the window never fires before the window is full.
    expect(
      automaticCompactionRatio(
        policy({ type: 'input_tokens', tokens: 200_000 }),
        100_000,
      ),
    ).toBeNull();
  });

  it('reports no trigger when automatic Compaction is off or unknown', () => {
    expect(
      automaticCompactionRatio(
        policy({ type: 'context_ratio', threshold: 0.8 }, false),
        100_000,
      ),
    ).toBeNull();
    expect(automaticCompactionRatio(null, 100_000)).toBeNull();
    expect(
      automaticCompactionRatio(
        policy({ type: 'context_ratio', threshold: 0.8 }),
        null,
      ),
    ).toBeNull();
  });
});

describe('contextLimitWarning', () => {
  const policy = {
    enabled: true,
    trigger: { type: 'input_tokens', tokens: 40_000 },
    strategy: { type: 'continuation' },
  };

  it('warns ahead of the automatic Compaction trigger and at the window end', () => {
    expect(contextLimitWarning(0.29, 100_000, policy).level).toBe('normal');
    const near = contextLimitWarning(0.3, 100_000, policy);
    const reached = contextLimitWarning(0.4, 100_000, policy);
    const full = contextLimitWarning(0.9, 100_000, policy);

    expect([near.level, reached.level, full.level]).toEqual([
      'high',
      'high',
      'critical',
    ]);
    expect(new Set([near.message, reached.message, full.message]).size).toBe(3);
    expect(contextLimitWarning(null, 100_000, policy)).toEqual({
      level: 'normal',
      message: '',
    });
  });

  it('never claims automatic Compaction when it is disabled', () => {
    const disabled = { ...policy, enabled: false };
    expect(contextLimitWarning(0.5, 100_000, disabled).level).toBe('normal');
    const limit = contextLimitWarning(0.7, 100_000, disabled);

    expect(limit.level).toBe('high');
    expect(limit.message).not.toBe(
      contextLimitWarning(0.3, 100_000, policy).message,
    );
  });
});
