// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

import { reactiveProps } from '../../__tests__/reactiveProps.support.svelte.js';
import { contextMenuAnchor, isContextMenuKey } from '../contextMenu.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ContextMenu } = await import('../ContextMenu.svelte');

let mountedComponent = null;

beforeEach(() => {
  document.body.innerHTML = '';
});

afterEach(async () => {
  if (mountedComponent) await unmount(mountedComponent);
  mountedComponent = null;
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

const menuElement = () => document.querySelector('[role="menu"]');
const menuItems = () => [...document.querySelectorAll('[role="menuitem"]')];
const menuItem = (label) =>
  menuItems().find((item) => item.textContent.includes(label));

function key(target, keyName, init = {}) {
  target.dispatchEvent(
    new KeyboardEvent('keydown', {
      key: keyName,
      bubbles: true,
      cancelable: true,
      ...init,
    }),
  );
  flushSync();
}

async function openMenu(overrides = {}) {
  const row = document.createElement('button');
  row.textContent = 'row';
  document.body.append(row);
  row.focus();
  const onSelect = { open: vi.fn(), copy: vi.fn(), remove: vi.fn() };
  const props = reactiveProps({ menu: null });
  props.onClose = vi.fn(() => {
    props.menu = null;
  });
  mountedComponent = mount(ContextMenu, { target: document.body, props });
  props.menu = {
    x: 40,
    y: 30,
    returnFocus: row,
    label: 'Skill actions',
    items: [
      { id: 'open', label: 'Open', group: 'view', onSelect: onSelect.open },
      {
        id: 'edit',
        label: 'Edit',
        group: 'view',
        disabled: true,
        hint: 'Read-only',
      },
      {
        id: 'copy',
        label: 'Copy name',
        group: 'view',
        onSelect: onSelect.copy,
      },
      {
        id: 'remove',
        label: 'Delete…',
        group: 'danger',
        danger: true,
        onSelect: onSelect.remove,
      },
    ],
    ...overrides,
  };
  flushSync();
  await tick();
  return { row, props, onSelect };
}

describe('ContextMenu', () => {
  it('renders a labelled, portaled menu with separators and item states', async () => {
    await openMenu();

    expect(menuElement().parentElement).toBe(document.body);
    expect(menuElement().getAttribute('aria-label')).toBe('Skill actions');
    expect(menuItems().map((item) => item.textContent.trim())).toEqual([
      'Open',
      'Edit Read-only',
      'Copy name',
      'Delete…',
    ]);
    expect(menuElement().querySelectorAll('[role="separator"]')).toHaveLength(
      1,
    );
    expect(menuItem('Edit').disabled).toBe(true);
    expect(menuItem('Delete').classList).toContain(
      'context-menu__item--danger',
    );
    expect(menuElement().style.visibility).toBe('visible');
    expect(document.activeElement).toBe(menuItem('Open'));
  });

  it('moves focus with the arrow, Home and End keys, skipping disabled items', async () => {
    await openMenu();

    key(document.activeElement, 'ArrowDown');
    expect(document.activeElement).toBe(menuItem('Copy name'));
    key(document.activeElement, 'ArrowDown');
    expect(document.activeElement).toBe(menuItem('Delete'));
    key(document.activeElement, 'ArrowDown');
    expect(document.activeElement).toBe(menuItem('Open'));
    key(document.activeElement, 'ArrowUp');
    expect(document.activeElement).toBe(menuItem('Delete'));
    key(document.activeElement, 'Home');
    expect(document.activeElement).toBe(menuItem('Open'));
    key(document.activeElement, 'End');
    expect(document.activeElement).toBe(menuItem('Delete'));
  });

  it.each([
    ['Enter', (item) => key(item, 'Enter')],
    ['Space', (item) => key(item, ' ')],
    ['click', (item) => item.click()],
  ])(
    'runs the action on %s, then closes and restores focus',
    async (_, activate) => {
      const { row, props, onSelect } = await openMenu();
      let focusDuringAction = null;
      onSelect.copy.mockImplementation(() => {
        focusDuringAction = document.activeElement;
      });

      key(document.activeElement, 'ArrowDown');
      activate(menuItem('Copy name'));
      flushSync();

      expect(onSelect.copy).toHaveBeenCalledOnce();
      expect(focusDuringAction).toBe(row);
      expect(props.onClose).toHaveBeenCalledOnce();
      expect(menuElement()).toBeNull();
      expect(document.activeElement).toBe(row);
    },
  );

  it('ignores disabled items', async () => {
    const { props } = await openMenu();

    menuItem('Edit').click();
    flushSync();

    expect(props.onClose).not.toHaveBeenCalled();
    expect(menuElement()).toBeTruthy();
  });

  it('closes on Escape, consuming it, and restores focus', async () => {
    const { row } = await openMenu();
    const underneath = vi.fn();
    window.addEventListener('keydown', underneath);

    const escape = new KeyboardEvent('keydown', {
      key: 'Escape',
      bubbles: true,
      cancelable: true,
    });
    document.activeElement.dispatchEvent(escape);
    flushSync();
    window.removeEventListener('keydown', underneath);

    expect(escape.defaultPrevented).toBe(true);
    expect(underneath).not.toHaveBeenCalled();
    expect(menuElement()).toBeNull();
    expect(document.activeElement).toBe(row);
  });

  it.each([
    [
      'an outside press',
      () =>
        document.body.dispatchEvent(
          new MouseEvent('pointerdown', { bubbles: true }),
        ),
    ],
    ['an ancestor scroll', (row) => row.dispatchEvent(new Event('scroll'))],
    ['a resize', () => window.dispatchEvent(new Event('resize'))],
    ['a window blur', () => window.dispatchEvent(new Event('blur'))],
  ])('closes on %s without moving focus', async (_, dismiss) => {
    const { row, props } = await openMenu();
    const outside = document.createElement('input');
    document.body.append(outside);
    outside.focus();

    menuItem('Open').dispatchEvent(
      new MouseEvent('pointerdown', { bubbles: true }),
    );
    flushSync();
    expect(menuElement()).toBeTruthy();

    dismiss(row);
    flushSync();

    expect(props.onClose).toHaveBeenCalledOnce();
    expect(menuElement()).toBeNull();
    expect(document.activeElement).toBe(outside);
  });

  it('keeps the native menu from opening over itself', async () => {
    await openMenu();
    const event = new MouseEvent('contextmenu', {
      bubbles: true,
      cancelable: true,
    });

    menuItem('Open').dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
  });

  it('clamps the menu into the viewport', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      width: 224,
      height: 100,
      left: 0,
      top: 0,
      right: 224,
      bottom: 100,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    await openMenu({ x: window.innerWidth, y: window.innerHeight });

    expect(menuElement().style.left).toBe(`${window.innerWidth - 224 - 8}px`);
    expect(menuElement().style.top).toBe(`${window.innerHeight - 100 - 8}px`);
  });
});

describe('contextMenu helpers', () => {
  it.each([
    [{ key: 'ContextMenu' }, true],
    [{ key: 'F10', shiftKey: true }, true],
    [{ key: 'F10' }, false],
    [{ key: 'Enter' }, false],
  ])('recognizes %o as a context menu key: %s', (init, expected) => {
    expect(isContextMenuKey(new KeyboardEvent('keydown', init))).toBe(expected);
  });

  it('anchors pointer events at the pointer and keyboard events below the target', () => {
    const row = document.createElement('button');
    document.body.append(row);
    row.focus();
    vi.spyOn(row, 'getBoundingClientRect').mockReturnValue({
      left: 12,
      bottom: 64,
    });

    const pointer = new MouseEvent('contextmenu', {
      clientX: 120,
      clientY: 80,
    });
    row.dispatchEvent(pointer);
    const keyboard = new KeyboardEvent('keydown', { key: 'ContextMenu' });
    row.dispatchEvent(keyboard);

    expect(contextMenuAnchor(pointer)).toEqual({
      x: 120,
      y: 80,
      returnFocus: row,
    });
    expect(contextMenuAnchor(keyboard)).toEqual({
      x: 12,
      y: 64,
      returnFocus: row,
    });
  });
});
