<script>
  import { onMount, tick, untrack } from 'svelte';

  import WakewordVoiceSettings from './WakewordVoiceSettings.svelte';
  import DesktopConnectionSettings from './settings/DesktopConnectionSettings.svelte';
  import SettingsAppearancePanel from './settings/SettingsAppearancePanel.svelte';
  import SettingsChannelsPanel from './settings/SettingsChannelsPanel.svelte';
  import SettingsDebugPanel from './settings/SettingsDebugPanel.svelte';
  import SettingsExtensionsPanel from './settings/SettingsExtensionsPanel.svelte';
  import SettingsGeneralPanel from './settings/SettingsGeneralPanel.svelte';
  import SettingsProvidersPanel from './settings/SettingsProvidersPanel.svelte';
  import SettingsRecallPanel from './settings/SettingsRecallPanel.svelte';
  import SettingsReflectionPanel from './settings/SettingsReflectionPanel.svelte';
  import SettingsSessionTitlesPanel from './settings/SettingsSessionTitlesPanel.svelte';
  import SettingsSpecializedModelsPanel from './settings/SettingsSpecializedModelsPanel.svelte';
  import SettingsSubAgentsPanel from './settings/SettingsSubAgentsPanel.svelte';
  import SettingsWebFetchPanel from './settings/SettingsWebFetchPanel.svelte';
  import SettingsWebSearchPanel from './settings/SettingsWebSearchPanel.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import Dropdown from './Dropdown.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import { getSettings } from '$lib/api.js';
  import { setApplicationTimeZone } from '$lib/dateTimePrefs.svelte.js';
  import { init, t } from '$lib/i18n.js';
  import { useAutosaveContext } from '$lib/autosave.js';
  import { SETTINGS_LAYOUT_CLASS } from '$lib/settingsView.js';

  const noop = () => {};

  let {
    providerAuthEvent = null,
    connectProvider = null,
    disconnectProvider = null,
    onToast = noop,
    onSettingsCommit = noop,
    onNavigateToAgentDefaults = noop,
    agents = [],
    desktopCapabilities = null,
    targetPanelId = '',
    targetPanelRequestId = 0,
    onDebugEnabledChange = noop,
    onOpenSetupGuide = noop,
    modelsRefreshToken = 0,
    clientsRefreshToken = 0,
    channelsRefreshToken = 0,
    initialScrollPosition = null,
    onScrollPositionChange = noop,
  } = $props();

  export function handleProviderAuthCompleted(event) {
    providersPanel?.handleProviderAuthCompleted(event);
  }

  export function getScrollPosition() {
    return captureScrollPosition();
  }

  const autosaveContext = useAutosaveContext();
  let catalog = $derived(
    new Map(
      [
        {
          id: 'providers',
          label: () => t('settings.providers.title', 'Providers'),
          subtitle: () =>
            t(
              'settings.providers.subtitle',
              'Connected providers and their credentials.',
            ),
        },
        {
          id: 'channels',
          label: () => t('settings.channels.title', 'Channels'),
          subtitle: () =>
            t(
              'settings.channels.subtitle',
              'Manage channel routing and runtime status.',
            ),
        },
        {
          id: 'extensions',
          label: () => t('settings.extensions.title', 'Extensions'),
          subtitle: () =>
            t(
              'settings.extensions.subtitle',
              'Loaded extensions and their capabilities. Toggles take effect immediately.',
            ),
        },
        {
          id: 'specialized_models',
          label: () =>
            t('settings.specializedModels.title', 'Specialized Models'),
          subtitle: () =>
            t(
              'settings.specializedModels.subtitle',
              'Task-specific model bindings for speech, images, video, music, embeddings, and decisions. These bindings are independent of agent and project defaults.',
            ),
        },
        {
          id: 'session_titles',
          label: () => t('settings.sessionTitles.title', 'Session titles'),
          subtitle: () =>
            t(
              'settings.sessionTitles.subtitle',
              'Immediate local names and optional Model-generated titles for new Sessions.',
            ),
        },
        {
          id: 'recall',
          label: () => t('settings.recall.title', 'Recall'),
          subtitle: () =>
            t(
              'settings.recall.subtitle',
              'How agents search past conversations.',
            ),
        },
        {
          id: 'voice',
          label: () => t('settings.voice.title', 'Voice'),
          subtitle: () =>
            t(
              'settings.voice.subtitle',
              'Live voice, transcription audio, and wakeword command settings.',
            ),
        },
        {
          id: 'web_fetch',
          label: () => t('settings.webFetch.title', 'Web Fetch'),
          subtitle: () =>
            t(
              'settings.webFetch.subtitle',
              'Page reading and optional extraction services.',
            ),
        },
        {
          id: 'web_search',
          label: () => t('settings.webSearch.title', 'Web Search'),
          subtitle: () =>
            t(
              'settings.webSearch.subtitle',
              'Provider used by the web_search tool.',
            ),
        },
        {
          id: 'subagents',
          label: () => t('settings.subagents.title', 'Sub-Agents'),
          subtitle: () =>
            t(
              'settings.subagents.subtitle',
              'Depth, fan-out, and timeout limits for spawned agent sessions.',
            ),
        },
        {
          id: 'reflection',
          label: () => t('settings.reflection.title', 'Reflection'),
          subtitle: () =>
            t(
              'settings.reflection.subtitle',
              'Automatic background self-review that saves durable memory and skill updates from finished conversations.',
            ),
        },
        {
          id: 'appearance',
          label: () => t('settings.appearance.title', 'Appearance'),
          subtitle: () =>
            t(
              'settings.appearance.subtitle',
              'Language and chat reading width.',
            ),
        },
        {
          id: 'debug',
          label: () => t('debug.settings', 'Debug'),
          subtitle: () =>
            t(
              'debug.settingsSubtitle',
              'Control debug tracing of provider requests and responses.',
            ),
        },
        {
          id: 'general',
          label: () => t('settings.general.title', 'Server info'),
          subtitle: () =>
            t(
              'settings.general.subtitle',
              'Server address, data directory, and connected clients.',
            ),
        },
        {
          id: 'desktop_connection',
          label: () => t('settings.desktop.connection.title', 'Connection'),
          subtitle: () =>
            t(
              'settings.desktop.connection.subtitle',
              'Choose which vBot server this Desktop app connects to.',
            ),
        },
      ].map((panel) => [panel.id, panel]),
    ),
  );
  let preferencesPanel = $derived({
    id: 'preferences',
    labelKey: 'settings.preferences.title',
    labelFallback: 'General',
    label: () => t('settings.preferences.title', 'Region & setup'),
    subtitle: () =>
      t('settings.preferences.subtitle', 'Time zone and getting started.'),
  });
  let groups = $derived([
    {
      id: 'models',
      label: () => t('settings.groups.models', 'Models'),
      sections: ['providers', 'specialized_models'].map((id) =>
        catalog.get(id),
      ),
    },
    {
      id: 'personal',
      label: () => t('settings.groups.personal', 'Personal'),
      sections: [
        catalog.get('appearance'),
        preferencesPanel,
        catalog.get('voice'),
      ],
    },
    {
      id: 'capabilities',
      label: () => t('settings.groups.capabilities', 'Capabilities'),
      sections: ['web_search', 'web_fetch', 'channels', 'extensions'].map(
        (id) => catalog.get(id),
      ),
    },
    {
      id: 'sessions',
      label: () => t('settings.categories.sessions', 'Sessions & Memory'),
      sections: ['session_titles', 'recall', 'reflection', 'subagents'].map(
        (id) => catalog.get(id),
      ),
    },
    {
      id: 'system',
      label: () => t('settings.groups.system', 'System'),
      sections: [
        'general',
        ...(desktopCapabilities?.serverSelection ? ['desktop_connection'] : []),
        'debug',
      ].map((id) => catalog.get(id)),
    },
  ]);
  let panels = $derived(groups.flatMap((group) => group.sections));
  let panelById = $derived(new Map(panels.map((panel) => [panel.id, panel])));
  let mobileSectionOptions = $derived(
    groups.flatMap((group) => [
      ...group.sections.map((panel) => ({
        value: panel.id,
        label: panel.label(),
        secondaryLabel: group.label(),
      })),
      ...(group.id === 'models'
        ? [
            {
              value: 'agent_defaults',
              label: t('settings.agentShortcut.nav', 'Model & Thinking'),
              secondaryLabel: group.label(),
            },
          ]
        : []),
    ]),
  );
  let settings = $state(null);
  let loading = $state(true);
  let loadError = $state('');
  let providersPanel = $state(null);
  let scrollContainer = $state(null);
  let documentRoot = $state(null);
  let activeSectionId = $state(
    untrack(() => initialScrollPosition?.sectionId || 'providers'),
  );
  let searchQuery = $state('');
  let searchResults = $state([]);
  let searchActive = $derived(searchQuery.trim().length > 0);
  let handledTargetPanelRequestId = -1;
  let restoreTop = untrack(() => Math.max(0, initialScrollPosition?.top || 0));
  let restorePending = untrack(() => Boolean(initialScrollPosition));
  let restoreFrame = null;

  onMount(() => {
    loadSettings();
    return () => {
      if (restoreFrame !== null) cancelAnimationFrame(restoreFrame);
    };
  });

  $effect(() => {
    if (!loading && !panelById.has(activeSectionId))
      activeSectionId = panels[0].id;
  });

  $effect(() => {
    if (
      !loading &&
      targetPanelId &&
      targetPanelRequestId !== handledTargetPanelRequestId &&
      panelById.has(targetPanelId)
    ) {
      handledTargetPanelRequestId = targetPanelRequestId;
      // A deliberate deep link replaces the remembered page; an ordinary
      // return carries its saved page and may still have an old target prop.
      if (!initialScrollPosition) void selectSection(targetPanelId, false);
    }
  });

  $effect(() => {
    void searchQuery;
    void loading;
    void panels;
    if (documentRoot) untrack(updateSearchResults);
  });

  $effect(() => {
    if (!documentRoot) return;
    const observer = new MutationObserver(() => {
      if (searchQuery.trim()) updateSearchResults();
      queueRestore();
    });
    observer.observe(documentRoot, { childList: true, subtree: true });
    const resizeObserver =
      typeof ResizeObserver === 'function'
        ? new ResizeObserver(queueRestore)
        : null;
    resizeObserver?.observe(documentRoot);
    queueRestore();
    return () => {
      observer.disconnect();
      resizeObserver?.disconnect();
    };
  });

  function normalizedSearch(value) {
    return value
      .normalize('NFKD')
      .toLocaleLowerCase()
      .replace(/[\p{M}\s\p{P}\p{S}]/gu, '');
  }

  function updateSearchResults() {
    const terms = searchQuery
      .trim()
      .split(/\s+/)
      .map(normalizedSearch)
      .filter(Boolean);
    const results = [];
    if (terms.length) {
      for (const panel of panels) {
        const section = documentRoot?.querySelector(
          `[data-settings-section="${panel.id}"]`,
        );
        const text = normalizedSearch(section?.textContent ?? '');
        if (terms.every((term) => text.includes(term))) results.push(panel.id);
      }
    }
    if (
      terms.length &&
      terms.every((term) =>
        normalizedSearch(
          'Agent defaults global shared Model Thinking effort temperature fallback compaction',
        ).includes(term),
      )
    )
      results.push('agent_defaults');
    // Avoid observing our own result-list render as another result change.
    if (results.join('|') !== searchResults.join('|')) searchResults = results;
  }

  function queueRestore() {
    if (!restorePending || loading || !scrollContainer || restoreFrame !== null)
      return;
    restoreFrame = requestAnimationFrame(() => {
      restoreFrame = null;
      if (restorePending && scrollContainer)
        scrollContainer.scrollTop = restoreTop;
    });
  }

  function releaseRestore() {
    restorePending = false;
  }

  function captureScrollPosition() {
    return scrollContainer
      ? {
          top: Math.max(0, scrollContainer.scrollTop),
          sectionId: activeSectionId,
        }
      : null;
  }

  function handleContentScroll() {
    if (!restorePending && !searchActive)
      onScrollPositionChange(captureScrollPosition());
  }

  async function selectSection(panelId, focusHeading = true) {
    if (!panelById.has(panelId)) return;
    releaseRestore();
    searchQuery = '';
    activeSectionId = panelId;
    await tick();
    const target = documentRoot?.querySelector(
      `[data-settings-section="${panelId}"]`,
    );
    if (scrollContainer && target) scrollContainer.scrollTop = 0;
    onScrollPositionChange(captureScrollPosition());
    if (focusHeading)
      documentRoot
        ?.querySelector(`#settings-section-${panelId}`)
        ?.focus({ preventScroll: true });
  }

  function navigateToSection(panelId) {
    if (panelId === 'agent_defaults') return navigateToDefaults();
    return autosaveContext.requestTransition(() => selectSection(panelId));
  }

  function navigateToDefaults() {
    return autosaveContext.requestTransition(() =>
      onNavigateToAgentDefaults('defaults'),
    );
  }

  function handleSearchInput(event) {
    releaseRestore();
    searchQuery = event.currentTarget.value;
    if (scrollContainer) scrollContainer.scrollTop = 0;
  }

  // The single settings error seam: a panel's `onError` funnels here. A
  // non-empty message becomes a sticky error toast (a transport/server failure
  // the user must acknowledge); the empty-string "clear" calls panels make on a
  // fresh attempt are simply ignored, since a toast is dismissed by the user,
  // not by the next keystroke. Panels already build a full sentence, so it is
  // the toast body under a generic error title.
  function reportSettingsError(message) {
    if (!message) {
      return;
    }
    onToast({
      title: t('errors.appError', 'Error'),
      message,
      variant: 'error',
    });
  }

  function applySettings(nextSettings) {
    settings = nextSettings;
    setApplicationTimeZone(nextSettings?.general?.timezone);

    const language = nextSettings?.appearance?.language ?? 'en';
    init(language);
  }

  function commitSettings(nextSettings) {
    settings = nextSettings;
    setApplicationTimeZone(nextSettings?.general?.timezone);
    onSettingsCommit(nextSettings);
  }

  async function loadSettings() {
    loading = true;
    loadError = '';

    try {
      const nextSettings = await getSettings();
      applySettings(nextSettings);
    } catch (error) {
      loadError = `${t('settings.loadError', 'Settings could not be loaded.')} ${error.message}`;
    } finally {
      loading = false;
    }
  }
