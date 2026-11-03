// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../../lib/i18n.js';
import { TOOLTIP_SHOW_DELAY_MS } from '../../../lib/tooltip.js';
import { agentActivityState } from '../agentActivityTooltip.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatHeader } = await import('../ChatHeader.svelte');

// Roster order as set in the Agents tab; the bar shows the tabs that fit.
const AGENTS = [
  { id: 'alpha', name: 'Alpha', model: 'openai/gpt-5.2::api-key:work' },
  {
    id: 'beta',
    name: 'Beta',
    model: 'anthropic/claude-sonnet-4',
    thinking_effort: 'high',
  },
  { id: 'gamma', name: 'Gamma' },
  { id: 'delta', name: 'Delta' },
  { id: 'epsilon', name: 'Epsilon' },
  { id: 'zeta', name: 'Zeta' },
  { id: 'eta', name: 'Eta' },
];

// Beta runs and Epsilon waits for work, both since 12:00; Eta has the newest
// unread result, then Delta, then Gamma; Alpha (selected) and Zeta are idle.
const WORK_STARTED_AT = '2026-08-31T12:00:00+00:00';
const ACTIVITY = {
  alpha: { status: 'idle', unreadCount: 0, latestUnreadAt: 0 },
  beta: {
    status: 'running',
    unreadCount: 0,
    latestUnreadAt: 0,
    work: { state: 'working', workStartedAt: WORK_STARTED_AT },
  },
  gamma: { status: 'unread', unreadCount: 2, latestUnreadAt: 1_000 },
  delta: { status: 'unread', unreadCount: 1, latestUnreadAt: 5_000 },
  epsilon: {
    status: 'waiting',
    unreadCount: 0,
    latestUnreadAt: 0,
    work: {
      state: 'waiting',
      workStartedAt: WORK_STARTED_AT,
      waitingFor: { subagents: 2, commands: 1, results: 0 },
    },
  },
  zeta: { status: 'idle', unreadCount: 0, latestUnreadAt: 0 },
  eta: { status: 'unread', unreadCount: 1, latestUnreadAt: 9_000 },
};

const running = (name) => t('chat.agentActivity.running', { name });
const waiting = (name) => t('chat.agentActivity.waiting', { name });
const idle = (name) => t('chat.agentActivity.idle', { name });
const unread = (name, count) =>
  count === 1
    ? t('chat.agentActivity.unreadOne', { name })
    : t('chat.agentActivity.unreadCount', { name, count });

