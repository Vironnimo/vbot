// @vitest-environment jsdom

import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, tick, unmount } from 'svelte';

import { readStyleSheet } from '../../__tests__/styles.support.js';
import { init, t } from '../../lib/i18n.js';
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

const SIDEBAR_COLLAPSED_KEY = 'vbot.sidebar.collapsed.v1';
const CHAT = {
  id: 'chat',
  label: () => t('navigation.chat'),
  section: 'work',
};
const SETTINGS = {
  id: 'settings',
  label: () => t('navigation.settings'),
  section: 'configure',
};
const originalMatchMedia = window.matchMedia;

let mountedComponent = null;

beforeEach(() => {
  document.body.innerHTML = '';
  localStorage.clear();
  init('en');
  vi.clearAllMocks();
  desktopBridge.getDesktopClipboardText.mockResolvedValue('pasted');
  desktopBridge.openDesktopExternalUrl.mockResolvedValue({ opened: true });
  desktopBridge.setDesktopClipboardText.mockResolvedValue({ copied: true });
});

afterEach(async () => {
  if (mountedComponent) await unmount(mountedComponent);
  mountedComponent = null;
  document.body.innerHTML = '';
  if (originalMatchMedia === undefined) delete window.matchMedia;
  else window.matchMedia = originalMatchMedia;
  vi.restoreAllMocks();
});

function mountShell(props = {}) {
  mountedComponent = mount(AppShell, {
    target: document.body,
    props: { items: [], ...props },
  });
  flushSync();
}

const shell = () => document.querySelector('.app-shell');
const content = () => document.querySelector('.app-shell__content');
const sidebarToggle = () =>
  document.querySelector('.app-shell__sidebar-toggle');
const navItem = (item) =>
  [...document.querySelectorAll('.app-shell__nav-item')].find((element) =>
    element.textContent.includes(item.label()),
  );

