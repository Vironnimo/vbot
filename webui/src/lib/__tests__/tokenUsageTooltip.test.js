import { beforeEach, describe, expect, it } from 'vitest';

import { init } from '../i18n.js';
import { formatTokenUsageTooltip } from '../tokenUsageTooltip.js';

describe('formatTokenUsageTooltip', () => {
  beforeEach(() => {
    init('en');
  });

  it('returns undefined without any usage data', () => {
    expect(formatTokenUsageTooltip(null, null)).toBeUndefined();
    expect(formatTokenUsageTooltip(undefined, undefined)).toBeUndefined();
  });

  it('renders the last turn with cache shares, uncached remainder and percent', () => {
    const tooltip = formatTokenUsageTooltip(
      {
        input_tokens: 36704,
        output_tokens: 2190,
        cache_read_tokens: 6656,
        cache_write_tokens: 1200,
        reasoning_tokens: 1400,
      },
      null,
    );

    expect(tooltip).toBe(
      [
        'Last turn',
        'Input: 36,704 tok',
        '  · read from cache: 6,656 (18%)',
        '  · newly written to cache: 1,200',
        '  · uncached: 28,848',
        'Output: 2,190 tok',
        '  · reasoning (included in output): 1,400',
      ].join('\n'),
    );
  });

  it('omits cache lines when the provider reported none', () => {
    const tooltip = formatTokenUsageTooltip(
      { input_tokens: 500, output_tokens: 20 },
      null,
    );

    expect(tooltip).toBe(
      ['Last turn', 'Input: 500 tok', 'Output: 20 tok'].join('\n'),
    );
  });

  it('keeps the read-only cache split when no write was reported', () => {
    const tooltip = formatTokenUsageTooltip(
      { input_tokens: 1000, output_tokens: 10, cache_read_tokens: 900 },
      null,
    );

    expect(tooltip).toContain('  · read from cache: 900 (90%)');
    expect(tooltip).toContain('  · uncached: 100');
    expect(tooltip).not.toContain('newly written');
  });

  it('appends the estimation note for estimated usage', () => {
    const tooltip = formatTokenUsageTooltip(
      { input_tokens: 100, output_tokens: 10, estimated: true },
      null,
    );

    expect(tooltip).toContain('Estimated (provider sent no usage data)');
  });

  it('renders the session block with hit rate, per-turn average and estimated note', () => {
    const tooltip = formatTokenUsageTooltip(
      { input_tokens: 36704, output_tokens: 2190, cache_read_tokens: 6656 },
      {
        measured_turns: 42,
        estimated_turns: 3,
        cache_turns: 40,
        input_tokens: 1243000,
        output_tokens: 48300,
        cache_read_tokens: 1019000,
        cache_write_tokens: 22000,
        reasoning_turns: 30,
        reasoning_tokens: 32000,
      },
    );

    const [lastTurnBlock, sessionBlock] = tooltip.split('\n\n');
    expect(lastTurnBlock).toContain('Last turn');
    expect(sessionBlock).toBe(
      [
        'Session (42 measured turns)',
        'Input: 1,243,000 tok',
        '  · read from cache: 1,019,000 (82%)',
        'Output: 48,300 tok',
        '  · reasoning: 32,000 tok (30 reporting turns; included in output)',
        'Avg cache read per turn: 25,475 tok',
        '3 estimated turns excluded',
      ].join('\n'),
    );
  });

  it('hides the session cache lines when no turn reported cache fields', () => {
    const tooltip = formatTokenUsageTooltip(null, {
      measured_turns: 4,
      estimated_turns: 0,
      cache_turns: 0,
      input_tokens: 8000,
      output_tokens: 600,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
    });

    expect(tooltip).toBe(
      [
        'Session (4 measured turns)',
        'Input: 8,000 tok',
        'Output: 600 tok',
      ].join('\n'),
    );
  });

  it('keeps missing Reasoning detail absent instead of rendering zero', () => {
    const tooltip = formatTokenUsageTooltip(
      { input_tokens: 500, output_tokens: 20 },
      {
        measured_turns: 1,
        input_tokens: 500,
        output_tokens: 20,
      },
    );

    expect(tooltip).not.toContain('reasoning');
  });

  it('skips the session block entirely without measured turns', () => {
    const tooltip = formatTokenUsageTooltip(
      { input_tokens: 100, output_tokens: 5 },
      { measured_turns: 0, estimated_turns: 2, input_tokens: 0 },
    );

    expect(tooltip).not.toContain('Session (');
  });
});
