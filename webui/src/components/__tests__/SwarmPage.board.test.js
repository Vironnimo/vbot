// @vitest-environment jsdom
import { describe, expect, it, onTestFinished, vi } from 'vitest';
import {
  flushSync,
  tick,
  unmount,
  swarm,
  button,
  createBridge,
  overrideOperations,
  callsTo,
  settle,
  fill,
  render,
  openSwarm,
  fixtureState,
} from './SwarmPage.support.js';
import { t } from '../../lib/i18n.js';

const WRITE_POST = t('swarm.board.openComposer');
const POST = t('swarm.board.submit');

const board = () => document.querySelector('.board');
function selectDiscussion(id) {
  const dropdown = document.getElementById('swarm-discussion');
  dropdown.value = id;
  dropdown.dispatchEvent(new Event('change', { bubbles: true }));
}
// Replies to Board reads of the main discussion with the given posts.
function boardPosts(operation, entries) {
  overrideOperations(operation, { 'board.read': () => ({ entries }) });
}
// Answers Board reads like the Store over `posts`, ordered by number: a
// cited post, or a discussion's page of posts, oldest first, below a cursor
// or post number or after a post number.
function boardStore(posts) {
  return (args) => {
    if (args.message_id)
      return {
        entries: posts.filter(
          (post) => `#${post.sequence}` === args.message_id,
        ),
        has_more: false,
      };
    const own = posts.filter(
      (post) => post.discussion_id === args.discussion_id,
    );
    if ('after' in args) {
      const newer = own.filter((post) => post.sequence > args.after);
      return {
        entries: newer.slice(0, args.limit),
        has_more: newer.length > args.limit,
      };
    }
    const below = args.cursor ?? (args.before ? args.before - 1 : Infinity);
    const earlier = own.filter((post) => post.sequence <= below);
    const entries = earlier.slice(-args.limit);
    const more = earlier.length > entries.length;
    return {
      entries,
      has_more: more,
      cursor: more ? entries[0].sequence - 1 : null,
    };
  };
}
const numbers = () =>
  [...document.querySelectorAll('.board > li')].map((item) =>
    Number(item.dataset.postNumber),
  );
// The post numbers from `first` down to `last`.
const countDown = (first, last) =>
  Array.from({ length: first - last + 1 }, (_, index) => first - index);

