<script>
  import {
    settingsPages,
    panelById,
    modelTasksBySection,
  } from '$lib/settingsView/catalog.js';
  import { createSettingsPageNavigation } from '$lib/settingsView/pageNavigation.svelte.js';
  import { onMount } from 'svelte';

  import WakewordVoiceSettings from './WakewordVoiceSettings.svelte';
  import TranscriptionAudioSettings from './voice/TranscriptionAudioSettings.svelte';
  import ArchiveEntriesPanel from './archive/ArchiveEntriesPanel.svelte';
  import DesktopConnectionSettings from './settings/DesktopConnectionSettings.svelte';
  import DesktopLiveVoiceShortcut from './settings/DesktopLiveVoiceShortcut.svelte';
  import DesktopMicrophoneSettings from './settings/DesktopMicrophoneSettings.svelte';
  import DesktopDictationSettings from './settings/DesktopDictationSettings.svelte';
  import SettingsActivityPanel from './settings/SettingsActivityPanel.svelte';
  import SettingsAppearancePanel from './settings/SettingsAppearancePanel.svelte';
  import SettingsChannelsPanel from './settings/SettingsChannelsPanel.svelte';
  import SettingsCredentialsPanel from './settings/SettingsCredentialsPanel.svelte';
  import SettingsDebugPanel from './settings/SettingsDebugPanel.svelte';
  import SettingsArchivePanel from './settings/SettingsArchivePanel.svelte';
  import SettingsExtensionsPanel from './settings/SettingsExtensionsPanel.svelte';
  import SettingsGeneralPanel from './settings/SettingsGeneralPanel.svelte';
  import SettingsAccessPanel from './settings/SettingsAccessPanel.svelte';
  import SettingsLibrarianPanel from './settings/SettingsLibrarianPanel.svelte';
  import SettingsLiveVoicePanel from './settings/SettingsLiveVoicePanel.svelte';
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
  import { applyArchiveSettings } from '$lib/archiveRetention.svelte.js';
  import { t } from '$lib/i18n.js';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';

  import {
    SETTINGS_LAYOUT_CLASS,
    describeSecretStoreProblem,
  } from '$lib/settingsView.js';

  const noop = () => {};
  // The pages where credentials are entered (Provider keys and sign-ins, web
  // service keys, Channel tokens, Extension secrets and named credentials).
  const CREDENTIAL_PAGES = new Set(['providers', 'tools', 'integrations']);
  let {
    // The place is `[page]` or `[page, section]`, or `[page, subPage]` on a
    // page with sub-pages (an Archive entry: `['archive', entryId]`); an App
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
    onDebugEnabledChange = noop,
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

{#snippet panelContent(panelId, active)}
  {#if panelId === 'preferences'}
    <SettingsGeneralPanel
      page="preferences"
      {settings}
      {onOpenSetupGuide}
      onCommit={commitGeneralSettings}
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
      {active}
      {onToast}
      {channelsRefreshToken}
      {agentsRefreshToken}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'extensions'}
    <SettingsExtensionsPanel
      {onToast}
      {subscribeExtensionInvalidations}
      {agentsRefreshToken}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'credentials'}
    <SettingsCredentialsPanel {credentialsRefreshToken} {onToast} />
  {:else if panelId === 'live_voice_model'}
    <SettingsLiveVoicePanel
      {settings}
      onCommit={(next) => commitSettings(next, ['model_tasks.live_voice'])}
      onError={(message) => reportSettingsError(message)}
      {onOpenSession}
      {modelsRefreshToken}
      {agentsRefreshToken}
      {sessionsRefreshToken}
    />
  {:else if modelTasksBySection[panelId]}
    <SettingsSpecializedModelsPanel
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
    <SettingsSessionTitlesPanel
      {settings}
      onCommit={(next) => commitSettings(next, ['session_titles'])}
      onError={(message) => reportSettingsError(message)}
      {modelsRefreshToken}
    />
  {:else if panelId === 'recall'}
    <SettingsRecallPanel
      {settings}
      onCommit={(next) =>
        commitSettings(next, ['recall', 'model_tasks.text_embedding'])}
      onError={(message) => reportSettingsError(message)}
      {modelsRefreshToken}
      {recallIndexStatus}
    />
  {:else if panelId === 'voice_controls'}
    <WakewordVoiceSettings
      {agents}
      wakewordAvailable={desktopCapabilities?.wakeword === true}
      {desktopVoice}
      {onToast}
    />
  {:else if panelId === 'transcription_audio'}
    <TranscriptionAudioSettings
      {settings}
      onCommit={(next) => commitSettings(next, ['speech'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'web_fetch'}
    <SettingsWebFetchPanel
      {settings}
      onCommit={(next) =>
        commitSettings(next, ['web_fetch', 'web_search.services'])}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'web_search'}
    <SettingsWebSearchPanel
      {settings}
      onCommit={(next) =>
        commitSettings(next, ['web_search', 'web_fetch.services'])}
      {onToast}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'subagents'}
    <SettingsSubAgentsPanel
      {settings}
      onCommit={(next) => commitSettings(next, ['subagents'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'reflection'}
    <SettingsReflectionPanel
      {settings}
      onCommit={(next) => commitSettings(next, ['reflection'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'librarian'}
    <SettingsLibrarianPanel
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
    <SettingsNotificationsPanel
      {settings}
      onCommit={(next) => commitSettings(next, ['notifications'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'appearance'}
    <SettingsAppearancePanel
      {settings}
      {desktopZoom}
      onCommit={(next) => commitSettings(next, ['appearance'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'archive_retention'}
    <SettingsArchivePanel
      {settings}
      onCommit={(next) => commitSettings(next, ['archive'])}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'archive_entries'}
    <ArchiveEntriesPanel
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
  {:else if panelId === 'debug'}
    <SettingsDebugPanel
      {settings}
      onCommit={(next) => commitSettings(next, ['debug'])}
      onError={(message) => reportSettingsError(message)}
      {onDebugEnabledChange}
    />
  {:else if panelId === 'server'}
    <SettingsGeneralPanel
      {settings}
      {clientsRefreshToken}
      {onOpenSetupGuide}
      onCommit={commitGeneralSettings}
      onError={(message) => reportSettingsError(message)}
    />
  {:else if panelId === 'access'}
    <SettingsAccessPanel {devicesRefreshToken} {onToast} />
  {:else if panelId === 'desktop_connection'}
    <DesktopConnectionSettings {onToast} />
  {:else if panelId === 'live_voice_shortcut'}
    <DesktopLiveVoiceShortcut {onToast} />
  {:else if panelId === 'microphone'}
    <DesktopMicrophoneSettings {desktopVoice} {onToast} />
  {:else if panelId === 'desktop_dictation'}
    <DesktopDictationSettings {onToast} />
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
              {t('settings.search.resultCount', {
                count: pageNavigation.searchResults.length,
              })}
            </p>
          </header>
          <div class="settings-search-results">
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
              <EmptyState
                density="compact"
                description={t('settings.search.noMatches')}
              />
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
                    {@render panelContent(
                      panelId,
                      !pageNavigation.searchActive &&
                        pageNavigation.activePageId === page.id &&
                        (!subPage || panelId === page.subPages),
                    )}
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
