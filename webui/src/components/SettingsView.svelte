<script>
  import { onMount } from 'svelte';

  import { rpc } from '$lib/api.js';
  import { init, t } from '$lib/i18n.js';
  import * as settingsViewHelpers from '$lib/settingsView.js';
  import {
    CHANNEL_DM_SCOPES,
    CHANNEL_FORM_MODE_CREATE,
    CHANNEL_FORM_MODE_EDIT,
    CHANNEL_PLATFORMS,
    SETTINGS_LAYOUT_CLASS,
    applyChannelPanelList,
    buildLanguageOptions,
    buildChannelCreatePayload,
    buildChannelUpdatePayload,
    channelEnabledChipClass,
    channelRunningChipClass,
    createChannelFormValues,
    createChannelPanelState,
    createLanguageUpdatePayload,
    createSkillDirectoriesUpdatePayload,
    buildSubAgentSettingsPayload,
    describeProvider,
    formatAllowedChatIds,
    formatServerHost,
    getDataDirectoryValue,
    getDefaultSkillDirectoryValue,
    getAgentItems,
    getSkillDirectories,
    mergeChannelStatuses,
    normalizeSubAgentSettings,
    providerStatusClass,
    providerStatusLabel,
    getProviderItems,
    getOAuthConnectionStatus,
    getPublicConnectionId,
    getPersistedLanguageId,
    isOAuthConnection,
    isLanguageSaveDisabled,
  } from '$lib/settingsView.js';

  const COMPACTION_SETTING_DEFAULTS = Object.freeze({
    auto: true,
    threshold: 0.8,
    tail_tokens: 15000,
    summary_model: null,
  });

  const AUTO_SAVE_DEBOUNCE_MS = 800;

  function normalizeCompactionSettingsFallback(rawSettings) {
    const compaction = rawSettings?.compaction ?? {};
    const threshold = Number(compaction.threshold);
    const tailTokens = Number(compaction.tail_tokens);
    const summaryModel =
      typeof compaction.summary_model === 'string'
        ? compaction.summary_model.trim()
        : '';

    return {
      auto:
        typeof compaction.auto === 'boolean'
          ? compaction.auto
          : COMPACTION_SETTING_DEFAULTS.auto,
      threshold:
        Number.isFinite(threshold) && threshold > 0 && threshold <= 1
          ? threshold
          : COMPACTION_SETTING_DEFAULTS.threshold,
      tail_tokens:
        Number.isInteger(tailTokens) && tailTokens > 0
          ? tailTokens
          : COMPACTION_SETTING_DEFAULTS.tail_tokens,
      summary_model: summaryModel.length > 0 ? summaryModel : null,
    };
  }

  function buildCompactionSettingsPayloadFallback(formValues) {
    return {
      compaction: normalizeCompactionSettingsFallback({
        compaction: formValues,
      }),
    };
  }

  function getCompactionSettingsFallback(settings) {
    return normalizeCompactionSettingsFallback(settings);
  }

  const normalizeCompactionSettings =
    settingsViewHelpers.normalizeCompactionSettings ??
    normalizeCompactionSettingsFallback;
  const buildCompactionSettingsPayload =
    settingsViewHelpers.buildCompactionSettingsPayload ??
    buildCompactionSettingsPayloadFallback;
  const getCompactionSettings =
    settingsViewHelpers.getCompactionSettings ?? getCompactionSettingsFallback;

  let {
    providerAuthEvent = null,
    connectProvider = null,
    disconnectProvider = null,
  } = $props();

  export function handleProviderAuthCompleted(event) {
    handleProviderAuthEvent(event);
  }

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
  let skillDirectories = $state([]);
  let subAgentSettings = $state(normalizeSubAgentSettings(null));
  let compactionSettings = $state(normalizeCompactionSettings(null));
  let newSkillDirectory = $state('');
  let refreshingModels = $state(false);
  let modelRefreshMessage = $state('');
  let modelRefreshError = $state('');
  let oauthConnectionStates = $state({});
  let toastMessage = $state('');
  let toastVariant = $state('success');
  let toastTimer = null;
  let handledProviderAuthEvent = null;
  let copiedDeviceFlowConnectionId = $state('');
  let channelPanelState = $state(createChannelPanelState());
  let channelAgents = $state([]);
  let channelFormVisible = $state(false);
  let channelFormMode = $state(CHANNEL_FORM_MODE_CREATE);
  let channelFormValues = $state(createChannelFormValues());
  let channelBusy = $state(false);
  let channelActionChannelId = $state('');
  let channelNotice = $state('');
  let channelError = $state('');
  let channelsLoaded = $state(false);
  let languageAutoSaveTimer = null;
  let skillDirectoriesAutoSaveTimer = null;
  let subAgentSettingsAutoSaveTimer = null;
  let compactionSettingsAutoSaveTimer = null;

  let activePanel = $derived(
    panels.find((panel) => panel.id === activePanelId) ?? panels[0],
  );
  let serverHostValue = $derived(
    formatServerHost(settings?.general?.server, t),
  );
  let dataDirectoryValue = $derived(getDataDirectoryValue(settings, t));
  let defaultSkillDirectoryValue = $derived(
    getDefaultSkillDirectoryValue(settings, t),
  );
  let providerItems = $derived(getProviderItems(settings));
  let hasRefreshEligibleProvider = $derived(
    providerItems.some((provider) => providerAppearsRefreshEligible(provider)),
  );
  let availableLanguageOptions = $derived(
    buildLanguageOptions(settings?.appearance),
  );
  let persistedLanguageId = $derived(getPersistedLanguageId(settings));
  let channelPlatformOptions = $derived(
    CHANNEL_PLATFORMS.map((platformId) => ({
      id: platformId,
      label:
        platformId === 'telegram'
          ? t('sessions.platform_telegram', 'Telegram')
          : platformId,
    })),
  );
  let channelDmScopeOptions = $derived(
    CHANNEL_DM_SCOPES.map((scopeId) => ({
      id: scopeId,
      label: channelDmScopeLabel(scopeId),
    })),
  );
  let channelPanelBusy = $derived(
    channelBusy ||
      channelPanelState.loading ||
      channelActionChannelId.length > 0,
  );
  let saveDisabled = $derived(
    isLanguageSaveDisabled({
      loading,
      saving,
      selectedLanguageId,
      persistedLanguageId,
    }),
  );
  let skillDirectoriesSaveDisabled = $derived(
    loading ||
      saving ||
      directoriesMatch(skillDirectories, getSkillDirectories(settings)),
  );
  let subAgentSettingsSaveDisabled = $derived(
    loading ||
      saving ||
      subAgentSettingsMatch(
        subAgentSettings,
        normalizeSubAgentSettings(settings),
      ),
  );
  let compactionSettingsSaveDisabled = $derived(
    loading ||
      saving ||
      compactionSettingsMatch(
        compactionSettings,
        getCompactionSettings(settings),
      ),
  );

  onMount(() => {
    loadSettings();

    return () => {
      if (toastTimer) {
        clearTimeout(toastTimer);
      }

      clearLanguageAutoSaveTimer();
      clearSkillDirectoriesAutoSaveTimer();
      clearSubAgentSettingsAutoSaveTimer();
      clearCompactionSettingsAutoSaveTimer();
    };
  });

  $effect(() => {
    if (providerAuthEvent && providerAuthEvent !== handledProviderAuthEvent) {
      handledProviderAuthEvent = providerAuthEvent;
      handleProviderAuthEvent(providerAuthEvent);
    }
  });

  $effect(() => {
    if (activePanelId !== 'appearance') {
      return;
    }

    if (saveDisabled) {
      return;
    }

    languageAutoSaveTimer = setTimeout(() => {
      languageAutoSaveTimer = null;
      void saveLanguage();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearLanguageAutoSaveTimer();
    };
  });

  $effect(() => {
    if (activePanelId !== 'skills') {
      return;
    }

    if (skillDirectoriesSaveDisabled) {
      return;
    }

    skillDirectoriesAutoSaveTimer = setTimeout(() => {
      skillDirectoriesAutoSaveTimer = null;
      void saveSkillDirectories();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearSkillDirectoriesAutoSaveTimer();
    };
  });

  $effect(() => {
    if (activePanelId !== 'subagents') {
      return;
    }

    if (subAgentSettingsSaveDisabled) {
      return;
    }

    subAgentSettingsAutoSaveTimer = setTimeout(() => {
      subAgentSettingsAutoSaveTimer = null;
      void saveSubAgentSettings();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearSubAgentSettingsAutoSaveTimer();
    };
  });

  $effect(() => {
    if (activePanelId !== 'compaction') {
      return;
    }

    if (compactionSettingsSaveDisabled) {
      return;
    }

    compactionSettingsAutoSaveTimer = setTimeout(() => {
      compactionSettingsAutoSaveTimer = null;
      void saveCompactionSettings();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearCompactionSettingsAutoSaveTimer();
    };
  });

  function selectPanel(panelId) {
    activePanelId = panelId;
    saveError = '';
    saveNotice = '';

    if (panelId === 'channels') {
      void ensureChannelsLoaded();
    }
  }

  function applySettings(nextSettings) {
    settings = nextSettings;

    const language = nextSettings?.appearance?.language ?? 'en';
    selectedLanguageId = language;
    skillDirectories = getSkillDirectories(nextSettings);
    subAgentSettings = normalizeSubAgentSettings(nextSettings);
    compactionSettings = getCompactionSettings(nextSettings);
    newSkillDirectory = '';
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
      commitSettings(nextSettings);
      init(selectedLanguageId);
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

  async function saveSkillDirectories() {
    if (skillDirectoriesSaveDisabled) {
      return;
    }

    saving = true;
    saveError = '';
    saveNotice = '';

    try {
      const nextSettings = await rpc(
        'settings.update',
        createSkillDirectoriesUpdatePayload(skillDirectories),
      );
      commitSettings(nextSettings);
      saveNotice = t(
        'settings.skills.saveSuccess',
        'Skill directories updated.',
      );
    } catch (error) {
      saveError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      saving = false;
    }
  }

  async function saveSubAgentSettings() {
    if (subAgentSettingsSaveDisabled) {
      return;
    }

    saving = true;
    saveError = '';
    saveNotice = '';

    try {
      const nextSettings = await rpc(
        'settings.update',
        buildSubAgentSettingsPayload(subAgentSettings),
      );
      commitSettings(nextSettings);
      saveNotice = t(
        'settings.subagents.saveSuccess',
        'Sub-agent settings updated.',
      );
    } catch (error) {
      saveError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      saving = false;
    }
  }

  async function saveCompactionSettings() {
    if (compactionSettingsSaveDisabled) {
      return;
    }

    saving = true;
    saveError = '';
    saveNotice = '';

    try {
      const nextSettings = await rpc(
        'settings.update',
        buildCompactionSettingsPayload(compactionSettings),
      );
      commitSettings(nextSettings);
      saveNotice = t('settings.compaction.saved', 'Compaction settings saved.');
    } catch (error) {
      saveError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      saving = false;
    }
  }

  function clearLanguageAutoSaveTimer() {
    if (languageAutoSaveTimer !== null) {
      clearTimeout(languageAutoSaveTimer);
      languageAutoSaveTimer = null;
    }
  }

  function clearSkillDirectoriesAutoSaveTimer() {
    if (skillDirectoriesAutoSaveTimer !== null) {
      clearTimeout(skillDirectoriesAutoSaveTimer);
      skillDirectoriesAutoSaveTimer = null;
    }
  }

  function clearSubAgentSettingsAutoSaveTimer() {
    if (subAgentSettingsAutoSaveTimer !== null) {
      clearTimeout(subAgentSettingsAutoSaveTimer);
      subAgentSettingsAutoSaveTimer = null;
    }
  }

  function clearCompactionSettingsAutoSaveTimer() {
    if (compactionSettingsAutoSaveTimer !== null) {
      clearTimeout(compactionSettingsAutoSaveTimer);
      compactionSettingsAutoSaveTimer = null;
    }
  }

  function showAlreadySavedToast() {
    showLocalToast(t('common.alreadySaved', 'Already saved'), 'success');
  }

  function handleManualLanguageSave() {
    if (saving) {
      return;
    }

    if (saveDisabled) {
      showAlreadySavedToast();
      return;
    }

    clearLanguageAutoSaveTimer();
    void saveLanguage();
  }

  function handleManualSkillDirectoriesSave() {
    if (saving) {
      return;
    }

    if (skillDirectoriesSaveDisabled) {
      showAlreadySavedToast();
      return;
    }

    clearSkillDirectoriesAutoSaveTimer();
    void saveSkillDirectories();
  }

  function handleManualSubAgentSettingsSave() {
    if (saving) {
      return;
    }

    if (subAgentSettingsSaveDisabled) {
      showAlreadySavedToast();
      return;
    }

    clearSubAgentSettingsAutoSaveTimer();
    void saveSubAgentSettings();
  }

  function handleManualCompactionSettingsSave() {
    if (saving) {
      return;
    }

    if (compactionSettingsSaveDisabled) {
      showAlreadySavedToast();
      return;
    }

    clearCompactionSettingsAutoSaveTimer();
    void saveCompactionSettings();
  }

  function addSkillDirectory() {
    const directory = newSkillDirectory.trim();
    if (!directory) {
      return;
    }

    if (!skillDirectories.includes(directory)) {
      skillDirectories = [...skillDirectories, directory];
    }

    newSkillDirectory = '';
    saveError = '';
    saveNotice = '';
  }

  function removeSkillDirectory(directory) {
    skillDirectories = skillDirectories.filter((item) => item !== directory);
    saveError = '';
    saveNotice = '';
  }

  function handleLanguageChange(event) {
    selectedLanguageId = event.currentTarget.value;
    saveError = '';
    saveNotice = '';
  }

  function handleSkillDirectoryKeydown(event) {
    if (event.key !== 'Enter') {
      return;
    }

    event.preventDefault();
    addSkillDirectory();
  }

  function handleSubAgentSettingChange(key, event) {
    subAgentSettings = {
      ...subAgentSettings,
      [key]: event.currentTarget.value,
    };
    saveError = '';
    saveNotice = '';
  }

  function handleCompactionSettingChange(key, value) {
    compactionSettings = {
      ...compactionSettings,
      [key]: value,
    };
    saveError = '';
    saveNotice = '';
  }

  function subAgentSettingsMatch(left, right) {
    const normalizedLeft = normalizeSubAgentSettings({ subagents: left });
    const normalizedRight = normalizeSubAgentSettings({ subagents: right });

    return (
      normalizedLeft.max_subagent_depth ===
        normalizedRight.max_subagent_depth &&
      normalizedLeft.max_subagents_per_turn ===
        normalizedRight.max_subagents_per_turn &&
      normalizedLeft.subagent_timeout_minutes ===
        normalizedRight.subagent_timeout_minutes
    );
  }

  function compactionSettingsMatch(left, right) {
    const normalizedLeft = normalizeCompactionSettings({
      compaction: left,
    });
    const normalizedRight = normalizeCompactionSettings({
      compaction: right,
    });

    return (
      normalizedLeft.auto === normalizedRight.auto &&
      normalizedLeft.threshold === normalizedRight.threshold &&
      normalizedLeft.tail_tokens === normalizedRight.tail_tokens &&
      normalizedLeft.summary_model === normalizedRight.summary_model
    );
  }

  function directoriesMatch(left, right) {
    if (left.length !== right.length) {
      return false;
    }

    return left.every((item, index) => item === right[index]);
  }

  function providerAppearsRefreshEligible(provider) {
    return (
      typeof provider?.models_endpoint === 'string' &&
      provider.models_endpoint.length > 0 &&
      (provider.credentials_configured === true ||
        provider.status === 'configured')
    );
  }

  function getOAuthState(connectionId) {
    return (
      oauthConnectionStates[connectionId] ?? {
        flowActive: false,
        showDialog: false,
        dialogData: null,
      }
    );
  }

  function updateOAuthState(connectionId, patch) {
    oauthConnectionStates = {
      ...oauthConnectionStates,
      [connectionId]: {
        ...getOAuthState(connectionId),
        ...patch,
      },
    };
  }

  function isConnectionConfigured(connection) {
    return connection?.configured === true || connection?.usable === true;
  }

  function oauthStatus(connection) {
    return getOAuthConnectionStatus(
      providerItems,
      connection.id,
      getOAuthState(connection.id).flowActive,
    );
  }

  async function startOAuthConnect(provider, connection) {
    const connectionId = getPublicConnectionId(connection);

    saveError = '';
    saveNotice = '';
    copiedDeviceFlowConnectionId = '';
    updateOAuthState(connection.id, {
      flowActive: true,
      showDialog: false,
      dialogData: null,
    });

    try {
      const response = await callConnectProvider(provider.id, connectionId);
      updateOAuthState(connection.id, {
        flowActive: true,
        showDialog: Boolean(response?.user_code),
        dialogData: response,
      });
    } catch (error) {
      updateOAuthState(connection.id, {
        flowActive: false,
        showDialog: false,
        dialogData: null,
      });
      saveError = `${t('settings.providers.connectError', 'Provider connection could not be started.')} ${error.message}`;
    }
  }

  async function cancelOAuthFlow(provider, connection) {
    await disconnectOAuthProvider(provider, connection, { reload: false });
  }

  async function disconnectOAuthProvider(provider, connection, options = {}) {
    const connectionId = getPublicConnectionId(connection);
    saveError = '';
    saveNotice = '';
    copiedDeviceFlowConnectionId = '';

    try {
      await callDisconnectProvider(provider.id, connectionId);
      updateOAuthState(connection.id, {
        flowActive: false,
        showDialog: false,
        dialogData: null,
      });

      if (options.reload ?? true) {
        await loadSettings();
      }
    } catch (error) {
      saveError = `${t('settings.providers.disconnectError', 'Provider connection could not be disconnected.')} ${error.message}`;
    }
  }

  async function completeOAuthFlow(connectionId) {
    copiedDeviceFlowConnectionId = '';
    updateOAuthState(connectionId, {
      flowActive: false,
      showDialog: false,
      dialogData: null,
    });
    showLocalToast(
      t(
        'settings.providers.device_flow.success_toast',
        'GitHub Copilot connected successfully',
      ),
      'success',
    );
    await loadSettings();
  }

  function failOAuthFlow(connectionId) {
    copiedDeviceFlowConnectionId = '';
    updateOAuthState(connectionId, {
      flowActive: false,
      showDialog: false,
      dialogData: null,
    });
    showLocalToast(
      t(
        'settings.providers.device_flow.error_toast',
        'Authorization failed or timed out',
      ),
      'error',
    );
  }

  function showLocalToast(message, variant = 'success') {
    if (toastTimer) {
      clearTimeout(toastTimer);
    }

    toastMessage = message;
    toastVariant = variant;
    toastTimer = setTimeout(() => {
      toastMessage = '';
    }, 4000);
  }

  async function copyDeviceFlowUserCode(connection, userCode) {
    if (!userCode) {
      return;
    }

    if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) {
      showLocalToast(
        t(
          'settings.providers.device_flow.copy_error',
          'Device code could not be copied.',
        ),
        'error',
      );
      return;
    }

    try {
      await navigator.clipboard.writeText(userCode);
      copiedDeviceFlowConnectionId = connection.id;
      showLocalToast(
        t('settings.providers.device_flow.copy_success', 'Device code copied.'),
        'success',
      );
    } catch {
      showLocalToast(
        t(
          'settings.providers.device_flow.copy_error',
          'Device code could not be copied.',
        ),
        'error',
      );
    }
  }

  async function callConnectProvider(providerId, connectionId) {
    if (typeof connectProvider === 'function') {
      return connectProvider(providerId, connectionId, { rpc });
    }

    return rpc('provider.connect', {
      provider_id: providerId,
      connection_id: connectionId,
    });
  }

  async function callDisconnectProvider(providerId, connectionId) {
    if (typeof disconnectProvider === 'function') {
      return disconnectProvider(providerId, connectionId, { rpc });
    }

    return rpc('provider.disconnect', {
      provider_id: providerId,
      connection_id: connectionId,
    });
  }

  function handleProviderAuthEvent(event) {
    const payload = event.payload ?? event;
    const connectionStateId = findConnectionStateId(
      payload.provider_id,
      payload.connection_id,
    );

    if (!connectionStateId || !getOAuthState(connectionStateId).flowActive) {
      return;
    }

    if (payload.success === true) {
      completeOAuthFlow(connectionStateId);
      return;
    }

    failOAuthFlow(connectionStateId);
  }

  function findConnectionStateId(providerId, connectionId) {
    const provider = providerItems.find((item) => item.id === providerId);
    const connections = Array.isArray(provider?.connections)
      ? provider.connections
      : [];
    const connection = connections.find(
      (item) => getPublicConnectionId(item) === connectionId,
    );

    return connection?.id ?? '';
  }

  async function refreshModelDatabase() {
    if (!hasRefreshEligibleProvider || refreshingModels) {
      return;
    }

    refreshingModels = true;
    modelRefreshMessage = '';
    modelRefreshError = '';

    try {
      const result = await rpc('model.refresh_db');
      applyProviderRefreshResult(result);
      await rpc('model.list');
      modelRefreshMessage = t(
        'settings.providers.refreshSuccess',
        'Model DB updated: {providerCount} providers, {count} models available.',
        refreshSummaryValues(result),
      );
    } catch (error) {
      modelRefreshError = `${t(
        'settings.providers.refreshError',
        'Model DB could not be updated.',
      )} ${error.message}`;
    } finally {
      refreshingModels = false;
    }
  }

  function applyProviderRefreshResult(result) {
    if (!settings?.providers?.items) {
      return;
    }

    const refreshedProviders = getRefreshedProviders(result);

    if (refreshedProviders.length === 0) {
      return;
    }

    const modelCounts = new Map(
      refreshedProviders
        .filter((provider) => typeof provider?.provider_id === 'string')
        .map((provider) => [provider.provider_id, provider.model_count]),
    );

    settings = {
      ...settings,
      providers: {
        ...settings.providers,
        items: settings.providers.items.map((provider) =>
          modelCounts.has(provider.id)
            ? { ...provider, model_count: modelCounts.get(provider.id) }
            : provider,
        ),
      },
    };
  }

  function getRefreshedProviders(result) {
    if (Array.isArray(result?.providers)) {
      return result.providers;
    }

    if (typeof result?.provider_id === 'string') {
      return [result];
    }

    return [];
  }

  function refreshSummaryValues(result) {
    const refreshedProviders = getRefreshedProviders(result);
    const modelCount = Number.isFinite(result?.model_count)
      ? result.model_count
      : refreshedProviders.reduce(
          (total, provider) =>
            total +
            (Number.isFinite(provider?.model_count) ? provider.model_count : 0),
          0,
        );

    return {
      providerCount: result?.refreshed_count ?? refreshedProviders.length,
      count: modelCount,
    };
  }

  function clearChannelFeedback() {
    channelError = '';
    channelNotice = '';
  }

  function startCreateChannel() {
    channelFormMode = CHANNEL_FORM_MODE_CREATE;
    channelFormValues = createChannelFormValues();
    channelFormVisible = true;
    clearChannelFeedback();
  }

  function startEditChannel(channel) {
    channelFormMode = CHANNEL_FORM_MODE_EDIT;
    channelFormValues = createChannelFormValues(channel);
    channelFormVisible = true;
    clearChannelFeedback();
  }

  function cancelChannelForm() {
    channelFormMode = CHANNEL_FORM_MODE_CREATE;
    channelFormValues = createChannelFormValues();
    channelFormVisible = false;
    clearChannelFeedback();
  }

  function setChannelFormField(fieldName, value) {
    channelFormValues = {
      ...channelFormValues,
      [fieldName]: value,
    };
    clearChannelFeedback();
  }

  function channelDmScopeLabel(dmScope) {
    switch (dmScope) {
      case 'main':
        return t('settings.channels.dm_scope.main', 'Main');
      case 'per_peer':
        return t('settings.channels.dm_scope.per_peer', 'Per peer');
      case 'per_account_channel_peer':
        return t(
          'settings.channels.dm_scope.per_account_channel_peer',
          'Per account + channel + peer',
        );
      case 'per_conversation':
      default:
        return t(
          'settings.channels.dm_scope.per_conversation',
          'Per conversation',
        );
    }
  }

  function channelEnabledLabel(enabled) {
    return enabled
      ? t('settings.channels.enabled', 'Enabled')
      : t('settings.channels.disabled', 'Disabled');
  }

  function channelRunningLabel(running) {
    if (running === true) {
      return t('settings.channels.running', 'Running');
    }

    if (running === false) {
      return t('settings.channels.stopped', 'Stopped');
    }

    return t('common.unknown', 'Unknown');
  }

  async function ensureChannelsLoaded() {
    if (channelPanelState.loading || channelsLoaded) {
      return;
    }

    await loadChannelsPanel();
  }

  async function reloadChannelsPanel() {
    await loadChannelsPanel();
  }

  async function loadChannelsPanel() {
    channelPanelState = {
      ...channelPanelState,
      loading: true,
      error: null,
    };

    try {
      const [agentsResult, channelsResult] = await Promise.all([
        rpc('agent.list'),
        rpc('channel.list'),
      ]);
      channelAgents = getAgentItems(agentsResult);

      const nextState = applyChannelPanelList(
        channelPanelState,
        channelsResult,
      );
      const statusResults = await Promise.all(
        nextState.channels.map(async (channel) => {
          try {
            return await rpc('channel.status', { id: channel.id });
          } catch {
            return {
              id: channel.id,
              enabled: channel.enabled,
              running: channel.running,
            };
          }
        }),
      );

      channelPanelState = {
        ...nextState,
        channels: mergeChannelStatuses(nextState.channels, statusResults),
        loading: false,
        error: null,
      };
      channelsLoaded = true;
    } catch (error) {
      channelPanelState = {
        ...channelPanelState,
        loading: false,
        error: `${t('settings.loadError', 'Settings could not be loaded.')} ${error.message}`,
      };
      channelsLoaded = false;
    }
  }

  async function submitChannelForm(event) {
    event.preventDefault();

    if (channelBusy) {
      return;
    }

    channelBusy = true;
    clearChannelFeedback();

    try {
      if (channelFormMode === CHANNEL_FORM_MODE_CREATE) {
        await rpc(
          'channel.create',
          buildChannelCreatePayload(channelFormValues),
        );
        channelNotice = t(
          'settings.channels.createSuccess',
          'Channel created.',
        );
      } else {
        await rpc(
          'channel.update',
          buildChannelUpdatePayload(channelFormValues),
        );
        channelNotice = t(
          'settings.channels.updateSuccess',
          'Channel updated.',
        );
      }

      channelFormVisible = false;
      channelFormMode = CHANNEL_FORM_MODE_CREATE;
      channelFormValues = createChannelFormValues();
      await loadChannelsPanel();
    } catch (error) {
      channelError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      channelBusy = false;
    }
  }

  async function toggleChannelEnabled(channel) {
    await runChannelAction(channel.id, async () => {
      if (channel.enabled) {
        await rpc('channel.disable', { id: channel.id });
        channelNotice = t(
          'settings.channels.disableSuccess',
          'Channel disabled.',
        );
        return;
      }

      await rpc('channel.enable', { id: channel.id });
      channelNotice = t('settings.channels.enableSuccess', 'Channel enabled.');
    });
  }

  async function deleteChannel(channel) {
    const confirmed = confirm(
      t('settings.channels.delete_confirm', 'Delete channel {id}?', {
        id: channel.id,
      }),
    );
    if (!confirmed) {
      return;
    }

    await runChannelAction(channel.id, async () => {
      await rpc('channel.delete', { id: channel.id });
      channelNotice = t('settings.channels.deleteSuccess', 'Channel deleted.');
    });

    if (
      channelFormMode === CHANNEL_FORM_MODE_EDIT &&
      channelFormValues.id === channel.id
    ) {
      cancelChannelForm();
    }
  }

  async function runChannelAction(channelId, action) {
    if (channelActionChannelId.length > 0) {
      return;
    }

    channelActionChannelId = channelId;
    clearChannelFeedback();

    try {
      await action();
      await loadChannelsPanel();
    } catch (error) {
      channelError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      channelActionChannelId = '';
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

        {#if activePanelId === 'providers' && !loading && !loadError && hasRefreshEligibleProvider}
          <button
            class="btn-primary s-refresh-button"
            type="button"
            disabled={refreshingModels}
            onclick={refreshModelDatabase}
          >
            {refreshingModels
              ? t('settings.providers.refreshingModels', 'Updating…')
              : t('settings.providers.refreshModels', 'Update Model DB')}
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

        {#if toastMessage}
          <div
            class={`s-local-toast s-local-toast--${toastVariant}`}
            role="alert"
          >
            {toastMessage}
          </div>
        {/if}

        {#if activePanelId === 'providers' && modelRefreshError}
          <div class="s-feedback s-feedback--error">{modelRefreshError}</div>
        {:else if activePanelId === 'providers' && modelRefreshMessage}
          <div class="s-feedback s-feedback--success">
            {modelRefreshMessage}
          </div>
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
        {:else if activePanelId === 'skills'}
          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t(
                  'settings.skills.defaultDirectory',
                  'Default skill directory',
                )}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.skills.defaultDirectoryDescription',
                  'Always scanned from the vBot data directory and kept read-only here.',
                )}
              </div>
            </div>
            <div class="s-row-control s-row-control--input">
              <div class="s-value-box">{defaultSkillDirectoryValue}</div>
            </div>
          </div>

          <div class="s-row s-row--stacked">
            <div class="s-row-info">
              <div class="s-row-label">
                {t(
                  'settings.skills.extraDirectories',
                  'Additional skill directories',
                )}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.skills.extraDirectoriesDescription',
                  'Absolute or home-relative paths from settings.json skill_directories.',
                )}
              </div>
            </div>

            <div class="s-skill-directory-list">
              {#if skillDirectories.length === 0}
                <div class="s-feedback s-feedback--neutral s-feedback--compact">
                  {t(
                    'settings.skills.emptyDirectories',
                    'No additional skill directories configured.',
                  )}
                </div>
              {:else}
                {#each skillDirectories as directory (directory)}
                  <div class="s-skill-directory-item">
                    <span>{directory}</span>
                    <button
                      class="btn-outline s-directory-remove"
                      type="button"
                      aria-label={t(
                        'settings.skills.removeDirectory',
                        'Remove skill directory {path}',
                        { path: directory },
                      )}
                      onclick={() => removeSkillDirectory(directory)}
                    >
                      {t('common.remove', 'Remove')}
                    </button>
                  </div>
                {/each}
              {/if}
            </div>

            <div class="s-skill-directory-add">
              <input
                class="s-input"
                type="text"
                bind:value={newSkillDirectory}
                placeholder={t(
                  'settings.skills.pathPlaceholder',
                  'C:/path/to/skills',
                )}
                onkeydown={handleSkillDirectoryKeydown}
              />
              <button
                class="btn-outline"
                type="button"
                disabled={!newSkillDirectory.trim()}
                onclick={addSkillDirectory}
              >
                {t('settings.skills.addDirectory', 'Add directory')}
              </button>
            </div>

            <div class="s-sticky-footer">
              <button
                class="btn-primary s-save-button s-save-button--inline"
                type="button"
                onclick={handleManualSkillDirectoriesSave}
              >
                {saving
                  ? t('common.saving', 'Saving…')
                  : t('common.save', 'Save')}
              </button>
            </div>
          </div>
        {:else if activePanelId === 'subagents'}
          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.subagents.maxDepth', 'Max sub-agent depth')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.subagents.maxDepthDescription',
                  'Maximum nesting level allowed when sub-agents spawn their own sub-agents.',
                )}
              </div>
            </div>
            <div class="s-row-control s-row-control--number">
              <input
                class="s-input"
                type="number"
                min="1"
                step="1"
                value={subAgentSettings.max_subagent_depth}
                aria-label={t(
                  'settings.subagents.maxDepth',
                  'Max sub-agent depth',
                )}
                oninput={(event) =>
                  handleSubAgentSettingChange('max_subagent_depth', event)}
              />
            </div>
          </div>

          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.subagents.maxPerTurn', 'Max sub-agents per turn')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.subagents.maxPerTurnDescription',
                  'Maximum number of sub-agent sessions one parent run may spawn.',
                )}
              </div>
            </div>
            <div class="s-row-control s-row-control--number">
              <input
                class="s-input"
                type="number"
                min="1"
                step="1"
                value={subAgentSettings.max_subagents_per_turn}
                aria-label={t(
                  'settings.subagents.maxPerTurn',
                  'Max sub-agents per turn',
                )}
                oninput={(event) =>
                  handleSubAgentSettingChange('max_subagents_per_turn', event)}
              />
            </div>
          </div>

          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.subagents.timeoutMinutes', 'Timeout minutes')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.subagents.timeoutMinutesDescription',
                  'Maximum wait time for blocking sub-agent calls before they fail.',
                )}
              </div>
            </div>
            <div class="s-row-control s-row-control--number">
              <input
                class="s-input"
                type="number"
                min="1"
                step="1"
                value={subAgentSettings.subagent_timeout_minutes}
                aria-label={t(
                  'settings.subagents.timeoutMinutes',
                  'Timeout minutes',
                )}
                oninput={(event) =>
                  handleSubAgentSettingChange(
                    'subagent_timeout_minutes',
                    event,
                  )}
              />
            </div>
          </div>

          <div class="s-sticky-footer">
            <button
              class="btn-primary s-save-button s-save-button--inline"
              type="button"
              onclick={handleManualSubAgentSettingsSave}
            >
              {saving
                ? t('common.saving', 'Saving…')
                : t('common.save', 'Save')}
            </button>
          </div>
        {:else if activePanelId === 'compaction'}
          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.compaction.auto', 'Auto-compact')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.compaction.autoDescription',
                  'Automatically compact when the context threshold is reached.',
                )}
              </div>
            </div>
            <div class="s-row-control s-row-control--checkbox">
              <label class="s-checkbox-wrap">
                <input
                  class="s-checkbox"
                  type="checkbox"
                  checked={compactionSettings.auto === true}
                  aria-label={t('settings.compaction.auto', 'Auto-compact')}
                  onchange={(event) =>
                    handleCompactionSettingChange(
                      'auto',
                      event.currentTarget.checked,
                    )}
                />
              </label>
            </div>
          </div>

          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.compaction.threshold', 'Threshold')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.compaction.thresholdDescription',
                  'Compact when context usage exceeds this fraction (0–1).',
                )}
              </div>
            </div>
            <div class="s-row-control s-row-control--number">
              <input
                class="s-input"
                type="number"
                min="0.05"
                max="1"
                step="0.05"
                value={compactionSettings.threshold}
                aria-label={t('settings.compaction.threshold', 'Threshold')}
                oninput={(event) =>
                  handleCompactionSettingChange(
                    'threshold',
                    event.currentTarget.value,
                  )}
              />
            </div>
          </div>

          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.compaction.tailTokens', 'Tail tokens')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.compaction.tailTokensDescription',
                  'Number of tokens preserved verbatim at the end of context.',
                )}
              </div>
            </div>
            <div class="s-row-control s-row-control--number">
              <input
                class="s-input"
                type="number"
                min="1"
                step="1000"
                value={compactionSettings.tail_tokens}
                aria-label={t('settings.compaction.tailTokens', 'Tail tokens')}
                oninput={(event) =>
                  handleCompactionSettingChange(
                    'tail_tokens',
                    event.currentTarget.value,
                  )}
              />
            </div>
          </div>

          <div class="s-row">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.compaction.summaryModel', 'Summary model')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.compaction.summaryModelDescription',
                  'Model used for summarization. Leave blank to use the active agent model.',
                )}
              </div>
            </div>
            <div class="s-row-control s-row-control--input">
              <input
                class="s-input"
                type="text"
                value={compactionSettings.summary_model ?? ''}
                aria-label={t(
                  'settings.compaction.summaryModel',
                  'Summary model',
                )}
                oninput={(event) =>
                  handleCompactionSettingChange(
                    'summary_model',
                    event.currentTarget.value,
                  )}
              />
            </div>
          </div>

          <div class="s-sticky-footer">
            <button
              class="btn-primary s-save-button s-save-button--inline"
              type="button"
              onclick={handleManualCompactionSettingsSave}
            >
              {saving
                ? t('common.saving', 'Saving…')
                : t('settings.compaction.save', 'Save')}
            </button>
          </div>
        {:else if activePanelId === 'providers'}
          {#if providerItems.length === 0}
            <div class="s-feedback s-feedback--neutral">
              {t('settings.providers.empty', 'No providers are available.')}
            </div>
          {:else}
            {#each providerItems as provider (provider.id)}
              <div class="s-provider-card">
                <div class="s-row s-row--provider">
                  <div class="s-row-info">
                    <div class="s-row-label">
                      {provider.name ?? provider.id}
                    </div>
                    <div class="s-row-desc">
                      {describeProvider(provider, t)}
                    </div>
                  </div>
                  <div class="s-row-control">
                    <div class="s-row-actions s-row-actions--provider">
                      <span class={`chip ${providerStatusClass(provider)}`}
                        >{providerStatusLabel(provider, t)}</span
                      >
                    </div>
                  </div>
                </div>

                {#if provider.connections?.length > 0}
                  <div class="s-provider-connections">
                    {#each provider.connections as connection (connection.id)}
                      <div class="s-provider-connection-row">
                        <div class="s-row-info">
                          <div class="s-provider-connection-label">
                            {connection.label ?? connection.id}
                          </div>
                          <div class="s-row-desc">
                            {isOAuthConnection(connection)
                              ? t(
                                  'settings.providers.oauthDescription',
                                  'OAuth device authorization managed by the provider.',
                                )
                              : t(
                                  'settings.providers.apiKeyDescription',
                                  'Static credential configured from environment or data directory.',
                                )}
                          </div>
                        </div>

                        <div class="s-row-control">
                          {#if isOAuthConnection(connection)}
                            {@const state = getOAuthState(connection.id)}
                            {@const status = oauthStatus(connection)}
                            <div class="s-row-actions s-row-actions--provider">
                              {#if status === 'pending'}
                                <span class="s-inline-waiting">
                                  <span
                                    class="s-inline-spinner"
                                    aria-hidden="true"
                                  ></span>
                                  {t(
                                    'settings.providers.device_flow.waiting',
                                    'Waiting for authorization in GitHub…',
                                  )}
                                </span>
                                <button
                                  class="btn-outline"
                                  type="button"
                                  onclick={() =>
                                    cancelOAuthFlow(provider, connection)}
                                >
                                  {t(
                                    'settings.providers.device_flow.cancel',
                                    'Cancel',
                                  )}
                                </button>
                              {:else if status === 'connected'}
                                <span class="chip chip-green">
                                  {t(
                                    'settings.providers.connected',
                                    'Connected',
                                  )}
                                </span>
                                <button
                                  class="btn-outline"
                                  type="button"
                                  onclick={() =>
                                    disconnectOAuthProvider(
                                      provider,
                                      connection,
                                    )}
                                >
                                  {t(
                                    'settings.providers.disconnect',
                                    'Disconnect',
                                  )}
                                </button>
                              {:else}
                                <button
                                  class="btn-primary"
                                  type="button"
                                  onclick={() =>
                                    startOAuthConnect(provider, connection)}
                                >
                                  {t('settings.providers.connect', 'Connect')}
                                </button>
                              {/if}
                            </div>

                            {#if state.showDialog && state.dialogData}
                              <div
                                class="device-flow-inline"
                                role="dialog"
                                aria-modal="false"
                                aria-labelledby={`device-flow-title-${connection.id}`}
                              >
                                <div class="device-flow-header">
                                  <p class="device-flow-eyebrow">
                                    {t(
                                      'settings.providers.device_flow.eyebrow',
                                      'OAuth',
                                    )}
                                  </p>
                                  <h3 id={`device-flow-title-${connection.id}`}>
                                    {t(
                                      'settings.providers.device_flow.title',
                                      'Connect GitHub Copilot',
                                    )}
                                  </h3>
                                </div>
                                <p class="device-flow-instructions">
                                  {t(
                                    'settings.providers.device_flow.instructions',
                                    'Enter this code at the link below:',
                                  )}
                                </p>
                                <div class="device-flow-code-row">
                                  <code class="device-flow-code"
                                    >{state.dialogData.user_code}</code
                                  >
                                  <button
                                    class="btn-outline device-flow-copy"
                                    type="button"
                                    aria-label={t(
                                      'settings.providers.device_flow.copy_aria',
                                      'Copy device code {code}',
                                      { code: state.dialogData.user_code },
                                    )}
                                    onclick={() =>
                                      copyDeviceFlowUserCode(
                                        connection,
                                        state.dialogData.user_code,
                                      )}
                                  >
                                    {copiedDeviceFlowConnectionId ===
                                    connection.id
                                      ? t(
                                          'settings.providers.device_flow.copied',
                                          'Copied',
                                        )
                                      : t('common.copy', 'Copy')}
                                  </button>
                                </div>
                                <a
                                  class="device-flow-link"
                                  href={state.dialogData.verification_uri}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  {state.dialogData.verification_uri}
                                </a>
                                <div
                                  class="device-flow-waiting"
                                  aria-live="polite"
                                >
                                  <span
                                    class="s-inline-spinner"
                                    aria-hidden="true"
                                  ></span>
                                  <span>
                                    {t(
                                      'settings.providers.device_flow.waiting',
                                      'Waiting for authorization in GitHub…',
                                    )}
                                  </span>
                                </div>
                                <div class="device-flow-actions">
                                  <button
                                    class="btn-outline"
                                    type="button"
                                    onclick={() =>
                                      cancelOAuthFlow(provider, connection)}
                                  >
                                    {t(
                                      'settings.providers.device_flow.cancel',
                                      'Cancel',
                                    )}
                                  </button>
                                </div>
                              </div>
                            {/if}
                          {:else}
                            <span
                              class={`chip ${isConnectionConfigured(connection) ? 'chip-green' : 'chip-amber'}`}
                            >
                              {isConnectionConfigured(connection)
                                ? t(
                                    'settings.providers.status.configured',
                                    'Configured',
                                  )
                                : t(
                                    'settings.providers.status.missingCredentials',
                                    'Missing credentials',
                                  )}
                            </span>
                          {/if}
                        </div>
                      </div>
                    {/each}
                  </div>
                {/if}
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
        {:else if activePanelId === 'channels'}
          <div class="s-row s-row--stacked s-row--channels-header">
            <div class="s-row-info">
              <div class="s-row-label">
                {t('settings.channels.title', 'Channels')}
              </div>
              <div class="s-row-desc">
                {t(
                  'settings.channels.subtitle',
                  'Manage channel routing and runtime status.',
                )}
              </div>
            </div>
            <div class="s-row-control">
              <div class="s-row-actions s-row-actions--channel-header">
                <button
                  class="btn-outline"
                  type="button"
                  disabled={channelPanelBusy}
                  onclick={reloadChannelsPanel}
                >
                  {t('common.refresh', 'Refresh')}
                </button>
                <button
                  class="btn-primary"
                  type="button"
                  disabled={channelPanelBusy}
                  onclick={startCreateChannel}
                >
                  {t('settings.channels.add', 'Add channel')}
                </button>
              </div>
            </div>
          </div>

          {#if channelError}
            <div class="s-feedback s-feedback--error">{channelError}</div>
          {:else if channelNotice}
            <div class="s-feedback s-feedback--success">{channelNotice}</div>
          {/if}

          {#if channelFormVisible}
            <form class="s-channel-form" onsubmit={submitChannelForm}>
              <div class="s-channel-form-header">
                <h3 class="s-channel-form-title">
                  {channelFormMode === CHANNEL_FORM_MODE_CREATE
                    ? t('settings.channels.add', 'Add channel')
                    : t('common.edit', 'Edit')}
                </h3>
              </div>

              <div class="s-channel-form-grid">
                <label class="s-field" for="channel-id-input">
                  <span class="s-field-label">
                    {t('sessions.link_channel_id', 'Channel ID')}
                  </span>
                  <input
                    id="channel-id-input"
                    class="s-input"
                    type="text"
                    value={channelFormValues.id}
                    required
                    disabled={channelBusy ||
                      channelFormMode === CHANNEL_FORM_MODE_EDIT}
                    oninput={(event) =>
                      setChannelFormField('id', event.currentTarget.value)}
                  />
                </label>

                <label class="s-field" for="channel-platform-select">
                  <span class="s-field-label">
                    {t('settings.channels.platform', 'Platform')}
                  </span>
                  <select
                    id="channel-platform-select"
                    class="s-select"
                    value={channelFormValues.platform}
                    disabled={channelBusy}
                    onchange={(event) =>
                      setChannelFormField(
                        'platform',
                        event.currentTarget.value,
                      )}
                  >
                    {#each channelPlatformOptions as option (option.id)}
                      <option value={option.id}>{option.label}</option>
                    {/each}
                  </select>
                </label>

                <label class="s-field" for="channel-agent-select">
                  <span class="s-field-label">
                    {t('settings.channels.agent', 'Agent')}
                  </span>
                  <select
                    id="channel-agent-select"
                    class="s-select"
                    value={channelFormValues.agent_id}
                    required
                    disabled={channelBusy || channelAgents.length === 0}
                    onchange={(event) =>
                      setChannelFormField(
                        'agent_id',
                        event.currentTarget.value,
                      )}
                  >
                    <option value="" disabled>
                      {channelAgents.length > 0
                        ? t(
                            'settings.channels.agent.placeholder',
                            'Select agent',
                          )
                        : t(
                            'settings.channels.agent.none',
                            'No agents available',
                          )}
                    </option>
                    {#each channelAgents as agent (agent.id)}
                      <option value={agent.id}>{agent.name}</option>
                    {/each}
                  </select>
                </label>

                <label class="s-field" for="channel-dm-scope-select">
                  <span class="s-field-label">
                    {t('settings.channels.dm_scope', 'DM scope')}
                  </span>
                  <select
                    id="channel-dm-scope-select"
                    class="s-select"
                    value={channelFormValues.dm_scope}
                    disabled={channelBusy}
                    onchange={(event) =>
                      setChannelFormField(
                        'dm_scope',
                        event.currentTarget.value,
                      )}
                  >
                    {#each channelDmScopeOptions as option (option.id)}
                      <option value={option.id}>{option.label}</option>
                    {/each}
                  </select>
                </label>

                <label class="s-field" for="channel-token-env-input">
                  <span class="s-field-label">
                    {t('settings.channels.token_env_var', 'Token env var')}
                  </span>
                  <input
                    id="channel-token-env-input"
                    class="s-input"
                    type="text"
                    value={channelFormValues.token_env_var}
                    required
                    disabled={channelBusy}
                    oninput={(event) =>
                      setChannelFormField(
                        'token_env_var',
                        event.currentTarget.value,
                      )}
                  />
                </label>

                <label
                  class="s-field s-field--full"
                  for="channel-allowed-chat-ids-input"
                >
                  <span class="s-field-label">
                    {t(
                      'settings.channels.allowed_chat_ids',
                      'Allowed chat IDs',
                    )}
                  </span>
                  <input
                    id="channel-allowed-chat-ids-input"
                    class="s-input"
                    type="text"
                    value={channelFormValues.allowed_chat_ids}
                    disabled={channelBusy}
                    placeholder={t(
                      'settings.channels.allowed_chat_ids.placeholder',
                      '12345, -1009876543210',
                    )}
                    oninput={(event) =>
                      setChannelFormField(
                        'allowed_chat_ids',
                        event.currentTarget.value,
                      )}
                  />
                </label>
              </div>

              <div class="s-channel-form-actions">
                <button
                  class="btn-outline"
                  type="button"
                  onclick={cancelChannelForm}
                >
                  {t('common.cancel', 'Cancel')}
                </button>
                <button
                  class="btn-primary"
                  type="submit"
                  disabled={channelBusy}
                >
                  {channelBusy
                    ? t('common.saving', 'Saving…')
                    : channelFormMode === CHANNEL_FORM_MODE_CREATE
                      ? t('common.create', 'Create')
                      : t('common.save', 'Save')}
                </button>
              </div>
            </form>
          {/if}

          {#if channelPanelState.loading}
            <div class="s-feedback s-feedback--neutral">
              {t('common.loading', 'Loading…')}
            </div>
          {:else if channelPanelState.error}
            <div class="s-feedback s-feedback--error">
              {channelPanelState.error}
            </div>
          {:else if channelPanelState.channels.length === 0}
            <div class="s-feedback s-feedback--neutral">
              {t('settings.channels.empty', 'No channels configured.')}
            </div>
          {:else}
            <div class="s-channel-list">
              {#each channelPanelState.channels as channel (channel.id)}
                {@const rowBusy =
                  channelBusy || channelActionChannelId === channel.id}
                <div class="s-channel-card">
                  <div class="s-channel-head">
                    <div class="s-row-info">
                      <div class="s-row-label">{channel.id}</div>
                      <div class="s-row-desc">
                        {t('settings.channels.platform', 'Platform')}: {channel.platform}
                        · {t('settings.channels.agent', 'Agent')}: {channel.agent_id}
                      </div>
                      <div class="s-row-desc">
                        {t('settings.channels.dm_scope', 'DM scope')}: {channelDmScopeLabel(
                          channel.dm_scope,
                        )}
                      </div>
                      <div class="s-row-desc">
                        {t('settings.channels.token_env_var', 'Token env var')}: {channel.token_env_var}
                      </div>
                      <div class="s-row-desc">
                        {t(
                          'settings.channels.allowed_chat_ids',
                          'Allowed chat IDs',
                        )}: {formatAllowedChatIds(channel.allowed_chat_ids) ||
                          t('settings.channels.allowed_chat_ids.none', 'None')}
                      </div>
                    </div>

                    <div class="s-channel-controls">
                      <div class="s-channel-chips">
                        <span
                          class={`chip ${channelEnabledChipClass(channel.enabled)}`}
                        >
                          {channelEnabledLabel(channel.enabled)}
                        </span>
                        <span
                          class={`chip ${channelRunningChipClass(channel.running)}`}
                        >
                          {channelRunningLabel(channel.running)}
                        </span>
                      </div>

                      <div class="s-row-actions s-row-actions--channel">
                        <button
                          class="btn-outline"
                          type="button"
                          disabled={rowBusy}
                          aria-label={t(
                            'settings.channels.edit',
                            'Edit channel {id}',
                            {
                              id: channel.id,
                            },
                          )}
                          onclick={() => startEditChannel(channel)}
                        >
                          {t('common.edit', 'Edit')}
                        </button>
                        <button
                          class="btn-outline"
                          type="button"
                          disabled={rowBusy}
                          aria-label={channel.enabled
                            ? t(
                                'settings.channels.disableAria',
                                'Disable channel {id}',
                                {
                                  id: channel.id,
                                },
                              )
                            : t(
                                'settings.channels.enableAria',
                                'Enable channel {id}',
                                {
                                  id: channel.id,
                                },
                              )}
                          onclick={() => toggleChannelEnabled(channel)}
                        >
                          {channel.enabled
                            ? t('settings.channels.disable', 'Disable')
                            : t('settings.channels.enable', 'Enable')}
                        </button>
                        <button
                          class="btn-outline"
                          type="button"
                          disabled={rowBusy}
                          aria-label={t(
                            'settings.channels.delete',
                            'Delete channel {id}',
                            {
                              id: channel.id,
                            },
                          )}
                          onclick={() => deleteChannel(channel)}
                        >
                          {t('common.delete', 'Delete')}
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              {/each}
            </div>
          {/if}
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
            </div>
          </div>

          <div class="s-sticky-footer">
            <button
              class="btn-primary s-save-button s-save-button--inline"
              type="button"
              onclick={handleManualLanguageSave}
            >
              {saving
                ? t('common.saving', 'Saving…')
                : t('common.save', 'Save')}
            </button>
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
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 16px;
    padding: 14px 0;
    border-bottom: 1px solid var(--border);
  }

  .s-row:last-child {
    border-bottom: 0;
  }

  .s-provider-card {
    border-bottom: 1px solid var(--border);
  }

  .s-provider-card:last-of-type {
    border-bottom: 0;
  }

  .s-row--provider {
    border-bottom: 0;
  }

  .s-row--stacked {
    align-items: stretch;
    flex-direction: column;
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
  .s-select,
  .s-input {
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

  .s-input:focus-visible {
    border-color: rgba(232, 135, 10, 0.4);
    box-shadow: 0 0 0 3px rgba(232, 135, 10, 0.06);
    outline: none;
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

  .s-row-control--number {
    width: 132px;
    min-width: 132px;
  }

  .s-row-control--appearance {
    gap: 10px;
    width: min(360px, 100%);
    min-width: 220px;
  }

  .s-row-control--checkbox {
    width: auto;
    min-width: 0;
  }

  .s-checkbox-wrap {
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
  }

  .s-checkbox {
    width: 16px;
    height: 16px;
    accent-color: var(--accent);
  }

  .s-row-actions {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .s-row-actions--provider {
    justify-content: flex-end;
  }

  .s-provider-connections {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: -2px 0 14px;
    padding-left: 14px;
    border-left: 1px solid var(--border);
  }

  .s-provider-connection-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: rgba(255, 255, 255, 0.015);
  }

  .s-provider-connection-label {
    color: var(--text-med);
    font-size: 12.5px;
    font-weight: 500;
  }

  .s-inline-waiting {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    color: var(--text-med);
    font-size: 12.5px;
  }

  .s-inline-spinner {
    width: 10px;
    height: 10px;
    flex-shrink: 0;
    border: 2px solid rgba(245, 158, 11, 0.22);
    border-top-color: var(--amber);
    border-radius: 50%;
    animation: s-oauth-spin 800ms linear infinite;
  }

  .device-flow-inline {
    margin-top: 12px;
    padding: 14px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-lg);
    background: var(--surface-2);
  }

  .device-flow-header {
    margin-bottom: 12px;
  }

  .device-flow-eyebrow {
    margin: 0 0 6px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    line-height: 1;
    text-transform: uppercase;
  }

  .device-flow-header h3 {
    margin: 0;
    color: var(--text-hi);
    font-size: 15px;
    font-weight: 600;
    line-height: 1.3;
  }

  .device-flow-instructions {
    margin: 0 0 10px;
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.4;
  }

  .device-flow-code-row {
    display: flex;
    align-items: stretch;
    gap: 10px;
  }

  .device-flow-copy {
    flex-shrink: 0;
    min-width: 72px;
  }

  .device-flow-code {
    display: flex;
    flex: 1;
    align-items: center;
    min-width: 0;
    padding: 10px 12px;
    border: 1px solid rgba(232, 135, 10, 0.3);
    border-radius: var(--r-md);
    color: var(--text-hi);
    background: var(--bg);
    font-family: var(--font-mono);
    font-size: 18px;
    font-weight: 500;
    letter-spacing: 0.08em;
  }

  .device-flow-link {
    display: inline-flex;
    margin-top: 12px;
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: 12px;
    overflow-wrap: anywhere;
    text-decoration: none;
  }

  .device-flow-link:hover,
  .device-flow-link:focus-visible {
    text-decoration: underline;
  }

  .device-flow-waiting {
    display: flex;
    align-items: center;
    gap: 9px;
    margin-top: 14px;
    color: var(--text-med);
    font-size: 12.5px;
  }

  .device-flow-actions {
    display: flex;
    justify-content: flex-end;
    margin-top: 14px;
  }

  .s-local-toast {
    margin-bottom: 20px;
    padding: 10px 12px;
    border: 1px solid var(--border-2);
    border-left-width: 2px;
    border-radius: var(--r-md);
    background: var(--surface-2);
    color: var(--text-hi);
    font-size: 12.5px;
  }

  .s-local-toast--success {
    border-left-color: var(--green);
  }

  .s-local-toast--error {
    border-left-color: var(--red);
    color: var(--red);
  }

  @keyframes s-oauth-spin {
    to {
      transform: rotate(360deg);
    }
  }

  .s-skill-directory-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .s-skill-directory-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-2);
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.4;
  }

  .s-skill-directory-item span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .s-skill-directory-add {
    display: flex;
    gap: 10px;
  }

  .s-feedback--compact {
    margin-bottom: 0;
  }

  .s-directory-remove {
    flex-shrink: 0;
  }

  .s-refresh-button {
    white-space: nowrap;
  }

  .s-sticky-footer {
    position: sticky;
    bottom: 0;
    display: flex;
    justify-content: flex-end;
    padding: 12px 0 0;
    background: var(--surface);
  }

  .s-save-button {
    min-width: 84px;
  }

  .s-row--channels-header {
    gap: 14px;
  }

  .s-row-actions--channel-header {
    justify-content: flex-end;
    width: 100%;
  }

  .s-channel-form {
    margin: 14px 0 20px;
    padding: 14px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-lg);
    background: var(--surface-2);
  }

  .s-channel-form-header {
    margin-bottom: 12px;
  }

  .s-channel-form-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 15px;
    font-weight: 600;
    line-height: 1.3;
  }

  .s-channel-form-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  .s-field {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }

  .s-field--full {
    grid-column: 1 / -1;
  }

  .s-field-label {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11.5px;
    line-height: 1.2;
  }

  .s-channel-form-actions {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    margin-top: 14px;
  }

  .s-channel-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-top: 12px;
  }

  .s-channel-card {
    padding: 12px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: rgba(255, 255, 255, 0.015);
  }

  .s-channel-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .s-channel-controls {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 10px;
    min-width: fit-content;
  }

  .s-channel-chips {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
  }

  .s-row-actions--channel {
    justify-content: flex-end;
    flex-wrap: wrap;
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
    .s-row-control--input,
    .s-row-control--number {
      width: 100%;
      min-width: 0;
      max-width: none;
    }

    .s-provider-connection-row,
    .s-row-actions--provider {
      align-items: stretch;
      flex-direction: column;
    }

    .s-provider-connections {
      padding-left: 0;
      border-left: 0;
    }

    .s-row-control--appearance {
      width: 100%;
      min-width: 0;
      flex-direction: column;
      align-items: stretch;
    }

    .s-skill-directory-add,
    .s-skill-directory-item,
    .device-flow-code-row {
      align-items: stretch;
      flex-direction: column;
    }

    .s-sticky-footer {
      justify-content: stretch;
    }

    .s-save-button--inline {
      width: 100%;
    }

    .s-row-actions--channel-header {
      justify-content: flex-start;
    }

    .s-channel-form-grid {
      grid-template-columns: minmax(0, 1fr);
    }

    .s-channel-form-actions {
      justify-content: stretch;
      flex-direction: column;
    }

    .s-channel-head,
    .s-channel-controls,
    .s-row-actions--channel {
      align-items: stretch;
      flex-direction: column;
      width: 100%;
    }

    .s-channel-chips {
      justify-content: flex-start;
    }

    .s-feedback {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
