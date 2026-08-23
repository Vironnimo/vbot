import { beforeEach, describe, expect, it } from 'vitest';

import { init } from '../i18n.js';
import { formatTokenUsageTooltip } from '../tokenUsageTooltip.js';

describe('formatTokenUsageTooltip', () => {
  beforeEach(() => {
    init('en');
  });

  it('returns undefined without any usage data', () => {
    expect(formatTokenUsageTooltip(null, null, null)).toBeUndefined();
    expect(
      formatTokenUsageTooltip(undefined, undefined, undefined),
    ).toBeUndefined();
  });

  it('renders the context summary with provider in/out and estimated delta', () => {
    const tooltip = formatTokenUsageTooltip(
      {
        tokens: 155489,
        estimated: true,
        provider_input_tokens: 154731,
        provider_output_tokens: 243,
        estimated_delta_tokens: 515,
      },
      null,
      null,
      262144,
    );

    const lines = tooltip.split('\n');
    expect(lines).toHaveLength(3);
    expect(lines[0]).toBe('~155,489 / 262,144');
    expect(lines[1]).toBe('(in 154,731, out 243)');
    expect(lines[2]).toContain('515');
  });

  it('renders the last turn with cache shares, uncached remainder and percent', () => {
    const tooltip = formatTokenUsageTooltip(
      null,
      {
        input_tokens: 36704,
        output_tokens: 2190,
        cache_read_tokens: 6656,
        cache_write_tokens: 1200,
        reasoning_tokens: 1400,
      },
      null,
    );

    const lines = tooltip.split('\n');
    expect(lines).toHaveLength(7);
    expect(lines[1]).toContain('36,704');
    expect(lines[2]).toContain('6,656');
    expect(lines[2]).toContain('18%');
    expect(lines[3]).toContain('1,200');
    expect(lines[4]).toContain('28,848');
    expect(lines[5]).toContain('2,190');
    expect(lines[6]).toContain('1,400');
  });

  it('omits cache lines when the provider reported none', () => {
    const tooltip = formatTokenUsageTooltip(
      null,
      { input_tokens: 500, output_tokens: 20 },
      null,
    );

    const lines = tooltip.split('\n');
    expect(lines).toHaveLength(3);
    expect(lines[1]).toContain('500');
    expect(lines[2]).toContain('20');
  });

  it('keeps the read-only cache split when no write was reported', () => {
    const tooltip = formatTokenUsageTooltip(
      null,
      { input_tokens: 1000, output_tokens: 10, cache_read_tokens: 900 },
      null,
    );

    const lines = tooltip.split('\n');
    expect(lines).toHaveLength(5);
    expect(lines[2]).toContain('900');
    expect(lines[2]).toContain('90%');
    expect(lines[3]).toContain('100');
  });

  it('appends the estimation note for estimated usage', () => {
    const tooltip = formatTokenUsageTooltip(
      null,
      { input_tokens: 100, output_tokens: 10, estimated: true },
      null,
    );

    expect(tooltip.split('\n')).toHaveLength(4);
  });

  it('marks only omitted input as estimated when output is provider-reported', () => {
    const tooltip = formatTokenUsageTooltip(
      null,
      {
        input_tokens: 134547,
        input_tokens_estimated: true,
        output_tokens: 2572,
        estimated: true,
      },
      {
        measured_turns: 0,
        estimated_turns: 1,
        input_tokens: 0,
        output_tokens: 2572,
      },
    );

    const [turnLines, sessionLines] = tooltip
      .split('\n\n')
      .map((section) => section.split('\n'));
    expect(turnLines).toHaveLength(4);
    expect(turnLines[1]).toContain('~134,547');
    expect(turnLines[2]).toContain('2,572');
    expect(sessionLines).toHaveLength(4);
    expect(sessionLines[1]).toContain('0');
    expect(sessionLines[2]).toContain('2,572');
    expect(sessionLines[3]).toContain('1');
  });

  it('renders the session block with hit rate, per-turn average and estimated note', () => {
    const tooltip = formatTokenUsageTooltip(
      null,
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
    expect(lastTurnBlock.split('\n')).toHaveLength(5);
    const sessionLines = sessionBlock.split('\n');
    expect(sessionLines).toHaveLength(7);
    expect(sessionLines[0]).toContain('42');
    expect(sessionLines[1]).toContain('1,243,000');
    expect(sessionLines[2]).toContain('1,019,000');
    expect(sessionLines[2]).toContain('82%');
    expect(sessionLines[3]).toContain('48,300');
    expect(sessionLines[4]).toContain('32,000');
    expect(sessionLines[4]).toContain('30');
    expect(sessionLines[5]).toContain('25,475');
    expect(sessionLines[6]).toContain('3');
  });

  it('hides the session cache lines when no turn reported cache fields', () => {
    const tooltip = formatTokenUsageTooltip(null, null, {
      measured_turns: 4,
      estimated_turns: 0,
      cache_turns: 0,
      input_tokens: 8000,
      output_tokens: 600,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
    });

    const lines = tooltip.split('\n');
    expect(lines).toHaveLength(3);
    expect(lines[0]).toContain('4');
    expect(lines[1]).toContain('8,000');
    expect(lines[2]).toContain('600');
  });

  it('keeps missing Reasoning detail absent instead of rendering zero', () => {
    const tooltip = formatTokenUsageTooltip(
      null,
      { input_tokens: 500, output_tokens: 20 },
      {
        measured_turns: 1,
        input_tokens: 500,
        output_tokens: 20,
      },
    );

    const sections = tooltip
      .split('\n\n')
      .map((section) => section.split('\n'));
    expect(sections.map((section) => section.length)).toEqual([3, 3]);
    expect(sections[0][1]).toContain('500');
    expect(sections[0][2]).toContain('20');
    expect(sections[1][1]).toContain('500');
    expect(sections[1][2]).toContain('20');
  });

  it('skips the session block entirely without measured turns', () => {
    const tooltip = formatTokenUsageTooltip(
      null,
      { input_tokens: 100, output_tokens: 5 },
      { measured_turns: 0, estimated_turns: 2, input_tokens: 0 },
    );

    expect(tooltip.split('\n\n')).toHaveLength(1);
  });
});
