// @vitest-environment jsdom

import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, tick, unmount } from 'svelte';

import { readStyleSheet } from '../../__tests__/styles.support.js';
import { init } from '../../lib/i18n.js';
import { CONNECTION_STATUS_CONNECTED } from '../../lib/connectionState.js';

const appStyles = readStyleSheet(
  join(dirname(fileURLToPath(import.meta.url)), '../../styles/app.css'),
);

const desktopBridge = vi.hoisted(() => ({
  getDesktopClipboardText: vi.fn(),
  openDesktopExternalUrl: vi.fn(),
  setDesktopClipboardText: vi.fn(),
}));

vi.mock('$lib/desktopBridge.js', () => desktopBridge);
vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: AppShell } = await import('../AppShell.svelte');

describe('AppShell Desktop context menu', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    localStorage.clear();
    init('en');
    mountedComponent = null;
    vi.clearAllMocks();
    desktopBridge.getDesktopClipboardText.mockResolvedValue('pasted');
    desktopBridge.openDesktopExternalUrl.mockResolvedValue({ opened: true });
    desktopBridge.setDesktopClipboardText.mockResolvedValue({ copied: true });
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  function mountShell(desktopContextMenuEnabled) {
    mountedComponent = mount(AppShell, {
      target: document.body,
      props: {
        items: [],
        desktopContextMenuEnabled,
      },
    });
    flushSync();
    return document.querySelector('.app-shell__content');
  }

  it('places the optional Live control above microphone and connection status', () => {
    mountedComponent = mount(AppShell, {
      target: document.body,
      props: {
        items: [],
        voiceAvailable: true,
        sidebarFooter: createRawSnippet(() => ({
          render: () => '<button data-live>Start Live</button>',
        })),
      },
    });
    flushSync();
    const footer = document.querySelector('.app-shell__footer');
    expect(footer.firstElementChild.hasAttribute('data-live')).toBe(true);
    expect(footer.querySelector('.sidebar-footer__mic')).not.toBeNull();
    expect(
      document.querySelector('.app-shell__content [data-live]'),
    ).toBeNull();
  });

  it('keeps the sidebar toggle free of the shared button minimum height', () => {
    mountShell(false);
    const stylesheet = document.createElement('style');
    stylesheet.textContent = appStyles;
    document.head.append(stylesheet);
    const toggle = document.querySelector('.app-shell__sidebar-toggle');
    const ordinaryIconButton = toggle.cloneNode(false);
    ordinaryIconButton.classList.remove('app-shell__sidebar-toggle');
    document.body.append(ordinaryIconButton);

    try {
      expect(getComputedStyle(ordinaryIconButton).minHeight).toBe('32px');
      expect(getComputedStyle(toggle).minHeight).toBe('0px');
      toggle.click();
      flushSync();
      expect(getComputedStyle(toggle).minHeight).toBe('0px');
      toggle.click();
      flushSync();
      expect(getComputedStyle(toggle).minHeight).toBe('0px');
      expect(toggle.getAttribute('aria-pressed')).toBe('false');
      expect(toggle.getAttribute('aria-label')).toBe('Collapse sidebar');
      expect(localStorage.getItem('vbot.sidebar.collapsed.v1')).toBe('false');
    } finally {
      stylesheet.remove();
      ordinaryIconButton.remove();
    }
  });

  it('collapses navigation to accessible icons and saves the choice', () => {
    mountedComponent = mount(AppShell, {
      target: document.body,
      props: {
        activeViewId: 'chat',
        items: [
          {
            id: 'chat',
            labelKey: 'navigation.chat',
            labelFallback: 'Chat',
            section: 'work',
          },
        ],
      },
    });
    flushSync();

    const toggle = document.querySelector('.app-shell__sidebar-toggle');
    const navItem = document.querySelector('.app-shell__nav-item');

    expect(
      toggle.parentElement.classList.contains('app-shell__sidebar-header'),
    ).toBe(true);
    expect(document.querySelector('.sidebar-footer').contains(toggle)).toBe(
      false,
    );

    toggle.click();
    flushSync();

    expect(document.querySelector('.app-shell').dataset.sidebarCollapsed).toBe(
      'true',
    );
    expect(navItem.getAttribute('aria-label')).toBe('Chat');
    expect(localStorage.getItem('vbot.sidebar.collapsed.v1')).toBe('true');
    expect(toggle.getAttribute('aria-label')).toBe('Expand sidebar');
  });

  it('renders a visible symbol for Extension pages in expanded and collapsed navigation', () => {
    const onSelectView = vi.fn();
    mountedComponent = mount(AppShell, {
      target: document.body,
      props: {
        items: [
          {
            id: 'extension:swarm:swarms',
            labelKey: '',
            labelFallback: 'Swarms',
            section: 'work',
          },
        ],
        onSelectView,
      },
    });
    flushSync();
    const item = document.querySelector('.app-shell__nav-item');
    expect(item.querySelector('svg').childElementCount).toBeGreaterThan(0);
    document.querySelector('.app-shell__sidebar-toggle').click();
    flushSync();
    expect(item.getAttribute('aria-label')).toBe('Swarms');
    expect(item.querySelector('svg').childElementCount).toBeGreaterThan(0);
    item.click();
    expect(onSelectView).toHaveBeenCalledWith('extension:swarm:swarms');
  });

  it('restores the saved collapsed navigation on mount', () => {
    localStorage.setItem('vbot.sidebar.collapsed.v1', 'true');
    mountedComponent = mount(AppShell, {
      target: document.body,
      props: { items: [] },
    });
    flushSync();

    expect(document.querySelector('.app-shell').dataset.sidebarCollapsed).toBe(
      'true',
    );
    expect(
      document
        .querySelector('.app-shell__sidebar-toggle')
        .getAttribute('aria-label'),
    ).toBe('Expand sidebar');
  });

  function openContextMenu(target, options = {}) {
    const event = new MouseEvent('contextmenu', {
      bubbles: true,
      cancelable: true,
      clientX: 120,
      clientY: 80,
      ...options,
    });
    target.dispatchEvent(event);
    flushSync();
    return event;
  }

  it('leaves the browser native menu untouched outside Desktop mode', () => {
    const content = mountShell(false);
    const link = document.createElement('a');
    link.href = 'https://example.com/docs';
    content.append(link);

    const event = openContextMenu(link);

    expect(event.defaultPrevented).toBe(false);
    expect(document.querySelector('[role="menu"]')).toBeNull();
  });

  it('copies safe link addresses and opens them in the host browser', async () => {
    const content = mountShell(true);
    const link = document.createElement('a');
    link.href = 'https://example.com/docs?q=vbot';
    link.textContent = 'Documentation';
    content.append(link);

    const copyEvent = openContextMenu(link);
    const menu = document.querySelector('[role="menu"]');
    const copyLink = Array.from(
      menu.querySelectorAll('[role="menuitem"]'),
    ).find((item) => item.textContent.includes('Copy link address'));

    expect(copyEvent.defaultPrevented).toBe(true);
    expect(menu.textContent).toContain('Open in browser');
    copyLink.click();
    await vi.waitFor(() =>
      expect(desktopBridge.setDesktopClipboardText).toHaveBeenCalledWith(
        'https://example.com/docs?q=vbot',
      ),
    );

    openContextMenu(link);
    const openLink = Array.from(
      document.querySelectorAll('[role="menuitem"]'),
    ).find((item) => item.textContent.includes('Open in browser'));
    openLink.click();
    await vi.waitFor(() =>
      expect(desktopBridge.openDesktopExternalUrl).toHaveBeenCalledWith(
        'https://example.com/docs?q=vbot',
      ),
    );
  });

  it('does not expose executable or local link schemes', () => {
    const content = mountShell(true);
    const link = document.createElement('a');
    link.href = 'javascript:alert(1)';
    link.textContent = 'Unsafe';
    content.append(link);

    const event = openContextMenu(link);

    expect(event.defaultPrevented).toBe(false);
    expect(document.querySelector('[role="menu"]')).toBeNull();
    expect(desktopBridge.setDesktopClipboardText).not.toHaveBeenCalled();
    expect(desktopBridge.openDesktopExternalUrl).not.toHaveBeenCalled();
  });

  it('copies selected page text', async () => {
    const content = mountShell(true);
    const text = document.createElement('p');
    text.textContent = 'selected text';
    content.append(text);
    const range = document.createRange();
    range.selectNodeContents(text);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);

    openContextMenu(text);
    document.querySelector('[role="menuitem"]').click();

    await vi.waitFor(() =>
      expect(desktopBridge.setDesktopClipboardText).toHaveBeenCalledWith(
        'selected text',
      ),
    );
  });

  it('cuts and pastes text-field selections through the host clipboard', async () => {
    const content = mountShell(true);
    const input = document.createElement('input');
    input.type = 'text';
    input.value = 'hello world';
    content.append(input);
    input.focus();
    input.setSelectionRange(0, 5);

    openContextMenu(input);
    const cut = Array.from(document.querySelectorAll('[role="menuitem"]')).find(
      (item) => item.textContent.includes('Cut'),
    );
    cut.click();
    await vi.waitFor(() => expect(input.value).toBe(' world'));
    expect(desktopBridge.setDesktopClipboardText).toHaveBeenCalledWith('hello');

    input.setSelectionRange(0, 0);
    openContextMenu(input);
    const paste = Array.from(
      document.querySelectorAll('[role="menuitem"]'),
    ).find((item) => item.textContent.includes('Paste'));
    paste.click();
    await vi.waitFor(() => expect(input.value).toBe('pasted world'));
    expect(desktopBridge.getDesktopClipboardText).toHaveBeenCalledOnce();
  });

  it('allows paste into password fields without exposing their selected text', async () => {
    const content = mountShell(true);
    const input = document.createElement('input');
    input.type = 'password';
    input.value = 'secret';
    content.append(input);
    input.focus();
    input.setSelectionRange(0, input.value.length);

    openContextMenu(input);
    const menuText = document.querySelector('[role="menu"]').textContent;
    expect(menuText).toContain('Paste');
    expect(menuText).not.toContain('Copy');
    expect(menuText).not.toContain('Cut');

    document.querySelector('[role="menuitem"]').click();
    await vi.waitFor(() => expect(input.value).toBe('pasted'));
  });

  it('closes on Escape and restores focus to the context target', async () => {
    const content = mountShell(true);
    const input = document.createElement('input');
    input.type = 'text';
    input.value = 'value';
    content.append(input);
    input.focus();

    openContextMenu(input);
    expect(document.querySelector('[role="menu"]')).toBeTruthy();
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();
    await Promise.resolve();

    expect(document.querySelector('[role="menu"]')).toBeNull();
    expect(document.activeElement).toBe(input);
  });

  it('fits the menu to the viewport and closes on outside press or scroll', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      bottom: 100,
      height: 100,
      left: 0,
      right: 224,
      top: 0,
      width: 224,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    const content = mountShell(true);
    const link = document.createElement('a');
    link.href = 'https://example.com/docs';
    content.append(link);

    openContextMenu(link, {
      clientX: window.innerWidth,
      clientY: window.innerHeight,
    });
    await vi.waitFor(() => {
      const menu = document.querySelector('[role="menu"]');
      expect(Number.parseFloat(menu.style.left)).toBeLessThan(
        window.innerWidth,
      );
      expect(Number.parseFloat(menu.style.top)).toBeLessThan(
        window.innerHeight,
      );
      expect(menu.style.visibility).toBe('visible');
    });

    document.body.dispatchEvent(
      new MouseEvent('pointerdown', { bubbles: true }),
    );
    flushSync();
    expect(document.querySelector('[role="menu"]')).toBeNull();

    openContextMenu(link);
    content.dispatchEvent(new Event('scroll'));
    flushSync();
    expect(document.querySelector('[role="menu"]')).toBeNull();
  });
});

