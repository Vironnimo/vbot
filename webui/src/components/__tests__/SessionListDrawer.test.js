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
    drawer.mount();

    await waitForCondition(
      () => document.querySelector('.session-row__unread-dot') !== null,
    );
    const markers = document.querySelectorAll('.session-row__unread');
    expect(markers).toHaveLength(1);
    expect(markers[0].textContent.trim()).toBe('');
    expect(markers[0].getAttribute('aria-label')).toBe(
      t('sessions.unreadCompletion'),
    );
    expect(sessionRowButton('session-2').contains(markers[0])).toBe(true);
  });

  it('updates a mounted row from live running to unread activity without reloading', async () => {
    api.listSessions.mockResolvedValue({
      sessions: [
        session('session-1', {
          has_active_run: false,
          has_unread_completion: false,
        }),
      ],
    });
    const props = drawer.mount({
      currentSessionId: 'another-session',
      liveActivity: [],
    });
    await waitForCondition(() => rowCount() === 1);
    expect(document.querySelector('.session-row__active-dot')).toBeNull();

    const activity = (fields) => [
      { agent_address: 'alpha', session_id: 'session-1', ...fields },
    ];
    props.liveActivity = activity({
      has_active_run: true,
      has_unread_completion: false,
    });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row__active-dot') !== null,
    );

    props.liveActivity = activity({
      has_active_run: false,
      has_unread_completion: true,
      latest_completion_run_id: 'run-one',
      unread_run_id: 'run-one',
      unread_run_status: 'completed',
      unread_run_at: '2026-08-31T12:00:00+00:00',
    });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.session-row__unread-dot') !== null,
    );
    expect(document.querySelector('.session-row__active-dot')).toBeNull();
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
        }),
        session('origin-session', { title: 'Release planning' }),
      ],
    });
    drawer.mount({
      currentSessionId: 'child-session-with-a-long-identifier',
      initialFilters: { subagents: true },
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
  });

  it('reveals labelled execution Sessions through the filters and selects them with their sub-agent flag', async () => {
    api.listSessions.mockResolvedValue({
      sessions: [
        session('user-session', { run_kinds: ['user'] }),
        ...['cron', 'reflection', 'memory_reflection', 'skill_reflection'].map(
          (runKind) => session(`${runKind}-session`, { run_kinds: [runKind] }),
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
});
