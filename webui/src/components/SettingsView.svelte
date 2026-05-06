<script>
  import { onMount } from 'svelte';

  import { rpc } from '$lib/api.js';
  import { init, t } from '$lib/i18n.js';
  import {
    SETTINGS_LAYOUT_CLASS,
    buildLanguageOptions,
    createLanguageUpdatePayload,
    describeProvider,
    formatServerHost,
    getDataDirectoryValue,
    providerStatusClass,
    providerStatusLabel,
    getProviderItems,
    getPersistedLanguageId,
    isLanguageSaveDisabled,
  } from '$lib/settingsView.js';

  const panels = [
    {
      id: 'general',
      labelKey: 'settings.general.title',
      labelFallback: 'General',
      label: () => t('settings.general.title', 'General'),
      subtitle: () =>
        t(
          'settings.general.subtitle',
          'Bind address and application data directory.',
        ),
    },
    {
      id: 'providers',
      labelKey: 'settings.providers.title',
      labelFallback: 'Providers',
      label: () => t('settings.providers.title', 'Providers'),
      subtitle: () =>
        t(
          'settings.providers.subtitle',
          'API-key presence and endpoint metadata for available providers.',
        ),
    },
    {
      id: 'appearance',
      labelKey: 'settings.appearance.title',
      labelFallback: 'Appearance',
      label: () => t('settings.appearance.title', 'Appearance'),
      subtitle: () => t('settings.appearance.subtitle', 'Language preference.'),
    },
  ];

  let activePanelId = $state('general');
  let settings = $state(null);
  let loading = $state(true);
  let loadError = $state('');
  let saveError = $state('');
  let saveNotice = $state('');
  let saving = $state(false);
  let selectedLanguageId = $state('en');

  let activePanel = $derived(
    panels.find((panel) => panel.id === activePanelId) ?? panels[0],
  );
  let serverHostValue = $derived(
    formatServerHost(settings?.general?.server, t),
  );
  let dataDirectoryValue = $derived(getDataDirectoryValue(settings, t));
  let providerItems = $derived(getProviderItems(settings));
  let availableLanguageOptions = $derived(
    buildLanguageOptions(settings?.appearance),
  );
  let persistedLanguageId = $derived(getPersistedLanguageId(settings));
  let saveDisabled = $derived(
    isLanguageSaveDisabled({
      loading,
      saving,
      selectedLanguageId,
      persistedLanguageId,
    }),
  );

  onMount(() => {
    loadSettings();
  });

  function selectPanel(panelId) {
    activePanelId = panelId;
    saveError = '';
    saveNotice = '';
  }

  function applySettings(nextSettings) {
    settings = nextSettings;

    const language = nextSettings?.appearance?.language ?? 'en';
    selectedLanguageId = language;
    init(language);
  }

  async function loadSettings() {
    loading = true;
    loadError = '';

    try {
      const nextSettings = await rpc('settings.get');
      applySettings(nextSettings);
    } catch (error) {
      loadError = `${t('settings.loadError', 'Settings could not be loaded.')} ${error.message}`;
    } finally {
      loading = false;
    }
  }

  async function saveLanguage() {
    if (saveDisabled) {
      return;
    }

    saving = true;
    saveError = '';
    saveNotice = '';

    try {
      const nextSettings = await rpc('settings.update', {
        ...createLanguageUpdatePayload(selectedLanguageId),
      });
      applySettings(nextSettings);
      saveNotice = t(
        'settings.appearance.saveSuccess',
        'Language preference updated.',
      );
    } catch (error) {
      saveError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      saving = false;
    }
  }

  function handleLanguageChange(event) {
    selectedLanguageId = event.currentTarget.value;
    saveError = '';
    saveNotice = '';
  }
</script>

