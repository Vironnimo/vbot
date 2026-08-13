// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ToolAccessEditor } =
  await import('../ToolAccessEditor.svelte');

const tools = [
  {
    name: 'read',
    family: 'files',
    activation: 'configurable',
    ready: true,
  },
  {
    name: 'write',
    family: 'files',
    activation: 'configurable',
    ready: true,
  },
  {
    name: 'session_search',
    family: 'sessions',
    activation: 'configurable',
    ready: true,
  },
  {
    name: 'session_read',
    family: 'sessions',
    activation: 'follows',
    activation_source: 'session_search',
    ready: true,
  },
  {
    name: 'memory',
    family: null,
    activation: 'memory_mode',
    ready: true,
  },
  {
    name: 'analyze_image',
    family: 'media',
    activation: 'configurable',
    constraints: ['image_fallback_route'],
    ready: true,
  },
  {
    name: 'image_generation',
    family: 'media',
    activation: 'configurable',
    ready: true,
  },
];

describe('ToolAccessEditor', () => {
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
  });

  it('keeps automatic Tools visible and explains when their gate is inactive', () => {
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: {
        value: { mode: 'selected', allowed: ['read'] },
        tools,
        memoryPromptMode: 'off',
      },
    });
    flushSync();

    const sessionRead = toolRow('session_read');
    expect(sessionRead.textContent).toContain('Inactive');
    expect(sessionRead.textContent).toContain(
      'Inactive · waiting for session_search',
    );
    expect(
      sessionRead.querySelector('[data-tool-access-state="enabled"]'),
    ).toBeNull();

    const memory = toolRow('memory');
    expect(memory.textContent).toContain('Inactive · Memory is off');
    expect(toolRow('analyze_image').textContent).toContain(
      'Used only when the main Model cannot analyze images directly',
    );
    expect(document.body.textContent).toContain('Individual Tools');
  });

  it('applies one family action to every current member', () => {
    const onChange = vi.fn();
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: {
        value: { mode: 'selected', allowed: ['read'] },
        tools,
        onChange,
      },
    });
    flushSync();

    buttonByAriaLabel('Block current Files').click();
    expect(onChange).toHaveBeenCalledWith({
      mode: 'selected',
      allowed: [],
      denied: ['read', 'write'],
    });
  });

  it('filters live and exposes keyboard-focusable native controls', () => {
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: { value: { mode: 'all' }, tools },
    });
    flushSync();

    const search = document.querySelector('input[type="search"]');
    search.value = 'session';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(toolRow('session_search')).toBeTruthy();
    expect(document.querySelector('[data-tool-name="read"]')).toBeNull();

    const selectedMode = document.querySelector('[role="radio"]');
    selectedMode.focus();
    expect(document.activeElement).toBe(selectedMode);
  });
});

function toolRow(name) {
  const row = document.querySelector(`[data-tool-name="${name}"]`);
  expect(row, name).toBeTruthy();
  return row;
}

function buttonByAriaLabel(label) {
  const button = document.querySelector(`button[aria-label="${label}"]`);
  expect(button, label).toBeTruthy();
  return button;
}
