import { describe, expect, it, vi } from 'vitest';

import {
  createMentionIndex,
  extractMentionTokens,
  findOpenQuotedMention,
  formatMentionToken,
  isMentionTokenChar,
  mentionCandidates,
  mentionQueryParts,
  resolveMentionFiles,
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

describe('findOpenQuotedMention', () => {
  it.each([
    ['see @"meeting notes/ag', { start: 4, query: 'meeting notes/ag' }],
    ['@"', { start: 0, query: '' }],
    [String.raw`@"a \"b`, { start: 0, query: 'a "b' }],
    // Closed, interrupted by a line break, or glued to a word: not open.
    ['@"done.md" and more', null],
    ['@"two\nlines', null],
    ['mail x@"quoted', null],
    ['no mention', null],
  ])('reads %j', (text, expected) => {
    expect(findOpenQuotedMention(text, text.length)).toEqual(expected);
  });
});

describe('resolveMentionFiles', () => {
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
  ])('%s', async (_label, tokens, listed, matches) => {
    await expect(
      resolveMentionFiles(tokens, { files: listed }),
    ).resolves.toEqual(matches);
  });

  it('confirms files outside the index by listing their folder', async () => {
    const listEntries = vi.fn(async (directory) => {
      if (directory === 'missing') throw new Error('not_found');
      return directory === 'build'
        ? [
            { name: 'out.log', kind: 'file', ignored: true },
            { name: 'cache', kind: 'directory', ignored: true },
          ]
        : [{ name: '.env', kind: 'file', ignored: true }];
    });

    await expect(
      resolveMentionFiles(
        [
          'src/app.py',
          'build/out.log.',
          '.env',
          'build/cache',
          'missing/x',
          // Folders outside the root are never listed.
          '../secret.txt',
          '/etc/hosts',
          'C:\\x\\y.txt',
          'src/../build/out.log',
        ],
        { files, listEntries },
      ),
    ).resolves.toEqual(['src/app.py', 'build/out.log', '.env']);
    // Indexed tokens list nothing; each other folder is listed once.
    expect(
      listEntries.mock.calls.map(([directory]) => directory).sort(),
    ).toEqual(['', 'build', 'missing']);
  });
});

describe('formatMentionToken', () => {
  it('keeps simple paths bare and quotes everything else', () => {
    expect(formatMentionToken('docs/Übersicht.md')).toBe('@docs/Übersicht.md');
    expect(formatMentionToken('notes/meeting notes.md')).toBe(
      '@"notes/meeting notes.md"',
    );
    expect(formatMentionToken('a "b".txt')).toBe('@"a \\"b\\".txt"');
    // A chosen folder stays open for the rest of the path.
    expect(formatMentionToken('my notes/', { open: true })).toBe('@"my notes/');
    expect(formatMentionToken('docs/', { open: true })).toBe('@docs/');
  });

  it('round-trips every listed path into a mention', async () => {
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
      await expect(
        resolveMentionFiles(extractMentionTokens(text), { files }),
      ).resolves.toEqual([file]);
    }
  });
});

describe('createMentionIndex', () => {
  const files = [
    'core/chat/chat.py',
    '.vorch/domain-maps/tools/session_search.md',
    'core/tools/search.py',
    'webui/src/lib/api.js',
    'core/recall/vector.py',
  ];
  const paths = (matches) => matches.map((match) => match.path);

  it('returns the capped raw list when the query is empty', () => {
    expect(paths(createMentionIndex({ files }).search('', 3))).toEqual(
      files.slice(0, 3),
    );
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
    const results = paths(createMentionIndex({ files }).search(query));

    expect(results).toEqual(expect.arrayContaining(included));
    for (const file of excluded) {
      expect(results).not.toContain(file);
    }
  });

  it('ranks filename hits above path-only hits, folders like files', () => {
    const index = createMentionIndex({
      files: ['tools/other.py', 'src/tools.py'],
      directories: ['tools/', 'core/toolsets'],
    });
    expect(index.search('tools')).toEqual([
      { path: 'tools', kind: 'directory' },
      { path: 'src/tools.py', kind: 'file' },
      { path: 'core/toolsets', kind: 'directory' },
      { path: 'tools/other.py', kind: 'file' },
    ]);
  });

  it('applies the result limit, substring hits before subsequence hits', () => {
    const many = Array.from({ length: 20 }, (_, i) => `file-${i}.txt`);
    const index = createMentionIndex({
      files: ['f/i/l/e.md', ...many],
    });

    expect(paths(index.search('file', 5))).toEqual([
      'file-0.txt',
      'file-1.txt',
      'file-2.txt',
      'file-3.txt',
      'file-4.txt',
    ]);
    expect(paths(index.search('file', 50))).toHaveLength(21);
  });

  it('answers a growing or changed query as a fresh index would', () => {
    const corpus = Array.from(
      { length: 400 },
      (_, i) => `pkg${i % 7}/mod${i % 13}/file_${i}.${i % 2 ? 'py' : 'md'}`,
    );
    const index = createMentionIndex({ files: corpus });
    for (const query of ['p', 'pk', 'pkg3', 'pkg3/m', 'f1', 'f1.p', 'x', '']) {
      expect(index.search(query, 20)).toEqual(
        createMentionIndex({ files: corpus }).search(query, 20),
      );
    }
  });
});

describe('mentionCandidates', () => {
  const index = createMentionIndex({
    files: ['src/app.py', 'src/api.js', 'docs/api.md'],
    directories: ['src', 'docs'],
  });

  it('lists the typed folder first, folders first, then the index', () => {
    const entries = [
      { name: 'app.py', kind: 'file', ignored: false },
      { name: 'Api-cache', kind: 'directory', ignored: true },
      { name: 'README.md', kind: 'file', ignored: false },
    ];

    expect(
      mentionCandidates({ index, directory: 'src', entries, query: 'src/a' }),
    ).toEqual([
      { path: 'src/Api-cache', kind: 'directory', ignored: true },
      { path: 'src/app.py', kind: 'file', ignored: false },
      { path: 'src/api.js', kind: 'file', ignored: false },
    ]);
  });

  it.each([
    ['src/a', { directory: 'src', name: 'a' }],
    ['src\\lib\\x', { directory: 'src/lib', name: 'x' }],
    ['rea', { directory: '', name: 'rea' }],
    ['../rea', { directory: null, name: 'rea' }],
    ['src/../rea', { directory: null, name: 'rea' }],
    ['/etc/ho', { directory: null, name: 'ho' }],
    ['C:\\x', { directory: null, name: 'x' }],
  ])('reads the typed folder of %j as %j', (query, expected) => {
    expect(mentionQueryParts(query)).toEqual(expected);
  });

  it('caps the merged rows', () => {
    const entries = Array.from({ length: 60 }, (_, i) => ({
      name: `n${i}.md`,
      kind: 'file',
      ignored: false,
    }));
    expect(mentionCandidates({ index, entries, query: '' })).toHaveLength(50);
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
