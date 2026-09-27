import { describe, expect, it } from 'vitest';

import {
  formatAgentAddress,
  parseAgentAddress,
  qualifyAgentAddress,
} from '../agentAddress.js';

describe('agentAddress seam', () => {
  it('formats a bare id without a Project and agent@project with one', () => {
    for (const projectId of [null, '', undefined, '   ']) {
      expect(formatAgentAddress('builder', projectId)).toBe('builder');
    }
    expect(formatAgentAddress('builder', 'vbot')).toBe('builder@vbot');
    expect(formatAgentAddress('builder', '  vbot  ')).toBe('builder@vbot');
    expect(formatAgentAddress(undefined, null)).toBe('');
    expect(formatAgentAddress(null, 'vbot')).toBe('@vbot');
  });

  it('qualifies a bare id with its Project exactly once', () => {
    expect(qualifyAgentAddress('builder', 'vbot')).toBe('builder@vbot');
    expect(qualifyAgentAddress('builder@vbot', 'vbot')).toBe('builder@vbot');
    expect(qualifyAgentAddress(' builder@vbot ', null)).toBe('builder@vbot');
    expect(qualifyAgentAddress('builder', null)).toBe('builder');
    expect(qualifyAgentAddress('builder', '  ')).toBe('builder');
    expect(qualifyAgentAddress('', 'vbot')).toBe('');
    expect(qualifyAgentAddress(undefined, 'vbot')).toBe('');
  });

  it('parses addresses losslessly and keeps malformed input as an identity address', () => {
    const cases = [
      ['builder', 'builder', null],
      ['builder@vbot', 'builder', 'vbot'],
      ['', '', null],
      [null, '', null],
      ['a@b@c', 'a@b@c', null],
      ['builder@', 'builder@', null],
      ['@vbot', '@vbot', null],
    ];
    for (const [address, agentId, projectId] of cases) {
      const parsed = parseAgentAddress(address);
      expect(parsed).toEqual({ agentId, projectId });
      expect(formatAgentAddress(parsed.agentId, parsed.projectId)).toBe(
        address ?? '',
      );
    }
  });
});