describe('ChatHeader', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    localStorage.clear();
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  function mountHeader(props = {}) {
    mountedComponent = mount(ChatHeader, {
      target: document.body,
      props: {
        agents: AGENTS,
        agentActivity: ACTIVITY,
        selectedAddress: 'alpha',
        ...props,
      },
    });
    flushSync();
    return mountedComponent;
  }

  // jsdom lays nothing out, so every tab fits; this makes the tabs from
  // index `fitting` on report a second line, as on a narrow bar.
  async function mountOverflowing(props = {}, fitting = 5) {
    vi.spyOn(HTMLElement.prototype, 'offsetTop', 'get').mockImplementation(
      function () {
        const roster = this.parentElement;
        if (!roster?.classList.contains('chat-header__roster')) return 0;
        return Array.prototype.indexOf.call(roster.children, this) >= fitting
          ? 38
          : 0;
      },
    );
    mountHeader(props);
    await vi.waitFor(() => {
      expect(document.querySelector('.agent-pill--wrapped')).not.toBeNull();
    });
    flushSync();
  }

  // The tabs shown on the bar's line, without those wrapped out of sight.
  function pills() {
    return Array.from(
      document.querySelectorAll('button.agent-pill:not(.agent-pill--wrapped)'),
    );
  }

  function pillLabels() {
    return pills().map((pill) => pill.getAttribute('aria-label'));
  }

  function moreAgentsTrigger() {
    return document.querySelector(
      '.chat-header__more-agents button[aria-haspopup]',
    );
  }

  async function openMoreAgents() {
    moreAgentsTrigger().click();
    await vi.waitFor(() => {
      expect(document.activeElement?.getAttribute('role')).toBe('combobox');
    });
    return document.activeElement;
  }

  function key(target, name) {
    target.dispatchEvent(
      new KeyboardEvent('keydown', { key: name, bubbles: true }),
    );
    flushSync();
  }

  function tooltipDetails() {
    const tooltipElement = document.getElementById('app-tooltip');
    return {
      title: tooltipElement.querySelector('.app-tooltip__title')?.textContent,
      rows: Array.from(tooltipElement.querySelectorAll('dt'), (term) => [
        term.textContent,
        term.nextElementSibling.textContent,
      ]),
    };
  }

  it('marks an Agent waiting for the user’s approval above its other activity', () => {
    mountHeader({
      agentActivity: {
        ...ACTIVITY,
        beta: { ...ACTIVITY.beta, status: 'approval' },
      },
    });
    const beta = pills()[1];
    expect(
      beta.firstElementChild.classList.contains('tab-indicator--approval'),
    ).toBe(true);
    expect(beta.getAttribute('aria-label')).toBe(
      t('chat.agentActivity.approval', { name: 'Beta' }),
    );
  });

  it('shows the Agents that fit in roster order with any activity dot before the name', async () => {
    const onSelectAgent = vi.fn();
    await mountOverflowing({ onSelectAgent });

    expect(pillLabels()).toEqual([
      idle('Alpha'),
      running('Beta'),
      unread('Gamma', 2),
      unread('Delta', 1),
      waiting('Epsilon'),
    ]);
    expect(
      pills().map((pill) => [
        // The status dot leads the name; an idle Agent has none.
        pill.firstElementChild.className.match(/tab-indicator--\w+/)?.[0],
        pill.textContent.trim(),
        pill.getAttribute('aria-pressed'),
      ]),
    ).toEqual([
      [undefined, 'Alpha', 'true'],
      ['tab-indicator--running', 'Beta', 'false'],
      ['tab-indicator--unread', 'Gamma', 'false'],
      ['tab-indicator--unread', 'Delta', 'false'],
      ['tab-indicator--waiting', 'Epsilon', 'false'],
    ]);

    // The displayed Agent stays selectable: it lands on its unread Session.
    pills()[2].click();
    pills()[0].click();
    expect(onSelectAgent.mock.calls).toEqual([['gamma'], ['alpha']]);

    // The tooltip names the work and its span when it shows.
    vi.useFakeTimers({ now: Date.parse('2026-08-31T12:04:12+00:00') });
    pills()[1].dispatchEvent(new Event('pointerenter'));
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    expect(tooltipDetails()).toEqual({
      title: 'Beta',
      rows: [
        [t('chat.agentActivity.status'), 'Running · 4m 12s'],
        [t('chat.agentActivity.model'), 'anthropic/claude-sonnet-4'],
        [t('chat.agentActivity.thinkingEffort'), 'high'],
      ],
    });
    pills()[1].dispatchEvent(new Event('pointerleave'));
    pills()[4].dispatchEvent(new Event('pointerenter'));
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    expect(tooltipDetails().rows[0]).toEqual([
      t('chat.agentActivity.status'),
      'Waiting for 2 Sub-Agents, 1 command · 4m 12s',
    ]);
  });

  it('keeps a displayed Agent that does not fit on the bar', async () => {
    await mountOverflowing({ selectedAddress: 'zeta' });

    expect(pillLabels()).toHaveLength(6);
    const zeta = pills()[5];
    expect(zeta.classList.contains('agent-pill--extra')).toBe(true);
    expect(zeta.getAttribute('aria-pressed')).toBe('true');
    expect(zeta.getAttribute('aria-label')).toBe(idle('Zeta'));
  });

  it('names a displayed Agent the roster does not list', () => {
    mountHeader({ selectedAddress: 'librarian', displayedAgentName: 'Lib' });

    expect(
      document.querySelector('.agent-pill--current')?.textContent.trim(),
    ).toBe('Lib');
    expect(
      pills().filter((pill) => pill.getAttribute('aria-pressed') === 'true'),
    ).toEqual([]);
  });

  it('lists only the Agents off the bar under More agents, counting them and marking their activity', async () => {
    await mountOverflowing({
      agentActivity: {
        ...ACTIVITY,
        zeta: { status: 'unread', unreadCount: 3, latestUnreadAt: 2_000 },
      },
    });

    const trigger = moreAgentsTrigger();
    // Only Eta and Zeta are off the bar.
    expect(trigger.textContent.trim()).toBe('2');
    expect(trigger.querySelector('.tab-indicator--unread')).toBeTruthy();
    expect(trigger.getAttribute('aria-label')).toBe(
      t('chat.agentBar.moreAgentsActivity', {
        label: t('chat.agentBar.moreAgentsCount', { count: 2 }),
        activity: agentActivityState('unread', 4),
      }),
    );

    await openMoreAgents();
    const options = Array.from(document.querySelectorAll('[role="option"]'));
    // Unread by newest result first.
    expect(options.map((option) => option.getAttribute('aria-label'))).toEqual([
      unread('Eta', 1),
      unread('Zeta', 3),
    ]);
    expect(
      options.map(
        (option) => option.querySelector('.count-badge')?.textContent,
      ),
    ).toEqual(['1', '3']);
  });

  it('shows every Agent on a bar wide enough and offers no More agents list', () => {
    mountHeader();

    expect(pills()).toHaveLength(AGENTS.length);
    expect(moreAgentsTrigger()).toBeNull();
  });

  it('filters More agents by typing and returns focus on Escape', async () => {
    const onSelectAgent = vi.fn();
    await mountOverflowing({ onSelectAgent });

    let input = await openMoreAgents();
    expect(input.getAttribute('aria-label')).toBe(t('chat.agentPicker.filter'));
    input.value = 'ze';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(
      Array.from(document.querySelectorAll('[role="option"]'), (option) =>
        option.textContent.trim(),
      ),
    ).toEqual(['Zeta']);
    key(input, 'Enter');
    expect(onSelectAgent).toHaveBeenCalledWith('zeta');
    expect(document.activeElement).toBe(moreAgentsTrigger());

    input = await openMoreAgents();
    key(input, 'Escape');
    expect(document.querySelector('[role="listbox"]')).toBeNull();
    expect(document.activeElement).toBe(moreAgentsTrigger());
    expect(onSelectAgent).toHaveBeenCalledTimes(1);
  });

  it('toggles the Session list, starts a Session while the list is closed and ends with the area actions and the Session info toggle', async () => {
    const onToggleSessionList = vi.fn();
    const onNewSession = vi.fn();
    const onToggleInfoPanel = vi.fn();
    mountHeader({
      onToggleSessionList,
      onNewSession,
      infoPanelId: 'info',
      infoToggleId: 'info-toggle',
      infoRunningCount: 2,
      onToggleInfoPanel,
      actions: createRawSnippet(() => ({
        render: () =>
          '<button type="button" class="area-action">Split</button>',
      })),
    });
    const button = (label) =>
      document.querySelector(`.chat-header button[aria-label="${label}"]`);

    const toggle = button(t('chat.agentBar.sessionList'));
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    toggle.click();
    expect(button(t('chat.newSession')).getAttribute('aria-keyshortcuts')).toBe(
      'Control+Shift+O',
    );
    button(t('chat.newSession')).click();
    expect(onToggleSessionList).toHaveBeenCalledTimes(1);
    expect(onNewSession).toHaveBeenCalledTimes(1);
    const areaActions = document.querySelector('.chat-header').lastElementChild;
    expect(areaActions.firstElementChild.textContent).toBe('Split');
    // Closed, the toggle names the running work and marks it.
    const info = areaActions.lastElementChild;
    expect(info.id).toBe('info-toggle');
    expect(info.getAttribute('aria-label')).toBe(
      t('chat.activity.openManyRunning', { count: 2 }),
    );
    expect(info.getAttribute('aria-expanded')).toBe('false');
    expect(info.querySelector('.chat-header__info-running')).not.toBeNull();
    info.click();
    expect(onToggleInfoPanel).toHaveBeenCalledTimes(1);

    // The open Session list offers New session itself.
    await unmount(mountedComponent);
    mountHeader({ sessionListOpen: true });
    expect(
      button(t('chat.agentBar.sessionList')).getAttribute('aria-expanded'),
    ).toBe('true');
    expect(button(t('chat.newSession'))).toBeNull();
  });

  describe('Project Teams', () => {
    const PROJECT_GROUPS = [
      {
        projectId: 'web',
        name: 'Website',
        warning: true,
        members: [
          { agent_id: 'builder', display_name: 'Builder' },
          { agent_id: 'reviewer', display_name: 'Reviewer' },
        ],
      },
      {
        projectId: 'ops',
        name: 'Infra',
        warning: false,
        members: [{ agent_id: 'deployer', display_name: 'Deployer' }],
      },
      { projectId: 'empty', name: 'Empty', warning: false, members: [] },
    ];
    const SMALL_ROSTER = AGENTS.slice(0, 2);
    const projectAgent = (agent, project) =>
      t('chat.agentPicker.projectAgent', { agent, project });

    function mountWithProjects(props = {}) {
      return mountHeader({
        agents: SMALL_ROSTER,
        agentActivity: {},
        projectGroups: PROJECT_GROUPS,
        ...props,
      });
    }

    async function openTree() {
      expect(moreAgentsTrigger().getAttribute('aria-haspopup')).toBe('tree');
      return openMoreAgents();
    }

    function rows() {
      return Array.from(
        document.querySelectorAll('[role="tree"] [role="treeitem"]'),
        (row) => ({
          name: row.querySelector('.searchable-dropdown__option-label')
            .textContent,
          level: row.getAttribute('aria-level'),
          expanded: row.getAttribute('aria-expanded'),
        }),
      );
    }

    function activeRowName() {
      return document
        .querySelector('[role="treeitem"].active')
        ?.querySelector('.searchable-dropdown__option-label').textContent;
    }

    it('lists each Project with a Team as a group after the Identity Agents off the bar', async () => {
      const onSelectAgent = vi.fn();
      mountWithProjects({
        selectedAddress: 'reviewer@web',
        agentActivity: {
          'deployer@ops': { status: 'unread', unreadCount: 2 },
        },
        onSelectAgent,
      });
      // The displayed Project Agent joins the bar by its full name.
      expect(pills().map((pill) => pill.textContent.trim())).toEqual([
        'Alpha',
        'Beta',
        projectAgent('Reviewer', 'Website'),
      ]);

      await openTree();

      // Alpha and Beta are on the bar. The selected Agent's Project and a
      // Project with activity are open; the Project without members is left
      // out.
      expect(rows()).toEqual([
        { name: 'Website', level: '1', expanded: 'true' },
        { name: 'Builder', level: '2', expanded: null },
        { name: 'Reviewer', level: '2', expanded: null },
        { name: 'Infra', level: '1', expanded: 'true' },
        { name: 'Deployer', level: '2', expanded: null },
      ]);
      expect(activeRowName()).toBe('Reviewer');
      expect(
        document
          .querySelector('.searchable-dropdown__group-warning')
          .getAttribute('aria-label'),
      ).toBe(t('chat.agentPicker.projectScanWarning'));

      document.querySelectorAll('[role="treeitem"][aria-level="2"]')[0].click();
      expect(onSelectAgent).toHaveBeenCalledWith('builder@web');
    });

    it.each([
      ['running', 'chat.agentActivity.stateRunning'],
      ['waiting', 'chat.agentActivity.stateWaiting'],
    ])(
      'summarizes a closed group %s and marks More agents with its activity',
      async (status, stateKey) => {
        // This browser closed Infra earlier.
        localStorage.setItem(
          'vbot.chat.agentPicker.expandedProjects',
          JSON.stringify({ ops: false }),
        );
        mountWithProjects({
          selectedAddress: 'alpha',
          agentActivity: {
            'deployer@ops': { status, unreadCount: 1 },
          },
        });
        const activity = `${t(stateKey)} · ${t('chat.agentActivity.stateUnreadOne')}`;

        expect(
          moreAgentsTrigger().querySelector(`.tab-indicator--${status}`),
        ).toBeTruthy();
        // Every Identity Agent fits, so the trigger counts none.
        expect(moreAgentsTrigger().getAttribute('aria-label')).toBe(
          t('chat.agentBar.moreAgentsActivity', {
            label: t('chat.agentBar.moreAgents'),
            activity,
          }),
        );
        await openTree();

        const infra = Array.from(
          document.querySelectorAll('[role="treeitem"][aria-level="1"]'),
        ).find((row) => row.textContent.includes('Infra'));
        expect(infra.getAttribute('aria-expanded')).toBe('false');
        expect(infra.querySelector(`.tab-indicator--${status}`)).toBeTruthy();
        expect(infra.querySelector('.count-badge')?.textContent).toBe('1');
        expect(infra.getAttribute('aria-label')).toBe(
          t('chat.agentPicker.projectActivity', { project: 'Infra', activity }),
        );
      },
    );

    it('opens and closes groups with tree keys and remembers the choice', async () => {
      const onSelectAgent = vi.fn();
      // Only Alpha fits on the bar, so Beta leads the list.
      await mountOverflowing(
        {
          agents: SMALL_ROSTER,
          agentActivity: {},
          projectGroups: PROJECT_GROUPS,
          selectedAddress: 'alpha',
          onSelectAgent,
        },
        1,
      );
      const input = await openTree();
      expect(rows().map((row) => row.name)).toEqual([
        'Beta',
        'Website',
        'Infra',
      ]);
      expect(activeRowName()).toBe('Beta');

      key(input, 'ArrowDown');
      expect(activeRowName()).toBe('Website');
      key(input, 'ArrowRight');
      expect(rows().map((row) => row.name)).toEqual([
        'Beta',
        'Website',
        'Builder',
        'Reviewer',
        'Infra',
      ]);
      key(input, 'ArrowRight');
      expect(activeRowName()).toBe('Builder');
      key(input, 'ArrowLeft');
      expect(activeRowName()).toBe('Website');
      key(input, 'ArrowLeft');
      expect(rows().map((row) => row.name)).toEqual([
        'Beta',
        'Website',
        'Infra',
      ]);
      key(input, 'ArrowDown');
      key(input, 'Enter');
      expect(activeRowName()).toBe('Infra');
      key(input, 'ArrowDown');
      expect(activeRowName()).toBe('Deployer');
      key(input, 'Enter');
      expect(onSelectAgent).toHaveBeenCalledWith('deployer@ops');

      expect(
        JSON.parse(
          localStorage.getItem('vbot.chat.agentPicker.expandedProjects'),
        ),
      ).toEqual({ web: false, ops: true });
    });

    it('finds Project Agents by their name or their Project name', async () => {
      mountWithProjects({ selectedAddress: 'alpha' });
      const input = await openTree();
      const search = (query) => {
        input.value = query;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        flushSync();
      };

      search('review');
      expect(rows().map((row) => [row.name, row.expanded])).toEqual([
        ['Website', 'true'],
        ['Reviewer', null],
      ]);
      expect(activeRowName()).toBe('Reviewer');

      search('infra');
      expect(rows().map((row) => row.name)).toEqual(['Infra', 'Deployer']);
    });
  });

  it('disables selection while Agents load and explains an empty roster', async () => {
    await mountOverflowing({ loadingAgents: true });

    expect(moreAgentsTrigger().disabled).toBe(true);
    expect(pills().every((pill) => pill.disabled)).toBe(true);

    await unmount(mountedComponent);
    mountedComponent = null;
    mountHeader({ agents: [], agentActivity: {}, selectedAddress: '' });

    expect(moreAgentsTrigger()).toBeNull();
    expect(
      document.querySelector('.chat-header__agents')?.textContent,
    ).toContain(t('chat.noAgents'));
  });
});
