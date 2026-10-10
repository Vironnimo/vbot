// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  tick,
  unmount,
  swarm,
  button,
  createBridge,
  overrideOperations,
  callsTo,
  settle,
  fill,
  openSwarm,
  fixtureState,
} from './SwarmPage.support.js';
import { t } from '../../lib/i18n.js';
import { formatMoment } from '../../lib/timeText.js';

const WIKI = t('swarm.tabs.wiki');
const BOARD = t('swarm.tabs.board');
const EDIT = t('common.edit');
// Long past, so its distance from now stays the same during a test.
const UPDATED = '2026-01-05T10:00:00Z';

// A bridge whose Wiki holds one page, `w1`, with its version history. The
// page opens with its title as a heading, as participants usually write it.
function wikiBridge() {
  const fixture = createBridge();
  vi.spyOn(fixture.bridge, 'openLink');
  let page = {
    page_id: 'wpg-one',
    number: 1,
    title: 'Shared research',
    content: '# Shared research\n\nOriginal evidence from #1',
    revision: 1,
    current_revision: 1,
    deleted: false,
    updated_at: UPDATED,
    author: { name: 'Alpha', kind: 'participant' },
    link: '[Shared research](#wiki/w1)',
  };
  const versions = [structuredClone(page)];
  const version = (revision) =>
    versions.find((entry) => entry.revision === revision);
  const write = (args) => {
    if (args.action === 'create') {
      page = {
        ...page,
        title: args.title,
        content: args.content,
        page_id: 'wpg-new',
        number: 2,
        revision: 1,
        current_revision: 1,
      };
    } else {
      if (args.expected_revision !== page.revision)
        throw new Error('The page changed. Read the current page.');
      const source = args.action === 'restore' ? version(args.revision) : page;
      page = {
        ...page,
        ...source,
        title: args.title ?? source.title,
        content: args.content ?? source.content,
        deleted: args.action === 'delete',
        revision: page.revision + 1,
        current_revision: page.revision + 1,
      };
    }
    versions.push(structuredClone(page));
    return structuredClone(page);
  };
  const matches = (args) =>
    (!page.deleted || args.include_deleted) &&
    `${page.title}\n${page.content}`
      .toLowerCase()
      .includes((args.query ?? '').toLowerCase());
  overrideOperations(fixture.operation, {
    'swarms.get': () => ({
      swarm: {
        ...swarm,
        goal_post_id: 'pst-goal',
        newest_post_sequence: 1,
        newest_wiki_page_number: 1,
      },
    }),
    'board.read': () => ({
      entries: [
        {
          id: 'pst-link',
          sequence: 1,
          discussion_id: 'dsc-main',
          author: { name: 'Alpha' },
          text: '[Research](#wiki/w1)',
        },
      ],
      has_more: false,
    }),
    wiki: (args) => {
      if (args.action === 'list')
        return {
          entries: matches(args) ? [{ ...page, excerpt: page.content }] : [],
          has_more: false,
        };
      if (args.action === 'read')
        return {
          ...(args.revision ? version(args.revision) : page),
          current_revision: page.revision,
          total_chars: page.content.length,
          offset: 0,
        };
      if (args.action === 'history')
        return { entries: [...versions].reverse(), has_more: false };
      return write(args);
    },
  });
  return {
    ...fixture,
    peerEdit() {
      page = {
        ...page,
        content: 'Peer evidence',
        revision: page.revision + 1,
        current_revision: page.revision + 1,
      };
    },
  };
}

async function openWikiTab(fixture) {
  await openSwarm(fixture.bridge);
  await vi.waitFor(() => expect(button(WIKI)).toBeDefined());
  button(WIKI).click();
}
async function openWiki(fixture) {
  await openWikiTab(fixture);
  await vi.waitFor(() => expect(button('Shared research')).toBeDefined());
  button('Shared research').click();
  await vi.waitFor(() => expect(button(EDIT)).toBeDefined());
}
const wikiContent = () => document.querySelector('.wiki-content').textContent;
const wikiCalls = (fixture, action) =>
  callsTo(fixture.operation, 'wiki').filter(
    ([, args]) => args.action === action,
  );
// Holds the next Wiki call of `action` open until the test finishes it.
function holdWiki(fixture, action, finish) {
  overrideOperations(fixture.operation, {
    wiki: (args, fallback) =>
      args.action === action
        ? new Promise((resolve) => finish(resolve, fallback))
        : fallback(),
  });
}