describe('AppShell sidebar', () => {
  it('places the optional Live control above microphone and connection status', () => {
    mountShell({
      voiceAvailable: true,
      sidebarFooter: createRawSnippet(() => ({
        render: () => '<button data-live>Start Live</button>',
      })),
    });
    const footer = document.querySelector('.app-shell__footer');
    expect(footer.firstElementChild.hasAttribute('data-live')).toBe(true);
    expect(footer.querySelector('.sidebar-footer__mic')).not.toBeNull();
    expect(content().querySelector('[data-live]')).toBeNull();
  });

  it('keeps the sidebar toggle free of the shared button minimum height', () => {
    mountShell();
    const stylesheet = document.createElement('style');
    stylesheet.textContent = appStyles;
    document.head.append(stylesheet);
    const toggle = sidebarToggle();
    const ordinaryIconButton = toggle.cloneNode(false);
    ordinaryIconButton.classList.remove('app-shell__sidebar-toggle');
    document.body.append(ordinaryIconButton);

    try {
      expect(getComputedStyle(ordinaryIconButton).minHeight).toBe('32px');
      expect(getComputedStyle(toggle).minHeight).toBe('0px');
      toggle.click();
      flushSync();
      expect(getComputedStyle(toggle).minHeight).toBe('0px');
    } finally {
      stylesheet.remove();
    }
  });

  it('collapses navigation to accessible icons, saves the choice and expands again', () => {
    mountShell({ activeViewId: 'chat', items: [CHAT] });
    const toggle = sidebarToggle();

    expect(
      toggle.parentElement.classList.contains('app-shell__sidebar-header'),
    ).toBe(true);
    expect(document.querySelector('.sidebar-footer').contains(toggle)).toBe(
      false,
    );

    toggle.click();
    flushSync();

    expect(shell().dataset.sidebarCollapsed).toBe('true');
    expect(navItem(CHAT).getAttribute('aria-label')).toBe(CHAT.label());
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).toBe('true');
    expect(toggle.getAttribute('aria-label')).toBe(
      t('navigation.expandSidebar'),
    );

    toggle.click();
    flushSync();

    expect(shell().dataset.sidebarCollapsed).toBeUndefined();
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(toggle.getAttribute('aria-label')).toBe(
      t('navigation.collapseSidebar'),
    );
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).toBe('false');
  });

  it('renders a visible symbol for Extension pages in expanded and collapsed navigation', () => {
    const onSelectView = vi.fn();
    mountShell({
      items: [
        {
          id: 'extension:swarm:swarms',
          label: () => 'Swarms',
          section: 'work',
        },
      ],
      onSelectView,
    });
    const item = document.querySelector('.app-shell__nav-item');
    expect(item.querySelector('svg').childElementCount).toBeGreaterThan(0);
    sidebarToggle().click();
    flushSync();
    expect(item.getAttribute('aria-label')).toBe('Swarms');
    expect(item.querySelector('svg').childElementCount).toBeGreaterThan(0);
    item.click();
    expect(onSelectView).toHaveBeenCalledWith('extension:swarm:swarms');
  });

  it('shows the connection status as an icon with the matching state class', () => {
    mountShell({ connectionStatus: CONNECTION_STATUS_CONNECTED });

    expect(
      document
        .querySelector('.conn-icon')
        .classList.contains('conn-icon--connected'),
    ).toBe(true);
    expect(document.querySelector('.footer-text').textContent).toBe(
      t('status.connected'),
    );
  });

  it.each([
    [
      'microphone',
      '.sidebar-footer__mic',
      {
        voiceAvailable: true,
        voiceStatus: { enabled: true, state: 'listening' },
      },
      'voice.mic.tooltip.listening',
    ],
    [
      'connection',
      '.sidebar-footer__connection',
      { connectionStatus: CONNECTION_STATUS_CONNECTED },
      'status.connected',
    ],
  ])(
    'shows the %s status as a details card when collapsed',
    async (_label, selector, props, tooltipKey) => {
      mountShell(props);
      sidebarToggle().click();
      flushSync();

      document
        .querySelector(selector)
        .dispatchEvent(new MouseEvent('pointerenter', { bubbles: false }));

      await vi.waitFor(() =>
        expect(
          document.querySelector('#app-tooltip')?.dataset.floatingOpen,
        ).toBe('true'),
      );
      const card = document.querySelector('#app-tooltip');
      expect(card.querySelector('.app-tooltip__title').textContent).toBe(
        t(tooltipKey),
      );
      // The microphone names what a click does; the connection names the
      // server and since when the state holds.
      expect(card.textContent).toContain(
        selector === '.sidebar-footer__mic'
          ? t('voice.mic.openSettingsHint')
          : t('status.details.since'),
      );
    },
  );
});

