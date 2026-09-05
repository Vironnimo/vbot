// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../i18n.js';
import { createSettingsPayload } from './settingsView.support.js';
import { rpcBackedApiMock } from '../../components/__tests__/apiMock.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    listClients: () => Promise.resolve({ clients: [] }),
    getTaskModelOptions: (taskType, target) =>
      rpcMock('task_model.options', { task_type: taskType, target }),
    listTaskModelTargets: (taskType) =>
      rpcMock('task_model.list_targets', { task_type: taskType }),
    updateTaskModelSettings: (modelTasks) =>
      rpcMock('task_model.update', { model_tasks: modelTasks }),
  }),
);

const { default: SettingsView } =
  await import('../../components/SettingsView.svelte');

describe('SettingsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    mountedComponent = null;
    activeSection = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('renders the desktop split layout, loads settings data, and keeps token-count controls absent', async () => {
    rpcMock.mockResolvedValue(createSettingsPayload());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();

    const root = document.body.querySelector(
      'section.settings-layout.view.active',
    );
    expect(root).not.toBeNull();
    expect(root?.firstElementChild?.classList.contains('settings-nav')).toBe(
      true,
    );
    expect(root?.firstElementChild?.classList.contains('secondary-pane')).toBe(
      true,
    );
    expect(root?.lastElementChild?.classList.contains('settings-content')).toBe(
      true,
    );
    expect(document.querySelector('.s-doc > .banner--neutral')).toBeTruthy();

    // Providers is the default panel, so its content is the settings-loaded
    // signal now that General is no longer shown first.
    await waitForText('Add provider');

    expect(rpcMock).toHaveBeenCalledWith('settings.get');
    expect(document.body.textContent).toContain('OpenAI');
    expect(
      document.querySelector('.s-provider-card .chip.success'),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain('Anthropic');
    expect(document.body.textContent).toContain('Add provider');

    clickButton('Server info');

    expect(document.body.textContent).toContain('0.0.0.0:9001');
    expect(document.body.textContent).toContain('C:/Users/test/.vbot');
    expect(document.body.textContent).not.toMatch(
      /show[_ -]?token[_ -]?counts/i,
    );
    expect(document.body.textContent).not.toMatch(/token count/i);
  });

  it('uses the compact section picker to open one Settings topic', async () => {
    rpcMock.mockResolvedValue(createSettingsPayload());
    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await waitForText('Add provider');

    const scrollContainer = document.querySelector('.settings-content');
    const scrollTo = vi.fn();
    scrollContainer.scrollTo = scrollTo;

    const picker = document.querySelector('#settings-mobile-section');
    expect(picker).not.toBeNull();
    expect(picker.textContent).toContain('Providers');

    picker.click();
    flushSync();

    const appearanceOption = Array.from(
      document.body.querySelectorAll('.dropdown-option'),
    ).find((option) => option.textContent.includes('Appearance'));
    expect(appearanceOption).toBeTruthy();

    appearanceOption.click();
    flushSync();

    expect(picker.textContent).toContain('Appearance');
    expect(
      document.querySelector('[data-settings-section="appearance"]').hidden,
    ).toBe(false);
    expect(
      document.querySelector('[data-settings-section="providers"]').hidden,
    ).toBe(true);
  });

  it('keeps exactly one topic visible and does not change topics while scrolling', async () => {
    rpcMock.mockResolvedValue(createSettingsPayload());
    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await waitForText('Add provider');
    clickButton('Appearance');
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    const visibleTopics = () =>
      Array.from(document.querySelectorAll('[data-settings-section]')).filter(
        (section) => !section.hidden,
      );
    expect(
      visibleTopics().map((section) => section.dataset.settingsSection),
    ).toEqual(['appearance']);
    const scrollContainer = document.querySelector('.settings-content');
    scrollContainer.scrollTop = 400;
    scrollContainer.dispatchEvent(new Event('scroll'));
    flushSync();
    expect(
      visibleTopics().map((section) => section.dataset.settingsSection),
    ).toEqual(['appearance']);
    expect(document.activeElement.id).toBe('settings-section-appearance');
  });

  it('finds settings across hidden topics with spacing-insensitive search and preserves drafts', async () => {
    rpcMock.mockImplementation(createSettingsRpcHandler());
    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await waitForText('Add provider');
    clickButton('Agent defaults');
    const input = document.querySelector('#settings-defaults-temperature');
    input.value = '0.73';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    const search = document.querySelector('input[type="search"]');
    search.value = 'timezone';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    const results = document.querySelectorAll('.settings-search-result');
    expect(results).toHaveLength(1);
    results[0].click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    expect(
      document.querySelector('[data-settings-section="preferences"]').hidden,
    ).toBe(false);
    expect(
      document.querySelector('[data-settings-section="general"]').hidden,
    ).toBe(true);
    clickButton('Agent defaults');
    expect(document.querySelector('#settings-defaults-temperature')).toBe(
      input,
    );
    expect(input.value).toBe('0.73');
  });

  it('finds connected Providers while another topic is selected', async () => {
    rpcMock.mockResolvedValue(createSettingsPayload());
    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await waitForText('Add provider');
    clickButton('Appearance');
    const search = document.querySelector('input[type="search"]');
    search.value = 'OpenAI';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(
      Array.from(document.querySelectorAll('.settings-search-result')).map(
        (result) => result.textContent,
      ),
    ).toContainEqual(expect.stringContaining('Providers'));
  });

  it('shows an empty search state and can navigate out of it', async () => {
    rpcMock.mockResolvedValue(createSettingsPayload());
    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await waitForText('Add provider');
    const search = document.querySelector('input[type="search"]');
    search.value = 'no-such-setting-sentinel';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(
      document.querySelector('.settings-search-results .empty-state'),
    ).toBeTruthy();
    clickButton('Providers');
    flushSync();
    expect(search.value).toBe('');
    expect(
      document.querySelector('[data-settings-section="providers"]').hidden,
    ).toBe(false);
  });

  it('edits and saves sub-agent settings', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(
      createSettingsRpcHandler({
        'settings.update': (params) =>
          createSettingsPayload({
            subagents: params.subagents,
          }),
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForText('Add provider');
    openSection('Sub-Agents', 'subagents');

    const inputs = activeSection.querySelectorAll('input.s-input');
    expect(inputs).toHaveLength(3);
    expect(inputs[0].value).toBe('4');
    expect(inputs[1].value).toBe('8');
    expect(inputs[2].value).toBe('60');

    inputs[0].value = '5';
    inputs[0].dispatchEvent(new Event('input', { bubbles: true }));
    inputs[1].value = '12';
    inputs[1].dispatchEvent(new Event('input', { bubbles: true }));
    inputs[2].value = '45';
    inputs[2].dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    clickButton('Save');

    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      subagents: {
        max_subagent_depth: 5,
        max_subagents_per_turn: 12,
        subagent_timeout_minutes: 45,
      },
    });

    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'success',
      }),
    );
  });

  it('selects and saves the recall backend', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(
      createSettingsRpcHandler({
        'settings.update': (params) =>
          createSettingsPayload({
            recall: {
              backend: params.recall.backend,
              available_backends: ['canonical_scan', 'sqlite_fts'],
            },
          }),
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForText('Add provider');
    openSection('Recall', 'recall');

    const trigger = document.body.querySelector('#settings-recall-backend');
    expect(trigger).not.toBeNull();
    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const sqliteOption = Array.from(
      document.body.querySelectorAll('.dropdown-option'),
    ).find(
      (option) =>
        option.textContent.trim() ===
        'Full-text search — fast keyword search with an index',
    );
    expect(sqliteOption).toBeTruthy();
    sqliteOption.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    clickButton('Save');

    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      recall: {
        backend: 'sqlite_fts',
      },
    });

    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'success',
      }),
    );
  });

  it('selects and saves the web search provider', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(
      createSettingsRpcHandler({
        'settings.update': (params) =>
          createSettingsPayload({
            web_search: {
              provider: params.web_search.provider,
              available_providers: ['brave', 'searxng'],
              searxng: params.web_search.searxng,
            },
          }),
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForText('Add provider');
    openSection('Web Search', 'web_search');

    const trigger = document.body.querySelector(
      '#settings-web-search-provider',
    );
    expect(trigger).not.toBeNull();
    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const searxngOption = Array.from(
      document.body.querySelectorAll('.dropdown-option'),
    ).find((option) => option.textContent.trim() === 'SearXNG');
    expect(searxngOption).toBeTruthy();
    searxngOption.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const baseUrlInput = document.body.querySelector(
      '#settings-web-search-searxng-base-url',
    );
    expect(baseUrlInput).not.toBeNull();
    baseUrlInput.value = 'http://localhost:9999';
    baseUrlInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    clickButton('Save');

    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      web_search: {
        provider: 'searxng',
        default_count: 12,
        searxng: {
          base_url: 'http://localhost:9999',
        },
      },
    });

    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'success',
      }),
    );
  });

  it('enables automatic Session titles and saves a separate Title Model', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(
      createSettingsRpcHandler({
        'model.list': () => ({
          models: [
            {
              id: 'openai/gpt-4.1-mini',
              provider_id: 'openai',
              context_window: 128000,
              capabilities: { tools: true },
            },
          ],
        }),
        'connection.list': () => ({
          connections: [
            {
              id: 'openai:api-key',
              provider_id: 'openai',
              label: 'API Key',
              usable: true,
              accounts: [],
            },
          ],
        }),
        'settings.update': (params) =>
          createSettingsPayload({ session_titles: params.session_titles }),
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForText('Add provider');
    openSection('Session titles', 'session_titles');

    const toggle = activeSection.querySelector(
      'button[role="switch"][aria-label="Automatic Session titles"]',
    );
    expect(toggle).not.toBeNull();
    toggle.click();
    flushSync();

    const modelTrigger = activeSection.querySelector(
      '#settings-session-title-model',
    );
    expect(modelTrigger).not.toBeNull();
    expect(modelTrigger.disabled).toBe(false);
    modelTrigger.click();
    flushSync();

    await waitForCondition(
      () =>
        document.body.querySelectorAll('.searchable-dropdown__option').length >
        1,
    );

    const modelOption = Array.from(
      document.body.querySelectorAll('.searchable-dropdown__option'),
    ).find((option) => option.textContent.includes('openai/gpt-4.1-mini'));
    expect(modelOption).toBeTruthy();
    modelOption.click();
    flushSync();

    clickButton('Save');

    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      session_titles: {
        enabled: true,
        model: 'openai/gpt-4.1-mini::api-key',
      },
    });
    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'success',
      }),
    );
  });

  it('renders load failures and retries settings.get successfully', async () => {
    rpcMock
      .mockRejectedValueOnce(new Error('server offline'))
      .mockResolvedValueOnce(createSettingsPayload());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();

    await waitForCondition(() => document.querySelector('.banner--error'));
    expect(document.querySelector('.banner--error')?.textContent).toContain(
      'server offline',
    );

    clickButton('Retry');

    expect(rpcMock).toHaveBeenNthCalledWith(1, 'settings.get');
    expect(rpcMock).toHaveBeenNthCalledWith(2, 'settings.get');

    // A successful retry lands on the default Providers panel.
    await waitForText('Add provider');

    expect(document.body.textContent).toContain('Add provider');
    expect(document.body.textContent).not.toContain('server offline');
  });

  it('keeps appearance save enabled and persists language through settings.update', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(
      createSettingsRpcHandler({
        'settings.update': () =>
          createSettingsPayload({
            appearance: {
              language: 'fr',
              available_languages: ['en', 'fr'],
            },
          }),
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForText('Add provider');

    openSection('Appearance', 'appearance');

    const saveButton = getButton('Save');
    const languageTrigger = document.body.querySelector(
      '#settings-appearance-language',
    );

    expect(languageTrigger).not.toBeNull();
    expect(saveButton.disabled).toBe(false);

    saveButton.click();
    flushSync();

    // Save on a clean form must not write anything.
    expect(settingsUpdateCalls()).toHaveLength(0);

    languageTrigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const frenchOption = Array.from(
      document.body.querySelectorAll('.dropdown-option'),
    ).find((option) => option.textContent.trim() === 'fr');
    expect(frenchOption).toBeTruthy();
    frenchOption.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    getButton('Save').click();
    flushSync();

    expect(rpcMock).toHaveBeenCalledWith('settings.get');
    expect(settingsUpdateCalls()).toEqual([
      [
        'settings.update',
        {
          appearance: {
            language: 'fr',
            chat_width: 'comfortable',
            chat_working_mode: 'normal',
          },
        },
      ],
    ]);

    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: 'success',
      }),
    );
    expect(document.querySelector('.banner--success')).toBeNull();
    expect(
      document
        .querySelector(
          '#settings-appearance-language .dropdown-primitive__trigger-label',
        )
        ?.textContent.trim(),
    ).toBe('fr');
    expect(saveButton.disabled).toBe(false);
  });

  it('persists the application timezone from General settings', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcHandler({
        'settings.update': () =>
          createSettingsPayload({ general: { timezone: 'Europe/Berlin' } }),
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await waitForText('Add provider');
    openSection('General', 'preferences');

    document
      .getElementById('settings-general-timezone')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    const berlinOption = Array.from(
      document.body.querySelectorAll('.searchable-dropdown__option'),
    ).find((option) => option.textContent.trim() === 'Europe/Berlin');
    berlinOption.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    await waitForCondition(() => settingsUpdateCalls().length === 1);
    expect(settingsUpdateCalls()).toEqual([
      ['settings.update', { server: { timezone: 'Europe/Berlin' } }],
    ]);
  });
});

