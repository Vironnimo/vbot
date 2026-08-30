// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

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

describe('AppShell wakeword mic indicator', () => {
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

  function mountMicIndicator({ state, onStop, onNavigate }) {
    mountedComponent = mount(AppShell, {
      target: document.body,
      props: {
        items: [],
        desktopCapabilities: { wakeword: true },
        wakewordStatus: { enabled: true, state },
        onStopWakewordRecording: onStop,
        onNavigateToVoiceSettings: onNavigate,
      },
    });
    flushSync();
  }

  it('stops the recording when the label is clicked during recording', () => {
    const onStop = vi.fn();
    const onNavigate = vi.fn();
    mountMicIndicator({ state: 'recording', onStop, onNavigate });

    document.querySelector('.sidebar-footer__link').click();
    flushSync();

    expect(onStop).toHaveBeenCalledOnce();
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it('stops the recording when the dot is clicked during recording', () => {
    const onStop = vi.fn();
    const onNavigate = vi.fn();
    mountMicIndicator({ state: 'recording', onStop, onNavigate });

    document.querySelector('.mic-dot').click();
    flushSync();

    expect(onStop).toHaveBeenCalledOnce();
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it('navigates to voice settings when clicked while not recording', () => {
    const onStop = vi.fn();
    const onNavigate = vi.fn();
    mountMicIndicator({ state: 'listening', onStop, onNavigate });

    document.querySelector('.sidebar-footer__link').click();
    flushSync();

    expect(onNavigate).toHaveBeenCalledOnce();
    expect(onStop).not.toHaveBeenCalled();
  });
});