describe('AppShell Desktop context menu', () => {
  function mountContent(desktopContextMenuEnabled) {
    mountShell({ desktopContextMenuEnabled });
    return content();
  }

  function appendLink(container, href) {
    const link = document.createElement('a');
    link.href = href;
    link.textContent = 'link-sentinel';
    container.append(link);
    return link;
  }

  function appendInput(container, type, value) {
    const input = document.createElement('input');
    input.type = type;
    input.value = value;
    container.append(input);
    input.focus();
    return input;
  }

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

  const menu = () => document.querySelector('[role="menu"]');
  const menuItem = (key) =>
    [...document.querySelectorAll('[role="menuitem"]')].find((item) =>
      item.textContent.includes(t(key)),
    );

  it('leaves the browser native menu untouched outside Desktop mode', () => {
    const link = appendLink(mountContent(false), 'https://example.com/docs');

    const event = openContextMenu(link);

    expect(event.defaultPrevented).toBe(false);
    expect(menu()).toBeNull();
  });

  it('copies safe link addresses and opens them in the host browser', async () => {
    const link = appendLink(
      mountContent(true),
      'https://example.com/docs?q=vbot',
    );

    const copyEvent = openContextMenu(link);
    expect(copyEvent.defaultPrevented).toBe(true);
    expect(menuItem('desktop.contextMenu.openInBrowser')).toBeTruthy();
    menuItem('desktop.contextMenu.copyLinkAddress').click();
    await vi.waitFor(() =>
      expect(desktopBridge.setDesktopClipboardText).toHaveBeenCalledWith(
        'https://example.com/docs?q=vbot',
      ),
    );

    openContextMenu(link);
    menuItem('desktop.contextMenu.openInBrowser').click();
    await vi.waitFor(() =>
      expect(desktopBridge.openDesktopExternalUrl).toHaveBeenCalledWith(
        'https://example.com/docs?q=vbot',
      ),
    );
  });

  it('leaves events a component menu already handled alone', () => {
    const link = appendLink(mountContent(true), 'https://example.com/docs');
    link.addEventListener('contextmenu', (event) => event.preventDefault());

    openContextMenu(link);

    expect(menu()).toBeNull();
  });

  it('does not expose executable or local link schemes', () => {
    const link = appendLink(mountContent(true), 'javascript:alert(1)');

    const event = openContextMenu(link);

    expect(event.defaultPrevented).toBe(false);
    expect(menu()).toBeNull();
    expect(desktopBridge.setDesktopClipboardText).not.toHaveBeenCalled();
    expect(desktopBridge.openDesktopExternalUrl).not.toHaveBeenCalled();
  });

  it('copies selected page text', async () => {
    const text = document.createElement('p');
    text.textContent = 'selected text';
    mountContent(true).append(text);
    const range = document.createRange();
    range.selectNodeContents(text);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);

    openContextMenu(text);
    menuItem('common.copy').click();

    await vi.waitFor(() =>
      expect(desktopBridge.setDesktopClipboardText).toHaveBeenCalledWith(
        'selected text',
      ),
    );
  });

  it('cuts and pastes text-field selections through the host clipboard', async () => {
    const input = appendInput(mountContent(true), 'text', 'hello world');
    input.setSelectionRange(0, 5);

    openContextMenu(input);
    menuItem('desktop.contextMenu.cut').click();
    await vi.waitFor(() => expect(input.value).toBe(' world'));
    expect(desktopBridge.setDesktopClipboardText).toHaveBeenCalledWith('hello');

    input.setSelectionRange(0, 0);
    openContextMenu(input);
    menuItem('desktop.contextMenu.paste').click();
    await vi.waitFor(() => expect(input.value).toBe('pasted world'));
    expect(desktopBridge.getDesktopClipboardText).toHaveBeenCalledOnce();
  });

  it('allows paste into password fields without exposing their selected text', async () => {
    const input = appendInput(mountContent(true), 'password', 'secret');
    input.setSelectionRange(0, input.value.length);

    openContextMenu(input);
    expect(menuItem('common.copy')).toBeUndefined();
    expect(menuItem('desktop.contextMenu.cut')).toBeUndefined();

    menuItem('desktop.contextMenu.paste').click();
    await vi.waitFor(() => expect(input.value).toBe('pasted'));
  });

  it('closes on Escape and restores focus to the context target', async () => {
    const input = appendInput(mountContent(true), 'text', 'value');

    openContextMenu(input);
    expect(menu()).toBeTruthy();
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();
    await Promise.resolve();

    expect(menu()).toBeNull();
    expect(document.activeElement).toBe(input);
  });
});

