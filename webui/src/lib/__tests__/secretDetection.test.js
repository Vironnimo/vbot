import { describe, expect, it } from 'vitest';

import cases from '../../../../tests/core/secrets/api_key_detection_cases.json';
import {
  containsApiKey,
  containsPotentialApiKey,
  detectApiKeys,
} from '../secretDetection.js';

function keyOf(example) {
  const body = example.seed.repeat(10).slice(0, example.length);
  return (
    example.prefix +
    body +
    (example.repeat_body ? (example.middle ?? '') + body : '') +
    example.suffix
  );
}

describe('API key format contract shared with the server', () => {
  it.each(cases)(
    'recognizes complete $prefix values and exact spans',
    (example) => {
      const key = keyOf(example);
      const text = `🔑 Use "${key}"; then \`${key}\`.`;
      const found = detectApiKeys(text);
      expect(found.map((item) => item.value)).toEqual([key, key]);
      expect(found.every((item) => item.provider === example.provider)).toBe(
        true,
      );
      expect(
        found.every((item) => text.slice(item.start, item.end) === key),
      ).toBe(true);
      expect(containsApiKey(text)).toBe(true);
      expect(
        containsPotentialApiKey(JSON.stringify({ text: example.prefix + 'a' })),
      ).toBe(true);
      expect(detectApiKeys(`before${key}after`)).toEqual([]);
      expect(detectApiKeys(`${key}_`)).toEqual([]);
    },
  );

  it('leaves unknown values, partial tokens and obvious placeholders unchanged', () => {
    for (const value of [
      'API_KEY=0123456789abcdef0123456789abcdef',
      'sk-proj-example',
      'ghp_' + 'x'.repeat(36),
      'github_pat_' + 'a'.repeat(82),
      'sk-' + 'aB3dE6gH9jK2mN5pQ8sT1'.repeat(3),
      '{{secret:GITHUB_TOKEN}}',
      'pk_live_' + 'aB3dE6gH9jK2mN5pQ8sT1vW4yZ7',
      'AKIA0123456789ABCDEF',
    ]) {
      expect(detectApiKeys(value)).toEqual([]);
    }
    expect(
      containsPotentialApiKey('Task: review API_KEY and {{secret:OPENAI_KEY}}'),
    ).toBe(false);
  });

  it('reports different services in message order', () => {
    const values = [keyOf(cases[14]), keyOf(cases[6]), keyOf(cases[0])];
    expect(detectApiKeys(values.join('\n')).map((item) => item.value)).toEqual(
      values,
    );
  });
});
