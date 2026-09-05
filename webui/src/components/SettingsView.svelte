<script>
  import { onMount, tick, untrack } from 'svelte';

  import WakewordVoiceSettings from './WakewordVoiceSettings.svelte';
  import DesktopConnectionSettings from './settings/DesktopConnectionSettings.svelte';
  import SettingsAppearancePanel from './settings/SettingsAppearancePanel.svelte';
  import SettingsChannelsPanel from './settings/SettingsChannelsPanel.svelte';
  import SettingsCompactionPanel from './settings/SettingsCompactionPanel.svelte';
  import SettingsDebugPanel from './settings/SettingsDebugPanel.svelte';
  import SettingsDefaultsPanel from './settings/SettingsDefaultsPanel.svelte';
  import SettingsExtensionsPanel from './settings/SettingsExtensionsPanel.svelte';
  import SettingsGeneralPanel from './settings/SettingsGeneralPanel.svelte';
  import SettingsProvidersPanel from './settings/SettingsProvidersPanel.svelte';
  import SettingsRecallPanel from './settings/SettingsRecallPanel.svelte';
  import SettingsReflectionPanel from './settings/SettingsReflectionPanel.svelte';
  import SettingsSessionTitlesPanel from './settings/SettingsSessionTitlesPanel.svelte';
  import SettingsSpecializedModelsPanel from './settings/SettingsSpecializedModelsPanel.svelte';
  import SettingsSubAgentsPanel from './settings/SettingsSubAgentsPanel.svelte';
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

  let catalogGroups = $derived([
    {
      id: 'connect',
      label: () => t('settings.groups.connect', 'Connect'),
      sections: [
        {
          id: 'providers',
          labelKey: 'settings.providers.title',
          labelFallback: 'Providers',
          label: () => t('settings.providers.title', 'Providers'),
          subtitle: () =>
            t(
              'settings.providers.subtitle',
              'Connected providers and their credentials.',
            ),
        },
        {
          id: 'channels',
          labelKey: 'settings.channels.title',
          labelFallback: 'Channels',
          label: () => t('settings.channels.title', 'Channels'),
          subtitle: () =>
            t(
              'settings.channels.subtitle',
              'Manage channel routing and runtime status.',
            ),
        },
        {
          id: 'extensions',
          labelKey: 'settings.extensions.title',
          labelFallback: 'Extensions',
          label: () => t('settings.extensions.title', 'Extensions'),
          subtitle: () =>
            t(
              'settings.extensions.subtitle',
              'Loaded extensions and their capabilities. Toggles take effect immediately.',
            ),
        },
      ],
    },
    {
      id: 'models',
      label: () => t('settings.groups.models', 'Models'),
      sections: [
        {
          id: 'defaults',
          labelKey: 'settings.defaults.title',
          labelFallback: 'Agent defaults',
          label: () => t('settings.defaults.title', 'Agent defaults'),
          subtitle: () =>
            t(
              'settings.defaults.subtitle',
              'Model, temperature, and thinking effort used when an agent or project leaves them unset — shown there as "Inherited: … (global default)".',
            ),
        },
        {
          id: 'specialized_models',
          labelKey: 'settings.specializedModels.title',
          labelFallback: 'Specialized Models',
          label: () =>
            t('settings.specializedModels.title', 'Specialized Models'),
          subtitle: () =>
            t(
              'settings.specializedModels.subtitle',
              'Task-specific model bindings for speech, image, and embedding tools. These bindings are independent of agent and project defaults.',
            ),
        },
      ],
    },
    {
      id: 'conversation',
      label: () => t('settings.groups.conversation', 'Conversation'),
      sections: [
        {
          id: 'compaction',
          labelKey: 'settings.compaction.title',
          labelFallback: 'Compaction',
          label: () => t('settings.compaction.title', 'Compaction'),
          subtitle: () =>
            t(
              'settings.compaction.subtitle',
              'Automatic summarizing when a conversation nears the model context limit.',
            ),
        },
        {
          id: 'session_titles',
          labelKey: 'settings.sessionTitles.title',
          labelFallback: 'Session titles',
          label: () => t('settings.sessionTitles.title', 'Session titles'),
          subtitle: () =>
            t(
              'settings.sessionTitles.subtitle',
              'Immediate local names and optional Model-generated titles for new Sessions.',
            ),
        },
        {
          id: 'recall',
          labelKey: 'settings.recall.title',
          labelFallback: 'Recall',
          label: () => t('settings.recall.title', 'Recall'),
          subtitle: () =>
            t(
              'settings.recall.subtitle',
              'How agents search past conversations.',
            ),
        },
      ],
    },
    {
      id: 'behavior',
      label: () => t('settings.groups.behavior', 'Behavior'),
      sections: [
        {
          id: 'voice',
          labelKey: 'settings.voice.title',
          labelFallback: 'Voice',
          label: () => t('settings.voice.title', 'Voice'),
          subtitle: () =>
            t(
              'settings.voice.subtitle',
              'Transcription audio and wakeword command settings.',
            ),
        },
        {
          id: 'web_search',
          labelKey: 'settings.webSearch.title',
          labelFallback: 'Web Search',
          label: () => t('settings.webSearch.title', 'Web Search'),
          subtitle: () =>
            t(
              'settings.webSearch.subtitle',
              'Provider used by the web_search tool.',
            ),
        },
        {
          id: 'subagents',
          labelKey: 'settings.subagents.title',
          labelFallback: 'Sub-Agents',
          label: () => t('settings.subagents.title', 'Sub-Agents'),
          subtitle: () =>
            t(
              'settings.subagents.subtitle',
              'Depth, fan-out, and timeout limits for spawned agent sessions.',
            ),
        },
        {
          id: 'reflection',
          labelKey: 'settings.reflection.title',
          labelFallback: 'Reflection',
          label: () => t('settings.reflection.title', 'Reflection'),
          subtitle: () =>
            t(
              'settings.reflection.subtitle',
              'Automatic background self-review that saves durable memory and skill updates from finished conversations.',
            ),
        },
      ],
    },
    {
      id: 'system',
      label: () => t('settings.groups.system', 'System'),
      sections: [
        {
          id: 'appearance',
          labelKey: 'settings.appearance.title',
          labelFallback: 'Appearance',
          label: () => t('settings.appearance.title', 'Appearance'),
          subtitle: () =>
            t(
              'settings.appearance.subtitle',
              'Language and chat reading width.',
            ),
        },
        {
          id: 'debug',
          labelKey: 'debug.settings',
          labelFallback: 'Debug',
          label: () => t('debug.settings', 'Debug'),
          subtitle: () =>
            t(
              'debug.settingsSubtitle',
              'Control debug tracing of provider requests and responses.',
            ),
        },
        {
          id: 'general',
          labelKey: 'settings.general.title',
          labelFallback: 'Server info',
          label: () => t('settings.general.title', 'Server info'),
          subtitle: () =>
            t(
              'settings.general.subtitle',
              'Server address, data directory, and connected clients.',
            ),
        },
        ...(desktopCapabilities?.serverSelection
          ? [
              {
                id: 'desktop_connection',
                labelKey: 'settings.desktop.connection.title',
                labelFallback: 'Connection',
                label: () =>
                  t('settings.desktop.connection.title', 'Connection'),
                subtitle: () =>
                  t(
                    'settings.desktop.connection.subtitle',
                    'Choose which vBot server this Desktop app connects to.',
                  ),
              },
            ]
          : []),
      ],
    },
  ]);

  // Keep one owner for every editor while topic navigation changes visibility.
  // This preserves pending drafts and provider authentication across pages.
  const autosaveContext = useAutosaveContext();
  let catalog = $derived(
    new Map(
      catalogGroups
        .flatMap((group) => group.sections)
        .map((panel) => [panel.id, panel]),
    ),
  );
  let preferencesPanel = $derived({
    id: 'preferences',
    labelKey: 'settings.preferences.title',
    labelFallback: 'General',
    label: () => t('settings.preferences.title', 'General'),
    subtitle: () =>
      t('settings.preferences.subtitle', 'Time zone and getting started.'),
  });
  let groups = $derived([
    {
      id: 'general',
      label: () => t('settings.groups.general', 'General'),
      sections: [catalog.get('appearance'), preferencesPanel],
    },
    {
      id: 'models',
      label: () => t('settings.groups.models', 'Models'),
      sections: ['providers', 'defaults', 'specialized_models'].map((id) =>
        catalog.get(id),
      ),
    },
    {
      id: 'conversation',
      label: () => t('settings.groups.conversation', 'Conversation'),
      sections: ['compaction', 'session_titles', 'recall', 'reflection'].map(
        (id) => catalog.get(id),
      ),
    },
    {
      id: 'capabilities',
      label: () => t('settings.groups.capabilities', 'Capabilities'),
      sections: ['voice', 'web_search', 'subagents'].map((id) =>
        catalog.get(id),
      ),
    },
    {
      id: 'integrations',
      label: () => t('settings.groups.integrations', 'Integrations'),
      sections: ['channels', 'extensions'].map((id) => catalog.get(id)),
    },
    {
      id: 'system',
      label: () => t('settings.groups.system', 'System'),
      sections: [
        'general',
        'debug',
        ...(desktopCapabilities?.serverSelection ? ['desktop_connection'] : []),
      ].map((id) => catalog.get(id)),
    },
  ]);
  let panels = $derived(groups.flatMap((group) => group.sections));
  let panelById = $derived(new Map(panels.map((panel) => [panel.id, panel])));
  let mobileSectionOptions = $derived(
    groups.flatMap((group) =>
      group.sections.map((panel) => ({
        value: panel.id,
        label: panel.label(),
        secondaryLabel: group.label(),
      })),
    ),
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
  let activeGroup = $derived(
    groups.find((group) =>
      group.sections.some((panel) => panel.id === activeSectionId),
    ),
  );
  let matchCount = $derived(searchResults.length);
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
    if (scrollContainer) scrollContainer.scrollTop = 0;
    onScrollPositionChange(captureScrollPosition());
    if (focusHeading)
      documentRoot
        ?.querySelector(`#settings-section-${panelId}`)
        ?.focus({ preventScroll: true });
  }

  function navigateToSection(panelId) {
    return autosaveContext.requestTransition(() => selectSection(panelId));
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

{#snippet sectionHeader(panel)}
  <header class="s-section-header">
    <h2
      tabindex="-1"
      class="s-section-title"
      id={`settings-section-${panel.id}`}
    >
      {panel.label()}
    </h2>
    <p class="s-section-sub">{panel.subtitle()}</p>
  </header>
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
      <svg
        class="settings-search-icon"
        width="13"
        height="13"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2.4"
        aria-hidden="true"
      >
        <circle cx="11" cy="11" r="7" />
        <path d="M20 20l-4-4" />
      </svg>
      <input
        class="settings-search-input"
        type="search"
        value={searchQuery}
        oninput={handleSearchInput}
        placeholder={t('settings.search.placeholder', 'Search settings…')}
        aria-label={t('settings.search.label', 'Search settings')}
      />
    </div>
    <div class="settings-search-count" role="status">
      {#if searchActive}
        {t('settings.search.matches', 'Matches: {count}', {
          count: matchCount,
        })}
      {/if}
    </div>

    <div class="settings-desktop-index">
      {#each groups as group (group.id)}
        <div class="settings-nav-group">{group.label()}</div>
        {#each group.sections as panel (panel.id)}
          <button
            class:snav-item--active={panel.id === activeSectionId}
            class="snav-item"
            type="button"
            aria-current={panel.id === activeSectionId ? 'true' : undefined}
            aria-label={t(panel.labelKey, panel.labelFallback)}
            onclick={() => navigateToSection(panel.id)}
          >
            {panel.label()}
          </button>
        {/each}
      {/each}
    </div>

    <div class="settings-mobile-section-picker">
      <span class="settings-mobile-section-label">
        {t('settings.sections', 'Settings sections')}
      </span>
      <Dropdown
        id="settings-mobile-section"
        value={activeSectionId}
        options={mobileSectionOptions}
        ariaLabel={t('settings.sections', 'Settings sections')}
        triggerClass="settings-mobile-section-dropdown"
        onValueChange={navigateToSection}
      />
    </div>
  </nav>

  <!-- The scroll region needs keyboard focus; interaction releases restored scrolling. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex a11y_no_noninteractive_element_interactions -->
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
        <Banner variant="neutral">
          {t('settings.loading', 'Loading settings…')}
        </Banner>
      {:else if loadError}
        <Banner variant="error">
          <span>{loadError}</span>
          <Button variant="secondary" onClick={loadSettings}>
            {t('common.retry', 'Retry')}
          </Button>
        </Banner>
      {:else}
        {#if searchActive}
          <header class="settings-page-heading">
            <h2>{t('settings.search.results', 'Search results')}</h2>
            <p>
              {t(
                'settings.search.guidance',
                'Choose a topic to open its settings.',
              )}
            </p>
          </header>
          <div class="settings-search-results">
            {#each searchResults as panelId (panelId)}
              {@const panel = panelById.get(panelId)}
              <Button
                class="settings-search-result"
                onClick={() => navigateToSection(panelId)}
              >
                <span class="settings-search-result__title"
                  >{panel.label()}</span
                >
                <span class="settings-search-result__description"
                  >{panel.subtitle()}</span
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
        {:else}
          <div class="s-doc-group">{activeGroup?.label()}</div>
        {/if}

        <section
          class="s-section"
          data-settings-section="preferences"
          hidden={searchActive || activeSectionId !== 'preferences'}
          aria-labelledby="settings-section-preferences"
        >
          {@render sectionHeader(preferencesPanel)}
          <SettingsGeneralPanel
            page="preferences"
            {settings}
            {onOpenSetupGuide}
            onCommit={commitSettings}
            {onToast}
            onError={reportSettingsError}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="providers"
          hidden={searchActive || activeSectionId !== 'providers'}
          aria-labelledby="settings-section-providers"
        >
          {@render sectionHeader(panelById.get('providers'))}
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
        </section>

        <section
          class="s-section"
          data-settings-section="channels"
          hidden={searchActive || activeSectionId !== 'channels'}
          aria-labelledby="settings-section-channels"
        >
          {@render sectionHeader(panelById.get('channels'))}
          <SettingsChannelsPanel
            {onToast}
            {channelsRefreshToken}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="extensions"
          hidden={searchActive || activeSectionId !== 'extensions'}
          aria-labelledby="settings-section-extensions"
        >
          {@render sectionHeader(panelById.get('extensions'))}
          <SettingsExtensionsPanel
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="defaults"
          hidden={searchActive || activeSectionId !== 'defaults'}
          aria-labelledby="settings-section-defaults"
        >
          {@render sectionHeader(panelById.get('defaults'))}
          <SettingsDefaultsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {modelsRefreshToken}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="specialized_models"
          hidden={searchActive || activeSectionId !== 'specialized_models'}
          aria-labelledby="settings-section-specialized_models"
        >
          {@render sectionHeader(panelById.get('specialized_models'))}
          <SettingsSpecializedModelsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {modelsRefreshToken}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="compaction"
          hidden={searchActive || activeSectionId !== 'compaction'}
          aria-labelledby="settings-section-compaction"
        >
          {@render sectionHeader(panelById.get('compaction'))}
          <SettingsCompactionPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {modelsRefreshToken}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="session_titles"
          hidden={searchActive || activeSectionId !== 'session_titles'}
          aria-labelledby="settings-section-session_titles"
        >
          {@render sectionHeader(panelById.get('session_titles'))}
          <SettingsSessionTitlesPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {modelsRefreshToken}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="recall"
          hidden={searchActive || activeSectionId !== 'recall'}
          aria-labelledby="settings-section-recall"
        >
          {@render sectionHeader(panelById.get('recall'))}
          <SettingsRecallPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="voice"
          hidden={searchActive || activeSectionId !== 'voice'}
          aria-labelledby="settings-section-voice"
        >
          {@render sectionHeader(panelById.get('voice'))}
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
        </section>

        <section
          class="s-section"
          data-settings-section="web_search"
          hidden={searchActive || activeSectionId !== 'web_search'}
          aria-labelledby="settings-section-web_search"
        >
          {@render sectionHeader(panelById.get('web_search'))}
          <SettingsWebSearchPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="subagents"
          hidden={searchActive || activeSectionId !== 'subagents'}
          aria-labelledby="settings-section-subagents"
        >
          {@render sectionHeader(panelById.get('subagents'))}
          <SettingsSubAgentsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="reflection"
          hidden={searchActive || activeSectionId !== 'reflection'}
          aria-labelledby="settings-section-reflection"
        >
          {@render sectionHeader(panelById.get('reflection'))}
          <SettingsReflectionPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="appearance"
          hidden={searchActive || activeSectionId !== 'appearance'}
          aria-labelledby="settings-section-appearance"
        >
          {@render sectionHeader(panelById.get('appearance'))}
          <SettingsAppearancePanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="debug"
          hidden={searchActive || activeSectionId !== 'debug'}
          aria-labelledby="settings-section-debug"
        >
          {@render sectionHeader(panelById.get('debug'))}
          <SettingsDebugPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {onDebugEnabledChange}
          />
        </section>

        <section
          class="s-section"
          data-settings-section="general"
          hidden={searchActive || activeSectionId !== 'general'}
          aria-labelledby="settings-section-general"
        >
          {@render sectionHeader(panelById.get('general'))}
          <SettingsGeneralPanel
            {settings}
            {clientsRefreshToken}
            {onOpenSetupGuide}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        </section>

        {#if desktopCapabilities?.serverSelection}
          <section
            class="s-section"
            data-settings-section="desktop_connection"
            hidden={searchActive || activeSectionId !== 'desktop_connection'}
            aria-labelledby="settings-section-desktop_connection"
          >
            {@render sectionHeader(panelById.get('desktop_connection'))}
            <DesktopConnectionSettings {onToast} />
          </section>
        {/if}
      {/if}
    </div>
  </div>
</section>
