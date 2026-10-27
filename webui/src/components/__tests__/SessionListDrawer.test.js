// @vitest-environment jsdom

import { describe, expect, it, vi } from 'vitest';

import {
  api,
  buttonByText,
  filterSwitch,
  flushSync,
  markersByRow,
  openFilterMenu,
  rowCount,
  session,
  sessionRowButton,
  setupSessionListDrawerSuite,
  waitForCondition,
} from './SessionListDrawer.support.js';
import { appendSessionInvalidation } from '../../lib/sessionInvalidation.js';
import { sessionActivityFromSnapshot } from '../../lib/sessionActivity.js';
import { t } from '../../lib/i18n.js';
import { TOOLTIP_SHOW_DELAY_MS } from '../../lib/tooltip.js';

describe('SessionListDrawer list', () => {
  const drawer = setupSessionListDrawerSuite();

  it('loads once on mount and reloads when the reload token bumps', async () => {
    const props = drawer.mount({ reloadToken: 0 });
    await waitForCondition(() => api.listSessions.mock.calls.length === 1);
    // The initial token value must not trigger a second load on its own.
    flushSync();
    expect(api.listSessions).toHaveBeenCalledTimes(1);

    // A sessions resource_changed (forwarded as a token bump) reloads the list
    // so a new or switched Session shows up without pressing Refresh.
    props.reloadToken += 1;
    flushSync();
    await waitForCondition(() => api.listSessions.mock.calls.length === 2);
    expect(api.listSessions.mock.calls.at(-1)[0]).toBe('alpha');
    expect(api.listSessions.mock.calls.at(-1)[1]).toMatchObject({ limit: 35 });
  });

  it('reloads once per burst of Session changes that name a listed Agent', async () => {
    let invalidationId = 0;
    const push = (props, scope) => {
      invalidationId += 1;
      props.invalidations = appendSessionInvalidation(
        props.invalidations,
        invalidationId,
        scope,
      );
      flushSync();
    };
    // A signal that predates the drawer is already reflected by its first load.
    const props = drawer.mount({
      invalidations: appendSessionInvalidation([], 0, {
        agent_id: 'alpha',
        session_id: 'old',
      }),
    });
    await waitForCondition(() => api.listSessions.mock.calls.length === 1);

    vi.useFakeTimers();
    // Another Agent's Session and a read acknowledgement leave this list as is.
    push(props, { agent_id: 'beta', session_id: 'b1' });
    push(props, {
      agent_id: 'alpha',
      session_id: 'session-1',
      read_run_id: 'run-1',
    });
    await vi.advanceTimersByTimeAsync(500);
    expect(api.listSessions).toHaveBeenCalledTimes(1);

    // A burst naming the listed Agent collapses into one reload.
    push(props, { agent_id: 'alpha', session_id: 'new' });
    push(props, {
      agent_id: 'alpha',
      session_id: 'session-1',
      run_id: 'run-2',
    });
    await vi.advanceTimersByTimeAsync(500);
    expect(api.listSessions).toHaveBeenCalledTimes(2);
    expect(api.listSessions.mock.calls.at(-1)[0]).toBe('alpha');
  });

  it('hides the permanent refresh action and offers Retry only after a load failure', async () => {
    api.listSessions
      .mockRejectedValueOnce(new Error('session list unavailable'))
      .mockResolvedValueOnce({
        sessions: [
          { id: 'session-1', created_at: '2026-05-09T00:00:00+00:00' },
        ],
      });
    drawer.mount();

    await waitForCondition(() => buttonByText(t('common.retry')) !== null);
    expect(buttonByText(t('common.refresh'))).toBeNull();

    buttonByText(t('common.retry')).click();
    flushSync();
    await waitForCondition(() => rowCount() === 1);
    expect(api.listSessions).toHaveBeenCalledTimes(2);
    // An untitled Session shows a neutral label instead of its raw id.
    expect(
      document.querySelector('.session-row__name').textContent.trim(),
    ).toBe(t('sessions.newSession'));
    expect(buttonByText(t('common.retry'))).toBeNull();
    expect(buttonByText(t('common.refresh'))).toBeNull();
  });

  it('identifies an unread Session unless it is already displayed', async () => {
    const unread = (id, runId) =>
      session(id, {
        has_unread_completion: true,
        unread_run_id: runId,
        unread_run_status: 'completed',
        unread_run_at: '2026-07-20T10:00:00+00:00',
      });
    api.listSessions.mockResolvedValue({
      sessions: [
        unread('session-1', 'run-one'),
        unread('session-2', 'run-two'),
      ],
    });
    const props = drawer.mount();

    await waitForCondition(
      () => document.querySelector('.session-row__unread') !== null,
    );
    const markers = document.querySelectorAll('.session-row__unread');
    expect(markers).toHaveLength(1);
    expect(markers[0].textContent.trim()).toBe('');
    expect(markers[0].getAttribute('aria-label')).toBe(
      t('sessions.unreadCompletion'),
    );
    expect(sessionRowButton('session-2').contains(markers[0])).toBe(true);
    // The status dot leads the title.
    expect(
      markers[0].compareDocumentPosition(
        sessionRowButton('session-2').querySelector('.session-row__name'),
      ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // Opening a draft leaves every persisted Session unselected and restores
    // the unread marker of the Session the area just left.
    props.currentSessionId = '';
    flushSync();
    expect(document.querySelector('.session-row__select--active')).toBeNull();
    expect(document.querySelectorAll('.session-row__unread')).toHaveLength(2);
    expect(api.listSessions).toHaveBeenCalledTimes(1);
  });

  it('shows a mounted row working, then waiting, then its unread result once idle without reloading', async () => {
    api.listSessions.mockResolvedValue({
      sessions: [
        session('session-1', {
          activity_state: 'working',
          work_started_at: '2026-08-31T12:00:00+00:00',
        }),
      ],
    });
    const props = drawer.mount({
      currentSessionId: 'another-session',
      liveActivity: [],
      sessionActivity: null,
    });
    await waitForCondition(() => rowCount() === 1);
    const marker = () => document.querySelector('.session-row__activity');
    const dot = () => marker()?.querySelector('.tab-indicator');
    // Before the first connection the row shows the activity it was listed with.
    expect(dot().classList.contains('tab-indicator--running')).toBe(true);
    expect(marker().getAttribute('aria-label')).toBe(
      t('chat.sessionActivity.working'),
    );

    // Its Run ended with a result, but Sub-Agents and a command will wake it:
    // an amber ring naming them, and no unread marker yet.
    props.liveActivity = [
      {
        agent_address: 'alpha',
        session_id: 'session-1',
        has_unread_completion: true,
        latest_completion_run_id: 'run-one',
        unread_run_id: 'run-one',
        unread_run_status: 'completed',
        unread_run_at: '2026-08-31T12:03:00+00:00',
      },
    ];
    props.sessionActivity = sessionActivityFromSnapshot([
      {
        project_id: null,
        agent_id: 'alpha',
        session_id: 'session-1',
        state: 'waiting',
        work_started_at: '2026-08-31T12:00:00+00:00',
        waiting_for: { subagents: 2, commands: 1, results: 0 },
      },
    ]);
    flushSync();
    expect(dot().classList.contains('tab-indicator--waiting')).toBe(true);
    expect(dot().classList.contains('tab-indicator--running')).toBe(false);
    expect(marker().getAttribute('aria-label')).toBe(
      'Waiting for 2 Sub-Agents, 1 command',
    );
    expect(document.querySelector('.session-row__unread')).toBeNull();
    vi.useFakeTimers({ now: Date.parse('2026-08-31T12:04:12+00:00') });
    marker().dispatchEvent(new Event('pointerenter'));
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    expect(document.getElementById('app-tooltip').textContent).toBe(
      'Waiting for 2 Sub-Agents, 1 command · 4m 12s',
    );
    vi.useRealTimers();

    props.sessionActivity = {};
    flushSync();
    expect(marker()).toBeNull();
    expect(document.querySelector('.session-row__unread')).not.toBeNull();
    expect(api.listSessions).toHaveBeenCalledTimes(1);
  });

  it('labels forked and Channel Sessions with icon-only markers', async () => {
    api.listSessions.mockResolvedValue({
      sessions: [
        session('plain-session'),
        session('fork-session', {
          created_at: '2026-05-09T12:00:00+00:00',
          fork_source: { agent_id: 'alpha', session_id: 'plain-session' },
        }),
        // Untitled Channel Sessions are named after their conversation, and
        // the newest is listed first.
        ...['telegram', 'discord', 'matrix'].map((platform, index) =>
          session(`${platform}-session`, {
            title: null,
            created_at: `2026-05-1${index}T00:00:00+00:00`,
            platform,
            platform_conv_id: `${platform}-chat`,
          }),
        ),
      ],
    });
    drawer.mount({
      initialFilters: { channels: true },
      currentSessionId: 'fork-session',
    });
    await waitForCondition(() => rowCount() === 5);

    expect(Object.entries(markersByRow())).toEqual([
      // Other platforms fall back to their capitalized name.
      ['matrix/matrix-chat', ['Matrix']],
      ['discord/discord-chat', [t('sessions.platform_discord')]],
      ['telegram/telegram-chat', [t('sessions.platform_telegram')]],
      ['fork-session', [t('sessions.fork')]],
      ['plain-session', []],
    ]);
    for (const marker of document.querySelectorAll('[data-session-marker]')) {
      expect(marker.textContent.trim()).toBe('');
      expect(marker.querySelector('svg')).not.toBeNull();
    }
  });

  it('moves secondary Session metadata into the row details card', async () => {
    api.listSessions.mockResolvedValue({
      sessions: [
        session('child-session-with-a-long-identifier', {
          title: 'Child session title',
          source_channel_id: 'telegram-main',
          last_active_at: '2026-05-09T01:00:00+00:00',
          is_subagent_session: true,
          subagent_parent: {
            agent_id: 'orchestrator',
            session_id: 'parent-session',
          },
        }),
        session('fork-copy', {
          title: 'Fork copy',
          fork_source: { agent_id: 'alpha', session_id: 'origin-session' },
          run_kinds: ['skill_reflection', 'cron'],
        }),
        session('origin-session', { title: 'Release planning' }),
      ],
    });
    drawer.mount({
      currentSessionId: 'child-session-with-a-long-identifier',
      initialFilters: { subagents: true, skillReflections: true, cron: true },
      agents: [{ address: 'orchestrator', name: 'Orchestrator' }],
    });
    await waitForCondition(() => rowCount() === 3);

    const lastActive = t('sessions.last_active');
    const created = t('sessions.details.created');
    const sourceChannel = t('sessions.source_channel');
    const parent = t('sessions.subagent_parent');
    const originId = t('sessions.details.originId');
    const forkedFrom = t('sessions.details.forkedFrom');
    const [childButton, forkButton] = document.querySelectorAll(
      '.session-row__select',
    );
    expect(childButton.textContent).toContain('Child session title');
    for (const label of [lastActive, sourceChannel, parent]) {
      expect(childButton.textContent).not.toContain(label);
    }

    vi.useFakeTimers();
    async function detailsOf(button) {
      button.focus();
      await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
      flushSync();
      const tooltipElement = document.getElementById('app-tooltip');
      return {
        tooltipElement,
        title: tooltipElement.querySelector('.app-tooltip__title').textContent,
        legend: [
          ...tooltipElement.querySelectorAll('.app-tooltip__legend-item'),
        ].map((item) => item.textContent),
        legendIcons: [
          ...tooltipElement.querySelectorAll('.app-tooltip__legend-item svg'),
        ],
        rows: Object.fromEntries(
          [...tooltipElement.querySelectorAll('dt')].map((term) => [
            term.textContent,
            term.nextElementSibling?.textContent,
          ]),
        ),
      };
    }

    const child = await detailsOf(childButton);
    expect(child.title).toBe('Child session title');
    // The row's marker gives way to its "..." button on hover, so the card
    // shows that icon with its meaning, even where an origin row already
    // says it is a Subagent.
    expect(child.legend).toEqual([t('chat.subagent.label')]);
    // An unlisted parent leads with its Agent's name, then its id.
    expect(child.rows).toMatchObject({
      [sourceChannel]: 'telegram-main',
      [parent]: t('sessions.details.originOfAgent', { agent: 'Orchestrator' }),
      [originId]: 'parent-session',
    });
    // Moments are absolute and relative; creation differs from activity.
    expect(child.rows[lastActive]).toContain(' · ');
    expect(child.rows[created]).toContain(' · ');
    // Beside the row, so the card never covers the neighbouring Sessions.
    expect(child.tooltipElement.dataset.floatingSide).toBe('right');

    // A listed fork source is named instead of identified.
    const fork = await detailsOf(forkButton);
    expect(fork.rows[forkedFrom]).toBe(
      t('sessions.details.originValue', {
        session: 'Release planning',
        agent: 'alpha',
      }),
    );
    expect(fork.rows).not.toHaveProperty(originId);
    expect(fork.rows).not.toHaveProperty(created);
    // Further kinds follow the marked one, except a fork its origin row names.
    expect(fork.legend).toEqual([
      t('sessions.runKind.skill_reflection'),
      t('sessions.runKind.cron'),
    ]);
    // The card draws the icon of the row's marker.
    const shapes = (svg) => [...svg.children].map((shape) => shape.outerHTML);
    expect(shapes(fork.legendIcons[0])).toEqual(
      shapes(
        forkButton
          .closest('.session-row')
          .querySelector('[data-session-marker] svg'),
      ),
    );
  });

  it('reveals labelled execution Sessions through the filters and selects them with their sub-agent flag', async () => {
    api.listSessions.mockResolvedValue({
      sessions: [
        session('user-session', { run_kinds: ['user'] }),
        // Scheduled and reflection Runs work in forks; a row shows only its
        // most specific kind.
        ...['cron', 'reflection', 'memory_reflection', 'skill_reflection'].map(
          (runKind) =>
            session(`${runKind}-session`, {
              run_kinds: [runKind],
              fork_source: { agent_id: 'alpha', session_id: 'user-session' },
            }),
        ),
        session('subagent-session', {
          is_subagent_session: true,
          subagent_parent: { agent_id: 'alpha', session_id: 'user-session' },
        }),
      ],
    });
    const onSessionSelected = vi.fn();
    // No current Session: the list shows only what the filters allow.
    drawer.mount({ currentSessionId: '', onSessionSelected });

    await waitForCondition(() => rowCount() === 1);
    expect(Object.keys(markersByRow())).toEqual(['user-session']);

    openFilterMenu();
    for (const filter of [
      'subagents',
      'memoryReflections',
      'skillReflections',
      'cron',
    ]) {
      filterSwitch(filter).click();
      flushSync();
    }
    await waitForCondition(() => rowCount() === 6);
    expect(markersByRow()).toEqual({
      'user-session': [],
      'cron-session': [t('sessions.runKind.cron')],
      'reflection-session': [t('sessions.runKind.reflection')],
      'memory_reflection-session': [t('sessions.runKind.memory_reflection')],
      'skill_reflection-session': [t('sessions.runKind.skill_reflection')],
      'subagent-session': [t('chat.subagent.label')],
    });

    sessionRowButton('subagent-session').click();
    flushSync();
    expect(onSessionSelected).toHaveBeenCalledWith(
      'subagent-session',
      'alpha',
      true,
    );
  });

  it('points to the filters when every listed Session is hidden by default', async () => {
    api.listSessions.mockResolvedValue({
      sessions: [
        session('subagent-session', {
          is_subagent_session: true,
          subagent_parent: { agent_id: 'alpha', session_id: 'user-session' },
        }),
      ],
    });
    drawer.mount({ currentSessionId: 'user-session' });

    await waitForCondition(() =>
      document.body.textContent.includes(t('sessions.noImportantTitle')),
    );
    expect(document.body.textContent).toContain(
      t('sessions.noImportantDescription'),
    );
    expect(rowCount()).toBe(0);
  });

  it('lists Sessions for every roster Agent in one bounded request', async () => {
    api.listSessions.mockImplementation(async (requested) => {
      const addresses = Array.isArray(requested) ? requested : [requested];
      return {
        sessions: addresses.map((address) =>
          session(`session-${address}`, {
            title: `Session ${address}`,
            agent_address: address,
          }),
        ),
        total_count: addresses.length,
        next_cursor: null,
      };
    });
    const onSessionSelected = vi.fn();
    drawer.mount({
      currentSessionId: 'session-alpha',
      agents: [
        { address: 'alpha', name: 'Alpha' },
        { address: 'beta', name: 'Beta' },
      ],
      onSessionSelected,
    });
    await waitForCondition(() => rowCount() === 1);
    expect(api.listSessions.mock.calls[0][0]).toBe('alpha');
    expect(document.body.textContent).not.toContain('Beta');
    // The header names whose Sessions the list shows.
    const title = () =>
      document.querySelector('.session-drawer__title').textContent;
    expect(title()).toBe('Alpha');

    const allAgents = document.querySelector(
      `button[aria-label="${t('sessions.filters.allAgents')}"]`,
    );
    expect(allAgents.getAttribute('aria-pressed')).toBe('false');
    allAgents.click();
    flushSync();

    await waitForCondition(() => rowCount() === 2);
    expect(api.listSessions.mock.calls[1][0]).toEqual(['alpha', 'beta']);
    expect(api.listSessions.mock.calls[1][1]).toMatchObject({ limit: 35 });
    expect(allAgents.getAttribute('aria-pressed')).toBe('true');
    expect(document.querySelector('.session-drawer__filter-count')).toBeNull();
    expect(title()).toBe(t('sessions.filters.allAgents'));

    // Merged rows carry their owning Agent's name, and selecting one passes
    // that Agent's address so ChatView can navigate across Agents.
    expect(
      [...document.querySelectorAll('.session-row__agent')]
        .map((agent) => agent.textContent.trim())
        .sort(),
    ).toEqual(['Alpha', 'Beta']);
    sessionRowButton('Session beta').click();
    flushSync();
    expect(onSessionSelected).toHaveBeenCalledWith(
      'session-beta',
      'beta',
      false,
    );
  });

  it('requests Channels only when enabled and persists the filter choice', async () => {
    const onFiltersChange = vi.fn();
    api.listSessions.mockImplementation(async (_agents, query) => ({
      sessions: [
        { id: 'ordinary', title: 'Ordinary session' },
        ...(query.includeChannels
          ? [
              {
                id: 'telegram',
                title: 'Telegram session',
                platform: 'telegram',
                platform_conv_id: '1',
              },
            ]
          : []),
      ],
    }));
    drawer.mount({ onFiltersChange });
    await waitForCondition(() => rowCount() === 1);
    expect(api.listSessions.mock.calls[0][1].includeChannels).toBe(false);

    openFilterMenu();
    // All agents is a header toggle, not one of the dropdown filters.
    expect(
      document.querySelector(
        `[role="switch"][aria-label="${t('sessions.filters.allAgents')}"]`,
      ),
    ).toBeNull();
    filterSwitch('channels').click();
    flushSync();
    await waitForCondition(() => rowCount() === 2);
    expect(api.listSessions.mock.calls.at(-1)[1].includeChannels).toBe(true);
    expect(onFiltersChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ channels: true }),
    );
    expect(
      document
        .querySelector('.session-drawer__filter-count')
        .textContent.trim(),
    ).toBe('1');

    filterSwitch('channels').click();
    flushSync();
    await waitForCondition(() => rowCount() === 1);
  });

  it('loads 35 Sessions initially and requests 20 more with the server cursor', async () => {
    const page = (start, length) =>
      Array.from({ length }, (_, index) =>
        session(`session-${start + index}`, {
          title: `Session ${start + index}`,
          agent_address: 'alpha',
        }),
      );
    const cursor = {
      active_sort: 2460000,
      agent_id: 'alpha',
      session_id: 'session-34',
    };
    api.listSessions
      .mockResolvedValueOnce({
        sessions: page(0, 35),
        total_count: 55,
        next_cursor: cursor,
      })
      .mockResolvedValueOnce({
        sessions: page(35, 20),
        total_count: 55,
        next_cursor: null,
      });
    drawer.mount({ currentSessionId: 'session-0' });

    await waitForCondition(() => rowCount() === 35);
    expect(
      document.querySelector('.session-drawer__more-hint').textContent,
    ).toContain(
      t('sessions.moreHint', {
        count: 20,
      }),
    );

    const list = document.querySelector('.session-drawer__list');
    Object.defineProperties(list, {
      scrollTop: { configurable: true, value: 900 },
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 1000 },
    });
    list.dispatchEvent(new Event('scroll'));
    flushSync();

    await waitForCondition(() => rowCount() === 55);
    expect(api.listSessions.mock.calls[1][0]).toBe('alpha');
    expect(api.listSessions.mock.calls[1][1]).toMatchObject({
      limit: 20,
      cursor,
    });
    expect(document.querySelector('.session-drawer__more-hint')).toBeNull();
  });

  it('loads an externally selected Session without losing pages and ignores an older selection', async () => {
    const cursor = {
      active_sort: 2460000,
      agent_id: 'alpha',
      session_id: 'session-1',
    };
    let releaseOlder;
    let olderReads = 0;
    api.listSessions.mockImplementation(async (_agent, query) => {
      if (query.cursor) {
        return {
          sessions: [session('page-two')],
          total_count: 3,
          next_cursor: null,
        };
      }
      if (query.requiredSession?.sessionId === 'older-selection') {
        olderReads += 1;
        if (olderReads > 1) {
          return {
            sessions: [session('older-selection', { title: 'New title' })],
          };
        }
        return await new Promise((resolve) => {
          releaseOlder = () =>
            resolve({ sessions: [session('older-selection')] });
        });
      }
      if (query.requiredSession?.sessionId === 'hidden-selection') {
        return {
          sessions: [
            session('hidden-selection', { is_subagent_session: true }),
          ],
        };
      }
      return {
        sessions: [session('session-1')],
        total_count: 3,
        next_cursor: cursor,
      };
    });
    const props = drawer.mount();
    await waitForCondition(() => rowCount() === 1);
    props.currentSessionId = 'older-selection';
    flushSync();
    await waitForCondition(() => typeof releaseOlder === 'function');
    expect(document.querySelector('.session-row__select--active')).toBeNull();

    props.currentSessionId = 'hidden-selection';
    flushSync();
    await waitForCondition(() => rowCount() === 2);
    expect(
      sessionRowButton('hidden-selection').classList.contains(
        'session-row__select--active',
      ),
    ).toBe(true);
    // Returning to the same Session starts a new read; the first answer must
    // not win merely because its address matches the current selection again.
    props.currentSessionId = 'older-selection';
    flushSync();
    await waitForCondition(() =>
      document.body.textContent.includes('New title'),
    );
    releaseOlder();
    await Promise.resolve();
    flushSync();
    expect(document.body.textContent).not.toContain('older-selection');
    expect(
      sessionRowButton('New title').classList.contains(
        'session-row__select--active',
      ),
    ).toBe(true);

    props.currentSessionId = 'hidden-selection';
    flushSync();

    const list = document.querySelector('.session-drawer__list');
    Object.defineProperties(list, {
      scrollTop: { configurable: true, value: 900 },
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 1000 },
    });
    list.dispatchEvent(new Event('scroll'));
    flushSync();
    await waitForCondition(() => rowCount() === 4);
    expect(api.listSessions.mock.calls.at(-1)[1]).toMatchObject({
      cursor,
      limit: 20,
    });
    expect(
      sessionRowButton('hidden-selection').classList.contains(
        'session-row__select--active',
      ),
    ).toBe(true);
  });
});

