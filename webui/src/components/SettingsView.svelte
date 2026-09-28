<script>
  import { onMount, tick, untrack } from 'svelte';

  import WakewordVoiceSettings from './WakewordVoiceSettings.svelte';
  import DesktopConnectionSettings from './settings/DesktopConnectionSettings.svelte';
  import DesktopLiveVoiceShortcut from './settings/DesktopLiveVoiceShortcut.svelte';
  import SettingsAppearancePanel from './settings/SettingsAppearancePanel.svelte';
  import SettingsChannelsPanel from './settings/SettingsChannelsPanel.svelte';
  import SettingsDebugPanel from './settings/SettingsDebugPanel.svelte';
  import SettingsExtensionsPanel from './settings/SettingsExtensionsPanel.svelte';
  import SettingsGeneralPanel from './settings/SettingsGeneralPanel.svelte';
  import SettingsNotificationsPanel from './settings/SettingsNotificationsPanel.svelte';
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
  import { applyAppearanceSettings } from '$lib/appearancePrefs.svelte.js';
  import { t } from '$lib/i18n.js';
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
    // The app-level Desktop Voice owner (see app/desktop.svelte.js).
    desktopVoice = null,
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
  // Navigation follows user tasks; editor components do not define pages.
  const sections = [
    {
      id: 'appearance',
      label: () => t('settings.appearance.title'),
    },
    {
      id: 'session_titles',
      label: () => t('settings.sessionTitles.title'),
    },
    {
      id: 'preferences',
      label: () => t('settings.preferences.title'),
    },
    {
      id: 'notifications',
      label: () => t('settings.notifications.title'),
    },
    {
      id: 'providers',
      label: () => t('settings.providers.title'),
    },
    {
      id: 'voice_controls',
      label: () => t('settings.sections.voiceControls'),
    },
    {
      id: 'speech_models',
      label: () => t('settings.sections.speechModels'),
    },
    {
      id: 'live_voice_model',
      label: () => t('settings.sections.liveVoice'),
    },
    {
      id: 'live_voice_shortcut',
      label: () => t('settings.sections.liveVoiceShortcut'),
    },
    {
      id: 'recall',
      label: () => t('settings.sections.recall'),
    },
    {
      id: 'embedding_model',
      label: () => t('settings.specializedModels.embeddingModel'),
    },
    {
      id: 'reflection',
      label: () => t('settings.reflection.title'),
    },
    {
      id: 'web_search',
      label: () => t('settings.webSearch.title'),
    },
    { id: 'web_fetch', label: () => t('settings.webFetch.title') },
    {
      id: 'media_models',
      label: () => t('settings.sections.mediaModels'),
    },
    {
      id: 'decision_model',
      label: () => t('settings.sections.evaluation'),
    },
    {
      id: 'subagents',
      label: () => t('settings.sections.delegation'),
    },
    { id: 'channels', label: () => t('settings.channels.title') },
    {
      id: 'extensions',
      label: () => t('settings.extensions.title'),
    },
    { id: 'server', label: () => t('settings.general.title') },
    {
      id: 'desktop_connection',
      label: () => t('settings.desktop.connection.title'),
    },
    { id: 'debug', label: () => t('debug.settings') },
  ];
  const panelById = new Map(sections.map((section) => [section.id, section]));
  const modelTasksBySection = {
    speech_models: ['speech_to_text', 'text_to_speech'],
    live_voice_model: ['live_voice'],
    embedding_model: ['text_embedding'],
    media_models: [
      'image_understanding',
      'image_generation',
      'video_generation',
      'music_generation',
    ],
    decision_model: ['decision'],
  };
  let pages = $derived([
    {
      id: 'general',
      label: () => t('settings.pages.general'),
      description: () => t('settings.pages.generalDescription'),
      sections: [
        'appearance',
        'session_titles',
        'preferences',
        'notifications',
      ],
    },
    {
      id: 'providers',
      label: () => t('settings.providers.title'),
      description: () => t('settings.pages.providersDescription'),
      sections: ['providers'],
    },
    {
      id: 'voice',
      label: () => t('settings.voice.title'),
      description: () => t('settings.pages.voiceDescription'),
      sections: [
        'speech_models',
        'live_voice_model',
        ...(desktopCapabilities?.liveHotkey ? ['live_voice_shortcut'] : []),
        'voice_controls',
      ],
    },
    {
      id: 'memory',
      label: () => t('settings.pages.memory'),
      description: () => t('settings.pages.memoryDescription'),
      sections: ['recall', 'embedding_model', 'reflection'],
    },
    {
      id: 'tools',
      label: () => t('settings.pages.tools'),
      description: () => t('settings.pages.toolsDescription'),
      sections: [
        'web_search',
        'web_fetch',
        'media_models',
        'decision_model',
        'subagents',
      ],
    },
    {
      id: 'integrations',
      label: () => t('settings.pages.integrations'),
      description: () => t('settings.pages.integrationsDescription'),
      sections: ['channels', 'extensions'],
    },
    {
      id: 'system',
      label: () => t('settings.pages.system'),
      description: () => t('settings.pages.systemDescription'),
      sections: [
        'server',
        ...(desktopCapabilities?.serverSelection ? ['desktop_connection'] : []),
        'debug',
      ],
    },
  ]);
  let panels = $derived(
    pages.flatMap((page) => page.sections.map((id) => panelById.get(id))),
  );
  let mobileSectionOptions = $derived(
    pages.map((page) => ({
      value: page.id,
      label: page.label(),
    })),
  );
  let settings = $state(null);
  let loading = $state(true);
  let loadError = $state('');
  let providerRefreshGeneration = 0;
  let providerRefreshPending = false;
  let providersPanel = $state(null);
  let scrollContainer = $state(null);
  let documentRoot = $state(null);
  let activePageId = $state(
    untrack(() => initialScrollPosition?.pageId || 'general'),
  );
  let searchQuery = $state('');
  let searchResults = $state([]);
  let searchActive = $derived(searchQuery.trim().length > 0);
  let handledTargetPanelRequestId = -1;
  let restoreTop = untrack(() => Math.max(0, initialScrollPosition?.top || 0));
  let restorePending = untrack(() => Boolean(initialScrollPosition));
  let restoreFrame = null;
  let restoreAnchorId = '';

  onMount(() => {
    loadSettings();
    return () => {
      providerRefreshGeneration += 1;
      providerRefreshPending = false;
      if (restoreFrame !== null) cancelAnimationFrame(restoreFrame);
    };
  });

  $effect(() => {
    if (!loading && !pages.some((page) => page.id === activePageId))
      activePageId = pages[0].id;
  });

  $effect(() => {
    if (
      !loading &&
      targetPanelId &&
      targetPanelRequestId !== handledTargetPanelRequestId &&
      pageForDestination(targetPanelId)
    ) {
      handledTargetPanelRequestId = targetPanelRequestId;
      // A deliberate deep link replaces the remembered page; an ordinary
      // return carries its saved page and may still have an old target prop.
      if (!initialScrollPosition) void selectDestination(targetPanelId);
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
        const text = normalizedSearch(
          [
            pageForDestination(panel.id)?.label(),
            panel.label(),
            section?.textContent ?? '',
          ].join(' '),
        );
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

  function headingOffset(heading) {
    return Math.max(
      0,
      scrollContainer.scrollTop +
        heading.getBoundingClientRect().top -
        scrollContainer.getBoundingClientRect().top -
        24,
    );
  }

  function queueRestore() {
    if (
      (!restorePending && !restoreAnchorId) ||
      loading ||
      !scrollContainer ||
      restoreFrame !== null
    )
      return;
    restoreFrame = requestAnimationFrame(() => {
      restoreFrame = null;
      if (!scrollContainer) return;
      const anchor =
        restoreAnchorId && documentRoot?.querySelector('#' + restoreAnchorId);
      if (anchor) scrollContainer.scrollTop = headingOffset(anchor);
      else if (restorePending) scrollContainer.scrollTop = restoreTop;
    });
  }

  function releaseRestore() {
    restorePending = false;
    restoreAnchorId = '';
  }

  function captureScrollPosition() {
    return scrollContainer
      ? {
          top: Math.max(0, scrollContainer.scrollTop),
          pageId: activePageId,
        }
      : null;
  }

  function handleContentScroll() {
    if (!restorePending && !searchActive)
      onScrollPositionChange(captureScrollPosition());
  }

  function pageForDestination(id) {
    return (
      pages.find((page) => page.id === id) ??
      pages.find((page) => page.sections.includes(id))
    );
  }

  async function selectDestination(id) {
    const page = pageForDestination(id);
    if (!page) return;
    releaseRestore();
    searchQuery = '';
    activePageId = page.id;
    await tick();
    const headingId =
      page.id === id ? 'settings-page-' + page.id : 'settings-section-' + id;
    const heading = documentRoot?.querySelector('#' + headingId);
    if (scrollContainer) {
      scrollContainer.scrollTop =
        page.id === id || !heading ? 0 : headingOffset(heading);
      // Keep a deep link in view while asynchronous editors above it settle.
      if (page.id !== id && heading) {
        restoreAnchorId = headingId;
        queueRestore();
      }
    }
    onScrollPositionChange(captureScrollPosition());
    heading?.focus({ preventScroll: true });
  }

  function navigateToSection(id) {
    return autosaveContext.requestTransition(() => selectDestination(id));
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
      title: t('errors.appError'),
      message,
      variant: 'error',
    });
  }

  function applySettings(nextSettings) {
    settings = nextSettings;
    setApplicationTimeZone(nextSettings?.general?.timezone);

    applyAppearanceSettings(nextSettings?.appearance);
  }

  function commitSettings(nextSettings) {
    // Each other Settings editor saves its own section. Its full response may
    // predate a Provider refresh, whose projection has an independent owner.
    applySettings({
      ...nextSettings,
      providers: settings.providers,
      local_models: settings.local_models,
    });
    onSettingsCommit(settings);
  }

  function commitProviderSettings(nextSettings) {
    const refreshAfterCommit = providerRefreshPending;
    providerRefreshGeneration += 1;
    settings = {
      ...settings,
      providers: nextSettings.providers,
      local_models: nextSettings.local_models,
    };
    onSettingsCommit(settings);
    if (refreshAfterCommit) {
      // A Model-refresh result may predate another Provider change. Re-read
      // after its commit rather than losing the pending invalidation.
      void refreshProviderSettings().catch(reportProviderRefreshError);
    }
  }

  function reportProviderRefreshError(error) {
    reportSettingsError(`${t('settings.loadError')} ${error.message}`);
  }

  async function refreshProviderSettings() {
    const generation = ++providerRefreshGeneration;
    providerRefreshPending = true;
    try {
      const nextSettings = await getSettings();
      if (generation !== providerRefreshGeneration) return;
      // Provider invalidations also refresh local Model context limits. Other
      // editors retain their own baseline and pending draft; a Provider refresh
      // must neither remount them nor turn remote values into local edits.
      settings = {
        ...settings,
        providers: nextSettings.providers,
        local_models: nextSettings.local_models,
      };
      onSettingsCommit(settings);
    } catch (error) {
      if (generation === providerRefreshGeneration) throw error;
    } finally {
      if (generation === providerRefreshGeneration)
        providerRefreshPending = false;
    }
  }

  async function loadSettings() {
    loading = true;
    loadError = '';

    try {
      const nextSettings = await getSettings();
      applySettings(nextSettings);
    } catch (error) {
      loadError = `${t('settings.loadError')} ${error.message}`;
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
      onCommitProviderSettings={commitProviderSettings}
      {onToast}
      onError={(message) => reportSettingsError(message)}
      onRefreshProviderSettings={refreshProviderSettings}
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
  {:else if modelTasksBySection[panelId]}
    <SettingsSpecializedModelsPanel
      taskTypes={modelTasksBySection[panelId]}
      showTaskLabels={!['embedding_model', 'live_voice_model'].includes(
        panelId,
      )}
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
  {:else if panelId === 'voice_controls'}
    <WakewordVoiceSettings
      {agents}
      {settings}
      wakewordAvailable={desktopCapabilities?.wakeword === true}
      {desktopVoice}
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
  {:else if panelId === 'notifications'}
    <SettingsNotificationsPanel
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
  {:else if panelId === 'server'}
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
  {:else if panelId === 'live_voice_shortcut'}
    <DesktopLiveVoiceShortcut {onToast} />
  {/if}
{/snippet}

<section class={SETTINGS_LAYOUT_CLASS} aria-label={t('settings.title')}>
  <nav class="settings-nav secondary-pane" aria-label={t('settings.sections')}>
    <div class="settings-nav-title secondary-pane__title">
      {t('settings.title')}
    </div>
    <div class="settings-search">
      <input
        class="settings-search-input"
        type="search"
        spellcheck="false"
        autocomplete="off"
        value={searchQuery}
        oninput={handleSearchInput}
        placeholder={t('settings.search.placeholder')}
        aria-label={t('settings.search.label')}
      />
    </div>
    <div class="settings-desktop-index">
      {#each pages as page (page.id)}
        <button
          class="snav-item"
          class:snav-item--active={!searchActive && page.id === activePageId}
          type="button"
          aria-current={!searchActive && page.id === activePageId
            ? 'page'
            : undefined}
          onclick={() => navigateToSection(page.id)}>{page.label()}</button
        >
      {/each}
    </div>
    <div class="settings-mobile-section-picker">
      <Dropdown
        id="settings-mobile-section"
        value={activePageId}
        options={mobileSectionOptions}
        ariaLabel={t('settings.sections')}
        onValueChange={navigateToSection}
      />
    </div>
  </nav>
  <!-- Keyboard interaction releases scroll restoration for this scrollable region. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
  <div
    class="settings-content"
    role="region"
    aria-label={t('settings.content')}
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
        <Banner variant="neutral">{t('settings.loading')}</Banner>
      {:else if loadError}
        <Banner variant="error"
          ><span>{loadError}</span><Button
            variant="secondary"
            onClick={loadSettings}>{t('common.retry')}</Button
          ></Banner
        >
      {:else}
        {#if searchActive}
          <header class="settings-page-heading">
            <h2>{t('settings.search.results')}</h2>
            <p role="status">
              {t('settings.search.resultCount', {
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
                  >{panel ? panel.label() : t('agents.shared.title')}</span
                >
                <span class="settings-search-result__description"
                  >{panel
                    ? pageForDestination(panelId)?.label()
                    : t('settings.agentShortcut.search')}</span
                >
              </Button>
            {:else}
              <EmptyState
                density="compact"
                description={t('settings.search.noMatches')}
              />
            {/each}
          </div>
        {/if}
        <div class="settings-editors">
          {#each pages as page (page.id)}
            <article
              class="settings-page"
              data-settings-page={page.id}
              hidden={searchActive || activePageId !== page.id}
              aria-labelledby={'settings-page-' + page.id}
            >
              <header class="settings-page-heading">
                <h2 id={'settings-page-' + page.id} tabindex="-1">
                  {page.label()}
                </h2>
                <p>{page.description()}</p>
              </header>
              {#if page.id === 'providers'}
                <div class="settings-related">
                  <span>{t('settings.agentShortcut.hint')}</span>
                  <Button
                    class="settings-defaults-link"
                    variant="tertiary"
                    onClick={navigateToDefaults}
                  >
                    {t('agents.shared.title')}
                    <span aria-hidden="true">↗</span>
                  </Button>
                </div>
              {/if}
              {#each page.sections as panelId (panelId)}
                {@const panel = panelById.get(panelId)}
                <section
                  class="settings-editor s-section"
                  data-settings-section={panelId}
                  hidden={searchActive || activePageId !== page.id}
                  aria-labelledby={page.sections.length === 1
                    ? 'settings-page-' + page.id
                    : 'settings-section-' + panelId}
                >
                  {#if page.sections.length > 1}
                    <header class="settings-section-heading s-section__head">
                      <h3
                        class="s-section__title"
                        id={'settings-section-' + panelId}
                        tabindex="-1"
                      >
                        {panel.label()}
                      </h3>
                    </header>
                  {/if}
                  <div class="settings-editor-body s-section__body">
                    {@render panelContent(panelId)}
                  </div>
                </section>
              {/each}
            </article>
          {/each}
        </div>
      {/if}
    </div>
  </div>
</section>