describe('Swarm Wiki pages', () => {
  it('replaces the initial loading state with a list failure it can retry', async () => {
    const fixture = wikiBridge();
    let unavailable = true;
    overrideOperations(fixture.operation, {
      wiki: (args, fallback) =>
        args.action === 'list' && unavailable
          ? Promise.reject(new Error('Wiki unavailable'))
          : fallback(),
    });
    await openWikiTab(fixture);
    const panel = () => document.querySelector('.wiki-panel');
    await vi.waitFor(() =>
      expect(panel().textContent).toContain('Wiki unavailable'),
    );
    expect(panel().querySelector('[role="status"]')).toBeNull();
    expect(panel().textContent).not.toContain(t('swarm.wiki.empty'));
    unavailable = false;
    button(t('common.retry')).click();
    await vi.waitFor(() => expect(button('Shared research')).toBeDefined());
    expect(panel().textContent).not.toContain('Wiki unavailable');
  });

  it('opens internal Board links, displays the pinned request and restores a deleted page', async () => {
    const fixture = wikiBridge();
    await openSwarm(fixture.bridge);
    await vi.waitFor(() =>
      expect(document.querySelector('a[href="#wiki/w1"]')).not.toBeNull(),
    );
    expect(document.querySelector('.swarm-goal-post').textContent).toContain(
      swarm.prompt,
    );
    document.querySelector('a[href="#wiki/w1"]').click();
    await vi.waitFor(() =>
      expect(wikiContent()).toContain('Original evidence'),
    );
    expect(fixture.bridge.openLink).not.toHaveBeenCalled();
    // A page link names the page by the number participants use for it.
    expect(fixture.operation).toHaveBeenCalledWith(
      'wiki',
      expect.objectContaining({ action: 'read', page_id: 'w1' }),
    );
    // The link lands on the page title; the page header shows the title,
    // so the content's leading heading that repeats it is left out.
    const heading = document.querySelector('.wiki-content h3');
    expect(heading.querySelector('.wiki-number').textContent).toBe('w1');
    await vi.waitFor(() => expect(document.activeElement).toBe(heading));
    expect(document.querySelector('.wiki-content h1')).toBeNull();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    button(t('swarm.wiki.copyLink')).click();
    expect(writeText).toHaveBeenCalledWith('[Shared research](#wiki/w1)');
    // A narrow panel shows the list or a page; going back to the list
    // returns to the page's row.
    button(t('swarm.wiki.allPages')).click();
    const row = document.querySelector('.wiki-row');
    await vi.waitFor(() => expect(document.activeElement).toBe(row));
    expect(document.querySelector('.wiki-panel').dataset.view).toBe('list');
    expect(row.querySelector('.wiki-number').textContent).toBe('w1');
    // The list names pages; revision, author and time appear on hover or focus.
    expect(row.textContent).not.toContain('Original evidence');
    await vi.waitFor(() =>
      expect(
        [...document.querySelectorAll('#app-tooltip dt')].map((label) => [
          label.textContent,
          label.nextElementSibling.textContent,
        ]),
      ).toEqual([
        [t('swarm.wiki.revisionLabel'), '1'],
        [t('swarm.wiki.author'), 'Alpha'],
        [t('swarm.wiki.updated'), formatMoment(UPDATED)],
      ]),
    );
    const reads = wikiCalls(fixture, 'read').length;
    row.click();
    await vi.waitFor(() =>
      expect(document.querySelector('.wiki-panel').dataset.view).toBe('page'),
    );
    expect(wikiCalls(fixture, 'read')).toHaveLength(reads);
    const restore = t('swarm.wiki.restorePage');
    const remove = t('swarm.wiki.delete');
    button(remove).click();
    await vi.waitFor(() => expect(button(restore)).toBeDefined());
    button(restore).click();
    await vi.waitFor(() => expect(button(remove)).toBeDefined());
    expect(fixture.operation).toHaveBeenCalledWith(
      'wiki',
      expect.objectContaining({
        action: 'restore',
        expected_revision: 2,
        revision: 2,
      }),
    );
    // A post number in a page opens that post on the Board.
    document.querySelector('.wiki-content a[href="#post/1"]').click();
    await vi.waitFor(() =>
      expect(document.activeElement.dataset.postNumber).toBe('1'),
    );
    expect(fixture.bridge.openLink).not.toHaveBeenCalled();
  });

  it('creates free Markdown pages and searches without exposing content as HTML', async () => {
    const fixture = wikiBridge();
    await openWiki(fixture);
    // Cancel leaves an untouched new page and shows the page from before.
    button(t('swarm.wiki.new')).click();
    await vi.waitFor(() =>
      expect(document.activeElement.id).toBe('wiki-title'),
    );
    button(t('common.cancel')).click();
    await vi.waitFor(() =>
      expect(wikiContent()).toContain('Original evidence'),
    );
    button(t('swarm.wiki.new')).click();
    await tick();
    fill('wiki-content', '# Findings\n<script>bad()</script>');
    await tick();
    // Save creates the page only once it has a title.
    button(t('common.save')).click();
    await vi.waitFor(() =>
      expect(document.getElementById('wiki-title-error')?.textContent).toBe(
        t('swarm.wiki.titleRequired'),
      ),
    );
    expect(document.activeElement.id).toBe('wiki-title');
    fill('wiki-title', 'New findings');
    await tick();
    expect(document.getElementById('wiki-title-error')).toBeNull();
    button(t('common.save')).click();
    await vi.waitFor(() =>
      expect(button(t('swarm.wiki.preview'))).toBeDefined(),
    );
    expect(wikiCalls(fixture, 'create')).toEqual([
      [
        'wiki',
        expect.objectContaining({ action: 'create', title: 'New findings' }),
      ],
    ]);
    button(t('swarm.wiki.preview')).click();
    await vi.waitFor(() =>
      expect(document.querySelector('.wiki-content h1')).not.toBeNull(),
    );
    expect(document.querySelector('.wiki-content script')).toBeNull();
    fill('wiki-search', 'findings');
    document.getElementById('wiki-search').dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Enter',
        bubbles: true,
        cancelable: true,
      }),
    );
    await vi.waitFor(() =>
      expect(fixture.operation).toHaveBeenCalledWith(
        'wiki',
        expect.objectContaining({ action: 'list', query: 'findings' }),
      ),
    );
  });

  it('searches once typing rests and clears a search without matches', async () => {
    const fixture = wikiBridge();
    await openWikiTab(fixture);
    await vi.waitFor(() => expect(button('Shared research')).toBeDefined());
    vi.useFakeTimers();
    const lists = () => wikiCalls(fixture, 'list');
    const count = lists().length;
    fill('wiki-search', 'missing');
    await vi.advanceTimersByTimeAsync(200);
    expect(lists()).toHaveLength(count);
    await vi.advanceTimersByTimeAsync(100);
    expect(lists()).toHaveLength(count + 1);
    expect(lists().at(-1)[1]).toMatchObject({ query: 'missing' });
    await vi.waitFor(() =>
      expect(document.querySelector('.wiki-index').textContent).toContain(
        t('swarm.wiki.noMatchesHelp', { query: 'missing' }),
      ),
    );
    button(t('swarm.wiki.clearSearch')).click();
    await vi.waitFor(() => expect(button('Shared research')).toBeDefined());
    expect(document.getElementById('wiki-search').value).toBe('');
    expect(lists().at(-1)[1]).not.toHaveProperty('query');
  });

  it('shows earlier versions, returns to the current one and restores one', async () => {
    const fixture = wikiBridge();
    await openWiki(fixture);
    button(EDIT).click();
    await tick();
    fill('wiki-content', 'Second evidence');
    button(t('swarm.wiki.preview')).click();
    await vi.waitFor(() => expect(wikiContent()).toContain('Second evidence'));
    button(t('swarm.wiki.history')).click();
    const versions = () => [...document.querySelectorAll('.wiki-version')];
    await vi.waitFor(() => expect(versions()).toHaveLength(2));
    expect(versions()[0].textContent).toContain(t('swarm.wiki.current'));
    expect(versions()[0].getAttribute('aria-current')).toBe('true');
    versions()[1].click();
    await vi.waitFor(() =>
      expect(wikiContent()).toContain('Original evidence'),
    );
    expect(wikiContent()).toContain(
      t('swarm.wiki.viewingRevision', { revision: 1, total: 2 }),
    );
    expect(versions()[1].getAttribute('aria-current')).toBe('true');
    expect(button(EDIT).disabled).toBe(true);
    button(t('swarm.wiki.backToCurrent')).click();
    await vi.waitFor(() => expect(wikiContent()).toContain('Second evidence'));
    versions()[1].click();
    await vi.waitFor(() =>
      expect(button(t('swarm.wiki.restore'))).toBeDefined(),
    );
    button(t('swarm.wiki.restore')).click();
    await vi.waitFor(() => expect(versions()).toHaveLength(3));
    expect(fixture.operation).toHaveBeenCalledWith(
      'wiki',
      expect.objectContaining({
        action: 'restore',
        expected_revision: 2,
        revision: 1,
      }),
    );
    expect(wikiContent()).toContain('Original evidence');
    expect(button(EDIT).disabled).toBe(false);
  });
});