describe('Swarm Board posts', () => {
  it('renders Board Markdown and opens links through the host', async () => {
    const { bridge, operation } = createBridge();
    boardPosts(operation, [
      {
        id: 'pst-markdown',
        author: { id: 'prt-a', kind: 'participant', name: 'Alpha' },
        text: [
          '## QA dsc-main',
          '',
          '**Final verification** with *emphasis*.',
          '',
          '1. **Unit tests:** 31/31',
          '2. **Facade:** 23/23',
          '3. **Game loop:** 600 frames',
          '4. **Browser:** 0 errors',
          '5. **HTTP:** 200 OK',
          '',
          'Files: `core-stats.js` and `arpg-game/start.bat`.',
          '',
          '- First item',
          '- Second item',
          '',
          '> Quoted result',
          '',
          '| Check | Result |',
          '| --- | --- |',
          '| QA | Passed |',
          '',
          '[Report](https://example.test/report)',
          'https://example.test/game',
        ].join('\n'),
      },
    ]);
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.board h2')?.textContent).toBe(
        'QA dsc-main',
      ),
    );
    const post = document.querySelector('.board > li');
    expect(post.querySelector('p strong').textContent).toBe(
      'Final verification',
    );
    expect(post.querySelector('em').textContent).toBe('emphasis');
    expect(post.querySelectorAll('ol > li')).toHaveLength(5);
    expect(post.querySelectorAll('ul > li')).toHaveLength(2);
    expect(
      [...post.querySelectorAll('code')].map((el) => el.textContent),
    ).toEqual(['core-stats.js', 'arpg-game/start.bat']);
    expect(post.querySelector('blockquote').textContent.trim()).toBe(
      'Quoted result',
    );
    expect(post.querySelectorAll('table tbody td')).toHaveLength(2);
    expect(post.querySelector('p').textContent).not.toContain('**');
    const links = [...post.querySelectorAll('a')];
    expect(links.map((link) => link.href)).toEqual([
      'https://example.test/report',
      'https://example.test/game',
    ]);
    for (const link of links) {
      link.click();
      expect(operation).toHaveBeenCalledWith('link.open', { url: link.href });
    }
  });

  it('copies the exact fenced code from a Board post', async () => {
    const { bridge, operation } = createBridge();
    const code = 'const result = "<verified>";\n  console.log(result);\n';
    boardPosts(operation, [{ id: 'pst-code', text: '```js\n' + code + '```' }]);
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.board .msg-code__copy')).not.toBeNull(),
    );
    expect(document.querySelector('.board pre code').textContent).toBe(code);
    expect(
      document.querySelector('.board .msg-code__language').textContent,
    ).toBe('js');
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    document.querySelector('.board .msg-code__copy').click();
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith(code));
  });

  it('keeps raw HTML and unsafe Markdown links inert in Board posts', async () => {
    const { bridge, operation } = createBridge();
    const html = '<img src=x onerror=alert(1)><script>alert(1)</script>';
    boardPosts(operation, [
      {
        id: 'pst-unsafe',
        text: `${html}\n\n[unsafe](javascript:alert(1))\n\n**Safe formatting**`,
      },
    ]);
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.board p strong')?.textContent).toBe(
        'Safe formatting',
      ),
    );
    expect(board().textContent).toContain(html);
    expect(board().querySelector('img, script, [onerror], a')).toBeNull();
  });

  it('renders complete prompt, timestamped author headers and participants above posts', async () => {
    const prompt = 'test-owned long prompt '.repeat(15) + '\nsecond line';
    const { bridge, operation } = createBridge();
    overrideOperations(operation, {
      'swarms.get': () => ({ swarm: { ...structuredClone(swarm), prompt } }),
      'board.read': () => ({
        entries: [
          {
            id: 'post-time',
            sequence: 7,
            author: { name: 'Alpha' },
            text: 'test-owned board text',
            created_at: '2026-09-08T09:15:00+00:00',
          },
        ],
      }),
    });
    await render(bridge);
    bridge.updateContext({
      locale: 'en',
      timezone: 'Europe/Berlin',
      theme: {},
    });
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.board time')).not.toBeNull(),
    );
    const head = document.querySelector('.swarm-head');
    expect(head.querySelector('h2').textContent).toBe('Research');
    expect(head.textContent).not.toContain(prompt);
    expect(head.textContent).not.toContain('swr-a');
    expect(head.querySelector('.chip')).not.toBeNull();
    expect(
      document.querySelector('.swarm-goal-post .msg-markdown').textContent,
    ).toContain('second line');
    expect(document.querySelectorAll('.swarm-goal-post')).toHaveLength(1);
    expect(document.querySelector('.board-directory').textContent).toContain(
      'C:/work',
    );
    expect(document.querySelector('.post-header strong').textContent).toBe(
      'Alpha',
    );
    // The post number is the reference participants use for this post.
    expect(document.querySelector('.post-number').textContent).toBe('#7');
    expect(document.querySelector('.board time').dateTime).toBe(
      '2026-09-08T09:15:00+00:00',
    );
    expect(document.querySelector('.board time').textContent).toMatch(/11:15/);
    const roster = document.querySelector('.participant-pane');
    expect(
      roster.compareDocumentPosition(board()) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    button(t('swarm.tabs.usage')).click();
    await tick();
    expect(document.querySelector('.swarm-identity dd').textContent).toBe(
      'swr-a',
    );
  });

  it('keeps participant avatars consistent across Board, Activity and remounts', async () => {
    const participants = [
      { ...swarm.participants[0], display_name: 'Participant 9' },
      { ...swarm.participants[1], display_name: 'Participant 10' },
    ];
    const { bridge, operation } = createBridge(undefined, {
      ...structuredClone(swarm),
      participants,
    });
    boardPosts(
      operation,
      participants.map((participant) => ({
        id: `post-${participant.id}`,
        author: {
          id: participant.id,
          name: participant.display_name,
          kind: 'participant',
        },
        text: `message-${participant.id}`,
        recipients: participant === participants[0] ? [participants[1].id] : [],
        created_at: '2026-09-08T09:15:00+00:00',
      })),
    );
    const colors = new Map();
    for (let pass = 0; pass < 2; pass += 1) {
      await render(bridge);
      button('Investigate').click();
      await vi.waitFor(() =>
        expect(document.querySelectorAll('.board li')).toHaveLength(2),
      );
      for (const [index, participant] of participants.entries()) {
        const rosterAvatar = button(participant.display_name).querySelector(
          '.participant-avatar',
        );
        const post = [...document.querySelectorAll('.board li')].find((item) =>
          item.textContent.includes(`message-${participant.id}`),
        );
        const postAvatar = post.querySelector('.participant-avatar');
        expect(postAvatar.textContent.trim()).toBe(index === 0 ? 'P9' : 'P10');
        expect(postAvatar.getAttribute('aria-hidden')).toBe('true');
        expect(post.querySelector('strong').textContent).toBe(
          participant.display_name,
        );
        // Addressed participants appear by name, not by id.
        expect(post.textContent.includes('To: Participant 10')).toBe(
          index === 0,
        );
        const color = postAvatar.style.getPropertyValue('--participant-color');
        expect(color).toBe(
          rosterAvatar.style.getPropertyValue('--participant-color'),
        );
        expect(post.style.getPropertyValue('--participant-color')).toBe(color);
        expect(colors.get(participant.id) ?? color).toBe(color);
        colors.set(participant.id, color);
      }
      expect(new Set(colors.values()).size).toBe(2);
      button('Participant 9').click();
      await vi.waitFor(() =>
        expect(document.querySelector('.history')).not.toBeNull(),
      );
      const activityAvatar = document.querySelector(
        '.participants .participant-avatar',
      );
      expect(activityAvatar.style.getPropertyValue('--participant-color')).toBe(
        colors.get('prt-a'),
      );
      const activityChip = activityAvatar.closest('button');
      expect(activityChip.getAttribute('aria-pressed')).toBe('true');
      expect(activityChip.querySelector('.participant-status')).not.toBeNull();
      fixtureState.mounted = await unmount(fixtureState.mounted);
      document.body.innerHTML = '';
    }
  });

  it('shows newest Board posts first, appends earlier pages below and reads only newer posts on a change', async () => {
    const { bridge, operation } = createBridge();
    const posts = (ids) =>
      ids.map((id) => ({
        id: `post-${id}`,
        sequence: id,
        text: `message-${id}`,
        sender_id: 'prt-a',
        created_at: '2026-09-08T09:00:00+00:00',
      }));
    // Posts after a number arrive oldest first, in bounded pages.
    const newer = { 4: [[5, 6], true], 6: [[7], false], 7: [[], false] };
    overrideOperations(operation, {
      'board.read': (args) => {
        if ('after' in args) {
          const [ids, hasMore] = newer[args.after];
          return { entries: posts(ids), has_more: hasMore };
        }
        return args.cursor
          ? { entries: posts([1, 2]), cursor: null, has_more: false }
          : { entries: posts([3, 4]), cursor: 'older-page', has_more: true };
      },
    });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelectorAll('.board li')).toHaveLength(2),
    );
    const messages = () =>
      [...document.querySelectorAll('.board li p')].map((el) =>
        el.textContent.trim(),
      );
    expect(messages()).toEqual(['message-4', 'message-3']);
    const more = t('swarm.board.more');
    button(more).click();
    await vi.waitFor(() =>
      expect(messages()).toEqual([
        'message-4',
        'message-3',
        'message-2',
        'message-1',
      ]),
    );
    expect(operation).toHaveBeenCalledWith('board.read', {
      swarm_id: swarm.id,
      discussion_id: swarm.main_discussion_id,
      limit: 50,
      cursor: 'older-page',
    });
    expect(button(more)).toBeUndefined();
    const reads = callsTo(operation, 'board.read').length;
    bridge.invalidate({ resource: 'posts', ids: [swarm.id], revision: 2 });
    await vi.waitFor(() =>
      expect(messages()).toEqual(
        [7, 6, 5, 4, 3, 2, 1].map((id) => `message-${id}`),
      ),
    );
    expect(callsTo(operation, 'board.read').slice(reads)).toEqual(
      [4, 6].map((after) => [
        'board.read',
        {
          swarm_id: swarm.id,
          discussion_id: swarm.main_discussion_id,
          after,
          limit: 50,
        },
      ]),
    );
    expect(button(more)).toBeUndefined();
  });

  it('links cited post and Wiki page numbers and reveals a cited post', async () => {
    const post = (sequence, discussionId, name, text, extra = {}) => ({
      id: `pst-${sequence}`,
      sequence,
      discussion_id: discussionId,
      author: { id: `prt-${name}`, kind: 'participant', name },
      text,
      ...extra,
    });
    const baseline = post(5, 'dsc-main', 'Beta', 'Baseline   measured.');
    const finding = post(3, 'dsc-findings', 'Beta', 'Findings detail');
    const citing = post(
      7,
      'dsc-main',
      'Alpha',
      'Agrees with #5 and w2 after #3; not #7, #99, w3, `#5` or abc#5.',
      { reply_to: 'pst-5', reply_sequence: 5 },
    );
    const { bridge, operation } = createBridge(undefined, {
      ...structuredClone(swarm),
      prompt: 'Investigate #5 and w2',
      goal_post_sequence: 0,
      newest_post_sequence: 12,
      newest_wiki_page_number: 2,
    });
    overrideOperations(operation, {
      'board.read': boardStore([finding, baseline, citing]),
      wiki: () => ({
        title: 'Benchmarks',
        content: 'Median latency by build.',
        deleted: false,
      }),
    });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelectorAll('.board > li')).toHaveLength(2),
    );
    const references = () =>
      [
        ...document.querySelectorAll(
          '.board > li[data-post-number="7"] a[data-swarm-reference]',
        ),
      ].map((link) => [link.textContent, link.getAttribute('href')]);
    // A post cites only earlier posts and existing pages, outside code.
    expect(references()).toEqual([
      ['#5', '#post/5'],
      ['w2', '#wiki/w2'],
      ['#3', '#post/3'],
    ]);
    expect(
      document.querySelector('.swarm-goal-post a[data-swarm-reference]'),
    ).toBeNull();

    const tooltipText = () =>
      document.querySelector('#app-tooltip')?.textContent ?? '';
    const link = (text) =>
      [...document.querySelectorAll('a[data-swarm-reference]')].find(
        (item) => item.textContent === text,
      );
    link('#5').focus();
    await vi.waitFor(() =>
      expect(tooltipText()).toContain('Baseline measured.'),
    );
    expect(tooltipText()).toContain('#5 · Beta');
    link('w2').focus();
    await vi.waitFor(() =>
      expect(tooltipText()).toContain('Median latency by build.'),
    );
    expect(tooltipText()).toContain('w2 · Benchmarks');
    expect(operation).toHaveBeenCalledWith('wiki', {
      swarm_id: swarm.id,
      action: 'read',
      page_id: 'w2',
      limit: 300,
    });
    // The loaded post described itself; only the page needed a read.
    expect(operation).not.toHaveBeenCalledWith(
      'board.read',
      expect.objectContaining({ message_id: '#5' }),
    );

    document
      .querySelector('.board a[href="#post/5"]:not([data-swarm-reference])')
      .click();
    await vi.waitFor(() =>
      expect(
        document.querySelector('.board > li[data-post-number="5"]').dataset
          .revealed,
      ).toBe(''),
    );

    const reads = callsTo(operation, 'board.read').length;
    link('#3').click();
    await vi.waitFor(() =>
      expect(document.activeElement.dataset.postNumber).toBe('3'),
    );
    expect(document.querySelector('#swarm-discussion').value).toBe(
      'dsc-findings',
    );
    // The cited post names its discussion; its surroundings come together.
    const findings = { discussion_id: 'dsc-findings', limit: 50 };
    expect(callsTo(operation, 'board.read').slice(reads)).toEqual(
      [
        { message_id: '#3' },
        { ...findings, before: 4 },
        { ...findings, after: 3 },
      ].map((bounds) => ['board.read', { swarm_id: swarm.id, ...bounds }]),
    );
    expect(callsTo(operation, 'link.open')).toHaveLength(0);
  });

  it('reveals a post beyond the loaded pages among its neighbours and returns to the newest posts', async () => {
    const posts = [];
    const add = (sequence, text = `message-${sequence}`) =>
      posts.push({
        id: `pst-${sequence}`,
        sequence,
        discussion_id: 'dsc-main',
        author: { id: 'prt-a', kind: 'participant', name: 'Alpha' },
        text,
      });
    for (let sequence = 1; sequence < 200; sequence++) add(sequence);
    add(200, 'Builds on #60.');
    const { bridge, operation } = createBridge(undefined, {
      ...structuredClone(swarm),
      goal_post_sequence: 0,
      newest_post_sequence: 200,
    });
    overrideOperations(operation, { 'board.read': boardStore(posts) });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(numbers()).toEqual(countDown(200, 151)));
    const reads = () => callsTo(operation, 'board.read');
    const read = (bounds) => [
      'board.read',
      { swarm_id: swarm.id, discussion_id: 'dsc-main', limit: 50, ...bounds },
    ];
    const before = reads().length;
    // Far from the view, as rendered posts would place it.
    const box = vi
      .spyOn(Element.prototype, 'getBoundingClientRect')
      .mockReturnValue({ top: 20 * window.innerHeight, height: 300 });
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    onTestFinished(() => {
      box.mockRestore();
      delete Element.prototype.scrollIntoView;
    });
    document.querySelector('.board a[data-swarm-reference]').click();
    await vi.waitFor(() =>
      expect(document.activeElement.dataset.postNumber).toBe('60'),
    );
    // A long way jumps: smooth scrolling would render every post on the way.
    expect(scroll).toHaveBeenCalledWith({ block: 'center', behavior: 'auto' });
    expect(scroll).not.toHaveBeenCalledWith(
      expect.objectContaining({ behavior: 'smooth' }),
    );
    expect(numbers()).toEqual(countDown(110, 11));
    expect(reads().slice(before)).toEqual([
      ['board.read', { swarm_id: swarm.id, message_id: '#60' }],
      read({ before: 61 }),
      read({ after: 60 }),
    ]);

    // New posts wait while the newest posts are not shown.
    add(201);
    const shown = reads().length;
    const gets = callsTo(operation, 'swarms.get').length;
    bridge.invalidate({ resource: 'posts', ids: [swarm.id], revision: 2 });
    await vi.waitFor(() =>
      expect(callsTo(operation, 'swarms.get').length).toBeGreaterThan(gets),
    );
    await settle();
    expect(reads()).toHaveLength(shown);

    button(t('swarm.board.newer')).click();
    await vi.waitFor(() => expect(numbers()[0]).toBe(160));
    expect(reads().at(-1)).toEqual(read({ after: 110 }));
    button(t('swarm.board.newest')).click();
    await vi.waitFor(() =>
      expect(document.activeElement.dataset.postNumber).toBe('201'),
    );
    expect(numbers()).toEqual(countDown(201, 152));
    expect(reads().at(-1)).toEqual(read({}));
    expect(button(t('swarm.board.newer'))).toBeUndefined();
    // A post rendered again links its citations again.
    expect(
      document.querySelector(
        '.board > li[data-post-number="200"] a[data-swarm-reference]',
      )?.textContent,
    ).toBe('#60');

    add(202);
    bridge.invalidate({ resource: 'posts', ids: [swarm.id], revision: 3 });
    await vi.waitFor(() => expect(numbers()[0]).toBe(202));
    expect(reads().at(-1)).toEqual(read({ after: 201 }));
  });
});

