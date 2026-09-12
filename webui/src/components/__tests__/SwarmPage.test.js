// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  tick,
  unmount,
  swarm,
  button,
  createBridge,
  fill,
  render,
  fixtureState,
} from './SwarmPage.support.js';

describe('SwarmPage', () => {
  it.each(['dsc-main', 'dsc-findings'])(
    'renders Board Markdown and opens links through the host in %s',
    async (discussionId) => {
      const { bridge, operation } = createBridge();
      const original = operation.getMockImplementation();
      operation.mockImplementation((name, args) =>
        name === 'board.read'
          ? Promise.resolve({
              entries: [
                {
                  id: `pst-${args.discussion_id}`,
                  author: { id: 'prt-a', kind: 'participant', name: 'Alpha' },
                  text: [
                    `## QA ${args.discussion_id}`,
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
              ],
            })
          : original(name, args),
      );
      await render(bridge);
      button('Investigate').click();
      await vi.waitFor(() =>
        expect(document.querySelector('.board')).not.toBeNull(),
      );
      if (discussionId !== swarm.main_discussion_id) {
        const dropdown = document.getElementById('swarm-discussion');
        dropdown.value = discussionId;
        dropdown.dispatchEvent(new Event('change', { bubbles: true }));
      }
      await vi.waitFor(() =>
        expect(document.querySelector('.board h2')?.textContent).toBe(
          `QA ${discussionId}`,
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
    },
  );

  it('copies the exact fenced code from a Board post', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    const code = 'const result = "<verified>";\n  console.log(result);\n';
    operation.mockImplementation((name, args) =>
      name === 'board.read'
        ? Promise.resolve({
            entries: [{ id: 'pst-code', text: '```js\n' + code + '```' }],
          })
        : original(name, args),
    );
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
    const original = operation.getMockImplementation();
    const html = '<img src=x onerror=alert(1)><script>alert(1)</script>';
    operation.mockImplementation((name, args) =>
      name === 'board.read'
        ? Promise.resolve({
            entries: [
              {
                id: 'pst-unsafe',
                text: `${html}\n\n[unsafe](javascript:alert(1))\n\n**Safe formatting**`,
              },
            ],
          })
        : original(name, args),
    );
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.board p strong')?.textContent).toBe(
        'Safe formatting',
      ),
    );
    const board = document.querySelector('.board');
    expect(board.textContent).toContain(html);
    expect(board.querySelector('img, script, [onerror], a')).toBeNull();
  });

  it('opens an announced discussion outside the loaded selector page and preserves ordinary JSON posts', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    const title = '<img src=x onerror=alert(1)> topic-sentinel';
    const announcement = {
      discussion_id: 'dsc-unlisted',
      title,
      opening_post_id: 'pst-opening',
    };
    const ordinaryText = JSON.stringify(announcement);
    operation.mockImplementation((name, args) => {
      if (name === 'board.list')
        return Promise.resolve(
          args.cursor
            ? { entries: [{ id: 'dsc-unlisted', title }] }
            : { entries: [swarm.discussions[0]], cursor: 'more-discussions' },
        );
      if (name === 'board.read')
        return Promise.resolve({
          entries:
            args.discussion_id === 'dsc-unlisted'
              ? [
                  {
                    id: 'pst-opening',
                    author: { id: 'prt-a', kind: 'participant', name: 'Alpha' },
                    text: 'opening-sentinel',
                  },
                ]
              : [
                  {
                    id: 'pst-announcement',
                    author: { id: 'prt-a', kind: 'participant', name: 'Alpha' },
                    text: 'stored-body-sentinel',
                    discussion_announcement: announcement,
                  },
                  {
                    id: 'pst-ordinary',
                    author: { id: 'prt-b', kind: 'participant', name: 'Beta' },
                    text: ordinaryText,
                  },
                ],
        });
      return original(name, args);
    });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(
        document.querySelector('.discussion-announcement button'),
      ).not.toBeNull(),
    );
    const board = document.querySelector('.board');
    expect(board.textContent).toContain(ordinaryText);
    expect(board.textContent).not.toContain('stored-body-sentinel');
    expect(board.querySelector('img')).toBeNull();
    const target = document.querySelector('.discussion-announcement button');
    expect(target.textContent.trim()).toBe(title);
    target.click();
    await vi.waitFor(() =>
      expect(document.querySelector('.board').textContent).toContain(
        'opening-sentinel',
      ),
    );
    expect(operation).toHaveBeenCalledWith('board.read', {
      swarm_id: 'swr-a',
      discussion_id: 'dsc-unlisted',
      limit: 100,
    });
    expect(
      [...document.querySelectorAll('select')].some(
        (select) => select.value === 'dsc-unlisted',
      ),
    ).toBe(true);
    button('Load more discussions').click();
    await vi.waitFor(() =>
      expect(button('Load more discussions')).toBeUndefined(),
    );
    expect(
      document.querySelectorAll('option[value="dsc-unlisted"]'),
    ).toHaveLength(1);
  });

  it('keeps participant avatars consistent across Board, Activity and remounts', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    const participants = [
      { ...swarm.participants[0], display_name: 'Participant 9' },
      { ...swarm.participants[1], display_name: 'Participant 10' },
    ];
    operation.mockImplementation((name, args) => {
      if (name === 'swarms.get')
        return Promise.resolve({ swarm: { ...swarm, participants } });
      if (name === 'board.read')
        return Promise.resolve({
          entries: participants.map((participant) => ({
            id: `post-${participant.id}`,
            author: {
              id: participant.id,
              name: participant.display_name,
              kind: 'participant',
            },
            text: `message-${participant.id}`,
            created_at: '2026-09-08T09:15:00+00:00',
          })),
        });
      return original(name, args);
    });
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
        const color = postAvatar.style.getPropertyValue('--participant-color');
        expect(color).toBe(
          rosterAvatar.style.getPropertyValue('--participant-color'),
        );
        if (pass) expect(color).toBe(colors.get(participant.id));
        colors.set(participant.id, color);
      }
      expect(new Set(colors.values()).size).toBe(2);
      button('Participant 9').click();
      await vi.waitFor(() => expect(bridge.readHistory).toHaveBeenCalled());
      const activityAvatar = document.querySelector(
        '.participants .participant-avatar',
      );
      expect(activityAvatar.style.getPropertyValue('--participant-color')).toBe(
        colors.get('prt-a'),
      );
      fixtureState.mounted = await unmount(fixtureState.mounted);
      document.body.innerHTML = '';
    }
  });

  it('renders complete prompt, timestamped author headers and participants above posts', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    const prompt = 'test-owned long prompt '.repeat(15) + '\nsecond line';
    operation.mockImplementation((name, args) => {
      if (name === 'swarms.get')
        return Promise.resolve({
          swarm: { ...structuredClone(swarm), prompt },
        });
      if (name === 'board.read')
        return Promise.resolve({
          entries: [
            {
              id: 'post-time',
              author: { name: 'Alpha' },
              text: 'test-owned board text',
              created_at: '2026-09-08T09:15:00+00:00',
            },
          ],
        });
      return original(name, args);
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
    expect(document.querySelector('.goal').textContent).toBe(prompt);
    expect(document.querySelector('.swarm-head').textContent).not.toContain(
      'swr-a',
    );
    expect(document.querySelector('.swarm-tabs .chip')).not.toBeNull();
    expect(button('Results')).toBeUndefined();
    expect(document.querySelector('.post-header strong').textContent).toBe(
      'Alpha',
    );
    expect(document.querySelector('.board time').dateTime).toBe(
      '2026-09-08T09:15:00+00:00',
    );
    expect(document.querySelector('.board time').textContent).toMatch(/11:15/);
    const roster = document.querySelector('.participant-pane');
    expect(
      roster.compareDocumentPosition(document.querySelector('.board')) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    button('Usage').click();
    await tick();
    expect(document.querySelector('.swarm-identity dd').textContent).toBe(
      'swr-a',
    );
  });

  it('keeps a failed post in the modal and closes only after successful submission', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    let fail = true;
    operation.mockImplementation((name, args) =>
      name === 'board.post' && fail
        ? Promise.reject(new Error('post-failed-sentinel'))
        : original(name, args),
    );
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(button('Write post')).toBeDefined());
    button('Write post').click();
    await tick();
    expect(
      document.querySelector('[role="dialog"] #swarm-post'),
    ).not.toBeNull();
    fill('swarm-post', 'retained-draft-sentinel');
    await tick();
    button('Post').click();
    await vi.waitFor(() =>
      expect(
        document.querySelector('[role="dialog"] [role="alert"]').textContent,
      ).toContain('post-failed-sentinel'),
    );
    expect(document.getElementById('swarm-post').value).toBe(
      'retained-draft-sentinel',
    );
    fail = false;
    button('Post').click();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="dialog"]')).toBeNull(),
    );
  });

  it.each(['failed', 'cancelled', 'interrupted'])(
    'shows canonical context and resumes only the selected %s participant',
    async (state) => {
      const { bridge, operation } = createBridge();
      const original = operation.getMockImplementation();
      operation.mockImplementation((name, args) =>
        name === 'swarms.get'
          ? Promise.resolve({
              swarm: {
                ...structuredClone(swarm),
                participants: swarm.participants.map((peer) => ({
                  ...peer,
                  state: peer.id === 'prt-a' ? 'running' : state,
                  run_active: peer.id === 'prt-a',
                })),
              },
            })
          : original(name, args),
      );
      bridge.readHistory.mockImplementation((_swarm, participant) =>
        Promise.resolve({
          messages: [],
          context_usage: {
            tokens: participant === 'prt-a' ? 120 : 850,
            estimated: participant !== 'prt-a',
          },
          session_usage: { input_tokens: 99000 },
        }),
      );
      await render(bridge);
      button('Investigate').click();
      await vi.waitFor(() => expect(button('Beta')).toBeDefined());
      button('Beta').click();
      await vi.waitFor(() =>
        expect(button('Resume participant')).toBeDefined(),
      );
      expect(document.querySelector('.context-usage').textContent).toContain(
        '~850',
      );
      button('Resume participant').click();
      await vi.waitFor(() =>
        expect(operation).toHaveBeenCalledWith(
          'swarms.resume',
          expect.objectContaining({
            swarm_id: 'swr-a',
            participant_id: 'prt-b',
          }),
        ),
      );
      button('Alpha').click();
      await vi.waitFor(() =>
        expect(document.querySelector('.context-usage').textContent).toContain(
          '120 / 128,000',
        ),
      );
      expect(button('Resume participant')).toBeUndefined();
    },
  );

  it('posts a Board reply with explicit public recipients', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await tick();
    await tick();
    flushSync();
    button('Write post').click();
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
    button('Post').click();
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

  it('changes Board discussion without treating a human read as a mutation', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await tick();
    await tick();
    await new Promise((resolve) => setTimeout(resolve));
    const discussion = document.querySelector('#swarm-discussion');
    discussion.value = 'dsc-findings';
    discussion.dispatchEvent(new Event('change', { bubbles: true }));
    await tick();
    expect(operation).toHaveBeenCalledWith('board.read', {
      swarm_id: 'swr-a',
      discussion_id: 'dsc-findings',
      limit: 100,
    });
    expect(operation).not.toHaveBeenCalledWith('board.post', expect.anything());
  });

  it('shows newest Board posts first and appends earlier pages below them', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    const posts = (ids) =>
      ids.map((id) => ({
        id: `post-${id}`,
        text: `message-${id}`,
        sender_id: 'prt-a',
        created_at: '2026-09-08T09:00:00+00:00',
      }));
    operation.mockImplementation((name, args) =>
      name === 'board.read'
        ? Promise.resolve(
            args.cursor
              ? { entries: posts([1, 2]), cursor: null, has_more: false }
              : {
                  entries: posts([3, 4]),
                  cursor: 'older-page',
                  has_more: true,
                },
          )
        : original(name, args),
    );
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
    button('Load earlier messages').click();
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
      limit: 100,
      cursor: 'older-page',
    });
    expect(button('Load earlier messages')).toBeUndefined();
  });
});