</script>

{#snippet panelContent(panelId)}
  {#if panelId === 'preferences'}
    <SettingsGeneralPanel
      page="preferences"
      {settings}
      {onOpenSetupGuide}
      onCommit={commitSettings}
      {onToast}
      onError={reportSettingsError}
    />
  {:else if panelId === 'providers'}
    <SettingsProvidersPanel
      bind:this={providersPanel}
      {settings}
      visible={true}
      {providerAuthEvent}
      {connectProvider}
      {disconnectProvider}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
      onReloadSettings={loadSettings}
      {modelsRefreshToken}
    />
  {:else if panelId === 'channels'}
    <SettingsChannelsPanel
      {onToast}
      {channelsRefreshToken}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'extensions'}
    <SettingsExtensionsPanel
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'specialized_models'}
    <SettingsSpecializedModelsPanel
      {settings}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
      {modelsRefreshToken}
    />
  {:else if panelId === 'session_titles'}
    <SettingsSessionTitlesPanel
      {settings}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
      {modelsRefreshToken}
    />
  {:else if panelId === 'recall'}
    <SettingsRecallPanel
      {settings}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'voice'}
    <div class="settings-related">
      <Button
        variant="tertiary"
        onClick={() => navigateToSection('specialized_models')}
        >{t('settings.voice.modelsLink', 'Choose speech Models')}</Button
      >
    </div>
    <WakewordVoiceSettings
      {agents}
      {settings}
      wakewordAvailable={desktopCapabilities?.wakeword === true}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'web_fetch'}
    <SettingsWebFetchPanel
      {settings}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'web_search'}
    <SettingsWebSearchPanel
      {settings}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'subagents'}
    <SettingsSubAgentsPanel
      {settings}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'reflection'}
    <SettingsReflectionPanel
      {settings}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'appearance'}
    <SettingsAppearancePanel
      {settings}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'debug'}
    <SettingsDebugPanel
      {settings}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
      {onDebugEnabledChange}
    />
  {:else if panelId === 'general'}
    <SettingsGeneralPanel
      {settings}
      {clientsRefreshToken}
      {onOpenSetupGuide}
      onCommit={commitSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'desktop_connection'}
    <DesktopConnectionSettings {onToast} />
  {/if}
{/snippet}

