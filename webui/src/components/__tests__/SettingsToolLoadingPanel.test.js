// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { t } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import {
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  getButton,
  getSettingsUpdateCalls,
  resetSettingsViewHarness,
  rpcMock,
  settingsPayload,
  waitForCondition,
} from './SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: SettingsToolLoadingPanel } =
  await import('../settings/SettingsToolLoadingPanel.svelte');

const TOOLS = [
  { name: 'read', activation: 'configurable', loaded_by_default: true },
  { name: 'bash', activation: 'configurable', loaded_by_default: true },
  { name: 'web_fetch', activation: 'configurable', loaded_by_default: false },
  { name: 'message_parent', activation: 'session_grant' },
];

describe('SettingsToolLoadingPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    rpcMock.mockImplementation(createSettingsRpcMock());
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  // The host (AgentsView) passes each committed response back as settings.
  function mountPanel() {
    const props = reactiveProps({ settings: settingsPayload(), tools: TOOLS });
    props.onCommit = (next) => {
      props.settings = next;
    };
    mountedComponent = mount(SettingsToolLoadingPanel, {
      target: document.body,
      props,
    });
    flushSync();
  }

  async function saveCall(index) {
    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length > index);
    return getSettingsUpdateCalls()[index][1].defaults.agent.tool_loading;
  }

  const pin = (name) => document.querySelector(`[data-tool-name="${name}"]`);
  const checked = () =>
    TOOLS.map((tool) => tool.name).filter(
      (name) => pin(name)?.getAttribute('aria-checked') === 'true',
    );
  const onDemandSwitch = () =>
    document.querySelector(
      `[role="switch"][aria-label="${t('toolAccess.onDemand.label')}"]`,
    );

  // Starts from the built-in list, writes the whole list on the first change,
  // and removes each part again: off is the built-in default.
  it('stores the shared switch and list and removes them again', async () => {
    mountPanel();
    // Session-granted Tools offer no Always loaded choice.
    expect(pin('message_parent')).toBeNull();
    expect(checked()).toEqual(['read', 'bash']);

    onDemandSwitch().click();
    flushSync();
    expect(await saveCall(0)).toEqual({ on_demand: true });

    pin('web_fetch').click();
    flushSync();
    expect(await saveCall(1)).toEqual({
      on_demand: true,
      always_loaded: ['read', 'bash', 'web_fetch'],
    });

    getButton(t('toolAccess.alwaysLoaded.reset')).click();
    flushSync();
    expect(await saveCall(2)).toEqual({ on_demand: true });

    onDemandSwitch().click();
    flushSync();
    expect(await saveCall(3)).toBeNull();
  });
});
