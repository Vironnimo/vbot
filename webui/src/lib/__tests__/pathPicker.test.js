import { describe, expect, it, vi } from 'vitest';

import {
  breadcrumbTrail,
  browseStartPaths,
  completeTypedPath,
  createListingCache,
  extendPrefix,
  filterEntries,
  joinPath,
  listingErrorReason,
  listingPathFor,
  matchingEntries,
  parentPath,
  splitTypedPath,
  toNativePath,
  trimTrailingSeparator,
} from '../pathPicker.js';

const dir = (name, extra = {}) => ({ name, kind: 'directory', ...extra });
const file = (name, extra = {}) => ({ name, kind: 'file', ...extra });

describe('typed text', () => {
  it.each([
    ['C:\\Users\\Vi', { parent: 'C:\\Users\\', prefix: 'Vi' }],
    ['/home/u/pro', { parent: '/home/u/', prefix: 'pro' }],
    ['~/', { parent: '~/', prefix: '' }],
    ['npx', { parent: '', prefix: 'npx' }],
  ])('splits %j into directory part and name prefix', (text, expected) => {
    expect(splitTypedPath(text)).toEqual(expected);
  });

  it.each([
    ['C:\\Users\\', {}, 'C:/Users'],
    ['C:\\', {}, 'C:/'],
    ['/', {}, '/'],
    ['/home//u/', {}, '/home/u'],
    ['~/', {}, '~'],
    ['\\\\host\\share\\', {}, '//host/share'],
    // Relative text names nothing without a root, absolute text nothing with one.
    ['src/', {}, null],
    // Links (the Skill install source) never complete.
    ['https://example.com/skills/', {}, null],
    ['', {}, null],
    ['', { root: 'C:/repo' }, ''],
    ['docs\\./guides/', { root: 'C:/repo' }, 'docs/guides'],
    ['../', { root: 'C:/repo' }, null],
    ['C:/other/', { root: 'C:/repo' }, null],
  ])('lists %j with %j as %j', (parent, options, expected) => {
    expect(listingPathFor(parent, options)).toBe(expected);
  });

  it('offers matching entries case-insensitively, hidden ones only after a dot', () => {
    const entries = [
      file('Readme.md'),
      dir('src'),
      dir('.git', { hidden: true }),
      dir('scripts'),
      file('.env', { hidden: true }),
      dir('Secrets', { hidden: true }),
    ];
    const names = (list) => list.map((entry) => entry.name);

    expect(names(matchingEntries(entries, 's', { mode: 'directory' }))).toEqual(
      ['scripts', 'src'],
    );
    expect(names(matchingEntries(entries, 'r', { mode: 'file' }))).toEqual([
      'Readme.md',
    ]);
    expect(names(matchingEntries(entries, '.', { mode: 'any' }))).toEqual([
      '.git',
      '.env',
    ]);
  });

  it.each([
    ['C:\\Us', dir('Users'), 'C:\\Users\\'],
    ['/home/u/pr', dir('projects'), '/home/u/projects/'],
    ['/etc/ho', file('hosts'), '/etc/hosts'],
    ['', dir('C:/'), 'C:/'],
  ])('completes %j with %j to %j', (text, entry, expected) => {
    expect(completeTypedPath(text, entry)).toBe(expected);
  });

  it.each([
    ['C:/work/', 'C:/work'],
    ['C:\\Users\\me\\\\', 'C:\\Users\\me'],
    ['/home//', '/home'],
    ['~/', '~'],
    ['\\\\host\\share\\', '\\\\host\\share'],
    ['docs/', 'docs'],
    ['C:\\', 'C:\\'],
    ['C:/', 'C:/'],
    ['/', '/'],
    ['~', '~'],
  ])('leaves %j as %j', (text, expected) => {
    expect(trimTrailingSeparator(text)).toBe(expected);
  });

  it('extends the prefix to what every match shares', () => {
    expect(extendPrefix([dir('project-a'), dir('Project-b')], 'p')).toBe(
      'project-',
    );
    expect(extendPrefix([dir('src'), dir('scripts')], 's')).toBe('s');
  });
});

