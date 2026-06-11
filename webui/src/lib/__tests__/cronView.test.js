import { describe, expect, it } from 'vitest';

import { describeCronExpression } from '../cronView.js';

describe('describeCronExpression', () => {
  it('describes a standard five-field expression in plain text', () => {
    expect(describeCronExpression('0 9 * * 1-5')).toBe(
      'At 09:00, Monday through Friday',
    );
  });

  it('uses 24-hour time', () => {
    expect(describeCronExpression('30 17 * * *')).toBe('At 17:30');
  });

  it('returns an empty string for blank input', () => {
    expect(describeCronExpression('')).toBe('');
    expect(describeCronExpression('   ')).toBe('');
    expect(describeCronExpression(null)).toBe('');
    expect(describeCronExpression(undefined)).toBe('');
  });

  it('returns an empty string for unparseable expressions', () => {
    expect(describeCronExpression('not a cron')).toBe('');
    expect(describeCronExpression('99 99 * *')).toBe('');
  });
});