describe('AppShell Voice indicator', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    localStorage.clear();
    init('en');
    mountedComponent = null;
    vi.clearAllMocks();
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  function voiceStatus(overrides = {}) {
    return {
      enabled: true,
      state: 'listening',
      sequence: 1,
      recording: null,
      commands: [],
      ...overrides,
    };
  }

  function mountMicIndicator({
    status = voiceStatus(),
    voiceAvailable = true,
    onStop = vi.fn(),
    onNavigate = vi.fn(),
  } = {}) {
    mountedComponent = mount(AppShell, {
      target: document.body,
      props: {
        items: [],
        voiceAvailable,
        voiceStatus: status,
        onStopVoiceRecording: onStop,
        onNavigateToVoiceSettings: onNavigate,
      },
    });
    flushSync();
    return document.querySelector('.sidebar-footer__mic');
  }

  it('is absent without the Desktop Voice bridge', () => {
    expect(mountMicIndicator({ voiceAvailable: false })).toBeNull();
  });

  it.each([
    ['off', voiceStatus({ enabled: false, state: 'off' }), 'off', 'Disabled'],
    ['starting', voiceStatus({ state: 'starting' }), 'processing', 'Starting'],
    ['listening', voiceStatus(), 'listening', 'Listening'],
    [
      'a lost microphone',
      voiceStatus({ state: 'microphone_disconnected' }),
      'warning',
      'Microphone disconnected',
    ],
    ['an error', voiceStatus({ state: 'error' }), 'error', 'Voice error'],
    [
      'a recording',
      voiceStatus({ recording: { command_id: 'c-1' } }),
      'recording',
      'Recording',
    ],
    [
      'a command in flight',
      voiceStatus({
        commands: [{ command_id: 'c-1', model_id: null, stage: 'sending' }],
      }),
      'processing',
      'Sending',
    ],
    [
      'an error during a recording',
      voiceStatus({ state: 'error', recording: { command_id: 'c-1' } }),
      'error',
      'Voice error',
    ],
    ['no status yet', null, 'off', 'Disabled'],
  ])('shows %s', (_label, status, tone, text) => {
    const indicator = mountMicIndicator({ status });
    expect(indicator.querySelector(`.mic-icon--${tone}`)).toBeTruthy();
    expect(indicator.textContent).toContain(text);
  });

  it('stops the recording when the mic indicator is clicked during recording', () => {
    const onStop = vi.fn();
    const onNavigate = vi.fn();
    mountMicIndicator({
      status: voiceStatus({ recording: { command_id: 'c-1' } }),
      onStop,
      onNavigate,
    }).click();
    flushSync();

    expect(onStop).toHaveBeenCalledOnce();
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it('stops the recording when the mic icon is clicked during recording', () => {
    const onStop = vi.fn();
    const onNavigate = vi.fn();
    mountMicIndicator({
      status: voiceStatus({ recording: { command_id: 'c-1' } }),
      onStop,
      onNavigate,
    });

    document
      .querySelector('.mic-icon')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(onStop).toHaveBeenCalledOnce();
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it('navigates to voice settings when clicked while not recording', () => {
    const onStop = vi.fn();
    const onNavigate = vi.fn();
    mountMicIndicator({ onStop, onNavigate }).click();
    flushSync();

    expect(onNavigate).toHaveBeenCalledOnce();
    expect(onStop).not.toHaveBeenCalled();
  });

  it('shows the mic tooltip on the indicator when collapsed', async () => {
    mountMicIndicator();

    document.querySelector('.app-shell__sidebar-toggle').click();
    flushSync();

    document
      .querySelector('.sidebar-footer__mic')
      .dispatchEvent(new MouseEvent('pointerenter', { bubbles: false }));

    await vi.waitFor(() =>
      expect(document.querySelector('#app-tooltip')?.dataset.floatingOpen).toBe(
        'true',
      ),
    );
    expect(document.querySelector('#app-tooltip').textContent).toBe(
      'Listening for wake phrases',
    );
  });
});

describe('AppShell sidebar status icons', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    localStorage.clear();
    init('en');
    mountedComponent = null;
    vi.clearAllMocks();
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  it('renders the connection status as an icon with the matching state class', () => {
    mountedComponent = mount(AppShell, {
      target: document.body,
      props: {
        items: [],
        connectionStatus: CONNECTION_STATUS_CONNECTED,
      },
    });
    flushSync();

    const icon = document.querySelector('.conn-icon');
    expect(icon).toBeTruthy();
    expect(icon.classList.contains('conn-icon--connected')).toBe(true);
    expect(document.querySelector('.footer-text').textContent).toBe(
      'Connected',
    );
  });

  it('shows the connection status tooltip on the icon when collapsed', async () => {
    mountedComponent = mount(AppShell, {
      target: document.body,
      props: {
        items: [],
        connectionStatus: CONNECTION_STATUS_CONNECTED,
      },
    });
    flushSync();

    document.querySelector('.app-shell__sidebar-toggle').click();
    flushSync();

    document
      .querySelector('.conn-icon')
      .dispatchEvent(new MouseEvent('pointerenter', { bubbles: false }));

    await vi.waitFor(() =>
      expect(document.querySelector('#app-tooltip')?.dataset.floatingOpen).toBe(
        'true',
      ),
    );
    expect(document.querySelector('#app-tooltip').textContent).toBe(
      'Connected',
    );
  });
});

describe('AppShell mobile More sheet', () => {
  let mountedComponent;

  const items = [
    {
      id: 'chat',
      labelKey: 'navigation.chat',
      labelFallback: 'Chat',
      section: 'work',
    },
    {
      id: 'settings',
      labelKey: 'navigation.settings',
      labelFallback: 'Settings',
      section: 'configure',
    },
  ];

  beforeEach(() => {
    document.body.innerHTML = '';
    localStorage.clear();
    init('en');
  });

  afterEach(async () => {
    if (mountedComponent) await unmount(mountedComponent);
    mountedComponent = null;
    document.body.innerHTML = '';
  });

  function mountShell(onSelectView = vi.fn()) {
    mountedComponent = mount(AppShell, {
      target: document.body,
      props: { items, activeViewId: 'settings', onSelectView },
    });
    flushSync();
    return {
      shell: document.querySelector('.app-shell'),
      more: document.querySelector('.app-shell__nav-more'),
      main: document.querySelector('.app-shell__content'),
    };
  }

  it('marks sheet-only destinations and opens the sheet with focus on the current one', async () => {
    const { shell, more, main } = mountShell();
    const settingsItem = [
      ...document.querySelectorAll('.app-shell__nav-item'),
    ].find((item) => item.textContent.includes('Settings'));

    expect(
      settingsItem.classList.contains('app-shell__nav-item--mobile-secondary'),
    ).toBe(true);
    expect(more.classList.contains('app-shell__nav-more--active')).toBe(true);

    more.click();
    flushSync();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    expect(shell.dataset.mobileNavOpen).toBe('true');
    expect(more.getAttribute('aria-expanded')).toBe('true');
    expect(main.inert).toBe(true);
    expect(document.activeElement).toBe(settingsItem);
  });

  it('closes on Escape, restores focus to More and releases the content', () => {
    const { shell, more, main } = mountShell();
    more.click();
    flushSync();

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();

    expect(shell.dataset.mobileNavOpen).toBeUndefined();
    expect(Boolean(main.inert)).toBe(false);
    expect(document.activeElement).toBe(more);
  });

  it('closes when a destination is chosen from the sheet', () => {
    const onSelectView = vi.fn();
    const { shell, more } = mountShell(onSelectView);
    more.click();
    flushSync();

    [...document.querySelectorAll('.app-shell__nav-item')]
      .find((item) => item.textContent.includes('Chat'))
      .click();
    flushSync();

    expect(onSelectView).toHaveBeenCalledWith('chat');
    expect(shell.dataset.mobileNavOpen).toBeUndefined();
  });
});

describe('AppShell tablet navigation', () => {
  let mountedComponent;
  let viewport;

  const items = [
    {
      id: 'chat',
      labelKey: 'navigation.chat',
      labelFallback: 'Chat',
      section: 'work',
    },
    {
      id: 'settings',
      labelKey: 'navigation.settings',
      labelFallback: 'Settings',
      section: 'configure',
    },
  ];

  // A minimal matchMedia stand-in that evaluates the min/max-width queries
  // AppShell uses and notifies listeners when the simulated width changes.
  function stubViewport(initialWidth) {
    let width = initialWidth;
    const lists = [];
    const evaluate = (query) =>
      [...query.matchAll(/\((min|max)-width:\s*(\d+)px\)/g)].every(
        ([, bound, value]) =>
          bound === 'min' ? width >= Number(value) : width <= Number(value),
      );
    window.matchMedia = vi.fn((query) => {
      const listeners = new Set();
      const list = {
        media: query,
        get matches() {
          return evaluate(query);
        },
        addEventListener: (_type, listener) => listeners.add(listener),
        removeEventListener: (_type, listener) => listeners.delete(listener),
        listeners,
      };
      lists.push(list);
      return list;
    });
    return {
      resize(nextWidth) {
        const before = new Map(lists.map((list) => [list, list.matches]));
        width = nextWidth;
        for (const list of lists) {
          if (list.matches !== before.get(list)) {
            for (const listener of list.listeners) {
              listener({ matches: list.matches, media: list.media });
            }
          }
        }
        flushSync();
      },
      listenerCount: () =>
        lists.reduce((count, list) => count + list.listeners.size, 0),
    };
  }

  beforeEach(() => {
    document.body.innerHTML = '';
    localStorage.clear();
    init('en');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) await unmount(mountedComponent);
    mountedComponent = null;
    document.body.innerHTML = '';
    delete window.matchMedia;
    vi.restoreAllMocks();
  });

  function mountShell({ width, onSelectView = vi.fn() }) {
    viewport = stubViewport(width);
    mountedComponent = mount(AppShell, {
      target: document.body,
      props: { items, activeViewId: 'settings', onSelectView },
    });
    flushSync();
    return {
      shell: document.querySelector('.app-shell'),
      toggle: document.querySelector('.app-shell__sidebar-toggle'),
      main: document.querySelector('.app-shell__content'),
      settingsItem: [...document.querySelectorAll('.app-shell__nav-item')].find(
        (item) => item.textContent.includes('Settings'),
      ),
    };
  }

  async function settle() {
    flushSync();
    await tick();
    flushSync();
  }

  it('forces the compact rail at tablet width and ignores the saved preference', () => {
    localStorage.setItem('vbot.sidebar.collapsed.v1', 'false');
    const { shell, toggle, settingsItem } = mountShell({ width: 800 });

    expect(shell.dataset.sidebarCollapsed).toBe('true');
    expect(settingsItem.getAttribute('aria-label')).toBe('Settings');
    expect(toggle.getAttribute('aria-label')).toBe('Expand sidebar');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.hasAttribute('aria-pressed')).toBe(false);
    expect(localStorage.getItem('vbot.sidebar.collapsed.v1')).toBe('false');
  });

  it('opens the full menu as an overlay without changing the saved preference', async () => {
    localStorage.setItem('vbot.sidebar.collapsed.v1', 'true');
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const { shell, toggle, main, settingsItem } = mountShell({ width: 800 });

    toggle.click();
    await settle();

    expect(shell.dataset.tabletMenuOpen).toBe('true');
    expect(shell.dataset.sidebarCollapsed).toBeUndefined();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(toggle.getAttribute('aria-label')).toBe('Collapse sidebar');
    expect(main.inert).toBe(true);
    expect(document.querySelector('.app-shell__nav-backdrop')).not.toBeNull();
    expect(document.activeElement).toBe(settingsItem);
    expect(setItem).not.toHaveBeenCalled();
    expect(localStorage.getItem('vbot.sidebar.collapsed.v1')).toBe('true');

    toggle.click();
    await settle();
    expect(shell.dataset.tabletMenuOpen).toBeUndefined();
    expect(shell.dataset.sidebarCollapsed).toBe('true');
    expect(setItem).not.toHaveBeenCalled();
  });

  it('closes the overlay when a destination is chosen', async () => {
    const onSelectView = vi.fn();
    const { shell, toggle, main } = mountShell({ width: 800, onSelectView });
    toggle.click();
    await settle();

    [...document.querySelectorAll('.app-shell__nav-item')]
      .find((item) => item.textContent.includes('Chat'))
      .click();
    flushSync();

    expect(onSelectView).toHaveBeenCalledWith('chat');
    expect(shell.dataset.tabletMenuOpen).toBeUndefined();
    expect(Boolean(main.inert)).toBe(false);
  });

  it('closes on Escape and returns focus to the rail toggle', async () => {
    const { shell, toggle, main } = mountShell({ width: 800 });
    toggle.click();
    await settle();

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();

    expect(shell.dataset.tabletMenuOpen).toBeUndefined();
    expect(Boolean(main.inert)).toBe(false);
    expect(document.activeElement).toBe(toggle);
  });

  it('leaves an Escape consumed by a floating layer to that layer', async () => {
    const { shell, toggle } = mountShell({ width: 800 });
    toggle.click();
    await settle();

    const consumed = new KeyboardEvent('keydown', {
      key: 'Escape',
      cancelable: true,
    });
    consumed.preventDefault();
    window.dispatchEvent(consumed);
    flushSync();

    expect(shell.dataset.tabletMenuOpen).toBe('true');
  });

  it('closes on an outside click', async () => {
    const { shell, toggle } = mountShell({ width: 800 });
    toggle.click();
    await settle();

    document.querySelector('.app-shell__nav-backdrop').click();
    flushSync();

    expect(shell.dataset.tabletMenuOpen).toBeUndefined();
    expect(document.querySelector('.app-shell__nav-backdrop')).toBeNull();
  });

  it('closes when the viewport leaves the tablet range and restores the desktop preference', async () => {
    localStorage.setItem('vbot.sidebar.collapsed.v1', 'false');
    const { shell, toggle, main } = mountShell({ width: 800 });
    toggle.click();
    await settle();

    viewport.resize(1200);

    expect(shell.dataset.tabletMenuOpen).toBeUndefined();
    expect(shell.dataset.sidebarCollapsed).toBeUndefined();
    expect(Boolean(main.inert)).toBe(false);
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(toggle.hasAttribute('aria-expanded')).toBe(false);

    viewport.resize(700);
    expect(shell.dataset.sidebarCollapsed).toBe('true');
    expect(shell.dataset.tabletMenuOpen).toBeUndefined();
  });

  it('keeps the saved compact preference on desktop and toggles it there', () => {
    localStorage.setItem('vbot.sidebar.collapsed.v1', 'true');
    const { shell, toggle, main } = mountShell({ width: 1400 });

    expect(shell.dataset.sidebarCollapsed).toBe('true');
    expect(toggle.getAttribute('aria-pressed')).toBe('true');

    toggle.click();
    flushSync();

    expect(shell.dataset.sidebarCollapsed).toBeUndefined();
    expect(shell.dataset.tabletMenuOpen).toBeUndefined();
    expect(Boolean(main.inert)).toBe(false);
    expect(localStorage.getItem('vbot.sidebar.collapsed.v1')).toBe('false');
  });

  it('removes its viewport listeners on unmount', async () => {
    mountShell({ width: 800 });
    expect(viewport.listenerCount()).toBe(2);

    await unmount(mountedComponent);
    mountedComponent = null;

    expect(viewport.listenerCount()).toBe(0);
  });
});
