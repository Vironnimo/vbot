// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../../lib/i18n.js';
import { HOVER_CARD_SHOW_DELAY_MS } from '../../../lib/tooltip.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ChangeStats } = await import('../ChangeStats.svelte');

const STATS = {
  files: 4,
  added: 12,
  removed: 5,
  fileStats: [
    { path: 'C:\\game\\src\\actors\\player.gd', added: 9, removed: 0 },
    { path: 'C:\\game\\src\\actors\\enemy.gd', added: 0, removed: 1 },
    { path: 'C:\\game\\src\\world\\map.gd', added: 3, removed: 4 },
    { path: 'C:\\game\\tools\\cast.gd', added: null, removed: null },
  ],
};

describe('ChangeStats', () => {
  let component;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
  });

  afterEach(() => {
    if (component) {
      unmount(component);
      component = null;
    }
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  function render(props) {
    component = mount(ChangeStats, { target: document.body, props });
    flushSync();
    return document.querySelector('.change-stats');
  }

  function card() {
    return document.querySelector('.changed-files-card');
  }

  async function hover(summary) {
    vi.useFakeTimers();
    summary.dispatchEvent(new Event('pointerenter'));
    await vi.advanceTimersByTimeAsync(HOVER_CARD_SHOW_DELAY_MS);
    flushSync();
  }

  it('renders the summary as one colored block', () => {
    const summary = render({ stats: STATS, class: 'run-footer__changes' });

    expect(summary.classList.contains('run-footer__changes')).toBe(true);
    expect(
      [...summary.querySelectorAll('.change-stats__part')].map((part) => [
        part.textContent,
        part.className.match(/change-stats__part--(\w+)/)[1],
      ]),
    ).toEqual([
      [`${t('chat.changeStats.filesMany', { count: 4 })},`, 'files'],
      ['+12', 'added'],
      ['-5', 'removed'],
    ]);
  });

  it('lists each changed file with its own counts below its folder on hover', async () => {
    const summary = render({ stats: STATS, placement: 'left' });
    // Cards of summaries nobody opens stay empty.
    expect(card().querySelector('.changed-files-card__row')).toBeNull();

    await hover(summary);

    expect(card().dataset.floatingOpen).toBe('true');
    expect(card().querySelector('.changed-files-card__title').textContent).toBe(
      t('chat.changeStats.filesMany', { count: 4 }),
    );
    expect(card().querySelector('.changed-files-card__root').textContent).toBe(
      'C:\\game',
    );
    const counts = (item) =>
      [...item.querySelectorAll('.changed-files-card__count')].map(
        (count) => count.textContent,
      );
    expect(
      [
        ...card().querySelectorAll(
          '.changed-files-card__group, .changed-files-card__row',
        ),
      ].map((item) =>
        item.classList.contains('changed-files-card__group')
          ? [
              item.querySelector('.changed-files-card__directory').textContent,
              item.querySelector('.changed-files-card__folder-files')
                .textContent,
              counts(item),
            ]
          : [
              item.querySelector('.changed-files-card__name').textContent,
              counts(item),
            ],
      ),
    ).toEqual([
      [
        'src\\actors',
        t('chat.changedFiles.folderFilesMany', { count: 2 }),
        ['+9', '-1'],
      ],
      ['enemy.gd', ['+0', '-1']],
      ['player.gd', ['+9', '-0']],
      ['src\\world', t('chat.changedFiles.folderFilesOne'), ['+3', '-4']],
      ['map.gd', ['+3', '-4']],
      ['tools', t('chat.changedFiles.folderFilesOne'), ['', '']],
      ['cast.gd', ['', '']],
    ]);
  });

  it('copies the full path of a file from its row', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    const summary = render({ stats: STATS });
    await hover(summary);

    const copy = card().querySelector(
      '.changed-files-card__row .changed-files-card__copy',
    );
    expect(copy.getAttribute('aria-label')).toBe(
      t('chat.changedFiles.copyPath', { name: 'enemy.gd' }),
    );
    copy.click();
    await vi.runAllTimersAsync();

    expect(writeText).toHaveBeenCalledWith('C:\\game\\src\\actors\\enemy.gd');
  });

  it('counts changed files the statistics do not name', async () => {
    const summary = render({
      stats: { ...STATS, files: 205 },
    });
    await hover(summary);

    expect(
      card().querySelector('.changed-files-card__unlisted').textContent.trim(),
    ).toBe(t('chat.changedFiles.unlistedMany', { count: 201 }));
  });
});