describe('SessionListDrawer by Project', () => {
  const drawer = setupSessionListDrawerSuite();

  const roster = [
    { address: 'alpha', name: 'Alpha' },
    { address: 'builder@vbot', name: 'Builder' },
  ];
  const projects = [
    { project_id: 'vbot', display_name: 'vBot' },
    { project_id: 'idle', display_name: 'Idle' },
  ];
  const projectTeams = [
    {
      projectId: 'vbot',
      members: [{ agent_id: 'builder', display_name: 'Builder' }],
    },
    {
      projectId: 'idle',
      members: [{ agent_id: 'scout', display_name: 'Scout' }],
    },
  ];
  const vbotCursor = {
    active_sort: 2460000,
    project_id: 'vbot',
    agent_id: 'builder',
    session_id: 'build-1',
  };

  const serveGroups = (totalCount = 3) =>
    api.listSessionProjectGroups.mockResolvedValue({
      groups: [
        {
          project_id: 'vbot',
          sessions: [
            session('plan-1', {
              title: 'Planning',
              agent_address: 'alpha',
              last_active_at: '2026-05-12T00:00:00+00:00',
            }),
            session('build-1', {
              title: 'Build',
              agent_address: 'builder@vbot',
              last_active_at: '2026-05-11T00:00:00+00:00',
            }),
          ],
          next_cursor: vbotCursor,
          total_count: totalCount,
        },
        { project_id: 'idle', sessions: [], next_cursor: null, total_count: 0 },
        {
          project_id: null,
          sessions: [
            session('chat-1', { title: 'Chat', agent_address: 'alpha' }),
          ],
          next_cursor: null,
          total_count: 1,
        },
      ],
    });

  const groupNames = () =>
    [...document.querySelectorAll('.session-group__name')].map((name) =>
      name.textContent.trim(),
    );
  const group = (name) =>
    [...document.querySelectorAll('.session-group')].find(
      (section) =>
        section.querySelector('.session-group__name').textContent.trim() ===
        name,
    );
  const groupRows = (name) =>
    [...group(name).querySelectorAll('.session-row__name')].map((row) =>
      row.textContent.trim(),
    );
  const newSessionButton = (name) =>
    group(name).querySelector('.session-group__new');
  const menuItems = () =>
    [...document.querySelectorAll('.context-menu [role="menuitem"]')].map(
      (item) => item.textContent.trim(),
    );

  it('groups every Agent’s Sessions by Project and continues a group with its cursor', async () => {
    serveGroups(8);
    const lastPageCursor = { ...vbotCursor, session_id: 'older-4' };
    api.listProjectSessions
      .mockResolvedValueOnce({
        sessions: Array.from({ length: 5 }, (_, index) =>
          session(`older-${index}`, { agent_address: 'builder@vbot' }),
        ),
        next_cursor: lastPageCursor,
        total_count: 8,
      })
      .mockResolvedValueOnce({
        sessions: [session('oldest', { agent_address: 'builder@vbot' })],
        next_cursor: null,
        total_count: 8,
      });
    const onFiltersChange = vi.fn();
    drawer.mount({
      currentSessionId: 'plan-1',
      agents: roster,
      projects,
      projectTeams,
      onFiltersChange,
    });
    await waitForCondition(() => api.listSessions.mock.calls.length === 1);

    const toggle = document.querySelector('.session-drawer__by-project');
    toggle.click();
    flushSync();
    await waitForCondition(() => groupNames().length === 3);

    expect(toggle.getAttribute('aria-pressed')).toBe('true');
    expect(onFiltersChange.mock.calls.at(-1)[0]).toMatchObject({
      byProject: true,
    });
    expect(api.listSessionProjectGroups).toHaveBeenCalledWith({
      limit: 5,
      includeSubagents: false,
      includeMemoryReflections: false,
      includeSkillReflections: false,
      includeCron: false,
      includeChannels: false,
    });
    expect(document.querySelector('.session-drawer__title').textContent).toBe(
      t('sessions.projects.title'),
    );
    // The per-Agent "All agents" switch does not apply here.
    expect(document.querySelector('.session-drawer__all-agents')).toBeNull();
    expect(groupNames()).toEqual([
      'vBot',
      'Idle',
      t('sessions.projects.other'),
    ]);
    expect(groupRows('vBot')).toEqual(['Planning', 'Build']);
    // The row names its Agent only in its details card.
    expect(document.querySelector('.session-group .session-row__agent')).toBe(
      null,
    );
    vi.useFakeTimers();
    sessionRowButton('Build').focus();
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    flushSync();
    const agentTerm = [
      ...document.getElementById('app-tooltip').querySelectorAll('dt'),
    ].find((term) => term.textContent === t('sessions.details.agent'));
    expect(agentTerm.nextElementSibling.textContent).toBe('Builder');
    expect(group('Idle').textContent).toContain(t('sessions.projects.empty'));
    expect(
      sessionRowButton('Planning').classList.contains(
        'session-row__select--active',
      ),
    ).toBe(true);

    buttonByText(
      t('sessions.projects.showMore', { count: 5, total: 8 }),
    ).click();
    flushSync();
    await waitForCondition(() => groupRows('vBot').length === 7);
    expect(api.listProjectSessions).toHaveBeenLastCalledWith(
      'vbot',
      expect.objectContaining({ limit: 5, cursor: vbotCursor }),
    );
    buttonByText(
      t('sessions.projects.showMore', { count: 1, total: 8 }),
    ).click();
    flushSync();
    await waitForCondition(() => groupRows('vBot').length === 8);
    expect(api.listProjectSessions).toHaveBeenLastCalledWith(
      'vbot',
      expect.objectContaining({ limit: 5, cursor: lastPageCursor }),
    );
    expect(group('vBot').querySelector('.session-group__more')).toBeNull();

    const vbotToggle = group('vBot').querySelector('.session-group__toggle');
    vbotToggle.click();
    flushSync();
    expect(vbotToggle.getAttribute('aria-expanded')).toBe('false');
    expect(groupRows('vBot')).toEqual([]);
    expect(onFiltersChange.mock.calls.at(-1)[0]).toMatchObject({
      collapsedProjects: ['vbot'],
    });
  });

  it('orders the groups by the Project order and moves a Project group by its header', async () => {
    serveGroups();
    const onReorderProjects = vi.fn(async () => {});
    const props = drawer.mount({
      agents: roster,
      projects: [projects[1], projects[0]],
      projectTeams,
      initialFilters: { byProject: true },
      onReorderProjects,
    });
    await waitForCondition(() => groupNames().length === 3);
    const pressMoveUp = (target) => {
      target.focus();
      target.dispatchEvent(
        new KeyboardEvent('keydown', {
          key: 'ArrowUp',
          altKey: true,
          bubbles: true,
          cancelable: true,
        }),
      );
      flushSync();
    };

    // The Project order wins over the server's group order.
    expect(groupNames()).toEqual([
      'Idle',
      'vBot',
      t('sessions.projects.other'),
    ]);

    // A Session row and the group of Sessions in no Project move nothing.
    pressMoveUp(sessionRowButton('Build'));
    pressMoveUp(
      group(t('sessions.projects.other')).querySelector(
        '.session-group__toggle',
      ),
    );
    expect(onReorderProjects).not.toHaveBeenCalled();

    pressMoveUp(group('vBot').querySelector('.session-group__toggle'));
    await waitForCondition(() => onReorderProjects.mock.calls.length === 1);
    expect(onReorderProjects).toHaveBeenCalledWith(['vbot', 'idle']);
    props.projects = projects;
    flushSync();
    expect(groupNames()).toEqual([
      'vBot',
      'Idle',
      t('sessions.projects.other'),
    ]);
  });

  it('starts a group’s new Session with the displayed Agent where it can work, else with a chosen one', async () => {
    serveGroups();
    const onNewSessionInProject = vi.fn();
    const props = drawer.mount({
      agents: roster,
      projects,
      projectTeams,
      initialFilters: { byProject: true },
      onNewSessionInProject,
    });
    await waitForCondition(() => groupNames().length === 3);

    // An Identity Agent works in every Project and in none.
    newSessionButton('Idle').click();
    newSessionButton(t('sessions.projects.other')).click();
    expect(onNewSessionInProject.mock.calls).toEqual([
      ['idle', 'alpha'],
      [null, 'alpha'],
    ]);
    expect(document.querySelector('.context-menu')).toBeNull();

    // A Team member works only in its own Project; elsewhere the user picks
    // an Identity Agent or one of that Project's Team.
    props.agentId = 'builder@vbot';
    flushSync();
    onNewSessionInProject.mockClear();
    newSessionButton('vBot').click();
    expect(onNewSessionInProject).toHaveBeenCalledWith('vbot', 'builder@vbot');

    const idleButton = newSessionButton('Idle');
    expect(idleButton.getAttribute('aria-haspopup')).toBe('menu');
    idleButton.click();
    flushSync();
    expect(menuItems()).toEqual(['Alpha', 'Scout']);
    [...document.querySelectorAll('.context-menu [role="menuitem"]')]
      .find((item) => item.textContent.trim() === 'Scout')
      .click();
    flushSync();
    expect(onNewSessionInProject).toHaveBeenLastCalledWith(
      'idle',
      'scout@idle',
    );

    newSessionButton(t('sessions.projects.other')).click();
    flushSync();
    expect(menuItems()).toEqual(['Alpha']);
  });
});