<section
  class={SETTINGS_LAYOUT_CLASS}
  aria-label={t('settings.title', 'Settings')}
>
  <nav
    class="settings-nav secondary-pane"
    aria-label={t('settings.sections', 'Settings sections')}
  >
    <div class="settings-nav-title secondary-pane__title">
      {t('settings.title', 'Settings')}
    </div>
    <div class="settings-search">
      <input
        class="settings-search-input"
        type="search"
        value={searchQuery}
        oninput={handleSearchInput}
        placeholder={t('settings.search.placeholder', 'Search settingsâ€¦')}
        aria-label={t('settings.search.label', 'Search settings')}
      />
    </div>
    <div class="settings-desktop-index">
      {#each groups as group (group.id)}
        <div
          class="settings-nav-section"
          role="group"
          aria-labelledby={'settings-group-' + group.id}
        >
          <h3 class="settings-nav-group" id={'settings-group-' + group.id}>
            {group.label()}
          </h3>
          {#each group.sections as panel (panel.id)}
            <button
              class="snav-item"
              class:snav-item--active={!searchActive &&
                panel.id === activeSectionId}
              type="button"
              aria-current={!searchActive && panel.id === activeSectionId
                ? 'page'
                : undefined}
              onclick={() => navigateToSection(panel.id)}
              >{panel.label()}</button
            >
          {/each}
          {#if group.id === 'models'}
            <button
              class="snav-item settings-defaults-link"
              type="button"
              onclick={navigateToDefaults}
            >
              {t('settings.agentShortcut.nav', 'Model & Thinking')}
              <span aria-hidden="true">↗</span>
            </button>
          {/if}
        </div>
      {/each}
    </div>
    <div class="settings-mobile-section-picker">
      <Dropdown
        id="settings-mobile-section"
        value={activeSectionId}
        options={mobileSectionOptions}
        ariaLabel={t('settings.sections', 'Settings sections')}
        onValueChange={navigateToSection}
      />
    </div>
  </nav>
  <!-- Keyboard interaction releases scroll restoration for this scrollable region. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
  <div
    class="settings-content"
    role="region"
    aria-label={t('settings.content', 'Settings content')}
    tabindex="0"
    bind:this={scrollContainer}
    onscroll={handleContentScroll}
    onwheel={releaseRestore}
    ontouchstart={releaseRestore}
    onpointerdown={releaseRestore}
    onkeydown={releaseRestore}
  >
    <div class="s-doc" bind:this={documentRoot}>
      {#if loading}
        <Banner variant="neutral"
          >{t('settings.loading', 'Loading settingsâ€¦')}</Banner
        >
      {:else if loadError}
        <Banner variant="error"
          ><span>{loadError}</span><Button
            variant="secondary"
            onClick={loadSettings}>{t('common.retry', 'Retry')}</Button
          ></Banner
        >
      {:else}
        {#if searchActive}
          <header class="settings-page-heading">
            <div class="settings-page-eyebrow">
              {t('settings.title', 'Settings')}
            </div>
            <h2>{t('settings.search.results', 'Search results')}</h2>
            <p role="status">
              {t('settings.search.resultCount', 'Matching topics: {count}', {
                count: searchResults.length,
              })}
            </p>
          </header>
          <div class="settings-search-results">
            {#each searchResults as panelId (panelId)}
              {@const panel = panelById.get(panelId)}
              <Button
                class="settings-search-result"
                onClick={() =>
                  panel ? navigateToSection(panelId) : navigateToDefaults()}
              >
                <span class="settings-search-result__title"
                  >{panel
                    ? panel.label()
                    : t('agents.shared.title', 'Shared defaults')}</span
                >
                <span class="settings-search-result__description"
                  >{panel
                    ? panel.subtitle()
                    : t(
                        'settings.agentShortcut.search',
                        'Agents â†’ Shared defaults Â· Model, Thinking, fallbacks and Compaction',
                      )}</span
                >
              </Button>
            {:else}
              <EmptyState
                density="compact"
                description={t(
                  'settings.search.noMatches',
                  'No settings match your search.',
                )}
              />
            {/each}
          </div>
        {/if}
        <div class="settings-editors">
          {#each panels as panel (panel.id)}
            <section
              class="settings-editor"
              data-settings-section={panel.id}
              hidden={searchActive || activeSectionId !== panel.id}
              aria-labelledby={'settings-section-' + panel.id}
            >
              <header class="settings-page-heading">
                <div class="settings-page-eyebrow">
                  {groups
                    .find((group) => group.sections.includes(panel))
                    ?.label()}
                </div>
                <h2 id={'settings-section-' + panel.id} tabindex="-1">
                  {panel.label()}
                </h2>
                <p>{panel.subtitle()}</p>
              </header>
              <div class="settings-editor-body">
                {@render panelContent(panel.id)}
              </div>
            </section>
          {/each}
        </div>
      {/if}
    </div>
  </div>
</section>
