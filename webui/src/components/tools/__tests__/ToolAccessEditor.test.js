// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../../lib/i18n.js';
import { HOVER_CARD_SHOW_DELAY_MS } from '../../../lib/tooltip.js';

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
    name: 'generate_image',
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
    extension: 'homeassistant',
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

  it.each([
    ['read', 'true', { mode: 'all', denied: ['read'] }],
    // An opt-in Tool is granted explicitly without leaving All mode.
    ['computer', 'false', { mode: 'all', granted: ['computer'] }],
  ])(
    'toggles %s with one binary checkbox while keeping All mode',
    (name, checked, expected) => {
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
      expect(toolChip(name).getAttribute('aria-checked')).toBe(checked);
      toolChip(name).click();
      expect(onChange).toHaveBeenCalledWith(expected);
    },
  );

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
    buttonWithText(t('toolAccess.selectAll')).click();
    const selected = onChange.mock.calls.at(-1)[0];
    expect(selected.mode).toBe('selected');
    expect(selected.allowed).toEqual(
      expect.arrayContaining(['read', 'computer']),
    );
    expect(selected.allowed).toHaveLength(2);
    expect(selected.granted).toEqual(['computer']);
    expect(toolChip('read').disabled).toBe(false);
    // The image override only exists inside the Project ceiling.
    expect(
      document.querySelector('button[aria-label="Available with vision"]'),
    ).toBeNull();
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
    buttonWithText(t('toolAccess.deselectAll')).click();
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
    expect(sessionRead.getAttribute('aria-label')).toBe('session_read');
    expect(sessionRead.getAttribute('aria-checked')).toBe('true');
    expect(
      document.getElementById(sessionRead.getAttribute('aria-describedby'))
        .textContent,
    ).toBe(t('toolAccess.automatic'));
    expect(toolChip('read').textContent.trim()).toBe('read');
    const readTip = toolTipWithText('Read a file from disk.');
    expect(readTip.textContent).toContain('Read a file from disk.');
    // Every card names the Tool's source and how access works.
    expect(readTip.textContent).toContain(t('toolAccess.source.builtIn'));
    expect(readTip.textContent).toContain(t('toolAccess.access.selected'));
    // An Extension Tool names its Extension by id.
    expect(
      toolTipWithText('ha_get_state').querySelector('.tool-access-facts code')
        .textContent,
    ).toBe('homeassistant');
    expect(readTip.dataset.floatingOpen).toBe('false');

    vi.useFakeTimers();
    toolChip('read')
      .closest('.tool-access-chip-wrap')
      .dispatchEvent(new Event('pointerenter'));
    vi.advanceTimersByTime(HOVER_CARD_SHOW_DELAY_MS);
    vi.useRealTimers();
    expect(readTip.dataset.floatingOpen).toBe('true');

    expect(document.body.textContent).toContain(
      t('toolAccess.activation.memoryOff'),
    );
    expect(buttonByAriaLabel('Available with vision').disabled).toBe(true);
    expect(document.body.textContent).toContain(
      t('toolAccess.family.individual'),
    );
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

  it.each([
    ['All Files Tools', ['read'], ['read', 'write']],
    // An Extension-declared family is labelled by its declared name.
    ['All Home Assistant Tools', [], ['ha_call_service', 'ha_get_state']],
  ])(
    'uses one family checkbox (%s) for every member',
    (familyLabel, allowed, expected) => {
      const onChange = vi.fn();
      mountedComponent = mount(ToolAccessEditor, {
        target: document.body,
        props: { value: { mode: 'selected', allowed }, tools, onChange },
      });
      flushSync();

      buttonByAriaLabel(familyLabel).click();
      expect(onChange).toHaveBeenCalledWith({
        mode: 'selected',
        allowed: expected,
      });
    },
  );

  const liveCallTools = ['send_message', 'end_call'].map((name) => ({
    name,
    family: 'live',
    activation: 'configurable',
    requires_opt_in: true,
    constraints: ['live_call'],
    ready: true,
  }));

  it.each([
    // Only the Live voice Agents list the Live call Tools; a stored one is
    // not a missing Tool elsewhere.
    [false, ['read', 'session_read', 'memory'], ['send_message', 'end_call']],
    // Their policy is their whole Tool set: no automatic Tool is listed.
    [true, ['read', 'send_message', 'end_call'], ['session_read', 'memory']],
  ])(
    'lists the Tools its Agent can use (liveCall: %s)',
    (liveCall, shown, hidden) => {
      mountedComponent = mount(ToolAccessEditor, {
        target: document.body,
        props: {
          value: {
            mode: 'selected',
            allowed: ['read', 'send_message'],
            granted: ['send_message'],
          },
          tools: [...tools, ...liveCallTools],
          liveCall,
        },
      });
      flushSync();
      const names = [...document.querySelectorAll('[data-tool-name]')].map(
        (tool) => tool.dataset.toolName,
      );
      expect(names).toEqual(expect.arrayContaining(shown));
      for (const name of hidden) expect(names).not.toContain(name);
      expect(
        document.body.textContent.includes(t('toolAccess.family.live')),
      ).toBe(liveCall);
    },
  );

  it('filters live by name, description and family without changing hidden permissions', () => {
    const onChange = vi.fn();
    mountedComponent = mount(ToolAccessEditor, {
      target: document.body,
      props: { value: { mode: 'all' }, tools, onChange },
    });
    flushSync();
    const search = document.querySelector('input[type="search"]');
    const visibleTools = (query) => {
      search.value = query;
      search.dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();
      return [...document.querySelectorAll('[data-tool-name]')].map(
        (tool) => tool.dataset.toolName,
      );
    };

    expect(visibleTools('session')).toEqual(['session_read', 'session_search']);
    expect(visibleTools('from disk')).toEqual(['read']);
    // The family checkbox only changes the visible members.
    buttonByAriaLabel('All Files Tools').click();
    expect(onChange).toHaveBeenLastCalledWith({
      mode: 'all',
      denied: ['read'],
    });
    expect(visibleTools('Home Assistant')).toEqual([
      'ha_call_service',
      'ha_get_state',
    ]);

    const bulkAction = buttonWithText(t('toolAccess.selectAll'));
    bulkAction.focus();
    expect(document.activeElement).toBe(bulkAction);
    expect(document.querySelector('[role="radiogroup"]')).toBeNull();
  });

  describe('On-demand Tools', () => {
    const loadingTools = [
      ...tools.map((tool) =>
        ['read', 'write'].includes(tool.name)
          ? { ...tool, loaded_by_default: true }
          : tool,
      ),
      { name: 'message_parent', activation: 'session_grant', ready: true },
    ];

    function mountLoading(props) {
      mountedComponent = mount(ToolAccessEditor, {
        target: document.body,
        props: {
          value: { mode: 'all', denied: ['generate_image'] },
          tools: loadingTools,
          ...props,
        },
      });
      flushSync();
    }

    const pins = () => [
      ...document.querySelectorAll('[data-tool-always-loaded]'),
    ];
    const pinNames = (pressed) =>
      pins()
        .filter((pin) => pin.getAttribute('aria-pressed') === String(pressed))
        .map((pin) => pin.dataset.toolAlwaysLoaded);
    const loadingSwitch = () =>
      document.querySelector('[data-tool-loading-switch]');

    it.each([
      [false, null],
      [true, { on_demand: true }],
    ])(
      'offers the switch only to editors that enable it (%s)',
      (toolLoadingEditable, expected) => {
        const onToolLoadingChange = vi.fn();
        mountLoading({
          toolLoadingEditable,
          toolLoading: null,
          onToolLoadingChange,
        });
        expect(pins()).toEqual([]);
        if (!toolLoadingEditable) {
          expect(loadingSwitch()).toBeNull();
          return;
        }
        expect(loadingSwitch().getAttribute('aria-checked')).toBe('false');
        loadingSwitch().click();
        expect(onToolLoadingChange).toHaveBeenCalledWith(expected);
      },
    );

    // The stored value stays for when Tools are allowed again.
    it('hides the switch and pins without Tool access and keeps the stored value', async () => {
      const onChange = vi.fn();
      const onToolLoadingChange = vi.fn();
      const props = {
        toolLoadingEditable: true,
        toolLoading: { on_demand: true, always_loaded: ['read'] },
        onChange,
        onToolLoadingChange,
      };
      mountLoading(props);
      expect(loadingSwitch()).toBeTruthy();
      buttonWithText(t('toolAccess.deselectAll')).click();
      expect(onChange).toHaveBeenLastCalledWith({ mode: 'none' });

      await unmount(mountedComponent);
      mountLoading({ ...props, value: { mode: 'none' } });
      expect(loadingSwitch()).toBeNull();
      expect(pins()).toEqual([]);
      toolChip('read').click();
      expect(onChange.mock.calls.at(-1)[0]).toMatchObject({
        mode: 'selected',
        allowed: ['read'],
      });
      expect(onToolLoadingChange).not.toHaveBeenCalled();
    });

    it('shows the default set and writes the explicit list on the first change', () => {
      const onToolLoadingChange = vi.fn();
      mountLoading({
        toolLoadingEditable: true,
        toolLoading: { on_demand: true },
        onToolLoadingChange,
      });

      expect(pinNames(true)).toEqual(['read', 'write']);
      // Disallowed and session-granted Tools are listed but offer no choice.
      for (const name of ['generate_image', 'message_parent']) {
        expect(toolChip(name)).toBeTruthy();
        expect(pinNames(false)).not.toContain(name);
      }
      expect(
        document
          .querySelector('[data-tool-loading-summary]')
          .textContent.trim(),
      ).toBe(
        t('toolAccess.alwaysLoaded.summary', {
          alwaysLoaded: 2,
          onDemand: pinNames(false).length,
        }),
      );
      expect(
        [...document.querySelectorAll('button')].some(
          (button) =>
            button.textContent.trim() === t('toolAccess.alwaysLoaded.reset'),
        ),
      ).toBe(false);

      document
        .querySelector('[data-tool-always-loaded="session_search"]')
        .click();
      expect(onToolLoadingChange).toHaveBeenCalledWith({
        on_demand: true,
        always_loaded: ['read', 'write', 'session_search'],
      });
    });

    it('resets an explicit list and keeps it while switched off', () => {
      const onToolLoadingChange = vi.fn();
      const toolLoading = { on_demand: true, always_loaded: ['memory'] };
      mountLoading({
        toolLoadingEditable: true,
        toolLoading,
        onToolLoadingChange,
      });
      expect(pinNames(true)).toEqual(['memory']);

      buttonWithText(t('toolAccess.alwaysLoaded.reset')).click();
      expect(onToolLoadingChange).toHaveBeenLastCalledWith({ on_demand: true });
      loadingSwitch().click();
      expect(onToolLoadingChange).toHaveBeenLastCalledWith({
        on_demand: false,
        always_loaded: ['memory'],
      });
    });
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
