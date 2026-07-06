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
  import SettingsReflectionPanel from './settings/SettingsReflectionPanel.svelte';
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
    onOpenSetupGuide = noop,
    modelsRefreshToken = 0,
    clientsRefreshToken = 0,
  } = $props();

  export function handleProviderAuthCompleted(event) {
    providersPanel?.handleProviderAuthCompleted(event);
  }

  // Panel order is deliberate: setup (providers) → model behavior → skills &
  // limits → integrations → personal preferences → diagnostics/read-only.
  let panels = $derived([
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
      label: () => t('settings.specializedModels.title', 'Specialized Models'),
      subtitle: () =>
        t(
          'settings.specializedModels.subtitle',
          'Task-specific model bindings for speech, image, and embedding tools. These bindings are independent of agent and project defaults.',
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
          'Automatic summarizing when a conversation nears the model context limit.',
        ),
    },
    {
      id: 'recall',
      labelKey: 'settings.recall.title',
      labelFallback: 'Recall',
      label: () => t('settings.recall.title', 'Recall'),
      subtitle: () =>
        t('settings.recall.subtitle', 'How agents search past conversations.'),
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
      id: 'skills',
      labelKey: 'settings.skills.title',
      labelFallback: 'Skills',
      label: () => t('settings.skills.title', 'Skills'),
      subtitle: () =>
        t(
          'settings.skills.subtitle',
          'Manage skill files and skill scan directories.',
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
      subtitle: () =>
        t('settings.appearance.subtitle', 'Language and chat reading width.'),
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
  ]);

  let activePanelId = $state('providers');
  let settings = $state(null);
  let loading = $state(true);
  let loadError = $state('');
  let providersPanel = $state(null);
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
        <SettingsProvidersPanel
          bind:this={providersPanel}
          {settings}
          visible={activePanelId === 'providers'}
          {providerAuthEvent}
          {connectProvider}
          {disconnectProvider}
          onCommit={commitSettings}
          {onToast}
          onError={(message) => reportSettingsError(message)}
          onReloadSettings={loadSettings}
          {modelsRefreshToken}
        />

        {#if activePanelId === 'general'}
          <SettingsGeneralPanel
            {settings}
            {clientsRefreshToken}
            {onOpenSetupGuide}
          />
        {:else if activePanelId === 'defaults'}
          <SettingsDefaultsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {modelsRefreshToken}
          />
        {:else if activePanelId === 'skills'}
          <SettingsSkillsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        {:else if activePanelId === 'subagents'}
          <SettingsSubAgentsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        {:else if activePanelId === 'reflection'}
          <SettingsReflectionPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        {:else if activePanelId === 'compaction'}
          <SettingsCompactionPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {modelsRefreshToken}
          />
        {:else if activePanelId === 'recall'}
          <SettingsRecallPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        {:else if activePanelId === 'web_search'}
          <SettingsWebSearchPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        {:else if activePanelId === 'debug'}
          <SettingsDebugPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {onDebugEnabledChange}
          />
        {:else if activePanelId === 'specialized_models'}
          <SettingsSpecializedModelsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
            {modelsRefreshToken}
          />
        {:else if activePanelId === 'channels'}
          <SettingsChannelsPanel
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        {:else if activePanelId === 'extensions'}
          <SettingsExtensionsPanel
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        {:else if activePanelId === 'voice'}
          <WakewordVoiceSettings {agents} {onToast} />
        {:else if activePanelId === 'appearance'}
          <SettingsAppearancePanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => reportSettingsError(message)}
          />
        {/if}
      {/if}
    </div>
  </div>
</section>
