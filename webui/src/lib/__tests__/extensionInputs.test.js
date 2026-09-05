import { describe, expect, it } from 'vitest';
import { inputResponse, inputUrl } from '../extensionInputs.js';

describe('Extension input presentation', () => {
  it('preserves all primitive and array answers', () => {
    const request = {
      kind: 'elicitation',
      payload: {
        requestedSchema: {
          properties: {
            name: { type: 'string' },
            count: { type: 'integer' },
            selected: { type: 'boolean' },
            values: { type: 'array' },
          },
        },
      },
    };
    expect(
      inputResponse(request, {
        name: 'sentinel',
        count: '0',
        selected: 'false',
        values: '["a","b"]',
      }),
    ).toEqual({
      action: 'accept',
      content: {
        name: 'sentinel',
        count: 0,
        selected: false,
        values: ['a', 'b'],
      },
    });
  });
  it('never includes a draft in a declined response', () => {
    expect(
      inputResponse({ kind: 'elicitation' }, { secret: 'sentinel' }, 'decline'),
    ).toEqual({ action: 'decline' });
  });
  it('does not create executable links from external requests', () => {
    expect(inputUrl({ payload: { url: 'javascript:alert(1)' } })).toBe('');
  });
});
