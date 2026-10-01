// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { t } from '../../lib/i18n.js';
import { createStandaloneNavigation } from '../../lib/navigation.svelte.js';
import {
  applyAppearanceSettings,
  appearancePrefs,
} from '../../lib/appearancePrefs.svelte.js';
import {
  agentsPayload,
  buttonByAriaLabel,
  buttonByText,
  channelConfig,
  cleanupSettingsViewHarness,
  createSettingsRpcMock,
  flushAsyncUpdates,
  getButton,
  getSettingsUpdateCalls,
  getSimpleTrigger,
  openRecallPanel,
  openSearchableDropdown,
  openSettingsSection,
  openSimpleDropdown,
  openWebSearchPanel,
  recallIndexStatusPayload,
  resetSettingsViewHarness,
  rpcMock,
  saveStateText,
  selectSearchableOption,
  selectSimpleOption,
  setInputValue,
  settingsPayload,
  waitForCondition,
} from './SettingsView.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

// Provider details open through a SvelteSet, which needs the client build.
vi.mock('svelte/reactivity', async () => {
  return import('../../../node_modules/svelte/src/reactivity/index-client.js');
});

const { default: SettingsView } = await import('../SettingsView.svelte');

function clickButton(label) {
  getButton(label).click();
  flushSync();
}

function search(query) {
  const input = document.querySelector('input[type="search"]');
  input.value = query;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  return input;
}

function isSectionHidden(sectionId) {
  return document.querySelector(`[data-settings-section="${sectionId}"]`)
    .hidden;
}

function searchResultRows() {
  return Array.from(
    document.querySelectorAll('.settings-search-result'),
    (result) => ({
      title: result.querySelector('.settings-search-result__title').textContent,
      location: result.querySelector('.settings-search-result__description')
        .textContent,
    }),
  );
}

function searchLocation(page, section) {
  return t('settings.search.location', { page, section });
}

