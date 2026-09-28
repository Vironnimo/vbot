// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../../lib/i18n.js';
import { TOOLTIP_SHOW_DELAY_MS } from '../../../lib/tooltip.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatHeader } = await import('../ChatHeader.svelte');

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
];

// Beta and Epsilon run; Delta has the newest unread result, Gamma an older
// one; Alpha (selected) is idle.
const ACTIVITY = {
  alpha: { status: 'idle', unreadCount: 0, latestUnreadAt: 0 },
  beta: { status: 'running', unreadCount: 0, latestUnreadAt: 0 },
  gamma: { status: 'unread', unreadCount: 2, latestUnreadAt: 1_000 },
  delta: { status: 'unread', unreadCount: 1, latestUnreadAt: 5_000 },
  epsilon: { status: 'running', unreadCount: 0, latestUnreadAt: 0 },
};

const running = (name) => t('chat.agentActivity.running', '', { name });
const idle = (name) => t('chat.agentActivity.idle', '', { name });
const unread = (name, count) =>
  count === 1
    ? t('chat.agentActivity.unreadOne', '', { name })
    : t('chat.agentActivity.unreadCount', '', { name, count });

describe('ChatHeader', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
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
    delete window.matchMedia;
  });

  function mountHeader(props = {}) {
    mountedComponent = mount(ChatHeader, {
      target: document.body,
      props: {
        agents: AGENTS,
        agentActivity: ACTIVITY,
        selectedAgentId: 'alpha',
        ...props,
      },
    });
    flushSync();
    return mountedComponent;
  }

  function pickerTrigger() {
    return document.querySelector(
      '.chat-header__agent-picker button[aria-haspopup="listbox"]',
    );
  }

  function chipLabels() {
    return Array.from(
      document.querySelectorAll('.agent-chips > button'),
      (chip) => chip.getAttribute('aria-label'),
    );
  }

  async function openPicker() {
    pickerTrigger().click();
    await vi.waitFor(() => {
      expect(document.querySelector('[role="listbox"]')).toBeTruthy();
    });
    flushSync();
    return Array.from(document.querySelectorAll('[role="option"]'));
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

  it('shows the selected Agent with its status on the picker trigger', async () => {
    mountHeader({ selectedAgentId: 'beta' });

    const trigger = pickerTrigger();
    expect(trigger.textContent).toContain('Beta');
    expect(trigger.querySelector('.tab-indicator--running')).toBeTruthy();
    expect(trigger.getAttribute('aria-label')).toBe(
      t('chat.agentPicker.label', '', { activity: running('Beta') }),
    );

    vi.useFakeTimers();
    trigger.dispatchEvent(new Event('pointerenter'));
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    expect(tooltipDetails()).toEqual({
      title: running('Beta'),
      rows: [
        [t('chat.agentActivity.model'), 'anthropic/claude-sonnet-4'],
        [t('chat.agentActivity.thinkingEffort'), 'high'],
      ],
    });
  });

  it('describes every Agent on its picker option', async () => {
    mountHeader({ selectedAgentId: 'beta' });
    const options = await openPicker();
    const alpha = options.find(
      (option) => option.getAttribute('aria-label') === idle('Alpha'),
    );

    vi.useFakeTimers();
    alpha.dispatchEvent(new Event('pointerenter'));
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    expect(tooltipDetails()).toEqual({
      title: idle('Alpha'),
      rows: [
        [t('chat.agentActivity.model'), 'openai/gpt-5.2'],
        [
          t('chat.agentActivity.thinkingEffort'),
          t('chat.agentActivity.thinkingEffortDefault'),
        ],
      ],
    });
  });

  it('orders the picker running first, then unread by newest result, then the roster', async () => {
    mountHeader();

    const options = await openPicker();

    expect(options.map((option) => option.getAttribute('aria-label'))).toEqual([
      running('Beta'),
      running('Epsilon'),
      unread('Delta', 1),
      unread('Gamma', 2),
      idle('Alpha'),
    ]);
    expect(
      options.map(
        (option) => option.querySelector('.count-badge')?.textContent,
      ),
    ).toEqual([undefined, undefined, '1', '2', undefined]);
    const selected = options.filter(
      (option) => option.getAttribute('aria-selected') === 'true',
    );
    expect(selected.map((option) => option.textContent.trim())).toEqual([
      'Alpha',
    ]);
  });

  it('shows activity chips for other Agents, unread first, and selects through them', () => {
    const onSelectAgent = vi.fn();
    mountHeader({ selectedAgentId: 'delta', onSelectAgent });

    // The selected Agent (Delta) is not a chip; idle Alpha has none.
    expect(chipLabels()).toEqual([
      unread('Gamma', 2),
      running('Beta'),
      running('Epsilon'),
    ]);
    const [gammaChip, betaChip] = document.querySelectorAll(
      '.agent-chips > button',
    );
    expect(gammaChip.querySelector('.tab-indicator--unread')).toBeTruthy();
    expect(gammaChip.querySelector('.count-badge')?.textContent).toBe('2');
    expect(betaChip.querySelector('.tab-indicator--running')).toBeTruthy();

    gammaChip.click();
    expect(onSelectAgent).toHaveBeenCalledWith('gamma');
  });

  it('renders no chips while no other Agent is running or unread', () => {
    mountHeader({
      agentActivity: { beta: { status: 'running', unreadCount: 0 } },
      selectedAgentId: 'beta',
    });

    expect(document.querySelector('.agent-chips')).toBeNull();
  });

  it('collapses chips that do not fit into a more chip that opens the picker', async () => {
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(
      function rect() {
        let width = 0;
        if (this.classList?.contains('agent-chips')) {
          width = 260;
        } else if (this.hasAttribute?.('data-measure-chip')) {
          width = 100;
        } else if (this.hasAttribute?.('data-measure-more')) {
          width = 40;
        }
        return {
          width,
          height: 28,
          top: 0,
          left: 0,
          right: width,
          bottom: 28,
          x: 0,
          y: 0,
        };
      },
    );
    mountHeader();
    await vi.waitFor(() => {
      expect(document.querySelector('.agent-chip--more')).toBeTruthy();
    });

    expect(chipLabels()).toEqual([
      unread('Delta', 1),
      unread('Gamma', 2),
      t('chat.agentChips.more', '', { count: 2 }),
    ]);
    const more = document.querySelector('.agent-chip--more');
    expect(more.textContent.trim()).toBe('+2');

    more.click();
    await vi.waitFor(() => {
      expect(pickerTrigger().getAttribute('aria-expanded')).toBe('true');
    });
    expect(document.activeElement?.getAttribute('role')).toBe('listbox');
  });

  it('selects Agents with listbox keys and typeahead', async () => {
    const onSelectAgent = vi.fn();
    mountHeader({ onSelectAgent });
    const trigger = pickerTrigger();

    key(trigger, 'ArrowDown');
    await vi.waitFor(() => {
      expect(document.activeElement?.getAttribute('role')).toBe('listbox');
    });
    const listbox = document.activeElement;
    const activeLabel = () =>
      listbox
        .querySelector(
          '[role="option"].active .dropdown-primitive__option-label',
        )
        ?.textContent.trim();
    expect(activeLabel()).toBe('Alpha');

    key(listbox, 'Home');
    expect(activeLabel()).toBe('Beta');
    key(listbox, 'End');
    expect(activeLabel()).toBe('Alpha');
    key(listbox, 'ArrowUp');
    expect(activeLabel()).toBe('Gamma');
    key(listbox, 'd');
    expect(activeLabel()).toBe('Delta');

    key(listbox, 'Enter');
    expect(onSelectAgent).toHaveBeenCalledWith('delta');
    expect(document.activeElement).toBe(trigger);

    key(trigger, 'ArrowDown');
    await vi.waitFor(() => {
      expect(document.activeElement?.getAttribute('role')).toBe('listbox');
    });
    key(document.activeElement, 'Escape');
    expect(document.querySelector('[role="listbox"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(onSelectAgent).toHaveBeenCalledTimes(1);
  });

  it('filters a larger roster by typing', async () => {
    const onSelectAgent = vi.fn();
    mountHeader({
      agents: [
        ...AGENTS,
        { id: 'zeta', name: 'Zeta' },
        { id: 'eta', name: 'Eta' },
      ],
      onSelectAgent,
    });

    pickerTrigger().click();
    await vi.waitFor(() => {
      expect(document.activeElement?.getAttribute('role')).toBe('combobox');
    });
    const input = document.activeElement;
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
  });

  it('collapses chips to status dots on phone widths', () => {
    window.matchMedia = vi.fn((query) => ({
      matches: query === '(max-width: 640px)',
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    mountHeader({ selectedAgentId: 'gamma' });

    const chipRow = document.querySelector('.agent-chips');
    expect(chipRow.classList.contains('agent-chips--compact')).toBe(true);
    const chips = Array.from(chipRow.querySelectorAll(':scope > button'));
    expect(chips.map((chip) => chip.getAttribute('aria-label'))).toEqual([
      unread('Delta', 1),
      running('Beta'),
      running('Epsilon'),
    ]);
    for (const chip of chips) {
      expect(chip.textContent.trim()).toBe('');
      expect(chip.querySelector('.tab-indicator')).toBeTruthy();
    }
  });

  it('marks nothing and offers every active Agent as a chip while a Project Agent is selected', () => {
    mountHeader({
      selectedAgentId: '',
      selectedProjectId: 'vbot',
      projects: [{ project_id: 'vbot', display_name: 'vBot' }],
    });

    expect(
      document.querySelector('.agent-switcher__personal-label')?.textContent,
    ).toContain(t('chat.personalBarLabel'));
    const trigger = pickerTrigger();
    expect(trigger.getAttribute('aria-label')).toBe(t('chat.selectAgent'));
    expect(trigger.textContent).toContain(t('chat.selectAgent'));
    expect(trigger.querySelector('.tab-indicator')).toBeNull();
    expect(chipLabels()).toEqual([
      unread('Delta', 1),
      unread('Gamma', 2),
      running('Beta'),
      running('Epsilon'),
    ]);
  });

  it('disables selection while Agents load and explains an empty roster', async () => {
    mountHeader({ loadingAgents: true });

    expect(pickerTrigger().disabled).toBe(true);
    for (const chip of document.querySelectorAll('.agent-chips > button')) {
      expect(chip.disabled).toBe(true);
    }

    await unmount(mountedComponent);
    mountedComponent = null;
    mountHeader({ agents: [], agentActivity: {}, selectedAgentId: '' });

    expect(pickerTrigger()).toBeNull();
    expect(document.querySelector('.agent-switcher')?.textContent).toContain(
      t('chat.noAgents'),
    );
  });
});