describe('AppShell Voice indicator', () => {
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
    mountShell({
      voiceAvailable,
      voiceStatus: status,
      onStopVoiceRecording: onStop,
      onNavigateToVoiceSettings: onNavigate,
    });
    return document.querySelector('.sidebar-footer__mic');
  }

  it('is absent without the Desktop Voice bridge', () => {
    expect(mountMicIndicator({ voiceAvailable: false })).toBeNull();
  });

  // The tone and label table of every status lives in voiceLabels.test.js.
  it('shows the tone, label and name of the Voice status', () => {
    const indicator = mountMicIndicator({
      status: voiceStatus({ recording: { command_id: 'c-1' } }),
    });
    expect(indicator.querySelector('.mic-icon--recording')).toBeTruthy();
    expect(indicator.textContent).toContain(t('voice.state.recording'));
    expect(indicator.getAttribute('aria-label')).toBe(
      t('voice.mic.tooltip.recording'),
    );
  });

  it.each(['.sidebar-footer__mic', '.mic-icon'])(
    'stops the recording when %s is clicked during recording',
    (selector) => {
      const onStop = vi.fn();
      const onNavigate = vi.fn();
      mountMicIndicator({
        status: voiceStatus({ recording: { command_id: 'c-1' } }),
        onStop,
        onNavigate,
      });

      document
        .querySelector(selector)
        .dispatchEvent(new MouseEvent('click', { bubbles: true }));
      flushSync();

      expect(onStop).toHaveBeenCalledOnce();
      expect(onNavigate).not.toHaveBeenCalled();
    },
  );

  it('navigates to voice settings when clicked while not recording', () => {
    const onStop = vi.fn();
    const onNavigate = vi.fn();
    mountMicIndicator({ onStop, onNavigate }).click();
    flushSync();

    expect(onNavigate).toHaveBeenCalledOnce();
    expect(onStop).not.toHaveBeenCalled();
  });
});

describe('AppShell mobile More sheet', () => {
  function mountSheet(onSelectView = vi.fn()) {
    mountShell({
      items: [CHAT, SETTINGS],
      activeViewId: 'settings',
      onSelectView,
    });
    return document.querySelector('.app-shell__nav-more');
  }

  it('marks sheet-only destinations and opens the sheet with focus on the current one', async () => {
    const more = mountSheet();
    const settingsItem = navItem(SETTINGS);

    expect(
      settingsItem.classList.contains('app-shell__nav-item--mobile-secondary'),
    ).toBe(true);
    expect(more.classList.contains('app-shell__nav-more--active')).toBe(true);

    more.click();
    flushSync();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    expect(shell().dataset.mobileNavOpen).toBe('true');
    expect(more.getAttribute('aria-expanded')).toBe('true');
    expect(content().inert).toBe(true);
    expect(document.activeElement).toBe(settingsItem);
  });

  it('closes on Escape, restores focus to More and releases the content', () => {
    const more = mountSheet();
    more.click();
    flushSync();

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();

    expect(shell().dataset.mobileNavOpen).toBeUndefined();
    expect(Boolean(content().inert)).toBe(false);
    expect(document.activeElement).toBe(more);
  });

  it('closes when a destination is chosen from the sheet', () => {
    const onSelectView = vi.fn();
    mountSheet(onSelectView).click();
    flushSync();

    navItem(CHAT).click();
    flushSync();

    expect(onSelectView).toHaveBeenCalledWith('chat');
    expect(shell().dataset.mobileNavOpen).toBeUndefined();
  });
});

