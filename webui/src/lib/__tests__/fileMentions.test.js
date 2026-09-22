import { describe, expect, it } from 'vitest';

import {
  extractMentionTokens,
  formatMentionToken,
  fuzzyFilterFiles,
  isMentionTokenChar,
  matchMentionCandidates,
} from '../fileMentions.js';

describe('extractMentionTokens', () => {
  it('extracts boundary-anchored tokens', () => {
    expect(extractMentionTokens('look at @src/app.py please')).toEqual([
      'src/app.py',
    ]);
    expect(extractMentionTokens('@README.md')).toEqual(['README.md']);
  });

  it('ignores mid-word @ such as email addresses', () => {
    expect(extractMentionTokens('mail user@example.com now')).toEqual([]);
    expect(extractMentionTokens('agent@projekt is not a file')).toEqual([]);
    expect(extractMentionTokens('mail jürgen@exämple.de now')).toEqual([]);
    expect(extractMentionTokens('mail x@"quoted" now')).toEqual([]);
  });

  it('reads bare tokens in any script', () => {
    expect(extractMentionTokens('read @docs/Übersicht.md now')).toEqual([
      'docs/Übersicht.md',
    ]);
    expect(extractMentionTokens('@笔记/计划.md')).toEqual(['笔记/计划.md']);
  });

  it('reads quoted tokens with escaped quotes and backslashes', () => {
    expect(
      extractMentionTokens(
        String.raw`see @"notes/meeting notes.md" and @"a \"b\".txt"`,
      ),
    ).toEqual(['notes/meeting notes.md', 'a "b".txt']);
    expect(extractMentionTokens(String.raw`@"dir\\x y"`)).toEqual([
      String.raw`dir\x y`,
    ]);
    expect(extractMentionTokens('unclosed @"notes/meeting notes.md')).toEqual(
      [],
    );
  });

  it('deduplicates repeated mentions', () => {
    expect(extractMentionTokens('@a.txt and @a.txt again')).toEqual(['a.txt']);
  });

  it('returns empty for non-strings and text without @', () => {
    expect(extractMentionTokens(null)).toEqual([]);
    expect(extractMentionTokens('no mentions here')).toEqual([]);
  });

  it('collects multiple distinct mentions in order', () => {
    expect(extractMentionTokens('@one.md then @two/three.py')).toEqual([
      'one.md',
      'two/three.py',
    ]);
  });
});

describe('matchMentionCandidates', () => {
  const files = ['src/app.py', 'README.md', 'docs/guide.md'];

  it('keeps only tokens that are actual files', () => {
    expect(
      matchMentionCandidates(['src/app.py', 'staticmethod'], files),
    ).toEqual(['src/app.py']);
  });

  it('trims trailing sentence punctuation', () => {
    expect(matchMentionCandidates(['README.md.'], files)).toEqual([
      'README.md',
    ]);
    expect(matchMentionCandidates(['docs/guide.md,'], files)).toEqual([
      'docs/guide.md',
    ]);
  });

  it('normalizes backslashes to the server path form', () => {
    expect(matchMentionCandidates(['src\\app.py'], files)).toEqual([
      'src/app.py',
    ]);
  });

  it('matches a literal backslash in a listed name before normalizing', () => {
    expect(
      matchMentionCandidates([String.raw`a\b.txt`], [String.raw`a\b.txt`]),
    ).toEqual([String.raw`a\b.txt`]);
  });

  it('deduplicates matches', () => {
    expect(matchMentionCandidates(['README.md', 'README.md.'], files)).toEqual([
      'README.md',
    ]);
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

  it('matches the query anywhere in the filename, not only as prefix', () => {
    const results = fuzzyFilterFiles(files, 'search');

    expect(results).toContain('.vorch/domain-maps/tools/session_search.md');
    expect(results).toContain('core/tools/search.py');
    expect(results).not.toContain('webui/src/lib/api.js');
  });

  it('ranks filename hits above path-only hits', () => {
    const results = fuzzyFilterFiles(
      ['tools/other.py', 'src/tools.py'],
      'tools',
    );

    expect(results[0]).toBe('src/tools.py');
  });

  it('supports subsequence matches across the full path', () => {
    const results = fuzzyFilterFiles(files, 'dmtools');

    expect(results).toContain('.vorch/domain-maps/tools/session_search.md');
  });

  it('drops entries that do not contain the query as a subsequence', () => {
    expect(fuzzyFilterFiles(files, 'zzz')).toEqual([]);
  });

  it('applies the result limit', () => {
    const many = Array.from({ length: 20 }, (_, i) => `file-${i}.txt`);

    expect(fuzzyFilterFiles(many, 'file', 5)).toHaveLength(5);
  });

  it('is case-insensitive', () => {
    expect(fuzzyFilterFiles(files, 'SEARCH')).toContain('core/tools/search.py');
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
