<script>
  import { onMount } from 'svelte';

  import WakewordVoiceSettings from './WakewordVoiceSettings.svelte';
  import SettingsAppearancePanel from './settings/SettingsAppearancePanel.svelte';
  import SettingsChannelsPanel from './settings/SettingsChannelsPanel.svelte';
  import SettingsCompactionPanel from './settings/SettingsCompactionPanel.svelte';
  import SettingsDebugPanel from './settings/SettingsDebugPanel.svelte';
  import SettingsDefaultsPanel from './settings/SettingsDefaultsPanel.svelte';
  import SettingsExtensionsPanel from './settings/SettingsExtensionsPanel.svelte';
  import SettingsGeneralPanel from './settings/SettingsGeneralPanel.svelte';
  import SettingsProvidersPanel from './settings/SettingsProvidersPanel.svelte';
  import SettingsRecallPanel from './settings/SettingsRecallPanel.svelte';
  import SettingsSkillsPanel from './settings/SettingsSkillsPanel.svelte';
  import SettingsSpecializedModelsPanel from './settings/SettingsSpecializedModelsPanel.svelte';
  import SettingsSubAgentsPanel from './settings/SettingsSubAgentsPanel.svelte';
  import SettingsWebSearchPanel from './settings/SettingsWebSearchPanel.svelte';
  import Button from './ui/Button.svelte';
  import { rpc } from '$lib/api.js';
  import { init, t } from '$lib/i18n.js';
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
    modelsRefreshToken = 0,
    clientsRefreshToken = 0,
  } = $props();

  export function handleProviderAuthCompleted(event) {
    providersPanel?.handleProviderAuthCompleted(event);
  }

  let panels = $derived([
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
      id: 'defaults',
      labelKey: 'settings.defaults.title',
      labelFallback: 'Defaults',
      label: () => t('settings.defaults.title', 'Defaults'),
      subtitle: () =>
        t(
          'settings.defaults.subtitle',
          'Fallback values for agent fields that are not explicitly set.',
        ),
    },
    {
      id: 'skills',
      labelKey: 'settings.skills.title',
      labelFallback: 'Skills',
      label: () => t('settings.skills.title', 'Skills'),
      subtitle: () =>
        t(
          'settings.skills.subtitle',
          'Additional directories scanned for local skills.',
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
      id: 'compaction',
      labelKey: 'settings.compaction.title',
      labelFallback: 'Compaction',
      label: () => t('settings.compaction.title', 'Compaction'),
      subtitle: () =>
        t(
          'settings.compaction.subtitle',
          'Automatic context window management.',
        ),
    },
    {
      id: 'recall',
      labelKey: 'settings.recall.title',
      labelFallback: 'Recall',
      label: () => t('settings.recall.title', 'Recall'),
      subtitle: () => t('settings.recall.subtitle', 'Session search backend.'),
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
      id: 'specialized_models',
      labelKey: 'settings.specializedModels.title',
      labelFallback: 'Specialized Models',
      label: () => t('settings.specializedModels.title', 'Specialized Models'),
      subtitle: () =>
        t(
          'settings.specializedModels.subtitle',
          'Task-specific model bindings for speech and future media tools.',
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
          'Loaded extensions and their capabilities. Toggles apply after restart.',
        ),
    },
    ...(desktopCapabilities?.wakeword
      ? [
          {
            id: 'voice',
            labelKey: 'settings.voice.title',
            labelFallback: 'Voice',
            label: () => t('settings.voice.title', 'Voice'),
            subtitle: () =>
              t(
                'settings.voice.subtitle',
                'Wakeword detection and voice command settings.',
              ),
          },
        ]
      : []),
    {
      id: 'appearance',
      labelKey: 'settings.appearance.title',
      labelFallback: 'Appearance',
      label: () => t('settings.appearance.title', 'Appearance'),
      subtitle: () => t('settings.appearance.subtitle', 'Language preference.'),
    },
  ]);

  let activePanelId = $state('general');
  let settings = $state(null);
  let loading = $state(true);
  let loadError = $state('');
  let saveError = $state('');
  let providersPanel = $state(null);
  let providerHeaderAction = $state(null);
  let handledTargetPanelRequestId = -1;

  let activePanel = $derived(
    panels.find((panel) => panel.id === activePanelId) ?? panels[0],
  );
  onMount(() => {
    loadSettings();
  });

  $effect(() => {
    if (!panels.some((panel) => panel.id === activePanelId)) {
      activePanelId = panels[0]?.id ?? 'general';
      return;
    }
    if (
      targetPanelId &&
      targetPanelRequestId !== handledTargetPanelRequestId &&
      panels.some((panel) => panel.id === targetPanelId)
    ) {
      handledTargetPanelRequestId = targetPanelRequestId;
      activePanelId = targetPanelId;
    }
  });

  function selectPanel(panelId) {
    activePanelId = panelId;
    saveError = '';
  }

  function applySettings(nextSettings) {
    settings = nextSettings;

    const language = nextSettings?.appearance?.language ?? 'en';
    init(language);
  }

  function commitSettings(nextSettings) {
    settings = nextSettings;
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

  function updateProviderHeaderAction(action) {
    providerHeaderAction = action;
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

        {#if activePanelId === 'providers' && !loading && !loadError && providerHeaderAction}
          <Button
            variant="primary"
            class="s-refresh-button"
            disabled={providerHeaderAction.refreshing}
            onClick={() => providerHeaderAction?.refresh()}
          >
            {providerHeaderAction.refreshing
              ? t('settings.providers.refreshingModels', 'Updating…')
              : t('settings.providers.refreshModels', 'Update Model DB')}
          </Button>
        {/if}
      </div>

      {#if loading}
        <div class="s-feedback s-feedback--neutral">
          {t('settings.loading', 'Loading settings…')}
        </div>
      {:else if loadError}
        <div class="s-feedback s-feedback--error">
          <p>{loadError}</p>
          <Button variant="secondary" onClick={loadSettings}>
            {t('common.retry', 'Retry')}
          </Button>
        </div>
      {:else}
        {#if saveError}
          <div class="s-feedback s-feedback--error">{saveError}</div>
        {/if}

        <SettingsProvidersPanel
          bind:this={providersPanel}
          {settings}
          visible={activePanelId === 'providers'}
          {providerAuthEvent}
          {connectProvider}
          {disconnectProvider}
          onCommit={commitSettings}
          {onToast}
          onError={(message) => (saveError = message)}
          onReloadSettings={loadSettings}
          onHeaderActionChange={updateProviderHeaderAction}
          {modelsRefreshToken}
        />

        {#if activePanelId === 'general'}
          <SettingsGeneralPanel {settings} {clientsRefreshToken} />
        {:else if activePanelId === 'defaults'}
          <SettingsDefaultsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => (saveError = message)}
            {modelsRefreshToken}
          />
        {:else if activePanelId === 'skills'}
          <SettingsSkillsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => (saveError = message)}
          />
        {:else if activePanelId === 'subagents'}
          <SettingsSubAgentsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => (saveError = message)}
          />
        {:else if activePanelId === 'compaction'}
          <SettingsCompactionPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => (saveError = message)}
            {modelsRefreshToken}
          />
        {:else if activePanelId === 'recall'}
          <SettingsRecallPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => (saveError = message)}
          />
        {:else if activePanelId === 'web_search'}
          <SettingsWebSearchPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => (saveError = message)}
          />
        {:else if activePanelId === 'debug'}
          <SettingsDebugPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => (saveError = message)}
            {onDebugEnabledChange}
          />
        {:else if activePanelId === 'specialized_models'}
          <SettingsSpecializedModelsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => (saveError = message)}
            {modelsRefreshToken}
          />
        {:else if activePanelId === 'channels'}
          <SettingsChannelsPanel />
        {:else if activePanelId === 'extensions'}
          <SettingsExtensionsPanel {onToast} />
        {:else if activePanelId === 'voice'}
          <WakewordVoiceSettings {agents} {onToast} />
        {:else if activePanelId === 'appearance'}
          <SettingsAppearancePanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => (saveError = message)}
          />
        {/if}
      {/if}
    </div>
  </div>
</section>