describe('listed paths', () => {
  it.each([
    ['C:/', 'Users', 'C:/Users'],
    ['/', 'home', '/home'],
    ['/home', 'u', '/home/u'],
    ['', 'docs', 'docs'],
    ['docs', 'a.md', 'docs/a.md'],
  ])('joins %j and %j', (base, name, expected) => {
    expect(joinPath(base, name)).toBe(expected);
  });

  it.each([
    ['C:/Users', {}, 'C:/'],
    ['C:/', {}, null],
    ['/home', {}, '/'],
    ['/', {}, null],
    ['//host/share/x', {}, '//host/share'],
    ['//host/share', {}, null],
    ['docs/guides', { relative: true }, 'docs'],
    ['docs', { relative: true }, ''],
    ['', { relative: true }, null],
  ])('gives %j the parent %j', (path, options, expected) => {
    expect(parentPath(path, options)).toBe(expected);
  });

  it('writes paths in the server OS separators', () => {
    expect(toNativePath('C:/Users/me', '\\')).toBe('C:\\Users\\me');
    expect(toNativePath('/home/me', '/')).toBe('/home/me');
  });

  it.each([
    [
      'C:/Users/me',
      {},
      [
        ['C:', 'C:/'],
        ['Users', 'C:/Users'],
        ['me', 'C:/Users/me'],
      ],
    ],
    [
      '/srv/data',
      {},
      [
        ['/', '/'],
        ['srv', '/srv'],
        ['data', '/srv/data'],
      ],
    ],
    [
      '//host/share/team',
      {},
      [
        ['//host/share', '//host/share'],
        ['team', '//host/share/team'],
      ],
    ],
    [
      'docs/guides',
      { root: 'C:/work/vBot' },
      [
        ['vBot', ''],
        ['docs', 'docs'],
        ['guides', 'docs/guides'],
      ],
    ],
  ])('builds the breadcrumbs of %j', (path, options, expected) => {
    expect(
      breadcrumbTrail(path, options).map((crumb) => [crumb.label, crumb.path]),
    ).toEqual(expected);
  });

  it('filters the dialog list by substring and the hidden toggle', () => {
    const entries = [
      file('notes.txt'),
      dir('Docs'),
      dir('.cache', { hidden: true }),
      file('docker.yml'),
    ];
    const names = (list) => list.map((entry) => entry.name);

    expect(names(filterEntries(entries, 'DO'))).toEqual(['Docs', 'docker.yml']);
    expect(names(filterEntries(entries, ''))).toEqual([
      'Docs',
      'docker.yml',
      'notes.txt',
    ]);
    expect(names(filterEntries(entries, '', { showHidden: true }))).toEqual([
      '.cache',
      'Docs',
      'docker.yml',
      'notes.txt',
    ]);
    expect(names(filterEntries(entries, '.ca'))).toEqual(['.cache']);
  });

  it('sorts numbered names naturally', () => {
    const names = filterEntries([dir('v10'), dir('v9'), dir('v1')]).map(
      (entry) => entry.name,
    );
    expect(names).toEqual(['v1', 'v9', 'v10']);
  });
});

describe('opening the dialog', () => {
  it.each([
    [
      'C:\\repo\\app',
      {},
      [
        { path: 'C:/repo/app', highlight: '' },
        { path: 'C:/repo', highlight: 'app' },
      ],
    ],
    ['C:\\', {}, [{ path: 'C:/', highlight: '' }]],
    ['npx', {}, []],
    ['', {}, []],
    [
      'docs/a.md',
      { root: 'C:/repo' },
      [
        { path: 'docs/a.md', highlight: '' },
        { path: 'docs', highlight: 'a.md' },
      ],
    ],
    ['C:/elsewhere/a.md', { root: 'C:/repo' }, []],
  ])('tries %j with %j at %j', (value, options, expected) => {
    expect(browseStartPaths(value, options)).toEqual(expected);
  });

  it.each([
    [
      { code: 'domain_error', details: { data: { reason: 'unreadable' } } },
      'unreadable',
    ],
    [
      { code: 'domain_error', details: { data: { reason: 'timeout' } } },
      'timeout',
    ],
    [{ code: 'invalid_request', details: {} }, 'invalid'],
    [new Error('offline'), 'failed'],
  ])('reads the listing failure %j as %j', (error, expected) => {
    expect(listingErrorReason(error)).toBe(expected);
  });
});

describe('createListingCache', () => {
  it('lists each directory once, failures included, until refreshed', async () => {
    const listDirectory = vi
      .fn()
      .mockRejectedValueOnce(new Error('slow share'))
      .mockResolvedValue({ entries: [] });
    const cache = createListingCache(listDirectory);
    const params = { path: '//host/share', include_files: false };

    await expect(cache.list(params)).rejects.toThrow('slow share');
    await expect(cache.list({ ...params })).rejects.toThrow('slow share');
    expect(listDirectory).toHaveBeenCalledTimes(1);

    await expect(cache.list(params, { refresh: true })).resolves.toEqual({
      entries: [],
    });
    await cache.list({ ...params, include_files: true });
    expect(listDirectory).toHaveBeenCalledTimes(3);
  });
});
