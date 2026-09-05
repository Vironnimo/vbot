<script>
  import { onMount, tick, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

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
              'Task-specific model bindings for speech, image, and embedding tools. These bindings are independent of agent and project defaults.',
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
              'Transcription audio and wakeword command settings.',
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
      id: 'personal',
      label: () => t('settings.categories.general', 'General'),
      description: () =>
        t(
          'settings.categories.generalDescription',
          'Make vBot comfortable to work with.',
        ),
      sections: [catalog.get('appearance'), preferencesPanel],
    },
    {
      id: 'sessions',
      label: () => t('settings.categories.sessions', 'Sessions & Memory'),
      description: () =>
        t(
          'settings.categories.sessionsDescription',
          'Name conversations, find earlier work and learn from finished Sessions.',
        ),
      sections: ['session_titles', 'recall', 'reflection'].map((id) =>
        catalog.get(id),
      ),
    },
    {
      id: 'tools',
      label: () => t('settings.categories.tools', 'Tools & Media'),
      description: () =>
        t(
          'settings.categories.toolsDescription',
          'Set up speech, images, web search and delegation.',
        ),
      sections: ['specialized_models', 'voice', 'web_search', 'subagents'].map(
        (id) => catalog.get(id),
      ),
    },
    {
      id: 'connections',
      label: () => t('settings.categories.connections', 'Connections'),
      description: () =>
        t(
          'settings.categories.connectionsDescription',
          'Connect Model Providers, messaging Channels and Extensions.',
        ),
      sections: ['providers', 'channels', 'extensions'].map((id) =>
        catalog.get(id),
      ),
    },
    {
      id: 'system',
      label: () => t('settings.groups.system', 'System'),
      description: () =>
        t(
          'settings.categories.systemDescription',
          'Server, connected devices and diagnostics.',
        ),
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
    groups.map((group) => ({ value: group.id, label: group.label() })),
  );
  let expandedSections = $state(
    new Set(
      untrack(
        () =>
          initialScrollPosition?.expandedSections ?? [
            'appearance',
            'preferences',
          ],
      ),
    ),
  );
  let settings = $state(null);
  let loading = $state(true);
  let loadError = $state('');
  let providersPanel = $state(null);
  let scrollContainer = $state(null);
  let documentRoot = $state(null);
  let activeSectionId = $state(
    untrack(() => initialScrollPosition?.sectionId || 'appearance'),
  );
  let searchQuery = $state('');
  let searchResults = $state([]);
  let searchActive = $derived(searchQuery.trim().length > 0);
  let activeGroup = $derived(
    groups.find((group) =>
      group.sections.some((panel) => panel.id === activeSectionId),
    ),
  );
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
          expandedSections: [...expandedSections],
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
    expandedSections = new Set([...expandedSections, panelId]);
    await tick();
    const target = documentRoot?.querySelector(
      `[data-settings-section="${panelId}"]`,
    );
    if (scrollContainer && target)
      scrollContainer.scrollTop = target.offsetTop - documentRoot.offsetTop;
    onScrollPositionChange(captureScrollPosition());
    if (focusHeading)
      documentRoot
        ?.querySelector(`#settings-section-${panelId}`)
        ?.focus({ preventScroll: true });
  }

  function navigateToSection(panelId) {
    return autosaveContext.requestTransition(() => selectSection(panelId));
  }

  function navigateToCategory(categoryId) {
    const group = groups.find((item) => item.id === categoryId);
    if (!group) return;
    return autosaveContext.requestTransition(async () => {
      releaseRestore();
      searchQuery = '';
      activeSectionId = group.sections[0].id;
      await tick();
      if (scrollContainer) scrollContainer.scrollTop = 0;
      onScrollPositionChange(captureScrollPosition());
      documentRoot
        ?.querySelector('.settings-page-heading h2')
        ?.focus({ preventScroll: true });
    });
  }

  function toggleSection(panelId) {
    return autosaveContext.requestTransition(() => {
      activeSectionId = panelId;
      searchQuery = '';
      const next = new SvelteSet(expandedSections);
      if (next.has(panelId)) next.delete(panelId);
      else next.add(panelId);
      expandedSections = next;
      onScrollPositionChange(captureScrollPosition());
    });
  }

  function sectionSummary(panelId) {
    const onOff = (value) =>
      value ? t('common.enabled', 'Enabled') : t('common.disabled', 'Disabled');
    switch (panelId) {
      case 'appearance':
        return t(
          'settings.summary.appearance',
          'Language, reading width and work details',
        );
      case 'preferences':
        return (
          settings?.general?.timezone ||
          t('settings.summary.hostTimezone', 'Server time zone')
        );
      case 'session_titles':
        return onOff(settings?.session_titles?.enabled);
      case 'recall':
        return settings?.recall?.backend === 'sqlite_fts'
          ? t('settings.summary.indexedSearch', 'Indexed search')
          : t('settings.summary.historySearch', 'Session history search');
      case 'reflection':
        return onOff(settings?.reflection?.enabled !== false);
      case 'web_search':
        return (
          settings?.web_search?.provider ||
          t('settings.summary.notConfigured', 'Not configured')
        );
      case 'subagents':
        return t(
          'settings.summary.delegation',
          'Depth, parallel work and time limits',
        );
      case 'providers':
        return t(
          'settings.summary.providers',
          'Accounts, credentials and available Models',
        );
      case 'channels':
        return t('settings.summary.channels', 'Messaging accounts and access');
      case 'extensions':
        return t(
          'settings.summary.extensions',
          'Installed capabilities and configuration',
        );
      case 'specialized_models':
        return t('settings.summary.media', 'Speech, images and embeddings');
      case 'voice':
        return t('settings.summary.voice', 'Microphone and voice activation');
      case 'general':
        return (
          settings?.general?.server_address ||
          t('settings.summary.server', 'Server and connected clients')
        );
      case 'debug':
        return onOff(settings?.debug?.enabled);
      default:
        return t('settings.summary.connection', 'Current server connection');
    }
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
        placeholder={t('settings.search.placeholder', 'Search settings…')}
        aria-label={t('settings.search.label', 'Search settings')}
      />
    </div>
    <div class="settings-desktop-index">
      {#each groups as group (group.id)}
        <button
          class="snav-item"
          class:snav-item--active={!searchActive &&
            group.id === activeGroup?.id}
          type="button"
          aria-current={!searchActive && group.id === activeGroup?.id
            ? 'page'
            : undefined}
          onclick={() => navigateToCategory(group.id)}>{group.label()}</button
        >
      {/each}
    </div>
    <div class="settings-mobile-section-picker">
      <Dropdown
        id="settings-mobile-section"
        value={activeGroup?.id}
        options={mobileSectionOptions}
        ariaLabel={t('settings.sections', 'Settings sections')}
        onValueChange={navigateToCategory}
      />
    </div>
    <div class="settings-agents-link">
      <p>
        {t(
          'settings.agentShortcut.description',
          'Changing an Agent’s Model or Thinking level?',
        )}
      </p>
      <Button
        variant="secondary"
        onClick={() => onNavigateToAgentDefaults('agent')}
        >{t('settings.agentShortcut.action', 'Go to Agents')}
        <span aria-hidden="true">↗</span></Button
      >
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
          >{t('settings.loading', 'Loading settings…')}</Banner
        >
      {:else if loadError}
        <Banner variant="error"
          ><span>{loadError}</span><Button
            variant="secondary"
            onClick={loadSettings}>{t('common.retry', 'Retry')}</Button
          ></Banner
        >
      {:else}
        <header class="settings-page-heading">
          <div class="settings-page-eyebrow">
            {t('settings.title', 'Settings')}
          </div>
          <h2 tabindex="-1">
            {searchActive
              ? t('settings.search.results', 'Search results')
              : activeGroup?.label()}
          </h2>
          <p>
            {searchActive
              ? t(
                  'settings.search.guidance',
                  'Choose a topic to open its settings.',
                )
              : activeGroup?.description()}
          </p>
        </header>
        {#if searchActive}
          <div class="settings-search-results">
            {#each searchResults as panelId (panelId)}
              {@const panel = panelById.get(panelId)}
              <Button
                class="settings-search-result"
                onClick={() =>
                  panel
                    ? navigateToSection(panelId)
                    : onNavigateToAgentDefaults('defaults')}
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
                        'Agents → Shared defaults · Model, Thinking, fallbacks and Compaction',
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
        <div class="settings-feature-grid">
          {#each panels as panel (panel.id)}
            <section
              class="s-section settings-feature"
              class:settings-feature--expanded={expandedSections.has(panel.id)}
              data-settings-section={panel.id}
              hidden={searchActive || !activeGroup?.sections.includes(panel)}
              aria-labelledby={`settings-section-${panel.id}`}
            >
              <header class="settings-feature-heading">
                <div class="settings-feature-summary">
                  {sectionSummary(panel.id)}
                </div>
                <h3>
                  <button
                    type="button"
                    id={`settings-section-${panel.id}`}
                    aria-label={panel.label()}
                    aria-expanded={expandedSections.has(panel.id)}
                    aria-controls={`settings-body-${panel.id}`}
                    onclick={() => toggleSection(panel.id)}
                    >{panel.label()}<span
                      class="settings-feature-indicator"
                      aria-hidden="true"
                    ></span></button
                  >
                </h3>
                <p>{panel.subtitle()}</p>
              </header>
              <div
                id={`settings-body-${panel.id}`}
                class="settings-feature-body"
                hidden={!expandedSections.has(panel.id)}
              >
                {@render panelContent(panel.id)}
              </div>
            </section>
          {/each}
        </div>
      {/if}
    </div>
  </div>
</section>
