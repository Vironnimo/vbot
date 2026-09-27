import { describe, expect, it } from 'vitest';

import {
  extractMentionTokens,
  formatMentionToken,
  fuzzyFilterFiles,
  isMentionTokenChar,
  matchMentionCandidates,
} from '../fileMentions.js';

describe('extractMentionTokens', () => {
  it.each([
    ['look at @src/app.py please', ['src/app.py']],
    ['@README.md', ['README.md']],
    // A mid-word @ such as an email address is no mention.
    ['mail user@example.com now', []],
    ['agent@projekt is not a file', []],
    ['mail jürgen@exämple.de now', []],
    ['mail x@"quoted" now', []],
    // Bare tokens read in any script.
    ['read @docs/Übersicht.md now', ['docs/Übersicht.md']],
    ['@笔记/计划.md', ['笔记/计划.md']],
    // Quoted tokens unescape quotes and backslashes; unclosed quotes read nothing.
    [
      String.raw`see @"notes/meeting notes.md" and @"a \"b\".txt"`,
      ['notes/meeting notes.md', 'a "b".txt'],
    ],
    [String.raw`@"dir\\x y"`, [String.raw`dir\x y`]],
    ['unclosed @"notes/meeting notes.md', []],
    ['@a.txt and @a.txt again', ['a.txt']],
    ['@one.md then @two/three.py', ['one.md', 'two/three.py']],
    ['no mentions here', []],
    [null, []],
  ])('extracts from %j', (text, tokens) => {
    expect(extractMentionTokens(text)).toEqual(tokens);
  });
});

describe('matchMentionCandidates', () => {
  const files = ['src/app.py', 'README.md', 'docs/guide.md'];

  it.each([
    [
      'keeps only actual files',
      ['src/app.py', 'staticmethod'],
      files,
      ['src/app.py'],
    ],
    ['trims a trailing period', ['README.md.'], files, ['README.md']],
    ['trims a trailing comma', ['docs/guide.md,'], files, ['docs/guide.md']],
    [
      'normalizes backslashes to the server path form',
      ['src\\app.py'],
      files,
      ['src/app.py'],
    ],
    [
      'matches a literal backslash in a listed name before normalizing',
      [String.raw`a\b.txt`],
      [String.raw`a\b.txt`],
      [String.raw`a\b.txt`],
    ],
    ['deduplicates matches', ['README.md', 'README.md.'], files, ['README.md']],
  ])('%s', (_label, tokens, listed, matches) => {
    expect(matchMentionCandidates(tokens, listed)).toEqual(matches);
  });
});

describe('formatMentionToken', () => {
  it('keeps simple paths bare and quotes everything else', () => {
    expect(formatMentionToken('docs/Übersicht.md')).toBe('@docs/Übersicht.md');
    expect(formatMentionToken('notes/meeting notes.md')).toBe(
      '@"notes/meeting notes.md"',
    );
    expect(formatMentionToken('a "b".txt')).toBe('@"a \\"b\\".txt"');
  });

  it('round-trips every listed path into a mention', () => {
    const files = [
      'README.md',
      'docs/Übersicht.md',
      'docs/U\u0308bersicht-nfd.md',
      'notes/meeting notes.md',
      'src/app+util.js',
      'node_modules/@scope/pkg/index.js',
      'a "quoted" name.txt',
      String.raw`dir\with\backslash.txt`,
      'report (final).md',
      'trailing.dot.',
    ];
    for (const file of files) {
      const text = `please read ${formatMentionToken(file)} now, thanks.`;
      expect(matchMentionCandidates(extractMentionTokens(text), files)).toEqual(
        [file],
      );
    }
  });
});

describe('fuzzyFilterFiles', () => {
  const files = [
    'core/chat/chat.py',
    '.vorch/domain-maps/tools/session_search.md',
    'core/tools/search.py',
    'webui/src/lib/api.js',
    'core/recall/vector.py',
  ];

  it('returns the capped raw list when the query is empty', () => {
    expect(fuzzyFilterFiles(files, '', 3)).toEqual(files.slice(0, 3));
  });

  it.each([
    [
      'anywhere in the filename, not only as prefix',
      'search',
      ['.vorch/domain-maps/tools/session_search.md', 'core/tools/search.py'],
      ['webui/src/lib/api.js'],
    ],
    [
      'as a subsequence across the full path',
      'dmtools',
      ['.vorch/domain-maps/tools/session_search.md'],
      [],
    ],
    ['case-insensitively', 'SEARCH', ['core/tools/search.py'], []],
    ['nothing without the query as a subsequence', 'zzz', [], files],
  ])('matches %s', (_label, query, included, excluded) => {
    const results = fuzzyFilterFiles(files, query);

    expect(results).toEqual(expect.arrayContaining(included));
    for (const file of excluded) {
      expect(results).not.toContain(file);
    }
  });

  it('ranks filename hits above path-only hits', () => {
    expect(
      fuzzyFilterFiles(['tools/other.py', 'src/tools.py'], 'tools')[0],
    ).toBe('src/tools.py');
  });

  it('applies the result limit', () => {
    const many = Array.from({ length: 20 }, (_, i) => `file-${i}.txt`);

    expect(fuzzyFilterFiles(many, 'file', 5)).toHaveLength(5);
  });
});

describe('isMentionTokenChar', () => {
  it('accepts path characters and rejects separators', () => {
    for (const char of ['a', 'Z', '0', '_', '-', '.', '/', '\\', 'ü', '中']) {
      expect(isMentionTokenChar(char)).toBe(true);
    }
    for (const char of [' ', '\n', '@', '(', '"', ':', '+']) {
      expect(isMentionTokenChar(char)).toBe(false);
    }
  });
});
