// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';
import { reactiveProps } from '../../__tests__/reactiveProps.support.svelte.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const listProjects = vi.fn();
vi.mock('$lib/api.js', () => ({ listProjects }));

const { default: PathField } = await import('../PathField.svelte');

const dir = (name, extra = {}) => ({
  name,
  kind: 'directory',
  link: false,
  hidden: false,
  ...extra,
});
const file = (name) => ({ name, kind: 'file', link: false, hidden: false });
const listingFailure = (reason) =>
  Object.assign(new Error(reason), {
    code: 'domain_error',
    details: { code: 'domain_error', data: { reason } },
  });

// A fake server: listings by path; a listed Error is thrown.
function fakeServer(listings, separator = '\\') {
  return vi.fn(async ({ path }) => {
    const listing = listings[path ?? 'places'];
    if (listing instanceof Error) throw listing;
    if (!listing) throw listingFailure('not_found');
    return { path, parent: null, truncated: false, separator, ...listing };
  });
}

describe('PathField', () => {
  let mounted;
  let props;

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = '';
    init('en');
    listProjects.mockReset();
  });

  afterEach(async () => {
    if (mounted) await unmount(mounted);
    mounted = null;
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  function render(initial) {
    props = reactiveProps({
      value: '',
      onInput: (next) => {
        props.value = next;
      },
      ...initial,
    });
    mounted = mount(PathField, { target: document.body, props });
    flushSync();
    return document.body.querySelector('input');
  }

  async function settle() {
    await vi.advanceTimersByTimeAsync(200);
    flushSync();
  }

  function type(input, text) {
    input.value = text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
  }

  function press(target, key, options = {}) {
    const event = new KeyboardEvent('keydown', {
      key,
      bubbles: true,
      cancelable: true,
      ...options,
    });
    target.dispatchEvent(event);
    flushSync();
    return event;
  }

  const suggestions = () =>
    [...document.body.querySelectorAll('.path-field__option')].map((option) =>
      option.textContent.trim(),
    );

  it('completes typed folders from the server, one listed folder at a time', async () => {
    const listDirectory = fakeServer({
      'C:/': {
        entries: [
          dir('Users'),
          dir('Windows'),
          dir('$Recycle.Bin', { hidden: true }),
        ],
      },
      'C:/Users': { entries: [dir('me'), dir('Public')] },
      'C:/Users/me': { entries: [] },
    });
    const callerKeydown = vi.fn();
    const input = render({ listDirectory, onkeydown: callerKeydown });
    input.focus();

    type(input, 'npx');
    await settle();
    expect(listDirectory).not.toHaveBeenCalled();

    type(input, 'C:\\u');
    type(input, 'C:\\us');
    await settle();
    expect(listDirectory).toHaveBeenCalledTimes(1);
    expect(listDirectory).toHaveBeenCalledWith({
      path: 'C:/',
      include_files: false,
    });
    expect(suggestions()).toEqual(['Users/']);
    expect(input.getAttribute('aria-expanded')).toBe('true');

    // Tab takes the only match and lists inside it at once.
    expect(press(input, 'Tab').defaultPrevented).toBe(true);
    expect(props.value).toBe('C:\\Users\\');
    await settle();
    expect(suggestions()).toEqual(['me/', 'Public/']);

    // Enter without a highlighted suggestion stays the caller's.
    press(input, 'Enter');
    expect(callerKeydown).toHaveBeenCalledTimes(1);

    press(input, 'ArrowDown');
    expect(input.getAttribute('aria-activedescendant')).toBe(
      document.body.querySelector('.path-field__option').id,
    );
    expect(press(input, 'Enter').defaultPrevented).toBe(true);
    expect(props.value).toBe('C:\\Users\\me\\');
    expect(callerKeydown).toHaveBeenCalledTimes(1);

    type(input, 'C:\\Users\\P');
    await settle();
    expect(press(input, 'Escape').defaultPrevented).toBe(true);
    expect(suggestions()).toEqual([]);
    expect(press(input, 'Escape').defaultPrevented).toBe(false);

    // Leaving the field drops a completed folder's separator, not a root's.
    type(input, 'C:\\Users\\me\\');
    input.blur();
    flushSync();
    expect(props.value).toBe('C:\\Users\\me');
    input.focus();
    type(input, 'C:\\');
    input.blur();
    flushSync();
    expect(props.value).toBe('C:\\');
  });

  it('shows only the newest listing and says when a folder cannot be read', async () => {
    let finishSlow;
    const listDirectory = vi.fn(({ path }) => {
      if (path === '/slow') {
        return new Promise((resolve) => {
          finishSlow = () => resolve({ entries: [dir('stale')] });
        });
      }
      if (path === '/root') return Promise.reject(listingFailure('unreadable'));
      return Promise.resolve({ entries: [dir('fresh')] });
    });
    const input = render({ listDirectory });
    input.focus();

    type(input, '/slow/s');
    await settle();
    type(input, '/fast/');
    await settle();
    finishSlow();
    await settle();
    expect(suggestions()).toEqual(['fresh/']);

    type(input, '/root/');
    await settle();
    expect(document.body.querySelector('.path-field__note').textContent).toBe(
      'vBot is not allowed to read this folder.',
    );
  });

  it('browses from the places to a chosen folder in the server separators', async () => {
    listProjects.mockResolvedValue({
      projects: [
        { project_id: 'vbot', display_name: 'vBot', cwd: 'C:\\work\\vBot' },
      ],
    });
    const listDirectory = fakeServer({
      places: {
        path: '',
        entries: [dir('C:/'), dir('D:/')],
        home: 'C:/Users/me',
      },
      'C:/work/vBot': {
        entries: [dir('webui'), dir('.git', { hidden: true })],
      },
      'C:/work/vBot/webui': { entries: [] },
    });
    render({ listDirectory, projectShortcuts: true });

    document.body.querySelector('.path-field__browse').click();
    await settle();
    const dialog = document.body.querySelector('.modal.path-browser');
    const rows = () =>
      [...dialog.querySelectorAll('.path-browser__row')].map((row) =>
        row.textContent.replace(/\s+/g, ' ').trim(),
      );
    expect(dialog.querySelector('.modal-title').textContent).toBe(
      'Choose a folder',
    );
    expect(rows()).toEqual([
      'Home C:\\Users\\me',
      'C:\\',
      'D:\\',
      'vBot C:\\work\\vBot',
    ]);

    dialog.querySelectorAll('.path-browser__row')[3].click();
    await settle();
    expect(listDirectory).toHaveBeenLastCalledWith({
      path: 'C:/work/vBot',
      include_files: false,
    });
    expect(
      [...dialog.querySelectorAll('.path-browser__crumb')].map((crumb) =>
        crumb.textContent.trim(),
      ),
    ).toEqual(['Places', 'C:', 'work', 'vBot']);
    expect(rows()).toEqual(['webui']);
    dialog.querySelector('.path-browser__hidden button').click();
    flushSync();
    expect(rows()).toEqual(['.git', 'webui']);

    // A filter highlights its first match, and Enter opens it.
    const filter = dialog.querySelector('.path-browser__filter');
    type(filter, 'WE');
    expect(
      dialog.querySelector('.path-browser__row.active').textContent.trim(),
    ).toBe('webui');
    press(filter, 'Enter');
    await settle();
    expect(dialog.querySelector('.path-browser__state').textContent).toContain(
      'This folder has no subfolders.',
    );
    expect(dialog.querySelector('.path-browser__choice').textContent).toBe(
      'C:\\work\\vBot\\webui',
    );
    dialog.querySelector('.modal-footer .btn-primary').click();
    flushSync();
    expect(props.value).toBe('C:\\work\\vBot\\webui');
    expect(document.body.querySelector('.path-browser')).toBeNull();
  });

  it('keeps a root, opens at the value and returns files relative to it', async () => {
    const listDirectory = fakeServer(
      {
        'docs/a.md': listingFailure('not_a_directory'),
        docs: {
          entries: [dir('locked'), file('a.md'), file('b.md')],
        },
        'docs/locked': listingFailure('unreadable'),
      },
      '/',
    );
    render({
      listDirectory,
      mode: 'file',
      root: 'C:/work/vBot',
      value: 'docs/a.md',
    });

    document.body.querySelector('.path-field__browse').click();
    await settle();
    const dialog = document.body.querySelector('.modal.path-browser');
    const crumbs = () =>
      [...dialog.querySelectorAll('.path-browser__crumb')].map((crumb) =>
        crumb.textContent.trim(),
      );
    expect(listDirectory).toHaveBeenCalledWith({
      path: 'docs',
      root: 'C:/work/vBot',
      include_files: true,
    });
    expect(crumbs()).toEqual(['vBot', 'docs']);
    expect(
      dialog.querySelector('.path-browser__row.active').textContent.trim(),
    ).toBe('a.md');

    const list = dialog.querySelector('[role="listbox"]');
    press(list, 'ArrowUp');
    press(list, 'Enter');
    await settle();
    expect(dialog.querySelector('[role="alert"]').textContent).toContain(
      'vBot is not allowed to read this folder.',
    );
    press(list, 'Backspace');
    await settle();
    expect(crumbs()).toEqual(['vBot', 'docs']);

    dialog.querySelectorAll('.path-browser__row')[2].click();
    flushSync();
    dialog.querySelector('.modal-footer .btn-primary').click();
    flushSync();
    expect(props.value).toBe('docs/b.md');
  });
});
