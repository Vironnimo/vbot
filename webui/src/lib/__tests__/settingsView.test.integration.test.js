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
    expect(document.body.textContent).toContain('Loading settings…');

    // Providers is the default panel, so its content is the settings-loaded
    // signal now that General is no longer shown first.
    await waitForText('Add provider');

    expect(rpcMock).toHaveBeenCalledWith('settings.get');
    expect(document.body.textContent).toContain('OpenAI');
    expect(document.body.textContent).toContain('Connected');
    expect(document.body.textContent).not.toContain('Anthropic');
    expect(document.body.textContent).toContain('Add provider');
    expect(document.body.textContent).toContain('Session titles');

    clickButton('Server info');

    expect(document.body.textContent).toContain('Server host');
    expect(document.body.textContent).toContain('0.0.0.0:9001');
    expect(document.body.textContent).toContain('Data directory');
    expect(document.body.textContent).toContain('C:/Users/test/.vbot');
    expect(document.body.textContent).not.toMatch(
      /show[_ -]?token[_ -]?counts/i,
    );
    expect(document.body.textContent).not.toMatch(/token count/i);
  });

  it('uses the compact section picker to navigate the mobile Settings document', async () => {
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
    expect(scrollTo).toHaveBeenCalledWith({
      behavior: 'smooth',
      top: 0,
    });
  });

  it('shares an upper-third reading line between click navigation, scrollspy, and the document tail', async () => {
    rpcMock.mockResolvedValue(createSettingsPayload());
    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await waitForText('Add provider');

    const scrollContainer = document.querySelector('.settings-content');
    const appearanceSection = document.querySelector(
      '[data-settings-section="appearance"]',
    );
    Object.defineProperties(scrollContainer, {
      clientHeight: { configurable: true, value: 1000 },
      scrollHeight: { configurable: true, value: 4000 },
      scrollTop: { configurable: true, value: 400, writable: true },
    });
    scrollContainer.getBoundingClientRect = () => ({ top: 100 });
    appearanceSection.getBoundingClientRect = () => ({ top: 900 });
    const scrollTo = vi.fn();
    scrollContainer.scrollTo = scrollTo;

    clickButton('Appearance');

    // 400 current + 800 section delta - 320 reading-line offset.
    expect(scrollTo).toHaveBeenCalledWith({
      behavior: 'smooth',
      top: 880,
    });

    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn(() => ({ matches: true }));
    try {
      clickButton('Appearance');
      expect(scrollTo).toHaveBeenLastCalledWith({
        behavior: 'auto',
        top: 880,
      });
    } finally {
      window.matchMedia = originalMatchMedia;
    }

    window.dispatchEvent(new Event('resize'));
    flushSync();
    expect(document.querySelector('.settings-scroll-tail').style.height).toBe(
      '680px',
    );

    const sections = Array.from(
      document.querySelectorAll('[data-settings-section]'),
    );
    sections.forEach((section, index) => {
      section.getBoundingClientRect = () => ({ top: 100 + index * 100 });
    });
    scrollContainer.dispatchEvent(new Event('wheel'));
    scrollContainer.dispatchEvent(new Event('scroll'));
    await new Promise((resolve) => setTimeout(resolve, 20));
    flushSync();

    const activeIndexItem = document.querySelector(
      '.settings-desktop-index .snav-item[aria-current="true"]',
    );
    expect(activeIndexItem.textContent.trim()).toBe('Agent defaults');

    const appearanceIndex = sections.indexOf(appearanceSection);
    sections.forEach((section, index) => {
      section.getBoundingClientRect = () => ({
        top:
          index < appearanceIndex
            ? -100
            : index === appearanceIndex
              ? 420.5
              : 1000,
      });
    });
    scrollContainer.dispatchEvent(new Event('scroll'));
    await new Promise((resolve) => setTimeout(resolve, 20));
    flushSync();

    expect(
      document
        .querySelector(
          '.settings-desktop-index .snav-item[aria-current="true"]',
        )
        .textContent.trim(),
    ).toBe('Appearance');
  });

  it('adds, removes, and saves skill directories', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(
      createSettingsRpcHandler({
        'settings.update': (params) =>
          createSettingsPayload({
            skills: {
              default_directory: 'C:/Users/test/.vbot/skills',
              directories: params.skills.directories,
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
    openSection('Skills', 'skills');

    expect(document.body.textContent).toContain('Default skill directory');
    expect(document.body.textContent).toContain('C:/Users/test/.vbot/skills');
    expect(document.body.textContent).toContain('C:/skills/shared');

    const input = activeSection.querySelector('input.s-input');
    expect(input).not.toBeNull();
    input.value = 'D:/skills/team';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    clickButton('Add directory');
    expect(document.body.textContent).toContain('D:/skills/team');

    clickButton('Remove');
    expect(document.body.textContent).not.toContain('C:/skills/shared');

    clickButton('Save');

    expect(rpcMock).toHaveBeenCalledWith('settings.update', {
      skills: {
        directories: ['D:/skills/team'],
      },
    });

    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Skill directories updated.',
        variant: 'success',
      }),
    );
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

    expect(document.body.textContent).toContain('Max sub-agent depth');
    expect(document.body.textContent).toContain('Max sub-agents per turn');
    expect(document.body.textContent).toContain('Timeout minutes');

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
        title: 'Sub-agent settings updated.',
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
              available_backends: ['jsonl_scan', 'sqlite_fts'],
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

    expect(document.body.textContent).toContain('Recall backend');

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
        title: 'Recall backend updated.',
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

    expect(document.body.textContent).toContain('Search provider');

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
        title: 'Web search settings updated.',
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
        title: 'Session title settings updated.',
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

    await waitForText('Settings could not be loaded. server offline');

    expect(document.body.textContent).toContain(
      'Settings could not be loaded. server offline',
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
          },
        },
      ],
    ]);

    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Appearance updated.',
        variant: 'success',
      }),
    );
    expect(document.body.textContent).not.toContain('Appearance updated.');
    expect(
      document
        .querySelector(
          '#settings-appearance-language .dropdown-primitive__trigger-label',
        )
        ?.textContent.trim(),
    ).toBe('fr');
    expect(saveButton.disabled).toBe(false);
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
