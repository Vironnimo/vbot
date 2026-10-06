import { describe, expect, it } from 'vitest';

import { t } from '../i18n.js';
import {
  automaticCompactionRatio,
  contextLimitWarning,
  contextUsageCardModel,
  formatTokenUsageTooltip,
} from '../tokenUsageTooltip.js';

const label = (key, params) => t(`chat.contextCard.${key}`, params);

function rowsText(section) {
  return section.rows.map(
    (row) => `${row.sub ? '· ' : ''}${row.label}: ${row.value}`,
  );
}

describe('contextUsageCardModel', () => {
  it('leads with the Session cache hit rate and totals, then the last turn', () => {
    const card = contextUsageCardModel(
      {
        tokens: 155489,
        estimated: true,
        provider_input_tokens: 154731,
        provider_output_tokens: 243,
        estimated_delta_tokens: 515,
      },
      {
        input_tokens: 36704,
        output_tokens: 2190,
        cache_read_tokens: 6656,
        cache_write_tokens: 1200,
        reasoning_tokens: 1400,
      },
      {
        // The hit rate divides by the input of every turn.
        input_tokens: 10000,
        output_tokens: 400,
        cache_read_tokens: 3000,
        cache_write_tokens: 500,
        reasoning_tokens: 90,
      },
      262144,
    );

    expect(card.summary).toBe('155,489 / 262,144');
    expect(
      card.sections.map((section) => ({
        id: section.id,
        title: section.title,
        rows: rowsText(section),
      })),
    ).toEqual([
      {
        id: 'session',
        title: '',
        rows: [
          `${label('cacheHitRate')}: 30%`,
          `${label('totalInput')}: 10,000`,
          `${label('totalOutput')}: 400`,
          `· ${label('reasoning')}: 90`,
        ],
      },
      {
        id: 'last-turn',
        title: label('lastTurn'),
        rows: [
          `${label('input')}: 36,704`,
          `· ${label('cacheRead')}: 6,656 (18%)`,
          `· ${label('cacheWrite')}: 1,200`,
          `· ${label('uncached')}: 28,848`,
          `${label('output')}: 2,190`,
          `· ${label('reasoning')}: 1,400`,
        ],
      },
    ]);
  });

  it('counts figures the usage does not report as zero', () => {
    const card = contextUsageCardModel(
      { tokens: 900, estimated: true },
      {
        input_tokens: 500,
        input_tokens_estimated: true,
        output_tokens: 20,
        estimated: true,
      },
      { input_tokens: 500, output_tokens: 20 },
      null,
    );

    expect(card.summary).toBe('900');
    expect(card.sections.map(rowsText)).toEqual([
      [
        `${label('cacheHitRate')}: 0%`,
        `${label('totalInput')}: 500`,
        `${label('totalOutput')}: 20`,
      ],
      [
        `${label('input')}: 500`,
        `· ${label('cacheRead')}: 0 (0%)`,
        `· ${label('cacheWrite')}: 0`,
        `· ${label('uncached')}: 500`,
        `${label('output')}: 20`,
      ],
    ]);
  });

  it('has no headline or sections without any usage', () => {
    expect(contextUsageCardModel(null, null, null, 262144)).toEqual({
      summary: null,
      sections: [],
    });
  });
});

describe('formatTokenUsageTooltip', () => {
  it('renders the card model as text blocks with indented shares', () => {
    const tooltip = formatTokenUsageTooltip(
      { tokens: 2100, estimated: true },
      { input_tokens: 1000, output_tokens: 50, cache_read_tokens: 800 },
      {
        input_tokens: 3000,
        output_tokens: 150,
        cache_read_tokens: 2400,
      },
      200000,
    );

    expect(tooltip).toBe(
      [
        '2,100 / 200,000',
        '',
        `${label('cacheHitRate')}: 80%`,
        `${label('totalInput')}: 3,000`,
        `${label('totalOutput')}: 150`,
        '',
        label('lastTurn'),
        `${label('input')}: 1,000`,
        `  · ${label('cacheRead')}: 800 (80%)`,
        `  · ${label('cacheWrite')}: 0`,
        `  · ${label('uncached')}: 200`,
        `${label('output')}: 50`,
      ].join('\n'),
    );
  });

  it('returns undefined without any usage data', () => {
    expect(formatTokenUsageTooltip(null, null, null)).toBeUndefined();
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
