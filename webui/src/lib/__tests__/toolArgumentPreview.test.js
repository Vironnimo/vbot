import { describe, expect, it } from 'vitest';

import {
  TOOL_ARGUMENT_PREVIEW_VALUE_MAX_LENGTH,
  createToolArgumentPreviewScanner,
} from '../toolArgumentPreview.js';

describe('createToolArgumentPreviewScanner', () => {
  it.each([
    [
      'a completed field',
      '{"path": "notes/todo.md"}',
      { path: 'notes/todo.md' },
    ],
    [
      'decoded escape sequences',
      '{"path": "C:\\\\dev\\\\a \\"b\\".txt", "x": "\\u00e4"}',
      { path: 'C:\\dev\\a "b".txt', x: 'ä' },
    ],
    [
      'only top-level keys, not nested ones',
      '{"meta": {"path": "inner"}, "list": ["path", {"path": "deep"}], "path": "outer.txt"}',
      { path: 'outer.txt' },
    ],
    [
      'no key-shaped text from inside a string value',
      '{"content": "{\\"path\\": \\"fake.txt\\"}", "path": "real.txt"}',
      { content: '{"path": "fake.txt"}', path: 'real.txt' },
    ],
    [
      'only string values',
      '{"count": 3, "force": true, "opts": null, "path": "a.txt"}',
      { path: 'a.txt' },
    ],
    [
      'nothing after the top-level object closes',
      '{"path": "a.txt"} {"path": "b.txt"}',
      { path: 'a.txt' },
    ],
  ])('captures %s from one fragment', (_label, fragment, fields) => {
    const scanner = createToolArgumentPreviewScanner();

    expect(scanner.push(fragment)).toBe(true);
    expect(scanner.fields()).toEqual(fields);
  });

  it('extracts the first field while a later value is still streaming', () => {
    const scanner = createToolArgumentPreviewScanner();

    expect(scanner.push('{"pa')).toBe(false);
    expect(scanner.push('th": "src/app')).toBe(false);
    expect(scanner.push('.py", "content": "def main():')).toBe(true);
    expect(scanner.fields()).toEqual({ path: 'src/app.py' });

    expect(scanner.push('\\n    print(1)')).toBe(false);
    expect(scanner.fields()).toEqual({ path: 'src/app.py' });
  });

  it('captures each additional field as it completes', () => {
    const scanner = createToolArgumentPreviewScanner();

    expect(scanner.push('{"pattern": "TODO", "pa')).toBe(true);
    expect(scanner.fields()).toEqual({ pattern: 'TODO' });

    expect(scanner.push('th": "core/"}')).toBe(true);
    expect(scanner.fields()).toEqual({ pattern: 'TODO', path: 'core/' });
  });

  it('returns no fields for a fragment that is not an object', () => {
    const scanner = createToolArgumentPreviewScanner();

    expect(scanner.push('"just a string"')).toBe(false);
    expect(scanner.push('{"path": "late.txt"}')).toBe(false);
    expect(scanner.fields()).toBeNull();
  });

  it('truncates an overlong value and marks it with an ellipsis', () => {
    const scanner = createToolArgumentPreviewScanner();
    const longValue = 'x'.repeat(TOOL_ARGUMENT_PREVIEW_VALUE_MAX_LENGTH + 50);

    scanner.push(`{"command": "${longValue}"}`);

    expect(scanner.fields()).toEqual({
      command: `${'x'.repeat(TOOL_ARGUMENT_PREVIEW_VALUE_MAX_LENGTH)}…`,
    });
  });

  it('decodes a capture cut off inside an escape sequence cleanly', () => {
    const scanner = createToolArgumentPreviewScanner();
    const rawCap = TOOL_ARGUMENT_PREVIEW_VALUE_MAX_LENGTH * 2;
    // Fill the raw buffer so the cut lands right after a lone backslash.
    const filler = 'a'.repeat(rawCap - 1);

    scanner.push(`{"command": "${filler}\\n${'b'.repeat(30)}"}`);

    const fields = scanner.fields();
    expect(fields.command.endsWith('…')).toBe(true);
    expect(fields.command).not.toContain('\\');
  });

  it('keeps empty pushes and empty objects side-effect free', () => {
    const scanner = createToolArgumentPreviewScanner();

    expect(scanner.push('')).toBe(false);
    expect(scanner.push(undefined)).toBe(false);
    expect(scanner.push('{}')).toBe(false);
    expect(scanner.fields()).toBeNull();
  });
});
