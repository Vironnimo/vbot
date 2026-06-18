import { describe, expect, it } from 'vitest';

import {
  AGENT_ADDRESS_SEPARATOR,
  formatAgentAddress,
  parseAgentAddress,
} from '../agentAddress.js';

describe('agentAddress seam', () => {
  it('uses the @ separator that mirrors the server seam', () => {
    expect(AGENT_ADDRESS_SEPARATOR).toBe('@');
  });

  describe('formatAgentAddress', () => {
    it('returns the bare agent id when there is no project (identity)', () => {
      expect(formatAgentAddress('builder', null)).toBe('builder');
      expect(formatAgentAddress('builder', '')).toBe('builder');
      expect(formatAgentAddress('builder', undefined)).toBe('builder');
      expect(formatAgentAddress('builder', '   ')).toBe('builder');
    });

    it('builds agent@projekt when a project id is present', () => {
      expect(formatAgentAddress('builder', 'vbot')).toBe('builder@vbot');
    });

    it('trims surrounding whitespace on the project id', () => {
      expect(formatAgentAddress('builder', '  vbot  ')).toBe('builder@vbot');
    });

    it('coerces a non-string agent id to an empty bare id', () => {
      expect(formatAgentAddress(undefined, null)).toBe('');
      expect(formatAgentAddress(null, 'vbot')).toBe('@vbot');
    });
  });

  describe('parseAgentAddress', () => {
    it('treats a bare agent id as identity (projectId null)', () => {
      expect(parseAgentAddress('builder')).toEqual({
        agentId: 'builder',
        projectId: null,
      });
    });

    it('splits agent@projekt into both parts', () => {
      expect(parseAgentAddress('builder@vbot')).toEqual({
        agentId: 'builder',
        projectId: 'vbot',
      });
    });

    it('round-trips with formatAgentAddress for both kinds', () => {
      expect(parseAgentAddress(formatAgentAddress('builder', null))).toEqual({
        agentId: 'builder',
        projectId: null,
      });
      expect(parseAgentAddress(formatAgentAddress('builder', 'vbot'))).toEqual({
        agentId: 'builder',
        projectId: 'vbot',
      });
    });

    it('falls back to an identity address for malformed input', () => {
      expect(parseAgentAddress('')).toEqual({ agentId: '', projectId: null });
      expect(parseAgentAddress(null)).toEqual({ agentId: '', projectId: null });
      expect(parseAgentAddress('a@b@c')).toEqual({
        agentId: 'a@b@c',
        projectId: null,
      });
      expect(parseAgentAddress('builder@')).toEqual({
        agentId: 'builder@',
        projectId: null,
      });
      expect(parseAgentAddress('@vbot')).toEqual({
        agentId: '@vbot',
        projectId: null,
      });
    });
  });
});
