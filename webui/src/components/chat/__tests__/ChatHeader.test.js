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

// Roster order as set in the Agents tab: the bar shows the first five.
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

// Beta and Epsilon run; Eta has the newest unread result, then Delta, then
// Gamma; Alpha (selected) and Zeta are idle.
const ACTIVITY = {
  alpha: { status: 'idle', unreadCount: 0, latestUnreadAt: 0 },
  beta: { status: 'running', unreadCount: 0, latestUnreadAt: 0 },
  gamma: { status: 'unread', unreadCount: 2, latestUnreadAt: 1_000 },
  delta: { status: 'unread', unreadCount: 1, latestUnreadAt: 5_000 },
  epsilon: { status: 'running', unreadCount: 0, latestUnreadAt: 0 },
  zeta: { status: 'idle', unreadCount: 0, latestUnreadAt: 0 },
  eta: { status: 'unread', unreadCount: 1, latestUnreadAt: 9_000 },
};

const running = (name) => t('chat.agentActivity.running', { name });
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

  function pills() {
    return Array.from(document.querySelectorAll('button.agent-pill'));
  }

  function pillLabels() {
    return pills().map((pill) => pill.getAttribute('aria-label'));
  }

  function allAgentsTrigger() {
    return document.querySelector(
      '.chat-header__all-agents button[aria-haspopup]',
    );
  }

  async function openAllAgents() {
    allAgentsTrigger().click();
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

  it('shows the first five Agents in roster order with any activity dot before the name', async () => {
    const onSelectAgent = vi.fn();
    mountHeader({ onSelectAgent });

    expect(pillLabels()).toEqual([
      idle('Alpha'),
      running('Beta'),
      unread('Gamma', 2),
      unread('Delta', 1),
      running('Epsilon'),
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
      ['tab-indicator--running', 'Epsilon', 'false'],
    ]);

    // The displayed Agent stays selectable: it lands on its unread Session.
    pills()[2].click();
    pills()[0].click();
    expect(onSelectAgent.mock.calls).toEqual([['gamma'], ['alpha']]);

    vi.useFakeTimers();
    pills()[1].dispatchEvent(new Event('pointerenter'));
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    expect(tooltipDetails()).toEqual({
      title: 'Beta',
      rows: [
        [t('chat.agentActivity.status'), t('chat.agentActivity.stateRunning')],
        [t('chat.agentActivity.model'), 'anthropic/claude-sonnet-4'],
        [t('chat.agentActivity.thinkingEffort'), 'high'],
      ],
    });
  });

  it('keeps a displayed Agent outside the first five on the bar', () => {
    mountHeader({ selectedAddress: 'zeta' });

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

  it('lists every Agent under All agents and marks the activity of those off the bar', async () => {
    mountHeader();

    const trigger = allAgentsTrigger();
    // Only Eta (unread) and Zeta (idle) are off the bar.
    expect(trigger.querySelector('.tab-indicator--unread')).toBeTruthy();
    expect(trigger.getAttribute('aria-label')).toBe(
      t('chat.agentBar.allAgentsActivity', {
        activity: agentActivityState('unread', 1),
      }),
    );

    await openAllAgents();
    const options = Array.from(document.querySelectorAll('[role="option"]'));
    // Running first, then unread by newest result, then the roster.
    expect(options.map((option) => option.getAttribute('aria-label'))).toEqual([
      running('Beta'),
      running('Epsilon'),
      unread('Eta', 1),
      unread('Delta', 1),
      unread('Gamma', 2),
      idle('Alpha'),
      idle('Zeta'),
    ]);
    expect(
      options.map(
        (option) => option.querySelector('.count-badge')?.textContent,
      ),
    ).toEqual([undefined, undefined, '1', '1', '2', undefined, undefined]);
    expect(
      options
        .filter((option) => option.getAttribute('aria-selected') === 'true')
        .map((option) => option.textContent.trim()),
    ).toEqual(['Alpha']);
  });

  it('offers no All agents list while the bar shows every Agent', () => {
    mountHeader({ agents: AGENTS.slice(0, 5) });

    expect(pills()).toHaveLength(5);
    expect(allAgentsTrigger()).toBeNull();
  });

  it('filters All agents by typing and returns focus on Escape', async () => {
    const onSelectAgent = vi.fn();
    mountHeader({ onSelectAgent });

    let input = await openAllAgents();
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
    expect(document.activeElement).toBe(allAgentsTrigger());

    input = await openAllAgents();
    key(input, 'Escape');
    expect(document.querySelector('[role="listbox"]')).toBeNull();
    expect(document.activeElement).toBe(allAgentsTrigger());
    expect(onSelectAgent).toHaveBeenCalledTimes(1);
  });

  it('toggles the Session list, starts a Session and ends with the area actions', () => {
    const onToggleSessionList = vi.fn();
    const onNewSession = vi.fn();
    mountHeader({
      sessionListOpen: true,
      onToggleSessionList,
      onNewSession,
      actions: createRawSnippet(() => ({
        render: () =>
          '<button type="button" class="area-action">Split</button>',
      })),
    });
    const button = (label) =>
      document.querySelector(`.chat-header button[aria-label="${label}"]`);

    const toggle = button(t('chat.agentBar.sessionList'));
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    toggle.click();
    button(t('chat.newSession')).click();
    expect(onToggleSessionList).toHaveBeenCalledTimes(1);
    expect(onNewSession).toHaveBeenCalledTimes(1);
    expect(
      document.querySelector('.chat-header').lastElementChild.textContent,
    ).toBe('Split');
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
      expect(allAgentsTrigger().getAttribute('aria-haspopup')).toBe('tree');
      return openAllAgents();
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

    it('lists each Project with a Team as a group after the Identity Agents', async () => {
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

      // The selected Agent's Project and a Project with activity are open;
      // the Project without members is left out.
      expect(rows()).toEqual([
        { name: 'Alpha', level: '1', expanded: null },
        { name: 'Beta', level: '1', expanded: null },
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

    it('summarizes a closed group and marks All agents with its activity', async () => {
      // This browser closed Infra earlier.
      localStorage.setItem(
        'vbot.chat.agentPicker.expandedProjects',
        JSON.stringify({ ops: false }),
      );
      mountWithProjects({
        selectedAddress: 'alpha',
        agentActivity: {
          'deployer@ops': { status: 'running', unreadCount: 1 },
        },
      });
      const activity = `${t('chat.agentActivity.stateRunning')} · ${t('chat.agentActivity.stateUnreadOne')}`;

      expect(
        allAgentsTrigger().querySelector('.tab-indicator--running'),
      ).toBeTruthy();
      expect(allAgentsTrigger().getAttribute('aria-label')).toBe(
        t('chat.agentBar.allAgentsActivity', { activity }),
      );
      await openTree();

      const infra = Array.from(
        document.querySelectorAll('[role="treeitem"][aria-level="1"]'),
      ).find((row) => row.textContent.includes('Infra'));
      expect(infra.getAttribute('aria-expanded')).toBe('false');
      expect(infra.querySelector('.tab-indicator--running')).toBeTruthy();
      expect(infra.querySelector('.count-badge')?.textContent).toBe('1');
      expect(infra.getAttribute('aria-label')).toBe(
        t('chat.agentPicker.projectActivity', { project: 'Infra', activity }),
      );
    });

    it('opens and closes groups with tree keys and remembers the choice', async () => {
      const onSelectAgent = vi.fn();
      mountWithProjects({ selectedAddress: 'beta', onSelectAgent });
      const input = await openTree();
      expect(rows().map((row) => row.name)).toEqual([
        'Alpha',
        'Beta',
        'Website',
        'Infra',
      ]);
      expect(activeRowName()).toBe('Beta');

      key(input, 'ArrowDown');
      expect(activeRowName()).toBe('Website');
      key(input, 'ArrowRight');
      expect(rows().map((row) => row.name)).toEqual([
        'Alpha',
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
        'Alpha',
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
    mountHeader({ loadingAgents: true });

    expect(allAgentsTrigger().disabled).toBe(true);
    expect(pills().every((pill) => pill.disabled)).toBe(true);

    await unmount(mountedComponent);
    mountedComponent = null;
    mountHeader({ agents: [], agentActivity: {}, selectedAddress: '' });

    expect(allAgentsTrigger()).toBeNull();
    expect(
      document.querySelector('.chat-header__agents')?.textContent,
    ).toContain(t('chat.noAgents'));
  });
});