describe('AppShell tablet navigation', () => {
  let viewport;

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

  function mountAtWidth(width, onSelectView = vi.fn()) {
    viewport = stubViewport(width);
    mountShell({
      items: [CHAT, SETTINGS],
      activeViewId: 'settings',
      onSelectView,
    });
    return sidebarToggle();
  }

  async function openTabletMenu(onSelectView) {
    const toggle = mountAtWidth(800, onSelectView);
    toggle.click();
    flushSync();
    await tick();
    flushSync();
    return toggle;
  }

  it('forces the compact rail at tablet width and ignores the saved preference', () => {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, 'false');
    const toggle = mountAtWidth(800);

    expect(shell().dataset.sidebarCollapsed).toBe('true');
    expect(navItem(SETTINGS).getAttribute('aria-label')).toBe(SETTINGS.label());
    expect(toggle.getAttribute('aria-label')).toBe(
      t('navigation.expandSidebar'),
    );
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.hasAttribute('aria-pressed')).toBe(false);
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).toBe('false');
  });

  it('opens the full menu as an overlay without changing the saved preference', async () => {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, 'true');
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const toggle = await openTabletMenu();

    expect(shell().dataset.tabletMenuOpen).toBe('true');
    expect(shell().dataset.sidebarCollapsed).toBeUndefined();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(toggle.getAttribute('aria-label')).toBe(
      t('navigation.collapseSidebar'),
    );
    expect(content().inert).toBe(true);
    expect(document.querySelector('.app-shell__nav-backdrop')).not.toBeNull();
    expect(document.activeElement).toBe(navItem(SETTINGS));

    toggle.click();
    flushSync();
    await tick();
    flushSync();
    expect(shell().dataset.tabletMenuOpen).toBeUndefined();
    expect(shell().dataset.sidebarCollapsed).toBe('true');
    expect(setItem).not.toHaveBeenCalled();
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).toBe('true');
  });

  it('closes the overlay when a destination is chosen', async () => {
    const onSelectView = vi.fn();
    await openTabletMenu(onSelectView);

    navItem(CHAT).click();
    flushSync();

    expect(onSelectView).toHaveBeenCalledWith('chat');
    expect(shell().dataset.tabletMenuOpen).toBeUndefined();
    expect(Boolean(content().inert)).toBe(false);
  });

  it('closes on Escape and returns focus to the rail toggle', async () => {
    const toggle = await openTabletMenu();

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    flushSync();

    expect(shell().dataset.tabletMenuOpen).toBeUndefined();
    expect(Boolean(content().inert)).toBe(false);
    expect(document.activeElement).toBe(toggle);
  });

  it('leaves an Escape consumed by a floating layer to that layer', async () => {
    await openTabletMenu();

    const consumed = new KeyboardEvent('keydown', {
      key: 'Escape',
      cancelable: true,
    });
    consumed.preventDefault();
    window.dispatchEvent(consumed);
    flushSync();

    expect(shell().dataset.tabletMenuOpen).toBe('true');
  });

  it('closes on an outside click', async () => {
    await openTabletMenu();

    document.querySelector('.app-shell__nav-backdrop').click();
    flushSync();

    expect(shell().dataset.tabletMenuOpen).toBeUndefined();
    expect(document.querySelector('.app-shell__nav-backdrop')).toBeNull();
  });

  it('closes when the viewport leaves the tablet range and restores the desktop preference', async () => {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, 'false');
    const toggle = await openTabletMenu();

    viewport.resize(1200);

    expect(shell().dataset.tabletMenuOpen).toBeUndefined();
    expect(shell().dataset.sidebarCollapsed).toBeUndefined();
    expect(Boolean(content().inert)).toBe(false);
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(toggle.hasAttribute('aria-expanded')).toBe(false);

    viewport.resize(700);
    expect(shell().dataset.sidebarCollapsed).toBe('true');
    expect(shell().dataset.tabletMenuOpen).toBeUndefined();
  });

  it('restores the saved compact preference on desktop and toggles it there', () => {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, 'true');
    const toggle = mountAtWidth(1400);

    expect(shell().dataset.sidebarCollapsed).toBe('true');
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
    expect(toggle.getAttribute('aria-label')).toBe(
      t('navigation.expandSidebar'),
    );

    toggle.click();
    flushSync();

    expect(shell().dataset.sidebarCollapsed).toBeUndefined();
    expect(shell().dataset.tabletMenuOpen).toBeUndefined();
    expect(Boolean(content().inert)).toBe(false);
    expect(localStorage.getItem(SIDEBAR_COLLAPSED_KEY)).toBe('false');
  });

  it('removes its viewport listeners on unmount', async () => {
    mountAtWidth(800);
    expect(viewport.listenerCount()).toBe(2);

    await unmount(mountedComponent);
    mountedComponent = null;

    expect(viewport.listenerCount()).toBe(0);
  });
});
