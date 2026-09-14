// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  button,
  createBridge,
  fill,
  render,
  swarm,
  tick,
} from './SwarmPage.support.js';

function wikiBridge() {
  const fixture = createBridge();
  const original = fixture.operation.getMockImplementation();
  vi.spyOn(fixture.bridge, 'openLink');
  let page = {
    page_id: 'wpg-one',
    title: 'Shared research',
    content: 'Original evidence',
    revision: 1,
    current_revision: 1,
    deleted: false,
    author: { name: 'Alpha', kind: 'participant' },
    link: '[Shared research](#wiki/wpg-one)',
  };
  const versions = [structuredClone(page)];
  fixture.operation.mockImplementation(async (name, args) => {
    if (name === 'swarms.get')
      return { swarm: { ...swarm, goal_post_id: 'pst-goal' } };
    if (name === 'board.read')
      return {
        entries: [
          {
            id: 'pst-link',
            author: { name: 'Alpha' },
            text: '[Research](#wiki/wpg-one)',
          },
        ],
        has_more: false,
      };
    if (name !== 'wiki') return original(name, args);
    if (args.action === 'list')
      return {
        entries:
          page.deleted && !args.include_deleted
            ? []
            : [{ ...page, excerpt: page.content }],
        has_more: false,
      };
    if (args.action === 'read')
      return {
        ...(args.revision
          ? versions.find((version) => version.revision === args.revision)
          : page),
        current_revision: page.revision,
        total_chars: page.content.length,
        offset: 0,
      };
    if (args.action === 'history')
      return { entries: [...versions].reverse(), has_more: false };
    if (args.action === 'create') {
      page = {
        ...page,
        title: args.title,
        content: args.content,
        page_id: 'wpg-new',
        revision: 1,
        current_revision: 1,
      };
    } else {
      if (args.expected_revision !== page.revision)
        throw new Error('The page changed. Read the current page.');
      const source =
        args.action === 'restore'
          ? versions.find((version) => version.revision === args.revision)
          : page;
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

async function openWiki(fixture) {
  await render(fixture.bridge);
  button('Investigate').click();
  await vi.waitFor(() => expect(button('Wiki')).toBeDefined());
  button('Wiki').click();
  await vi.waitFor(() => expect(button('Shared research')).toBeDefined());
  button('Shared research').click();
  await vi.waitFor(() => expect(button('Edit')).toBeDefined());
}

describe('Swarm Wiki', () => {
  it('opens internal Board links, displays the pinned request and restores a deleted page', async () => {
    const fixture = wikiBridge();
    await render(fixture.bridge);
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('a[href="#wiki/wpg-one"]')).not.toBeNull(),
    );
    expect(document.querySelector('.swarm-goal-post').textContent).toContain(
      'pst-goal',
    );
    document.querySelector('a[href="#wiki/wpg-one"]').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.wiki-content').textContent).toContain(
        'Original evidence',
      ),
    );
    expect(fixture.bridge.openLink).not.toHaveBeenCalled();
    button('Delete page').click();
    await vi.waitFor(() =>
      expect(button('Restore this version')).toBeDefined(),
    );
    button('Restore this version').click();
    await vi.waitFor(() => expect(button('Delete page')).toBeDefined());
    expect(fixture.operation).toHaveBeenCalledWith(
      'wiki',
      expect.objectContaining({
        action: 'restore',
        expected_revision: 2,
        revision: 2,
      }),
    );
  });

  it('saves the current draft before switching tabs and preserves a conflicting draft', async () => {
    const fixture = wikiBridge();
    await openWiki(fixture);
    button('Edit').click();
    await tick();
    fill('wiki-content', 'My evidence');
    button('Board').click();
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
    button('Wiki').click();
    await vi.waitFor(() => expect(button('Shared research')).toBeDefined());
    button('Shared research').click();
    await vi.waitFor(() => expect(button('Edit')).toBeDefined());
    button('Edit').click();
    await tick();
    fill('wiki-content', 'Unsaved conflicting evidence');
    fixture.peerEdit();
    button('Board').click();
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain('The page changed.'),
    );
    expect(document.getElementById('wiki-content').value).toBe(
      'Unsaved conflicting evidence',
    );
    button('Discard changes and continue').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.wiki-panel')).toBeNull(),
    );
  });

  it('creates free Markdown pages and searches without exposing content as HTML', async () => {
    const fixture = wikiBridge();
    await openWiki(fixture);
    button('New page').click();
    await tick();
    fill('wiki-title', 'New findings');
    fill('wiki-content', '# Findings\n<script>bad()</script>');
    button('Save').click();
    await vi.waitFor(() =>
      expect(fixture.operation).toHaveBeenCalledWith(
        'wiki',
        expect.objectContaining({ action: 'create', title: 'New findings' }),
      ),
    );
    button('View page').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.wiki-content h1')).not.toBeNull(),
    );
    expect(document.querySelector('.wiki-content script')).toBeNull();
    fill('wiki-search', 'findings');
    document
      .querySelector('.wiki-index form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await vi.waitFor(() =>
      expect(fixture.operation).toHaveBeenCalledWith(
        'wiki',
        expect.objectContaining({ action: 'list', query: 'findings' }),
      ),
    );
  });
});
