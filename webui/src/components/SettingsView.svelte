<script>
  import {
    settingsPages,
    panelById,
    modelTasksBySection,
  } from '$lib/settingsView/catalog.js';
  import { createSettingsPageNavigation } from '$lib/settingsView/pageNavigation.svelte.js';
  import { onMount, tick, untrack } from 'svelte';

  import SettingsActivityPanel from './settings/SettingsActivityPanel.svelte';
  import LazyContent from './ui/LazyContent.svelte';
  import { loadComponent } from '$lib/lazyContent.js';
  import { settingsPanelLoaders } from '$lib/settingsView/panelLoaders.js';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import Dropdown from './Dropdown.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import { getSettings } from '$lib/api.js';
  import { setApplicationTimeZone } from '$lib/dateTimePrefs.svelte.js';
  import { applyAppearanceSettings } from '$lib/appearancePrefs.svelte.js';
  import { applyArchiveSettings } from '$lib/archiveRetention.svelte.js';
  import { t } from '$lib/i18n.js';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';

  import {
    SETTINGS_LAYOUT_CLASS,
    describeSecretStoreProblem,
  } from '$lib/settingsView.js';

  const noop = () => {};
  // The pages where credentials are entered (Provider keys and sign-ins, web
  // service keys, Channel tokens, Extension secrets, named credentials and
  // Agent Secrets).
  const CREDENTIAL_PAGES = new Set([
    'providers',
    'tools',
    'integrations',
    'secrets',
  ]);
  let {
    // The place is `[page]` or `[page, section]`, or `[page, subPage]` on a
    // page with sub-pages (an Archive entry or Debug trace); an App
    // deep link may name a page or a section id alone. An empty place shows
    // the General page.
    navigation = createStandaloneNavigation(),
    providerAuthEvent = null,
    connectProvider = null,
    disconnectProvider = null,
    onToast = noop,
    onSettingsCommit = noop,
    onNavigateToAgentDefaults = noop,
    agents = [],
    projects = [],
    desktopCapabilities = null,
    // The app-level Desktop Voice owner (see app/desktop.svelte.js).
    desktopVoice = null,
    // The app-level window zoom owner in a Desktop that offers it, else null
    // (see app/desktopZoom.svelte.js).
    desktopZoom = null,
    debugTracesRefreshToken = 0,
    onOpenSetupGuide = noop,
    modelsRefreshToken = 0,
    // The latest pushed Recall index status, applied by the Conversation
    // search panel.
    recallIndexStatus = null,
    // The server's running downloads, installations and indexing passes,
    // kept current by its pushes; General shows them at the top.
    backgroundActivity = [],
    // The `secrets` projection of `data_store.status`; the pages that manage
    // credentials warn while the Secret Store cannot be read.
    secretStore = null,
    clientsRefreshToken = 0,
    // Paired Devices and decided pairings changed (Access & devices).
    devicesRefreshToken = 0,
    // Saved credentials changed (Credentials).
    credentialsRefreshToken = 0,
    // Agent Secrets, Secret requests or their Sources changed (Secrets).
    agentSecretsRefreshToken = 0,
    channelsRefreshToken = 0,
    archiveRefreshToken = 0,
    agentsRefreshToken = 0,
    projectsRefreshToken = 0,
    sessionsRefreshToken = 0,
    // A Librarian pass starts and ends as a Skills change.
    skillsRefreshToken = 0,
    // Opens a Session in Chat (Skill maintenance opens the Librarian's).
    onOpenSession = noop,
    initialScrollPosition = null,
    onScrollPositionChange = noop,
    // The App's Extension invalidations (see app/extensions.svelte.js).
    subscribeExtensionInvalidations = null,
  } = $props();

  const secretStoreProblem = $derived(describeSecretStoreProblem(secretStore));

  export function handleProviderAuthCompleted(event) {
    providersPanel?.handleProviderAuthCompleted(event);
  }

  export function getScrollPosition() {
    return pageNavigation.captureScrollPosition();
  }

  let pages = $derived(settingsPages(desktopCapabilities));
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
  let lastModelsRefreshToken = null;
  let visitedPages = $state({});
  let searchLoading = $state(false);
  let searchLoadError = $state(false);
  const pageNavigation = createSettingsPageNavigation({
    get navigation() {
      return navigation;
    },
    get pages() {
      return pages;
    },
    get loading() {
      return loading;
    },
    get initialScrollPosition() {
      return initialScrollPosition;
    },
    get onScrollPositionChange() {
      return onScrollPositionChange;
    },
    get onNavigateToAgentDefaults() {
      return onNavigateToAgentDefaults;
    },
  });

  // Opening a page admits its editors once; hiding it never discards drafts,
  // authentication state, or a save already in flight. Search explicitly needs
  // every page's rendered labels and help, so it admits the remaining pages.
  $effect(() => {
    const wanted = pageNavigation.searchActive
      ? pages.map((page) => page.id)
      : [pageNavigation.activePageId];
    untrack(() => {
      for (const pageId of wanted) visitedPages[pageId] = true;
    });
  });

  $effect(() => {
    if (!pageNavigation.searchActive || loading) return;
    const loaders = new Set(
      pages.flatMap((page) =>
        page.sections.map((id) => settingsPanelLoaders[id]),
      ),
    );
    let current = true;
    searchLoading = true;
    searchLoadError = false;
    void Promise.all([...loaders].map(loadComponent))
      .then(async () => {
        // Imports and their mounted labels must settle before reporting the
        // complete search result count. Later server data re-indexes as usual.
        await tick();
        if (current) pageNavigation.refreshSearchResults();
      })
      .catch(() => {
        if (current) searchLoadError = true;
      })
      .finally(() => {
        if (current) searchLoading = false;
      });
    return () => {
      current = false;
    };
  });

  // Before Providers has ever opened there is no panel to handle catalog
  // invalidations. Refresh its projection here; after mounting, the panel
  // retains its existing modal-aware refresh gate.
  $effect(() => {
    const token = modelsRefreshToken;
    if (loading) return;
    if (lastModelsRefreshToken !== null && token !== lastModelsRefreshToken) {
      if (!providersPanel)
        void refreshProviderSettings().catch(reportProviderRefreshError);
    }
    lastModelsRefreshToken = token;
  });

  onMount(() => {
    loadSettings();
    return () => {
      providerRefreshGeneration += 1;
      providerRefreshPending = false;
    };
  });

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
    applyArchiveSettings(nextSettings?.archive);
  }

  function commitSettings(nextSettings, sections) {
    // A save owns only these sections or bindings; unrelated response values
    // may predate another editor's completed save.
    const merged = { ...settings };
    for (const section of sections) {
      const [key, child] = section.split('.');
      if (child) {
        merged[key] = { ...merged[key], [child]: nextSettings[key]?.[child] };
      } else {
        merged[key] = nextSettings[key];
      }
    }
    applySettings(merged);
    onSettingsCommit(settings);
  }

  function commitGeneralSettings(nextSettings, field) {
    commitSettings(
      {
        general: { ...settings.general, [field]: nextSettings.general[field] },
      },
      ['general'],
    );
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
    // The loaded content shows the current place afresh, also after a retry.
    pageNavigation.appliedPlaceKey = null;

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

{#snippet panelContent(panelId, active, Panel)}
  {#if panelId === 'preferences'}
    <Panel
      page="preferences"
      {settings}
      {onOpenSetupGuide}
      onCommit={commitGeneralSettings}
      onError={reportSettingsError}
    />
  {:else if panelId === 'providers'}
    <Panel
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
    <Panel
      {active}
      {onToast}
      {channelsRefreshToken}
      {agentsRefreshToken}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'extensions'}
    <Panel
      {onToast}
      {subscribeExtensionInvalidations}
      {agentsRefreshToken}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'credentials'}
    <Panel {credentialsRefreshToken} {onToast} />
  {:else if panelId === 'agent_secrets'}
    <Panel {agentSecretsRefreshToken} {agents} {onToast} />
  {:else if panelId === 'live_voice_model'}
    <Panel
      {settings}
      onCommit={(next) => commitSettings(next, ['model_tasks.live_voice'])}
      onError={(message) => reportSettingsError(message)}
      {onOpenSession}
      {modelsRefreshToken}
      {agentsRefreshToken}
      {sessionsRefreshToken}
    />
  {:else if modelTasksBySection[panelId]}
    <Panel
      {active}
      taskTypes={modelTasksBySection[panelId]}
      {settings}
      onCommit={(next) =>
        commitSettings(
          next,
          modelTasksBySection[panelId].map((task) => `model_tasks.${task}`),
        )}
      onError={(message) => reportSettingsError(message)}
      {modelsRefreshToken}
    />
  {:else if panelId === 'session_titles'}
    <Panel
      {settings}
      onCommit={(next) => commitSettings(next, ['session_titles'])}
      onError={(message) => reportSettingsError(message)}
      {modelsRefreshToken}
    />
  {:else if panelId === 'recall'}
    <Panel
      {settings}
      onCommit={(next) =>
        commitSettings(next, ['recall', 'model_tasks.text_embedding'])}
      onError={(message) => reportSettingsError(message)}
      {modelsRefreshToken}
      {recallIndexStatus}
    />
  {:else if panelId === 'voice_controls'}
    <Panel
      {agents}
      wakewordAvailable={desktopCapabilities?.wakeword === true}
      {desktopVoice}
      {onToast}
    />
  {:else if panelId === 'transcription_audio'}
    <Panel
      {settings}
      onCommit={(next) => commitSettings(next, ['speech'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'web_fetch'}
    <Panel
      {settings}
      onCommit={(next) =>
        commitSettings(next, ['web_fetch', 'web_search.services'])}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'web_search'}
    <Panel
      {settings}
      onCommit={(next) =>
        commitSettings(next, ['web_search', 'web_fetch.services'])}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'subagents'}
    <Panel
      {settings}
      onCommit={(next) => commitSettings(next, ['subagents'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'reflection'}
    <Panel
      {settings}
      onCommit={(next) => commitSettings(next, ['reflection'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'librarian'}
    <Panel
      {settings}
      {agents}
      onCommit={(next) => commitSettings(next, ['librarian'])}
      onError={(message) => reportSettingsError(message)}
      {onOpenSession}
      {modelsRefreshToken}
      {agentsRefreshToken}
      {skillsRefreshToken}
    />
  {:else if panelId === 'notifications'}
    <Panel
      {settings}
      onCommit={(next) => commitSettings(next, ['notifications'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'appearance'}
    <Panel
      {settings}
      {desktopZoom}
      onCommit={(next) => commitSettings(next, ['appearance'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'archive_retention'}
    <Panel
      {settings}
      onCommit={(next) => commitSettings(next, ['archive'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'archive_entries'}
    <Panel
      navigation={pageNavigation.archiveNavigation}
      active={pageNavigation.activePageId === 'archive'}
      {agents}
      {projects}
      {archiveRefreshToken}
      {agentsRefreshToken}
      {projectsRefreshToken}
      {sessionsRefreshToken}
      {onToast}
    />
  {:else if panelId === 'debug_capture'}
    <Panel
      {settings}
      onCommit={(next) => commitSettings(next, ['debug'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'debug_traces'}
    <Panel
      {settings}
      {active}
      navigation={pageNavigation.debugNavigation}
      {debugTracesRefreshToken}
    />
  {:else if panelId === 'server'}
    <Panel
      {settings}
      {clientsRefreshToken}
      {onOpenSetupGuide}
      onCommit={commitGeneralSettings}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'access'}
    <Panel {devicesRefreshToken} {onToast} />
  {:else if panelId === 'desktop_connection'}
    <Panel {onToast} />
  {:else if panelId === 'computer_sharing'}
    <Panel />
  {:else if panelId === 'live_voice_shortcut'}
    <Panel {onToast} />
  {:else if panelId === 'microphone'}
    <Panel {desktopVoice} {onToast} />
  {:else if panelId === 'desktop_dictation'}
    <Panel {onToast} />
  {/if}
{/snippet}

<section class={SETTINGS_LAYOUT_CLASS} aria-label={t('settings.title')}>
  <nav class="settings-nav secondary-pane" aria-label={t('settings.sections')}>
    <div class="settings-nav-head secondary-pane__header">
      <span class="settings-nav-title secondary-pane__title">
        {t('settings.title')}
      </span>
    </div>
    <div class="settings-search secondary-pane__pinned">
      <svg
        class="settings-search-icon"
        viewBox="0 0 16 16"
        width="16"
        height="16"
        fill="none"
        stroke="currentColor"
        aria-hidden="true"
        ><circle cx="7" cy="7" r="4.5" /><path d="m10.5 10.5 3 3" /></svg
      >
      <input
        class="settings-search-input"
        type="search"
        spellcheck="false"
        autocomplete="off"
        value={pageNavigation.searchQuery}
        oninput={pageNavigation.handleSearchInput}
        onkeydown={pageNavigation.handleSearchKeydown}
        placeholder={t('settings.search.placeholder')}
        aria-label={t('settings.search.label')}
      />
    </div>
    <div class="settings-desktop-index secondary-pane__scroll secondary-list">
      {#each pages as page (page.id)}
        <button
          class="snav-item secondary-list__item"
          class:active={!pageNavigation.searchActive &&
            page.id === pageNavigation.activePageId}
          type="button"
          aria-current={!pageNavigation.searchActive &&
          page.id === pageNavigation.activePageId
            ? 'page'
            : undefined}
          onclick={() => pageNavigation.openDestination(page.id)}
          >{page.label()}</button
        >
      {/each}
    </div>
    <div class="settings-mobile-section-picker">
      <Dropdown
        id="settings-mobile-section"
        value={pageNavigation.activePageId}
        options={mobileSectionOptions}
        ariaLabel={t('settings.sections')}
        onValueChange={pageNavigation.openDestination}
      />
    </div>
  </nav>
  <!-- Keyboard interaction releases scroll restoration for this scrollable region. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
  <div
    class="settings-content page-scroll"
    role="region"
    aria-label={t('settings.content')}
    tabindex="0"
    bind:this={pageNavigation.scrollContainer}
    onscroll={pageNavigation.handleContentScroll}
    onwheel={pageNavigation.releaseRestore}
    ontouchstart={pageNavigation.releaseRestore}
    onpointerdown={pageNavigation.releaseRestore}
    onkeydown={pageNavigation.releaseRestore}
  >
    <div class="s-doc" bind:this={pageNavigation.documentRoot}>
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
        {#if pageNavigation.searchActive}
          <header class="settings-page-heading">
            <h2 class="view-header__title">{t('settings.search.results')}</h2>
            <p class="view-header__subtitle" role="status">
              {#if searchLoading}
                {t('settings.loading')}
              {:else}
                {t('settings.search.resultCount', {
                  count: pageNavigation.searchResults.length,
                })}
              {/if}
            </p>
          </header>
          {#if searchLoadError}
            <Banner variant="error" role="alert">
              <span>{t('common.viewLoadError')}</span>
              <Button
                variant="secondary"
                onClick={() => window.location.reload()}
              >
                {t('common.reloadPage')}
              </Button>
            </Banner>
          {/if}
          <div class="settings-search-results" aria-busy={searchLoading}>
            {#each pageNavigation.searchResults as result (result.key)}
              <Button
                class="settings-search-result"
                onClick={() => pageNavigation.openResult(result)}
              >
                <span class="settings-search-result__title"
                  >{pageNavigation.resultTitle(result)}</span
                >
                <span class="settings-search-result__description"
                  >{pageNavigation.resultLocation(result)}</span
                >
              </Button>
            {:else}
              {#if !searchLoading && !searchLoadError}
                <EmptyState
                  density="compact"
                  description={t('settings.search.noMatches')}
                />
              {/if}
            {/each}
          </div>
        {/if}
        <div class="settings-editors" bind:this={pageNavigation.editorsElement}>
          {#each pages as page (page.id)}
            <!-- A shown sub-page replaces its page's heading and other sections. -->
            {@const subPage =
              pageNavigation.subPageShown &&
              pageNavigation.activePageId === page.id}
            <article
              class="settings-page"
              class:settings-page--sub-page={subPage}
              data-settings-page={page.id}
              hidden={pageNavigation.searchActive ||
                pageNavigation.activePageId !== page.id}
              aria-labelledby={'settings-page-' + page.id}
            >
              <header class="settings-page-heading" hidden={subPage}>
                <h2
                  id={'settings-page-' + page.id}
                  class="view-header__title"
                  tabindex="-1"
                >
                  {page.label()}
                </h2>
                <p class="view-header__subtitle">{page.description()}</p>
              </header>
              {#if page.id === 'providers'}
                <p class="settings-related">
                  {t('settings.agentShortcut.hint')}
                  <button
                    type="button"
                    class="settings-defaults-link"
                    onclick={pageNavigation.navigateToDefaults}
                  >
                    {t('agents.shared.title')}<span aria-hidden="true">↗</span>
                  </button>
                </p>
              {/if}
              <!-- Only while something runs or needs attention; not a section,
                   so search and navigation never lead to it. -->
              {#if secretStoreProblem && CREDENTIAL_PAGES.has(page.id)}
                <Banner variant="error" data-secret-store-problem>
                  {secretStoreProblem}
                </Banner>
              {/if}
              {#if page.id === 'general' && backgroundActivity.length > 0}
                <SettingsActivityPanel
                  activities={backgroundActivity}
                  onOpen={pageNavigation.openDestination}
                  onError={(message) => reportSettingsError(message)}
                />
              {/if}
              {#each page.sections as panelId (panelId)}
                {@const panel = panelById.get(panelId)}
                <section
                  class="settings-editor s-section"
                  data-settings-section={panelId}
                  hidden={pageNavigation.searchActive ||
                    pageNavigation.activePageId !== page.id ||
                    (subPage && panelId !== page.subPages)}
                  aria-labelledby={page.sections.length === 1
                    ? 'settings-page-' + page.id
                    : 'settings-section-' + panelId}
                >
                  {#if page.sections.length > 1}
                    <header
                      class="settings-section-heading s-section__head"
                      hidden={subPage}
                    >
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
                    {#if visitedPages[page.id]}
                      <LazyContent load={settingsPanelLoaders[panelId]}>
                        {#snippet children(Panel)}
                          {@render panelContent(
                            panelId,
                            !pageNavigation.searchActive &&
                              pageNavigation.activePageId === page.id &&
                              (!subPage || panelId === page.subPages),
                            Panel,
                          )}
                        {/snippet}
                      </LazyContent>
                    {/if}
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
