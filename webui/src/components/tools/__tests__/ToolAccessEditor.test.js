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
  {
    name: 'ha_get_state',
    family: 'extension:homeassistant:home_assistant',
    family_label: 'Home Assistant',
    activation: 'configurable',
    ready: true,
  },
  {
    name: 'ha_call_service',
    family: 'extension:homeassistant:home_assistant',
    family_label: 'Home Assistant',
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

  it('offers explicit opt-in while retaining the default policy', () => {
    const onChange = vi.fn();
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: {
        value: { mode: 'all' },
        tools: [
          ...tools,
          {
            name: 'computer',
            activation: 'configurable',
            requires_opt_in: true,
            ready: true,
          },
        ],
        onChange,
      },
    });
    flushSync();
    expect(toolChip('computer').getAttribute('aria-checked')).toBe('false');
    toolChip('computer').click();
    expect(onChange).toHaveBeenCalledWith({
      mode: 'all',
      granted: ['computer'],
    });
  });

  it('selects all Tools inside the ceiling, including explicit permissions', () => {
    const onChange = vi.fn();
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: {
        value: { mode: 'none' },
        tools: [...tools, { name: 'computer', requires_opt_in: true }],
        ceiling: ['read', 'computer'],
        onChange,
      },
    });
    flushSync();
    buttonWithText('Select all').click();
    const selected = onChange.mock.calls.at(-1)[0];
    expect(selected.mode).toBe('selected');
    expect(selected.allowed).toEqual(
      expect.arrayContaining(['read', 'computer']),
    );
    expect(selected.allowed).toHaveLength(2);
    expect(selected.granted).toEqual(['computer']);
    expect(toolChip('read').disabled).toBe(false);
  });

  it('clears all access and permits individual selection from an empty policy', async () => {
    const onChange = vi.fn();
    const props = {
      value: { mode: 'all', granted: ['analyze_image', 'computer'] },
      tools,
      onChange,
    };
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props,
    });
    flushSync();
    buttonWithText('Deselect all').click();
    flushSync();
    expect(onChange).toHaveBeenLastCalledWith({ mode: 'none' });
    await unmount(mountedComponent);
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: { ...props, value: { mode: 'none' } },
    });
    flushSync();
    expect(
      [...document.querySelectorAll('[data-tool-name]')].every(
        (tool) =>
          tool.getAttribute('aria-checked') === 'false' && !tool.disabled,
      ),
    ).toBe(true);
    toolChip('read').click();
    expect(onChange.mock.calls.at(-1)[0]).toEqual({
      mode: 'selected',
      allowed: ['read'],
      denied: ['session_read', 'memory'],
    });
    expect(onChange.mock.calls.at(-1)[0].granted).toBeUndefined();
  });

  it('keeps Tool rows compact and shows complete details on hover', () => {
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
    const readTip = toolTipWithText('Read a file from disk.');
    expect(readTip.textContent).toContain('Read a file from disk.');
    expect(readTip.dataset.floatingOpen).toBe('false');

    toolChip('read')
      .closest('.tool-access-chip-wrap')
      .dispatchEvent(new Event('pointerenter'));
    expect(readTip.dataset.floatingOpen).toBe('true');

    expect(document.body.textContent).toContain('Memory is currently off');
    expect(buttonByAriaLabel('Available with vision').disabled).toBe(true);
    expect(document.body.textContent).toContain('Individual Tools');
    expect(document.body.textContent).not.toContain('Allow current');
    expect(document.body.textContent).not.toContain('Block current');
  });

  it.each([false, true])(
    'edits the saved vision grant (%s) from the image Tool details',
    (granted) => {
      const onChange = vi.fn();
      const value = {
        mode: 'all',
        granted: granted ? ['analyze_image', 'computer'] : ['computer'],
      };
      mountedComponent = mount(ToolAccessEditor, {
        target: document.body,
        props: { value, tools, onChange },
      });
      flushSync();
      toolChip('analyze_image').focus();
      const control = buttonByAriaLabel('Available with vision');
      expect(control.closest('.tool-access-tip').dataset.floatingOpen).toBe(
        'true',
      );
      expect(control.getAttribute('aria-checked')).toBe(String(granted));
      control.click();
      expect(onChange).toHaveBeenCalledWith({
        mode: 'all',
        granted: granted ? ['computer'] : ['computer', 'analyze_image'],
      });
    },
  );

  it.each([
    { value: { mode: 'none', granted: ['analyze_image'] } },
    { value: { mode: 'all', denied: ['analyze_image'] } },
    { value: { mode: 'selected', allowed: [] } },
    { value: { mode: 'all' }, disabled: true },
  ])('disables the image override with disabled access: %j', (props) => {
    const onChange = vi.fn();
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: { ...props, tools, onChange },
    });
    flushSync();
    const control = buttonByAriaLabel('Available with vision');
    expect(control.disabled).toBe(true);
    control.click();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('omits the image override outside the Project ceiling', () => {
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: { value: { mode: 'all' }, tools, ceiling: ['read'] },
    });
    flushSync();
    expect(
      document.querySelector('button[aria-label="Available with vision"]'),
    ).toBeNull();
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

  it('renders an Extension-declared family label and controls its Tools together', () => {
    const onChange = vi.fn();
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: {
        value: { mode: 'selected', allowed: [] },
        tools,
        onChange,
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('Home Assistant');
    buttonByAriaLabel('Turn on Home Assistant').click();
    expect(onChange).toHaveBeenCalledWith({
      mode: 'selected',
      allowed: ['ha_call_service', 'ha_get_state'],
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

    const bulkAction = buttonWithText('Select all');
    bulkAction.focus();
    expect(document.activeElement).toBe(bulkAction);
    expect(document.querySelector('[role="radiogroup"]')).toBeNull();
  });

  it('finds Tools by description and family without changing hidden permissions', () => {
    const onChange = vi.fn();
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: { value: { mode: 'all' }, tools, onChange },
    });
    flushSync();
    const search = document.querySelector('input[type="search"]');
    search.value = 'from disk';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(
      [...document.querySelectorAll('[data-tool-name]')].map(
        (tool) => tool.dataset.toolName,
      ),
    ).toEqual(['read']);
    buttonByAriaLabel('Turn off Files').click();
    expect(onChange).toHaveBeenLastCalledWith({
      mode: 'all',
      denied: ['read'],
    });
    search.value = 'Home Assistant';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(
      [...document.querySelectorAll('[data-tool-name]')].map(
        (tool) => tool.dataset.toolName,
      ),
    ).toEqual(['ha_call_service', 'ha_get_state']);
  });
});

function toolChip(name) {
  const chip = document.querySelector(`[data-tool-name="${name}"]`);
  expect(chip, name).toBeTruthy();
  return chip;
}

function toolTipWithText(text) {
  const tip = [...document.querySelectorAll('.tool-access-tip')].find(
    (candidate) => candidate.textContent?.includes(text),
  );
  expect(tip, text).toBeTruthy();
  return tip;
}

function buttonByAriaLabel(label) {
  const button = document.querySelector(`button[aria-label="${label}"]`);
  expect(button, label).toBeTruthy();
  return button;
}

function buttonWithText(text) {
  const button = [...document.querySelectorAll('button')].find(
    (candidate) => candidate.textContent.trim() === text,
  );
  expect(button, text).toBeTruthy();
  return button;
}