describe('Swarm Board discussions', () => {
  it('reads a newly selected discussion without posting and ignores a late previous read', async () => {
    const { bridge, operation } = createBridge();
    let finishMain;
    overrideOperations(operation, {
      'board.read': (args) =>
        args.discussion_id === 'dsc-main'
          ? new Promise((resolve) => {
              finishMain = resolve;
            })
          : {
              entries: [
                {
                  id: 'review-new',
                  sender_id: 'prt-a',
                  text: 'new-discussion-sentinel',
                },
              ],
            },
    });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(finishMain).toBeTypeOf('function'));
    selectDiscussion('dsc-findings');
    await vi.waitFor(() =>
      expect(board().textContent).toContain('new-discussion-sentinel'),
    );
    expect(operation).toHaveBeenCalledWith('board.read', {
      swarm_id: 'swr-a',
      discussion_id: 'dsc-findings',
      limit: 50,
    });
    finishMain({
      entries: [
        {
          id: 'review-old',
          sender_id: 'prt-a',
          text: 'old-discussion-sentinel',
        },
      ],
    });
    await settle();
    expect(board().textContent).toContain('new-discussion-sentinel');
    expect(board().textContent).not.toContain('old-discussion-sentinel');
    expect(callsTo(operation, 'board.post')).toHaveLength(0);
  });

  it('opens an announced discussion outside the loaded selector page and preserves ordinary JSON posts', async () => {
    const { bridge, operation } = createBridge();
    const title = '<img src=x onerror=alert(1)> topic-sentinel';
    const announcement = {
      discussion_id: 'dsc-unlisted',
      title,
      opening_post_id: 'pst-opening',
    };
    const ordinaryText = JSON.stringify(announcement);
    const author = (id, name) => ({ id, kind: 'participant', name });
    overrideOperations(operation, {
      'board.list': (args) =>
        args.cursor
          ? { entries: [{ id: 'dsc-unlisted', title }] }
          : { entries: [swarm.discussions[0]], cursor: 'more-discussions' },
      'board.read': (args) => ({
        entries:
          args.discussion_id === 'dsc-unlisted'
            ? [
                {
                  id: 'pst-opening',
                  author: author('prt-a', 'Alpha'),
                  text: 'opening-sentinel',
                },
              ]
            : [
                {
                  id: 'pst-announcement',
                  author: author('prt-a', 'Alpha'),
                  text: 'stored-body-sentinel',
                  discussion_announcement: announcement,
                },
                {
                  id: 'pst-ordinary',
                  author: author('prt-b', 'Beta'),
                  text: ordinaryText,
                },
              ],
      }),
    });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(
        document.querySelector('.discussion-announcement button'),
      ).not.toBeNull(),
    );
    expect(board().textContent).toContain(ordinaryText);
    expect(board().textContent).not.toContain('stored-body-sentinel');
    expect(board().querySelector('img')).toBeNull();
    const target = document.querySelector('.discussion-announcement button');
    expect(target.textContent.trim()).toBe(title);
    target.click();
    await vi.waitFor(() =>
      expect(board().textContent).toContain('opening-sentinel'),
    );
    expect(operation).toHaveBeenCalledWith('board.read', {
      swarm_id: 'swr-a',
      discussion_id: 'dsc-unlisted',
      limit: 50,
    });
    expect(
      [...document.querySelectorAll('select')].some(
        (select) => select.value === 'dsc-unlisted',
      ),
    ).toBe(true);
    const moreDiscussions = t('swarm.board.moreDiscussions');
    button(moreDiscussions).click();
    await vi.waitFor(() => expect(button(moreDiscussions)).toBeUndefined());
    expect(
      document.querySelectorAll('option[value="dsc-unlisted"]'),
    ).toHaveLength(1);
  });

  it('shows compact discussion members with live status and refreshes join/leave changes', async () => {
    const { bridge, operation } = createBridge();
    let participants = structuredClone(swarm.participants);
    participants[0].state = 'idle';
    participants[0].run_active = true;
    overrideOperations(operation, {
      'swarms.get': () => ({
        swarm: { ...swarm, participants: structuredClone(participants) },
      }),
    });
    await render(bridge);
    button('Investigate').click();
    const names = () =>
      [...document.querySelectorAll('.participant-chip__name')].map(
        (node) => node.textContent,
      );
    await vi.waitFor(() => expect(names()).toEqual(['Alpha', 'Beta']));
    // A chip names the participant, its state and pending messages; only a
    // participant that is not idle shows a state dot.
    const [alpha, beta] = document.querySelectorAll('.participant-chip');
    expect(alpha.getAttribute('aria-label')).toBe(
      'Alpha, Running, 0 pending messages',
    );
    expect(alpha.querySelector('.participant-status').dataset.state).toBe(
      'running',
    );
    expect(beta.querySelector('.participant-status')).toBeNull();
    // Its details card adds the Model, its provider path muted, and the last
    // error when one is known.
    const card = () => document.querySelector('#app-tooltip');
    const rows = () =>
      [...card().querySelectorAll('dt')].map((label) => [
        label.textContent,
        label.nextElementSibling.textContent,
      ]);
    alpha.focus();
    await vi.waitFor(() => expect(card()?.textContent).toContain('Alpha'));
    expect(card().querySelector('.app-tooltip__title').textContent).toBe(
      'Alpha',
    );
    expect(rows()).toEqual([
      ['Model', 'demo/model'],
      ['State', 'Running'],
      ['Pending', 'None'],
    ]);
    expect(card().querySelector('.app-tooltip__lead').textContent).toBe(
      'demo/',
    );
    participants[1].state = 'failed';
    participants[1].pending_count = 3;
    participants[1].last_error = 'Provider quota exhausted';
    bridge.invalidate();
    await vi.waitFor(() =>
      expect(beta.getAttribute('aria-label')).toBe(
        'Beta, Failed, 3 pending messages',
      ),
    );
    beta.focus();
    await vi.waitFor(() => expect(card()?.textContent).toContain('Beta'));
    expect(rows()).toEqual([
      ['Model', 'demo/fallback'],
      ['State', 'Failed'],
      ['Pending', '3 messages'],
      ['Last error', 'Provider quota exhausted'],
    ]);
    selectDiscussion('dsc-findings');
    await vi.waitFor(() => expect(names()).toEqual(['Beta']));
    participants[0].discussion_ids.push('dsc-findings');
    bridge.invalidate();
    await vi.waitFor(() => expect(names()).toEqual(['Alpha', 'Beta']));
    participants = participants.map((peer) => ({
      ...peer,
      discussion_ids: ['dsc-main'],
    }));
    bridge.invalidate();
    await vi.waitFor(() => expect(names()).toEqual([]));
    expect(document.querySelector('.participant-pane .muted')).not.toBeNull();
    selectDiscussion('dsc-main');
    await vi.waitFor(() => expect(names()).toEqual(['Alpha', 'Beta']));
  });
});