describe('Swarm Wiki drafts and refresh', () => {
  it('saves the current draft before switching tabs and preserves a conflicting draft', async () => {
    const fixture = wikiBridge();
    await openWiki(fixture);
    button(EDIT).click();
    // The editor holds the whole Markdown source, title heading included.
    await vi.waitFor(() =>
      expect(document.activeElement.id).toBe('wiki-content'),
    );
    expect(document.activeElement.value).toBe(
      '# Shared research\n\nOriginal evidence from #1',
    );
    fill('wiki-content', 'My evidence');
    button(BOARD).click();
    await vi.waitFor(() =>
      expect(document.querySelector('.wiki-panel')).toBeNull(),
    );
    expect(fixture.operation).toHaveBeenCalledWith(
      'wiki',
      expect.objectContaining({
        action: 'update',
        content: 'My evidence',
        expected_revision: 1,
      }),
    );
    button(WIKI).click();
    await vi.waitFor(() => expect(button('Shared research')).toBeDefined());
    button('Shared research').click();
    await vi.waitFor(() => expect(button(EDIT)).toBeDefined());
    button(EDIT).click();
    await tick();
    fill('wiki-content', 'Unsaved conflicting evidence');
    fixture.peerEdit();
    button(BOARD).click();
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain(
        'The page changed. Read the current page.',
      ),
    );
    expect(document.getElementById('wiki-content').value).toBe(
      'Unsaved conflicting evidence',
    );
    button(t('swarm.wiki.discard')).click();
    await vi.waitFor(() =>
      expect(document.querySelector('.wiki-panel')).toBeNull(),
    );
  });

  it('retains Wiki entries and the open page across tabs while refreshing quietly', async () => {
    const fixture = wikiBridge();
    await openWiki(fixture);
    vi.useFakeTimers();
    const reads = () => wikiCalls(fixture, 'read').length;
    const count = reads();
    const wikiCallCount = callsTo(fixture.operation, 'wiki').length;
    // Only changes of this Swarm's Wiki reload it.
    for (const [resource, id] of [
      ['wiki', 'swr-b'],
      ['profiles', 'prf-a'],
      ['posts', 'swr-a'],
      ['participants', 'swr-a'],
    ])
      fixture.bridge.invalidate({ resource, ids: [id], revision: 2 });
    await vi.advanceTimersByTimeAsync(160);
    expect(callsTo(fixture.operation, 'wiki')).toHaveLength(wikiCallCount);
    fixture.bridge.invalidate({
      resource: 'wiki',
      ids: ['swr-a'],
      revision: 2,
    });
    // It follows the pass that just ran, so it waits for that pass's second.
    await vi.advanceTimersByTimeAsync(1000);
    expect(callsTo(fixture.operation, 'wiki').length).toBeGreaterThan(
      wikiCallCount,
    );
    fixture.bridge.invalidate();
    await vi.advanceTimersByTimeAsync(160);
    expect(reads()).toBe(count);
    button(BOARD).click();
    await tick();
    expect(document.querySelector('.wiki-panel')).toBeNull();
    const calls = callsTo(fixture.operation, 'wiki').length;
    fixture.bridge.invalidate();
    await vi.advanceTimersByTimeAsync(160);
    expect(callsTo(fixture.operation, 'wiki')).toHaveLength(calls);
    let finishList;
    holdWiki(fixture, 'list', (resolve) => (finishList = resolve));
    button(WIKI).click();
    await tick();
    expect(button('Shared research')).toBeDefined();
    expect(wikiContent()).toContain('Original evidence');
    expect(document.querySelector('.wiki-panel [role="status"]')).toBeNull();
    await vi.waitFor(() => expect(finishList).toBeTypeOf('function'));
    fixture.peerEdit();
    finishList({
      entries: [{ page_id: 'wpg-one', title: 'Shared research', revision: 2 }],
    });
    await vi.waitFor(() => expect(wikiContent()).toContain('Peer evidence'));
    expect(reads()).toBe(count + 1);
  });

  it('keeps the pages loaded with Load more when the Wiki refreshes', async () => {
    const fixture = wikiBridge();
    const pages = Array.from({ length: 25 }, (_, index) => ({
      page_id: `wpg-${index + 1}`,
      number: index + 1,
      title: `Page ${index + 1}`,
      revision: 1,
      updated_at: UPDATED,
    }));
    overrideOperations(fixture.operation, {
      wiki: (args, fallback) => {
        if (args.action !== 'list') return fallback();
        const offset = Number(args.cursor ?? 0);
        const more = offset + 20 < pages.length;
        return {
          entries: pages.slice(offset, offset + 20),
          has_more: more,
          ...(more
            ? {
                next_call: {
                  arguments: { ...args, cursor: String(offset + 20) },
                },
              }
            : {}),
        };
      },
    });
    await openWikiTab(fixture);
    const rows = () => document.querySelectorAll('.wiki-row');
    await vi.waitFor(() => expect(rows()).toHaveLength(20));
    button(t('swarm.wiki.loadMore')).click();
    await vi.waitFor(() => expect(rows()).toHaveLength(25));
    // Focus continues at the first page it loaded.
    await vi.waitFor(() =>
      expect(document.activeElement.dataset.pageId).toBe('wpg-21'),
    );
    const lists = wikiCalls(fixture, 'list').length;
    fixture.bridge.invalidate({
      resource: 'wiki',
      ids: ['swr-a'],
      revision: 2,
    });
    await vi.waitFor(() =>
      expect(wikiCalls(fixture, 'list')).toHaveLength(lists + 2),
    );
    await settle();
    expect(rows()).toHaveLength(25);
  });

  it('does not replace a Wiki draft with an in-flight background page read', async () => {
    const fixture = wikiBridge();
    await openWiki(fixture);
    fixture.peerEdit();
    let finishRead;
    holdWiki(fixture, 'read', (resolve, fallback) => {
      finishRead = async () => resolve(await fallback());
    });
    fixture.bridge.invalidate();
    await vi.waitFor(() => expect(finishRead).toBeTypeOf('function'));
    button(EDIT).click();
    await tick();
    fill('wiki-content', 'My draft remains');
    await finishRead();
    await tick();
    expect(document.getElementById('wiki-content').value).toBe(
      'My draft remains',
    );
  });

  it('opens the Wiki with twelve active participants without waiting for hidden reports', async () => {
    const { bridge, operation } = createBridge();
    let finishList;
    overrideOperations(operation, {
      'swarms.get': () => ({
        swarm: {
          ...structuredClone(swarm),
          participants: Array.from({ length: 12 }, (_, index) => ({
            ...swarm.participants[0],
            id: `peer-${index}`,
            display_name: `Peer ${index}`,
            run_active: true,
            lifecycle_run_id: `run-${index}`,
          })),
        },
      }),
      'swarms.usage': () => new Promise(() => {}),
      'swarms.events': () => new Promise(() => {}),
      wiki: (args, fallback) =>
        args.action === 'list'
          ? new Promise((resolve) => {
              finishList = resolve;
            })
          : fallback(),
    });
    await openSwarm(bridge);
    button(WIKI).click();
    await vi.waitFor(() => expect(finishList).toBeTypeOf('function'));
    for (let i = 0; i < 90; i++) bridge.invalidate();
    await settle(150);
    expect(callsTo(operation, 'wiki')).toHaveLength(1);
    finishList({
      entries: [
        { page_id: 'wpg-ready', title: 'Loaded under load', revision: 1 },
      ],
    });
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain('Loaded under load'),
    );
    await vi.waitFor(() => expect(callsTo(operation, 'wiki')).toHaveLength(2));
    expect(callsTo(operation, 'swarms.usage')).toHaveLength(0);
    expect(callsTo(operation, 'swarms.events')).toHaveLength(0);
    const calls = operation.mock.calls.length;
    fixtureState.mounted = await unmount(fixtureState.mounted);
    finishList({ entries: [] });
    await settle(150);
    expect(operation.mock.calls).toHaveLength(calls);
  });
});