async function openFirstSearchResult() {
  document.querySelector('.settings-search-result').click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

describe('SettingsView', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  async function mountSettings(options = {}, props = {}) {
    rpcMock.mockImplementation(createSettingsRpcMock(options));
    mountedComponent = mount(SettingsView, { target: document.body, props });
    flushSync();
    await waitForCondition(() => buttonByText('Add provider'));
  }

  describe('layout and navigation', () => {
    it('renders the split layout, loads settings, and keeps token-count controls absent', async () => {
      rpcMock.mockImplementation(createSettingsRpcMock());
      mountedComponent = mount(SettingsView, { target: document.body });
      flushSync();

      const root = document.body.querySelector(
        'section.settings-layout.view.active',
      );
      expect(root.firstElementChild.classList.contains('settings-nav')).toBe(
        true,
      );
      expect(root.firstElementChild.classList.contains('secondary-pane')).toBe(
        true,
      );
      expect(root.lastElementChild.classList.contains('settings-content')).toBe(
        true,
      );
      // The loading banner shows until settings.get answers.
      expect(document.querySelector('.s-doc > .banner--neutral')).toBeTruthy();
      await waitForCondition(() => buttonByText('Add provider'));
      expect(document.querySelector('.s-doc > .banner--neutral')).toBeNull();
      expect(rpcMock).toHaveBeenCalledWith('settings.get');
      // Editors stay mounted on other pages and load independently.
      expect(document.body.textContent).toContain('OpenAI');
      expect(
        document.querySelector('.s-provider-card .chip.success'),
      ).toBeTruthy();

      clickButton('System');
      expect(document.body.textContent).toContain('127.0.0.1:8420');
      expect(document.body.textContent).toContain('C:/data');
      expect(document.body.textContent).not.toMatch(
        /show[_ -]?token[_ -]?counts/i,
      );
      expect(document.body.textContent).not.toMatch(/token count/i);
    });

    it('renders load failures and retries settings.get successfully', async () => {
      rpcMock.mockImplementation(createSettingsRpcMock());
      rpcMock.mockRejectedValueOnce(new Error('server offline'));
      mountedComponent = mount(SettingsView, { target: document.body });
      flushSync();

      await waitForCondition(() => document.querySelector('.banner--error'));
      expect(document.querySelector('.banner--error').textContent).toContain(
        'server offline',
      );

      clickButton('Retry');
      expect(rpcMock).toHaveBeenNthCalledWith(1, 'settings.get');
      expect(rpcMock).toHaveBeenNthCalledWith(2, 'settings.get');
      await waitForCondition(() => buttonByText('Add provider'));
      expect(document.querySelector('.banner--error')).toBeNull();
    });

    it('uses the compact section picker to open one Settings topic', async () => {
      await mountSettings();
      document.querySelector('.settings-content').scrollTo = vi.fn();

      const picker = document.querySelector('#settings-mobile-section');
      expect(picker.textContent).toContain('General');
      picker.click();
      flushSync();
      Array.from(document.body.querySelectorAll('.dropdown-option'))
        .find((option) => option.textContent.includes('Voice'))
        .click();
      flushSync();

      expect(picker.textContent).toContain('Voice');
      expect(isSectionHidden('speech_models')).toBe(false);
      expect(isSectionHidden('providers')).toBe(true);
    });

    it('groups settings by purpose and keeps small controls out of page navigation', async () => {
      const navigate = vi.fn();
      const navigation = createStandaloneNavigation();
      await mountSettings(
        {},
        { navigation, onNavigateToAgentDefaults: navigate },
      );
      // The empty place shows the start page and records it.
      expect(navigation.place).toEqual(['general']);
      const visiblePages = () =>
        Array.from(document.querySelectorAll('[data-settings-page]'))
          .filter((page) => !page.hidden)
          .map((page) => page.dataset.settingsPage);
      expect(visiblePages()).toEqual(['general']);
      const buttons = document.querySelectorAll('.settings-nav .snav-item');
      expect(
        Array.from(buttons, (button) => button.textContent.trim()),
      ).toEqual([
        'General',
        'Providers',
        'Voice',
        'Memory',
        'Tools',
        'Integrations',
        'System',
      ]);
      const expectedSections = {
        general: [
          'appearance',
          'session_titles',
          'notifications',
          'preferences',
        ],
        voice: [
          'speech_models',
          'live_voice_model',
          'voice_controls',
          'transcription_audio',
        ],
        memory: ['reflection', 'recall'],
        tools: [
          'web_search',
          'web_fetch',
          'media_models',
          'decision_model',
          'subagents',
        ],
        integrations: ['channels', 'extensions'],
        system: ['server', 'debug'],
      };
      for (const [pageId, sectionIds] of Object.entries(expectedSections)) {
        const page = document.querySelector(`[data-settings-page="${pageId}"]`);
        expect(
          Array.from(
            page.querySelectorAll('[data-settings-section]'),
            (section) => section.dataset.settingsSection,
          ),
        ).toEqual(sectionIds);
      }
      // Task-specific Models sit beside their consumers, exactly once.
      for (const [task, pageId] of [
        ['speech_to_text', 'voice'],
        ['text_to_speech', 'voice'],
        ['live_voice', 'voice'],
        ['text_embedding', 'memory'],
        ['image_generation', 'tools'],
        ['decision', 'tools'],
      ]) {
        const inputs = document.querySelectorAll(
          '#settings-specialized-' + task,
        );
        expect(inputs).toHaveLength(1);
        expect(
          inputs[0].closest('[data-settings-page]').dataset.settingsPage,
        ).toBe(pageId);
      }
      for (const button of buttons) {
        button.click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        flushSync();
        expect(visiblePages()).toHaveLength(1);
        expect(navigation.place).toEqual(visiblePages());
        expect(document.activeElement.textContent).toBe(button.textContent);
        expect(button.getAttribute('aria-current')).toBe('page');
      }
      clickButton('Providers');
      document.querySelector('.settings-defaults-link').click();
      expect(navigate).toHaveBeenCalledWith('defaults');
      expect(document.querySelector('#settings-defaults-model')).toBeNull();
    });

    it('opens a section place inside its page and records opened sections as steps', async () => {
      rpcMock.mockImplementation(createSettingsRpcMock());
      // An App deep link names a bare section id.
      const navigation = createStandaloneNavigation(['decision_model']);
      const replace = vi.spyOn(navigation, 'replace');
      const navigate = vi.spyOn(navigation, 'navigate');
      mountedComponent = mount(SettingsView, {
        target: document.body,
        props: { navigation },
      });
      flushSync();
      await waitForCondition(
        () => document.activeElement?.id === 'settings-section-decision_model',
      );
      expect(
        document.querySelector('[data-settings-page="tools"]').hidden,
      ).toBe(false);
      expect(
        document.querySelector('.snav-item[aria-current="page"]').textContent,
      ).toBe('Tools');
      expect(isSectionHidden('web_search')).toBe(false);
      expect(replace).toHaveBeenCalledWith(['tools', 'decision_model']);
      expect(navigate).not.toHaveBeenCalled();

      search('timezone');
      document.querySelector('.settings-search-result').click();
      await new Promise((resolve) => setTimeout(resolve, 0));
      flushSync();
      expect(navigate).toHaveBeenLastCalledWith(['general', 'preferences']);
      expect(navigation.place).toEqual(['general', 'preferences']);
      // The found setting takes focus; revealing it is no further step.
      expect(document.activeElement.id).toBe('settings-general-timezone');
      expect(navigate).toHaveBeenCalledTimes(1);
    });

    it.each([
      ['resumes on its own page', ['tools'], 640],
      ['is not applied on another page', ['voice'], 0],
    ])('a kept reading position %s', async (_case, place, expectedTop) => {
      rpcMock.mockImplementation(createSettingsRpcMock());
      mountedComponent = mount(SettingsView, {
        target: document.body,
        props: {
          navigation: createStandaloneNavigation(place),
          initialScrollPosition: {
            top: 640,
            pageId: 'tools',
            place: ['tools'],
          },
        },
      });
      flushSync();
      await waitForCondition(() => buttonByText('Add provider'));
      const scrollport = document.querySelector('.settings-content');
      await new Promise((resolve) => setTimeout(resolve, 40));
      expect(scrollport.scrollTop).toBe(expectedTop);
      expect(
        document.querySelector('.snav-item[aria-current="page"]').textContent,
      ).toBe(place[0] === 'tools' ? 'Tools' : 'Voice');
    });

    it('keeps a deep-link destination in view as earlier content loads, until the user scrolls', async () => {
      let finishOptions;
      const options = new Promise((resolve) => {
        finishOptions = resolve;
      });
      const settings = settingsPayload();
      settings.model_tasks = {
        image_generation: { target: 'test/image', options: {} },
      };
      const backend = createSettingsRpcMock({ settings });
      rpcMock.mockImplementation((method, params) =>
        method === 'task_model.options' ? options : backend(method, params),
      );
      mountedComponent = mount(SettingsView, {
        target: document.body,
        props: {
          navigation: createStandaloneNavigation(['tools', 'decision_model']),
        },
      });
      flushSync();
      await waitForCondition(
        () => document.activeElement?.id === 'settings-section-decision_model',
      );
      const scrollport = document.querySelector('.settings-content');
      const heading = document.activeElement;
      let contentTop = 800;
      heading.getBoundingClientRect = () => ({
        top: contentTop - scrollport.scrollTop,
      });
      finishOptions({
        fields: [{ name: 'size', label: 'Size', type: 'text' }],
      });
      await waitForCondition(() => scrollport.scrollTop === 776, 40, 10);

      // Content moves again, but the user scrolls before the queued restore
      // frame runs; neither that frame nor later content changes move them.
      contentTop = 1000;
      heading.append(document.createTextNode(' '));
      await Promise.resolve();
      scrollport.dispatchEvent(new Event('wheel'));
      scrollport.scrollTop = 120;
      heading.append(document.createTextNode(' '));
      await new Promise((resolve) => setTimeout(resolve, 40));
      expect(scrollport.scrollTop).toBe(120);
    });
  });

  describe('search', () => {
    it('finds settings across hidden topics with spacing-insensitive search and preserves drafts', async () => {
      await mountSettings();
      clickButton('Tools');
      const input = document.querySelector(
        '#settings-web-search-default-count',
      );
      input.value = '9';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      flushSync();

      // One word matches the two-word label; the result names the setting
      // and where it lives.
      search('timezone');
      expect(searchResultRows()[0]).toEqual({
        title: t('settings.general.timezone'),
        location: searchLocation(
          t('settings.pages.general'),
          t('settings.preferences.title'),
        ),
      });
      await openFirstSearchResult();
      expect(isSectionHidden('preferences')).toBe(false);
      expect(isSectionHidden('server')).toBe(true);
      expect(document.activeElement.id).toBe('settings-general-timezone');

      clickButton('Tools');
      expect(document.querySelector('#settings-web-search-default-count')).toBe(
        input,
      );
      expect(input.value).toBe('9');
    });

    it('matches explanations behind a closed help hint and opens a setting hidden while off at its switch', async () => {
      const navigation = createStandaloneNavigation();
      await mountSettings({}, { navigation });

      search('sleep');
      expect(searchResultRows()).toEqual([
        {
          title: t('settings.general.keepAwake'),
          location: searchLocation(
            t('settings.pages.system'),
            t('settings.sections.server'),
          ),
        },
      ]);

      // Debug is off, so its trace limit row is hidden. Enter opens the first
      // result at the switch that reveals it.
      const input = search('trace limit');
      expect(searchResultRows()[0]).toEqual({
        title: t('debug.traceLimit'),
        location: searchLocation(
          t('settings.pages.system'),
          t('debug.settings'),
        ),
      });
      input.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
      flushSync();
      expect(navigation.place).toEqual(['system', 'debug']);
      expect(input.value).toBe('');
      const debugSwitch = document.querySelector(
        `button[role="switch"][aria-label="${t('debug.enabled')}"]`,
      );
      expect(document.activeElement).toBe(debugSwitch);
      expect(
        debugSwitch.closest('.s-row').classList.contains('settings-search-hit'),
      ).toBe(true);
      expect(
        document.querySelector('#settings-debug-trace-limit').closest('.s-row')
          .hidden,
      ).toBe(true);
    });

    it('ranks label matches above help and content matches', async () => {
      await mountSettings();
      search('trace');
      const titles = searchResultRows().map((row) => row.title);
      // Document order puts the Debug switch, whose help mentions traces,
      // before the Trace limit.
      expect(titles[0]).toBe(t('debug.traceLimit'));
      expect(titles).toContain(t('debug.enabled'));
    });

    it('opens an entity row when the match lies in its collapsed details', async () => {
      await mountSettings();
      // The endpoint is shown only in the Provider's details.
      search('openai example');
      expect(searchResultRows()).toEqual([
        { title: 'OpenAI', location: t('settings.providers.title') },
      ]);
      await openFirstSearchResult();
      const chevron = buttonByAriaLabel(
        t('settings.providers.detailsAria', { id: 'openai' }),
      );
      expect(chevron.getAttribute('aria-expanded')).toBe('true');
      expect(
        document.getElementById(chevron.getAttribute('aria-controls')).hidden,
      ).toBe(false);
      expect(document.activeElement).toBe(chevron);
    });

    it('finds Channel settings that are rendered only while a Channel is edited', async () => {
      const navigation = createStandaloneNavigation();
      await mountSettings({ channels: [channelConfig('ops')] }, { navigation });
      await waitForCondition(() => document.querySelector('.s-channel-list'));
      const dmScope = {
        title: t('settings.channels.dm_scope'),
        location: searchLocation(
          t('settings.pages.integrations'),
          t('settings.channels.title'),
        ),
      };

      search('DM scope');
      expect(searchResultRows()).toEqual([dmScope]);
      await openFirstSearchResult();
      expect(navigation.place).toEqual(['integrations', 'channels']);
      const list = document.querySelector('.s-channel-list');
      expect(list.classList.contains('settings-search-hit')).toBe(true);
      expect(list.contains(document.activeElement)).toBe(true);

      // An open edit form renders the field, which takes the declaration's
      // place.
      buttonByAriaLabel(t('settings.channels.edit', { id: 'ops' })).click();
      await waitForCondition(() =>
        document.querySelector('#channel-dm-scope-select'),
      );
      search('DM scope');
      expect(searchResultRows()).toEqual([dmScope]);
      await openFirstSearchResult();
      expect(document.activeElement.id).toBe('channel-dm-scope-select');
    });

    it('routes shared Agent defaults search to Agents without duplicating its editor', async () => {
      const navigate = vi.fn();
      await mountSettings({}, { onNavigateToAgentDefaults: navigate });
      search('thinking');
      Array.from(document.querySelectorAll('.settings-search-result'))
        .find((item) => item.textContent.includes(t('agents.shared.title')))
        .click();
      expect(navigate).toHaveBeenCalledWith('defaults');
      expect(document.querySelector('#settings-defaults-model')).toBeNull();
    });

    it('finds connected Providers while another topic is selected', async () => {
      await mountSettings();
      clickButton('General');
      search('OpenAI');
      expect(
        Array.from(document.querySelectorAll('.settings-search-result')).map(
          (result) => result.textContent,
        ),
      ).toContainEqual(expect.stringContaining('Providers'));
    });

    it('shows an empty search state and can navigate out of it', async () => {
      await mountSettings();
      const input = search('no-such-setting-sentinel');
      expect(
        document.querySelector('.settings-search-results .empty-state'),
      ).toBeTruthy();
      clickButton('Providers');
      expect(input.value).toBe('');
      expect(isSectionHidden('providers')).toBe(false);
    });
  });

  describe('General page', () => {
    it('persists the language through the appearance save state', async () => {
      const settings = settingsPayload();
      settings.appearance.available_languages = ['en', 'fr'];
      await mountSettings({ settings });
      await openSettingsSection('General', 'appearance');

      openSimpleDropdown('settings-appearance-language');
      selectSimpleOption('settings-appearance-language', 'fr');
      getButton('Save').click();
      flushSync();

      expect(getSettingsUpdateCalls()).toEqual([
        [
          'settings.update',
          {
            appearance: {
              language: 'fr',
              chat_width: 'comfortable',
              chat_working_mode: 'normal',
            },
            base: {
              appearance: {
                language: 'en',
                chat_width: 'comfortable',
                chat_working_mode: 'normal',
              },
            },
          },
        ],
      ]);
      await waitForCondition(
        () => saveStateText('appearance') === t('common.saved'),
      );
      expect(document.querySelector('.banner--success')).toBeNull();
      expect(
        getSimpleTrigger('settings-appearance-language').textContent.trim(),
      ).toBe('fr');
    });

    describe('Chat appearance preferences', () => {
      beforeEach(() => {
        applyAppearanceSettings();
      });

      afterEach(() => {
        applyAppearanceSettings();
      });

      it('refreshes the global Chat preferences when Settings loads', async () => {
        const settings = settingsPayload();
        settings.appearance.chat_width = 'wide';
        settings.appearance.chat_working_mode = 'compact';
        await mountSettings({ settings });

        expect(appearancePrefs.chatWidth).toBe('wide');
        expect(appearancePrefs.chatWorkingMode).toBe('compact');
        expect(getSettingsUpdateCalls()).toHaveLength(0);
      });

      it('applies the saved response while preserving a newer draft after a failed follow-up save', async () => {
        let finishFirstSave;
        const onToast = vi.fn();
        const save = vi
          .fn()
          .mockImplementationOnce(
            (patch, currentSettings) =>
              new Promise((resolve) => {
                finishFirstSave = () =>
                  resolve({
                    ...currentSettings,
                    appearance: {
                      ...currentSettings.appearance,
                      ...patch.appearance,
                    },
                  });
              }),
          )
          .mockRejectedValueOnce(new Error('save unavailable'));
        await mountSettings({ settingsUpdate: save }, { onToast });
        vi.useFakeTimers();

        openSimpleDropdown('settings-appearance-chat-width');
        selectSimpleOption('settings-appearance-chat-width', 'Wide');
        await vi.advanceTimersByTimeAsync(800);
        await flushAsyncUpdates();
        expect(save).toHaveBeenCalledTimes(1);
        expect(appearancePrefs.chatWidth).toBe('comfortable');

        openSimpleDropdown('settings-appearance-chat-width');
        selectSimpleOption('settings-appearance-chat-width', 'Full width');
        finishFirstSave();
        await flushAsyncUpdates();
        expect(appearancePrefs.chatWidth).toBe('wide');

        await vi.advanceTimersByTimeAsync(800);
        await flushAsyncUpdates();
        expect(save).toHaveBeenCalledTimes(2);
        expect(getSettingsUpdateCalls()[1][1].appearance.chat_width).toBe(
          'full',
        );
        expect(appearancePrefs.chatWidth).toBe('wide');
        expect(
          getSimpleTrigger('settings-appearance-chat-width').textContent,
        ).toContain('Full');
        expect(onToast).toHaveBeenCalledWith(
          expect.objectContaining({ variant: 'error' }),
        );
      });
    });

    it('enables automatic Session titles and saves a separate Title Model', async () => {
      await mountSettings();
      await openSettingsSection('General', 'session_titles');

      // The Title model only appears while automatic titles are on.
      expect(
        document
          .querySelector('#settings-session-title-model')
          .closest('.s-row').hidden,
      ).toBe(true);
      document
        .querySelector(
          'button[role="switch"][aria-label="Automatic Session titles"]',
        )
        .click();
      flushSync();
      expect(
        document.querySelector('#settings-session-title-model').disabled,
      ).toBe(false);
      await openSearchableDropdown('settings-session-title-model');
      await waitForCondition(
        () =>
          document.body.querySelectorAll('.searchable-dropdown__option')
            .length > 1,
      );
      selectSearchableOption('settings-session-title-model', 'openai/gpt-5.2');
      clickButton('Save');

      expect(rpcMock).toHaveBeenCalledWith('settings.update', {
        session_titles: {
          enabled: true,
          model: 'openai/gpt-5.2::api-key',
        },
        base: { session_titles: { enabled: false, model: '' } },
      });
      await waitForCondition(
        () => saveStateText('session_titles') === t('common.saved'),
      );
    });

    it('reports a failed Session titles model catalog as an error toast', async () => {
      const toastMock = vi.fn();
      const backend = createSettingsRpcMock();
      rpcMock.mockImplementation((method, params) =>
        method === 'model.list'
          ? Promise.reject(new Error('catalog offline'))
          : backend(method, params),
      );
      mountedComponent = mount(SettingsView, {
        target: document.body,
        props: { onToast: toastMock },
      });
      flushSync();

      await waitForCondition(() => toastMock.mock.calls.length > 0);
      expect(toastMock).toHaveBeenCalledWith({
        title: t('errors.appError'),
        message: `${t('settings.models.loadError')} catalog offline`,
        variant: 'error',
      });
    });

    it('persists the application timezone from General settings', async () => {
      const settings = settingsPayload();
      settings.general.timezone = 'UTC';
      settings.general.available_timezones = [
        'America/New_York',
        'Europe/Berlin',
        'UTC',
      ];
      await mountSettings({ settings });
      await openSettingsSection('General', 'preferences');

      await openSearchableDropdown('settings-general-timezone');
      selectSearchableOption('settings-general-timezone', 'Europe/Berlin');

      await waitForCondition(() => getSettingsUpdateCalls().length === 1);
      expect(getSettingsUpdateCalls()).toEqual([
        ['settings.update', { server: { timezone: 'Europe/Berlin' } }],
      ]);
    });
  });

  describe('Voice page', () => {
    it('shows server-wide Voice settings but hides Desktop-only connection settings', async () => {
      await mountSettings();

      expect(
        document.querySelector('[data-settings-section="desktop_connection"]'),
      ).toBeNull();
      expect(
        document.querySelector('[data-settings-section="live_voice_shortcut"]'),
      ).toBeNull();
      expect(buttonByText('Voice')).toBeTruthy();
      expect(
        document.querySelector(
          '[data-settings-section="transcription_audio"] button[aria-label="Transcription audio profile"]',
        ),
      ).toBeTruthy();
      expect(
        document.querySelector(
          '[role="switch"][aria-label="Enable wakeword listening"]',
        ),
      ).toBeNull();
    });

    it('adds the Live voice shortcut to Voice when the Desktop supports it', async () => {
      rpcMock.mockImplementation(createSettingsRpcMock());
      window.history.pushState({}, '', '/?accessor=desktop');
      window.pywebview = {
        api: {
          getLiveHotkey: vi.fn().mockResolvedValue({
            supported: true,
            enabled: false,
            hotkey: {
              ctrl: true,
              alt: true,
              shift: false,
              win: false,
              key: 'Space',
            },
            error_code: null,
          }),
        },
      };

      mountedComponent = mount(SettingsView, {
        target: document.body,
        props: { desktopCapabilities: { liveHotkey: true } },
      });
      flushSync();

      await waitForCondition(
        () =>
          document
            .querySelector('.live-shortcut__capture')
            ?.textContent.trim() === 'Ctrl + Alt + Space',
      );
      const page = document.querySelector('[data-settings-page="voice"]');
      expect(
        Array.from(
          page.querySelectorAll('[data-settings-section]'),
          (section) => section.dataset.settingsSection,
        ),
      ).toEqual([
        'speech_models',
        'live_voice_model',
        'live_voice_shortcut',
        'voice_controls',
        'transcription_audio',
      ]);
    });

    it('opens the Voice page with its Desktop sections from a place naming it', async () => {
      rpcMock.mockImplementation(createSettingsRpcMock());
      window.history.pushState({}, '', '/?accessor=desktop');
      window.pywebview = {
        api: {
          listServers: vi.fn().mockResolvedValue([
            {
              host: 'pi.lan',
              port: 8420,
              label: 'Home',
              active: true,
            },
          ]),
          listMicrophones: vi.fn().mockResolvedValue([]),
          listWakewordModels: vi.fn().mockResolvedValue([]),
        },
      };

      mountedComponent = mount(SettingsView, {
        target: document.body,
        props: {
          agents: agentsPayload(),
          desktopCapabilities: {
            wakeword: true,
            voiceApi: 2,
            serverSelection: true,
          },
          desktopVoice: {
            available: true,
            status: {
              enabled: false,
              mode: 'real',
              state: 'off',
              error_code: null,
              sequence: 1,
              microphone: null,
              active_microphone: null,
              echo_cancellation: { enabled: true, state: 'off' },
              default_agent_id: null,
              default_session_behavior: 'active',
              phrases: [],
              recording: null,
              commands: [],
              calibration: null,
              limits: {
                max_active_phrases: 8,
                min_sensitivity: 0.05,
                max_sensitivity: 0.95,
              },
            },
            adopt: vi.fn(),
            refresh: vi.fn(),
          },
          navigation: createStandaloneNavigation(['voice']),
        },
      });
      flushSync();

      // The Voice section renders (desktop capability) and the place marks
      // its index entry active.
      await waitForCondition(
        () =>
          document.querySelector(
            '[role="switch"][aria-label="Enable wakeword listening"]',
          ) !== null,
      );
      expect(
        document.querySelector('[data-settings-section="desktop_connection"]'),
      ).toBeTruthy();
      await waitForCondition(
        () =>
          buttonByText('Voice')?.classList.contains('snav-item--active') ===
          true,
      );

      // Navigating elsewhere moves the index highlight; the Voice section stays
      // in the document (sections are never unmounted).
      buttonByText('System').click();
      flushSync();
      await waitForCondition(
        () =>
          buttonByText('System')?.classList.contains('snav-item--active') ===
          true,
      );
      expect(
        buttonByText('Voice')?.classList.contains('snav-item--active'),
      ).toBe(false);
      expect(
        document.querySelector(
          '[role="switch"][aria-label="Enable wakeword listening"]',
        ),
      ).toBeTruthy();
      expect(
        document.querySelector('.desktop-connection-settings'),
      ).toBeTruthy();
    });
  });

  describe('Memory and Tools pages', () => {
    const embeddingTargets = [
      {
        id: 'openai/text-embedding-3-small',
        label: 'OpenAI / Text Embedding 3 Small',
        kind: 'provider',
        provider_id: 'openai',
        task_types: ['text_embedding'],
        usable: true,
        facts: {
          local: false,
          multilingual: true,
          recommended_rank: 3,
          input_price_per_million: 0.02,
          note: 'Inexpensive general-purpose OpenAI model.',
        },
      },
      {
        id: 'local/granite-embedding-r2',
        label: 'Granite Embedding R2',
        kind: 'local',
        task_types: ['text_embedding'],
        usable: false,
        facts: { local: true, multilingual: true, recommended_rank: 1 },
        metadata: { license: 'Apache-2.0', download_bytes: 346_806_730 },
      },
      {
        id: 'openrouter/unranked-embed',
        label: 'OpenRouter / Unranked Embed',
        kind: 'provider',
        provider_id: 'openrouter',
        task_types: ['text_embedding'],
        usable: true,
        facts: { local: false, recommended_rank: null },
      },
    ];
    const embeddingOptions = {
      'openai/text-embedding-3-small': {
        fields: [{ name: 'dimensions', type: 'number', label: 'Dimensions' }],
      },
    };

    function recallElement(selector) {
      return document.querySelector(
        `[data-settings-section="recall"] ${selector}`,
      );
    }

    // The open Model picker's options as [group, [option ids]].
    function pickerGroups() {
      const panel = document.body.querySelector('.searchable-dropdown__panel');
      return Array.from(
        panel.querySelectorAll('.searchable-dropdown__group'),
        (group) => [
          group
            .querySelector('.searchable-dropdown__group-label')
            .textContent.trim(),
          Array.from(
            group.querySelectorAll('.searchable-dropdown__option'),
            (option) =>
              option
                .querySelector('.searchable-dropdown__option-label')
                .textContent.trim(),
          ),
        ],
      );
    }

    function installDialog() {
      return document.body.querySelector('.modal [data-local-install]')
        ?.parentElement;
    }

    it('chooses the search method first, then an embedding model in the Model picker, and follows the index status', async () => {
      const settings = settingsPayload();
      settings.recall.available_backends = ['sqlite_fts', 'vector', 'hybrid'];
      const props = reactiveProps({ recallIndexStatus: null });
      await mountSettings(
        {
          settings,
          taskModelTargets: embeddingTargets,
          taskModelOptions: embeddingOptions,
        },
        props,
      );
      await openRecallPanel();
      const modelRow = recallElement('[data-recall-model]');
      const modelLine = recallElement('[data-recall-model-line]');
      const indexRow = recallElement('[data-recall-index]');
      expect(getSimpleTrigger('settings-recall-backend').textContent).toContain(
        'Keywords',
      );
      // Keyword search needs no embedding model.
      expect(modelRow.hidden).toBe(true);
      expect(indexRow.hidden).toBe(true);

      openSimpleDropdown('settings-recall-backend');
      selectSimpleOption('settings-recall-backend', 'Keywords and meaning');
      expect(modelRow.hidden).toBe(false);
      await waitForCondition(
        () => modelLine.dataset.recallModelLine === 'attention',
      );
      // The best recommended Model is suggested while none is chosen.
      expect(modelLine.textContent).toContain(
        'Granite Embedding R2 is a good start.',
      );
      expect(indexRow.hidden).toBe(true);

      await openSearchableDropdown('settings-specialized-text_embedding');
      expect(pickerGroups()).toEqual([
        ['On this computer', ['Granite Embedding R2']],
        [
          'Cloud',
          ['OpenAI / Text Embedding 3 Small', 'OpenRouter / Unranked Embed'],
        ],
      ]);
      selectSearchableOption(
        'settings-specialized-text_embedding',
        'OpenAI / Text Embedding 3 Small',
      );
      expect(modelLine.dataset.recallModelLine).toBe('chosen');
      expect(modelLine.textContent).toContain(
        'Conversation text is sent to OpenAI to build the search index, at $0.02 per 1M tokens.',
      );
      // The Model's options stay folded until asked for.
      await waitForCondition(() =>
        recallElement('#settings-recall-model-options-toggle'),
      );
      expect(recallElement('#task-model-text_embedding-dimensions')).toBeNull();
      recallElement('#settings-recall-model-options-toggle').click();
      flushSync();
      expect(
        recallElement('#task-model-text_embedding-dimensions'),
      ).toBeTruthy();

      // One save state covers the backend and the embedding binding.
      getButton('Save').click();
      await waitForCondition(
        () =>
          getSettingsUpdateCalls().length >= 1 &&
          rpcMock.mock.calls.some((call) => call[0] === 'task_model.update'),
      );
      expect(getSettingsUpdateCalls()[0][1]).toEqual({
        recall: { backend: 'hybrid' },
        base: { recall: { backend: 'sqlite_fts' } },
      });
      expect(
        rpcMock.mock.calls.find((call) => call[0] === 'task_model.update')[1],
      ).toEqual({
        model_tasks: {
          text_embedding: {
            target: 'openai/text-embedding-3-small',
            options: {},
          },
        },
      });
      await waitForCondition(
        () => saveStateText('recall') === t('common.saved'),
      );

      // Saving reads the status again; nothing indexed offers no rebuild.
      await waitForCondition(() => indexRow.hidden === false);
      expect(indexRow.textContent).toContain(t('settings.recall.status.empty'));
      expect(indexRow.textContent).not.toContain(t('settings.recall.rebuild'));
      // Pushed updates replace it.
      props.recallIndexStatus = recallIndexStatusPayload({
        state: 'indexing',
        indexed: 812,
        waiting: 183,
        estimate: { characters: 1_600_000, tokens: 400_000, cost: 0.004 },
      });
      flushSync();
      expect(indexRow.dataset.recallIndex).toBe('indexing');
      expect(indexRow.textContent).toContain(
        'Indexing: 812 of 995 passages · about 400K tokens waiting (~$0.004)',
      );

      openSimpleDropdown('settings-recall-backend');
      selectSimpleOption('settings-recall-backend', 'Keywords');
      expect(modelRow.hidden).toBe(true);
      expect(indexRow.hidden).toBe(true);
    });

    it('installs a local embedding model once when it is chosen, then uses it', async () => {
      const settings = settingsPayload();
      settings.recall = {
        backend: 'hybrid',
        available_backends: ['sqlite_fts', 'vector', 'hybrid'],
      };
      const localSetups = new Map();
      await mountSettings({
        settings,
        taskModelTargets: embeddingTargets,
        taskModelOptions: embeddingOptions,
        localSetups,
        localSetupInstall: {
          phase: 'downloading',
          progress: { completed: 120_000_000, total: 346_806_730 },
        },
      });
      await openRecallPanel();
      await waitForCondition(
        () => !getSimpleTrigger('settings-specialized-text_embedding').disabled,
      );
      await openSearchableDropdown('settings-specialized-text_embedding');
      const granite = Array.from(
        document.body.querySelectorAll('.searchable-dropdown__option'),
      ).find((option) => option.textContent.includes('Granite'));
      expect(granite.disabled).toBe(false);
      expect(granite.textContent).toContain('Not installed · 347 MB');
      selectSearchableOption(
        'settings-specialized-text_embedding',
        'Granite Embedding R2',
      );

      // Choosing it opens its installation instead of selecting it.
      await waitForCondition(() =>
        installDialog()?.querySelector('[data-local-install="missing"]'),
      );
      const dialog = document.body.querySelector('.modal');
      expect(dialog.textContent).toContain('Install Granite Embedding R2');
      expect(dialog.textContent).toContain(
        '347 MB download · Apache-2.0 license',
      );
      expect(
        getSimpleTrigger('settings-specialized-text_embedding').textContent,
      ).toContain(t('settings.recall.model.placeholder'));

      vi.useFakeTimers();
      buttonByText('Install').click();
      await flushAsyncUpdates();
      expect(rpcMock).toHaveBeenCalledWith('task_model.local_setup_install', {
        target: 'local/granite-embedding-r2',
      });
      expect(
        dialog
          .querySelector('[role="progressbar"]')
          .getAttribute('aria-valuenow'),
      ).toBe('34');
      expect(dialog.textContent).toContain('120 MB of 347 MB');

      // The finished installation closes the dialog and chooses the Model.
      localSetups.set('local/granite-embedding-r2', {
        state: 'ready',
        phase: 'verifying',
        error: '',
        restart_available: true,
      });
      await vi.advanceTimersByTimeAsync(1500);
      await flushAsyncUpdates(20);
      expect(document.body.querySelector('.modal')).toBeNull();
      expect(
        getSimpleTrigger('settings-specialized-text_embedding').textContent,
      ).toContain('Granite Embedding R2');
      expect(recallElement('[data-recall-model-line]').textContent).toContain(
        'Runs on this computer, free. Conversation text stays here.',
      );
    });

    it('shows a stored meaning-only method and rebuilds a failed index after confirmation', async () => {
      const settings = settingsPayload();
      settings.recall = {
        backend: 'vector',
        available_backends: ['sqlite_fts', 'vector', 'hybrid'],
      };
      settings.model_tasks = {
        text_embedding: { target: 'openrouter/unranked-embed', options: {} },
      };
      await mountSettings({
        settings,
        taskModelTargets: embeddingTargets,
        taskModelOptions: {
          'openrouter/unranked-embed': { fields: [] },
        },
        recallIndexStatus: recallIndexStatusPayload({
          state: 'error',
          indexed: 3,
          waiting: 2,
          estimate: { tokens: 500 },
          last_error: { code: 'provider_auth', message: 'English' },
        }),
        recallRebuildStatus: recallIndexStatusPayload({
          state: 'indexing',
          waiting: 5,
          estimate: { tokens: 900 },
        }),
      });
      await openRecallPanel();

      expect(getSimpleTrigger('settings-recall-backend').textContent).toContain(
        'Meaning only',
      );
      await waitForCondition(() =>
        getSimpleTrigger(
          'settings-specialized-text_embedding',
        ).textContent.includes('OpenRouter / Unranked Embed'),
      );
      // A Model without options offers no options disclosure.
      expect(recallElement('#settings-recall-model-options-toggle')).toBeNull();
      const indexRow = recallElement('[data-recall-index]');
      await waitForCondition(() => indexRow.dataset.recallIndex === 'error');
      expect(
        indexRow.querySelector('[data-recall-index-problem]').textContent,
      ).toContain(t('settings.recall.indexError.provider_auth'));

      buttonByText(t('settings.recall.rebuild')).click();
      flushSync();
      expect(document.body.textContent).toContain(
        t('settings.recall.rebuildTitle'),
      );
      getButton(t('settings.recall.rebuildConfirm')).click();
      await waitForCondition(() => indexRow.dataset.recallIndex === 'indexing');
      expect(
        rpcMock.mock.calls.filter((call) => call[0] === 'recall.rebuild_index'),
      ).toHaveLength(1);
      expect(indexRow.textContent).toContain(
        'Indexing: 0 of 5 passages · about 900 tokens waiting',
      );
    });

    it('selects SearXNG and saves the Web Search provider settings', async () => {
      await mountSettings();
      await openWebSearchPanel();

      openSimpleDropdown('settings-web-search-provider');
      selectSimpleOption(
        'settings-web-search-provider',
        t('settings.webSearch.providers.searxng'),
      );
      setInputValue(
        '#settings-web-search-searxng-base-url',
        'http://localhost:9999',
      );
      getButton('Save').click();
      await waitForCondition(() => getSettingsUpdateCalls().length >= 1);

      expect(getSettingsUpdateCalls()[0][1]).toEqual({
        web_search: {
          provider: 'searxng',
          default_count: 12,
          searxng: { base_url: 'http://localhost:9999' },
        },
        base: {
          web_search: {
            provider: 'brave',
            default_count: 12,
            searxng: { base_url: 'http://localhost:8888' },
          },
        },
      });
      await waitForCondition(
        () => saveStateText('web_search') === t('common.saved'),
      );
    });
  });
});