describe('Swarm Board composer', () => {
  it('retries a saved post with a lost reply using the same request id', async () => {
    const { bridge, operation } = createBridge();
    const saved = new Map();
    let loseReply = true;
    overrideOperations(operation, {
      'board.post': (args, fallback) => {
        if (!saved.has(args.request_id)) saved.set(args.request_id, fallback());
        if (loseReply) {
          loseReply = false;
          throw new Error('post-failed-sentinel');
        }
        return saved.get(args.request_id);
      },
    });
    await openSwarm(bridge);
    await vi.waitFor(() => expect(button(WRITE_POST)).toBeDefined());
    button(WRITE_POST).click();
    await tick();
    expect(
      document.querySelector('[role="dialog"] #swarm-post'),
    ).not.toBeNull();
    fill('swarm-post', 'retained-draft-sentinel');
    await tick();
    button(POST).click();
    await vi.waitFor(() =>
      expect(
        document.querySelector('[role="dialog"] [role="alert"]').textContent,
      ).toContain('post-failed-sentinel'),
    );
    expect(document.getElementById('swarm-post').value).toBe(
      'retained-draft-sentinel',
    );
    button(POST).click();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="dialog"]')).toBeNull(),
    );
    const attempts = callsTo(operation, 'board.post');
    expect(attempts).toHaveLength(2);
    expect(attempts[1]).toEqual(attempts[0]);
    expect(saved.size).toBe(1);
    // An intentional second post, even with identical text, is a new action.
    button(WRITE_POST).click();
    await tick();
    fill('swarm-post', 'retained-draft-sentinel');
    await tick();
    button(POST).click();
    await vi.waitFor(() =>
      expect(callsTo(operation, 'board.post')).toHaveLength(3),
    );
    expect(callsTo(operation, 'board.post')[2][1].request_id).not.toBe(
      attempts[0][1].request_id,
    );
    expect(saved.size).toBe(2);
  });

  it('posts a Board reply with explicit public recipients', async () => {
    const { bridge, operation } = createBridge();
    await openSwarm(bridge);
    await tick();
    flushSync();
    button(WRITE_POST).click();
    await tick();
    const textarea = document.querySelector('textarea');
    textarea.value = 'Finding';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    const options = document.querySelectorAll('.post-options input');
    options[0].value = 'pst-parent';
    options[0].dispatchEvent(new Event('input', { bubbles: true }));
    options[1].value = 'prt-a, prt-b';
    options[1].dispatchEvent(new Event('input', { bubbles: true }));
    await tick();
    button(POST).click();
    await tick();
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'board.post',
      expect.objectContaining({
        swarm_id: 'swr-a',
        discussion_id: 'dsc-main',
        text: 'Finding',
        reply_to: 'pst-parent',
        recipients: ['prt-a', 'prt-b'],
        request_id: expect.any(String),
      }),
    );
  });

  it.each(['swarm-post', 'swarm-reply', 'swarm-pings'])(
    'gives a changed %s draft a new request id after failure',
    async (field) => {
      const { bridge, operation } = createBridge();
      overrideOperations(operation, {
        'board.post': () => Promise.reject(new Error('post-failed-sentinel')),
      });
      await openSwarm(bridge);
      button(WRITE_POST).click();
      await tick();
      fill('swarm-post', 'initial-post');
      await tick();
      button(POST).click();
      await vi.waitFor(() =>
        expect(callsTo(operation, 'board.post')).toHaveLength(1),
      );
      await tick();
      fill(field, field === 'swarm-reply' ? '#1' : 'changed-draft');
      await tick();
      button(POST).click();
      await vi.waitFor(() =>
        expect(callsTo(operation, 'board.post')).toHaveLength(2),
      );
      const [first, second] = callsTo(operation, 'board.post').map(
        ([, args]) => args,
      );
      expect(second.request_id).not.toBe(first.request_id);
    },
  );
});
