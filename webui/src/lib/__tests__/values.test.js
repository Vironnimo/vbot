import { describe, expect, it } from 'vitest';

import { asOptionalText, asText, isPlainObject } from '../values.js';

describe('isPlainObject', () => {
  it('accepts object literals and null-prototype objects', () => {
    expect(isPlainObject({})).toBe(true);
    expect(isPlainObject({ key: 'value' })).toBe(true);
    expect(isPlainObject(Object.create(null))).toBe(true);
  });

  it('rejects arrays, null, and primitives', () => {
    expect(isPlainObject([])).toBe(false);
    expect(isPlainObject(null)).toBe(false);
    expect(isPlainObject(undefined)).toBe(false);
    expect(isPlainObject('text')).toBe(false);
    expect(isPlainObject(42)).toBe(false);
  });

  it('rejects exotic objects that JSON data can never contain', () => {
    expect(isPlainObject(new Date())).toBe(false);
    expect(isPlainObject(new Map())).toBe(false);
    expect(isPlainObject(new Set())).toBe(false);
  });
});

describe('asText', () => {
  it('maps null and undefined to the empty string', () => {
    expect(asText(null)).toBe('');
    expect(asText(undefined)).toBe('');
  });

  it('stringifies every other value', () => {
    expect(asText('text')).toBe('text');
    expect(asText(42)).toBe('42');
    expect(asText(true)).toBe('true');
  });
});

describe('asOptionalText', () => {
  it('maps null and undefined to null', () => {
    expect(asOptionalText(null)).toBe(null);
    expect(asOptionalText(undefined)).toBe(null);
  });

  it('trims text and maps blank results to null', () => {
    expect(asOptionalText('  spaced  ')).toBe('spaced');
    expect(asOptionalText('   ')).toBe(null);
    expect(asOptionalText('')).toBe(null);
  });

  it('stringifies non-string values before trimming', () => {
    expect(asOptionalText(42)).toBe('42');
  });
});