let activeSection = null;

function openSection(navLabel, sectionId) {
  activeSection = null;
  clickButton(navLabel);
  activeSection = document.body.querySelector(
    `[data-settings-section="${sectionId}"]`,
  );

  if (!activeSection) {
    throw new Error(`Settings section not found: ${sectionId}`);
  }
}

function getButton(label) {
  const root = activeSection ?? document.body;
  const button =
    Array.from(root.querySelectorAll('button')).find(
      (candidate) => candidate.textContent?.trim() === label,
    ) ??
    Array.from(document.body.querySelectorAll('button')).find(
      (candidate) => candidate.textContent?.trim() === label,
    );

  if (!button) {
    throw new Error(`Button not found: ${label}`);
  }

  return button;
}

function clickButton(label) {
  getButton(label).click();
  flushSync();
}

function settingsUpdateCalls() {
  return rpcMock.mock.calls.filter((call) => call[0] === 'settings.update');
}

// Method-routed RPC handler: every always-mounted section self-loads on
// mount, so a call-order mock would be consumed by whichever panel fires
// first. Overrides map method name → handler(params).
function createSettingsRpcHandler(overrides = {}) {
  return async (method, params) => {
    if (typeof overrides[method] === 'function') {
      return overrides[method](params);
    }

    switch (method) {
      case 'agent.list':
        return { agents: [] };
      case 'skill.read':
        return { skills: [] };
      case 'channel.list':
        return { channels: [] };
      case 'extensions.list':
        return { extensions: [] };
      case 'model.list':
        return { models: [] };
      case 'connection.list':
        return { connections: [] };
      case 'task_model.list_targets':
        return { targets: [] };
      default:
        return createSettingsPayload();
    }
  };
}

async function waitForText(text, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    flushSync();

    if (document.body.textContent?.includes(text)) {
      return;
    }
  }

  throw new Error(`Timed out waiting for text: ${text}`);
}

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    flushSync();

    if (check()) {
      return;
    }
  }

  throw new Error('Timed out waiting for condition.');
}