<section class={SETTINGS_LAYOUT_CLASS} aria-labelledby="settings-title">
  <nav
    class="settings-nav"
    aria-label={t('settings.sections', 'Settings sections')}
  >
    <div class="settings-nav-title">{t('settings.title', 'Settings')}</div>
    {#each panels as panel (panel.id)}
      <button
        class:snav-item--active={panel.id === activePanelId}
        class="snav-item"
        type="button"
        aria-current={panel.id === activePanelId ? 'page' : undefined}
        aria-label={t(panel.labelKey, panel.labelFallback)}
        onclick={() => selectPanel(panel.id)}
      >
        {panel.label()}
      </button>
    {/each}
  </nav>

  <div class="settings-content">
    <div class="s-panel">
      <div class="s-panel-header">
        <div>
          <h2 id="settings-title" class="s-panel-title">
            {activePanel.label()}
          </h2>
          <p class="s-panel-sub">{activePanel.subtitle()}</p>
        </div>

        {#if activePanelId === 'appearance' && !loading && !loadError}
          <button
            class="btn-primary s-save-button"
            type="button"
            disabled={saveDisabled}
            onclick={saveLanguage}
          >
            {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
          </button>
        {/if}
      </div>

      {#if loading}
        <div class="s-feedback s-feedback--neutral">
          {t('settings.loading', 'Loading settings…')}
        </div>
      {:else if loadError}
        <div class="s-feedback s-feedback--error">
          <p>{loadError}</p>
          <button class="btn-outline" type="button" onclick={loadSettings}>
            {t('common.retry', 'Retry')}
          </button>
        </div>
      {:else}
        {#if saveError}
          <div class="s-feedback s-feedback--error">{saveError}</div>
        {:else if saveNotice}
          <div class="s-feedback s-feedback--success">{saveNotice}</div>
        {/if}

        {#if activePanelId === 'general'}
          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.general.serverHost', 'Server host')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.general.serverHostDescription',
                  'Address and port the vBot server listens on.',
                )}
              </div>
            </div>
            <div class="s-row-control s-row-control--input">
              <div class="s-value-box">{serverHostValue}</div>
            </div>
          </div>
          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.general.dataDirectory', 'Data directory')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.general.dataDirectoryDescription',
                  'Root path for agents, sessions, and workspace files.',
                )}
              </div>
            </div>
            <div class="s-row-control s-row-control--input">
              <div class="s-value-box">{dataDirectoryValue}</div>
            </div>
          </div>
        {:else if activePanelId === 'providers'}
          {#if providerItems.length === 0}
            <div class="s-feedback s-feedback--neutral">
              {t('settings.providers.empty', 'No providers are available.')}
            </div>
          {:else}
            {#each providerItems as provider (provider.id)}
              <div class="s-row">
                <div class="s-row-info">
                  <div class="s-row-label">{provider.name ?? provider.id}</div>
                  <div class="s-row-desc">{describeProvider(provider, t)}</div>
                </div>
                <div class="s-row-control">
                  <span class={`chip ${providerStatusClass(provider)}`}
                    >{providerStatusLabel(provider, t)}</span
                  >
                </div>
              </div>
            {/each}
          {/if}

          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.providers.customEndpoint', 'Custom endpoint')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.providers.customEndpointDescription',
                  'OpenAI-compatible custom endpoints remain placeholder-only in this phase.',
                )}
              </div>
            </div>
            <div class="s-row-control">
              <div class="s-row-actions">
                <span class="chip chip-orange"
                  >{t(
                    'settings.providers.customEndpointStatus',
                    'Placeholder',
                  )}</span
                >
                <button class="btn-outline" type="button" disabled>
                  {t('settings.providers.configure', 'Configure…')}
                </button>
              </div>
            </div>
          </div>
        {:else}
          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.appearance.language', 'Language')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.appearance.languageDescription',
                  'Interface language.',
                )}
              </div>
            </div>
            <div class="s-row-control s-row-control--appearance">
              <select
                bind:value={selectedLanguageId}
                class="s-select"
                aria-label={t('settings.appearance.language', 'Language')}
                disabled={loading ||
                  saving ||
                  availableLanguageOptions.length <= 1}
                onchange={handleLanguageChange}
              >
                {#each availableLanguageOptions as language (language.id)}
                  <option value={language.id}>
                    {t(language.labelKey, language.labelFallback)}
                  </option>
                {/each}
              </select>

              <button
                class="btn-primary s-save-button s-save-button--inline"
                type="button"
                disabled={saveDisabled}
                onclick={saveLanguage}
              >
                {saving
                  ? t('common.saving', 'Saving…')
                  : t('common.save', 'Save')}
              </button>
            </div>
          </div>
        {/if}
      {/if}
    </div>
  </div>
</section>

<style>
  .settings-layout {
    display: flex;
    flex-direction: row;
    min-height: 0;
    min-width: 0;
    flex: 1;
    overflow: hidden;
    background: var(--surface);
  }

  .settings-nav {
    display: flex;
    width: 168px;
    min-width: 168px;
    flex-shrink: 0;
    flex-direction: column;
    gap: 1px;
    padding: 20px 0;
    border-right: 1px solid var(--border);
  }

  .settings-nav-title {
    padding: 0 16px 10px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .snav-item {
    width: 100%;
    padding: 8px 16px;
    border-radius: 0;
    color: var(--text-lo);
    background: transparent;
    font-size: 13.5px;
    font-weight: 500;
    text-align: left;
    transition:
      background 120ms ease,
      color 120ms ease;
  }

  .snav-item:hover,
  .snav-item:focus-visible {
    color: var(--text-med);
    background: rgba(255, 255, 255, 0.03);
    outline: none;
  }

  .snav-item:focus-visible {
    box-shadow: inset 0 0 0 1px rgba(232, 135, 10, 0.4);
  }

  .snav-item--active {
    color: var(--accent);
    background: var(--accent-dim);
  }

  .settings-content {
    display: flex;
    min-width: 0;
    flex: 1;
    overflow-y: auto;
    padding: 24px 32px;
  }

  .s-panel {
    width: 100%;
  }

  .s-panel-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 6px;
  }

  .s-panel-title {
    margin-bottom: 4px;
    color: var(--text-hi);
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }

  .s-panel-sub {
    margin-bottom: 24px;
    color: var(--text-lo);
    font-size: 12.5px;
  }

  .s-feedback {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 20px;
    padding: 12px 14px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .s-feedback p {
    margin: 0;
  }

  .s-feedback--neutral {
    color: var(--text-med);
    background: rgba(255, 255, 255, 0.02);
  }

  .s-feedback--error {
    color: var(--red);
    background: rgba(252, 129, 129, 0.08);
    border-color: rgba(252, 129, 129, 0.18);
  }

  .s-feedback--success {
    color: var(--green);
    background: rgba(74, 222, 128, 0.08);
    border-color: rgba(74, 222, 128, 0.2);
  }

  .s-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 14px 0;
    border-bottom: 1px solid var(--border);
  }

  .s-row:last-child {
    border-bottom: 0;
  }

  .s-row-info {
    flex: 1;
    min-width: 0;
  }

  .s-row-label {
    color: var(--text-hi);
    font-size: 14px;
    font-weight: 500;
  }

  .s-row-desc {
    margin-top: 2px;
    color: var(--text-lo);
    font-size: 12px;
    line-height: 1.4;
  }

  .s-value-box,
  .s-select {
    width: 100%;
    min-width: 0;
    padding: 7px 11px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    color: var(--text-hi);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .s-value-box {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .s-select {
    appearance: none;
    cursor: pointer;
  }

  .s-select:disabled {
    cursor: default;
    opacity: 0.7;
  }

  .s-row-desc :global(code) {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11.5px;
  }

  .s-row-control {
    display: flex;
    min-width: fit-content;
    flex-shrink: 0;
    align-items: center;
    justify-content: flex-end;
    margin-left: auto;
  }

  .s-row-control--input {
    width: min(220px, 100%);
    min-width: 180px;
  }

  .s-row-control--appearance {
    gap: 10px;
    width: min(360px, 100%);
    min-width: 220px;
  }

  .s-row-actions {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .s-save-button {
    min-width: 84px;
  }

  .s-save-button--inline {
    display: none;
  }

  @media (max-width: 760px) {
    .settings-layout {
      flex-direction: column;
      overflow: auto;
    }

    .settings-nav {
      width: 100%;
      min-width: 0;
      border-right: 0;
      border-bottom: 1px solid var(--border);
    }

    .s-row {
      align-items: flex-start;
      flex-direction: column;
    }

    .settings-content {
      padding: 20px;
    }

    .s-panel-header {
      flex-direction: column;
      align-items: stretch;
      margin-bottom: 2px;
    }

    .s-row-control,
    .s-row-control--input {
      width: 100%;
      min-width: 0;
      max-width: none;
    }

    .s-row-control--appearance {
      width: 100%;
      min-width: 0;
      flex-direction: column;
      align-items: stretch;
    }

    .s-save-button {
      display: none;
    }

    .s-save-button--inline {
      display: inline-flex;
      width: 100%;
    }

    .s-feedback {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
