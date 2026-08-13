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
    description: 'Read a file from disk.',
    family: 'files',
    activation: 'configurable',
    ready: true,
  },
  {
    name: 'write',
    description: 'Write a file to disk.',
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

  it('renders a compact name-only chip cloud and keeps automatic details out of the list', () => {
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: {
        value: { mode: 'selected', allowed: ['read'] },
        tools,
        memoryPromptMode: 'off',
      },
    });
    flushSync();

    const sessionRead = toolChip('session_read');
    expect(sessionRead.textContent).toBe('session_read');
    expect(sessionRead.getAttribute('aria-checked')).toBe('true');
    expect(sessionRead.classList.contains('is-automatic')).toBe(true);
    expect(toolChip('read').textContent).toBe('read');
    expect(document.body.textContent).not.toContain('Read a file from disk.');
    expect(document.body.textContent).not.toContain('Write a file to disk.');

    expect(document.body.textContent).toContain('Memory is currently off');
    expect(document.body.textContent).toContain(
      'Used only when the main Model cannot analyze images directly',
    );
    expect(document.body.textContent).toContain('Individual Tools');
    expect(document.body.textContent).not.toContain('Allow current');
    expect(document.body.textContent).not.toContain('Block current');
  });

  it('uses one family switch for every member', () => {
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

    buttonByAriaLabel('Turn on Files').click();
    expect(onChange).toHaveBeenCalledWith({
      mode: 'selected',
      allowed: ['read', 'write'],
    });
  });

  it('uses one binary Tool switch while preserving all-mode denials', () => {
    const onChange = vi.fn();
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: { value: { mode: 'all' }, tools, onChange },
    });
    flushSync();

    toolChip('read').click();
    expect(onChange).toHaveBeenCalledWith({
      mode: 'all',
      denied: ['read'],
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
    expect(toolChip('session_search')).toBeTruthy();
    expect(document.querySelector('[data-tool-name="read"]')).toBeNull();

    const selectedMode = document.querySelector('[role="radio"]');
    selectedMode.focus();
    expect(document.activeElement).toBe(selectedMode);
  });
});

function toolChip(name) {
  const chip = document.querySelector(`[data-tool-name="${name}"]`);
  expect(chip, name).toBeTruthy();
  return chip;
}

function buttonByAriaLabel(label) {
  const button = document.querySelector(`button[aria-label="${label}"]`);
  expect(button, label).toBeTruthy();
  return button;
}
