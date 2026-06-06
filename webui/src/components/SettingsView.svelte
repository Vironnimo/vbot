<script>
  import { onMount } from 'svelte';

  import SearchableDropdown from './SearchableDropdown.svelte';
  import WakewordVoiceSettings from './WakewordVoiceSettings.svelte';
  import SettingsAppearancePanel from './settings/SettingsAppearancePanel.svelte';
  import SettingsChannelsPanel from './settings/SettingsChannelsPanel.svelte';
  import SettingsDebugPanel from './settings/SettingsDebugPanel.svelte';
  import SettingsDefaultsPanel from './settings/SettingsDefaultsPanel.svelte';
  import SettingsGeneralPanel from './settings/SettingsGeneralPanel.svelte';
  import SettingsRecallPanel from './settings/SettingsRecallPanel.svelte';
  import SettingsSkillsPanel from './settings/SettingsSkillsPanel.svelte';
  import SettingsSubAgentsPanel from './settings/SettingsSubAgentsPanel.svelte';
  import SettingsWebSearchPanel from './settings/SettingsWebSearchPanel.svelte';
  import {
    getTaskModelOptions,
    listTaskModelTargets,
    rpc,
    updateTaskModelSettings,
  } from '$lib/api.js';
  import { init, t } from '$lib/i18n.js';
  import {
    buildModelSelectOptions,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';
  import * as settingsViewHelpers from '$lib/settingsView.js';
  import {
    TASK_MODEL_ROWS,
    applyOptionDefaults,
    createTaskModelUpdatePayload,
    normalizeOptionSchema,
    normalizeTargets,
    normalizeTaskModelSettings,
    taskModelBindingsMatch,
  } from '$lib/taskModelSettings.js';
  import {
    SETTINGS_LAYOUT_CLASS,
    describeProvider,
    providerStatusClass,
    providerStatusLabel,
    getProviderItems,
    getOAuthConnectionStatus,
    getPublicConnectionId,
    isOAuthDeviceFlowConnection,
    isOAuthConnection,
  } from '$lib/settingsView.js';

  const COMPACTION_SETTING_DEFAULTS = Object.freeze({
    auto: true,
    threshold: 0.8,
    tail_tokens: 15000,
    summary_model: null,
  });

  const AUTO_SAVE_DEBOUNCE_MS = 800;
  const noop = () => {};

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
    onToast = noop,
    agents = [],
    desktopCapabilities = null,
    targetPanelId = '',
    targetPanelRequestId = 0,
    onDebugEnabledChange = noop,
  } = $props();

  export function handleProviderAuthCompleted(event) {
    handleProviderAuthEvent(event);
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
  let saving = $state(false);
  let compactionSettings = $state(normalizeCompactionSettings(null));
  let taskModelBindings = $state(normalizeTaskModelSettings(null));
  let taskModelTargetsByType = $state({});
  let taskModelSchemasByType = $state({});
  let taskModelPanelLoaded = $state(false);
  let taskModelLoading = $state(false);
  let taskModelSaving = $state(false);
  let taskModelError = $state('');
  let availableModels = $state([]);
  let availableConnections = $state([]);
  let modelCatalogsLoaded = $state(false);
  let modelCatalogsLoading = $state(false);
  let refreshingModels = $state(false);
  let modelRefreshMessage = $state('');
  let modelRefreshError = $state('');
  let oauthConnectionStates = $state({});
  let handledProviderAuthEvent = null;
  let copiedDeviceFlowConnectionId = $state('');
  let compactionSettingsAutoSaveTimer = null;
  let handledTargetPanelRequestId = -1;

  let activePanel = $derived(
    panels.find((panel) => panel.id === activePanelId) ?? panels[0],
  );
  let providerItems = $derived(getProviderItems(settings));
  let hasRefreshEligibleProvider = $derived(
    providerItems.some((provider) => providerAppearsRefreshEligible(provider)),
  );
  let compactionSummaryModelOptions = $derived(
    selectModelOptions(
      compactionSettings.summary_model ?? '',
      t('settings.compaction.summaryModelPlaceholder', 'Active agent model'),
    ),
  );
  let compactionSummaryModelSelectValue = $derived(
    selectModelValue(
      compactionSettings.summary_model ?? '',
      compactionSummaryModelOptions,
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
  let taskModelSaveDisabled = $derived(
    loading ||
      saving ||
      taskModelSaving ||
      taskModelLoading ||
      taskModelBindingsMatch(
        taskModelBindings,
        normalizeTaskModelSettings(settings),
      ),
  );

  onMount(() => {
    loadSettings();

    return () => {
      clearCompactionSettingsAutoSaveTimer();
    };
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

  $effect(() => {
    if (providerAuthEvent && providerAuthEvent !== handledProviderAuthEvent) {
      handledProviderAuthEvent = providerAuthEvent;
      handleProviderAuthEvent(providerAuthEvent);
    }
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

    if (panelId === 'specialized_models') {
      void ensureTaskModelPanelLoaded();
    }

    if (panelUsesModelPicker(panelId)) {
      void ensureModelCatalogsLoaded();
    }
  }

  function panelUsesModelPicker(panelId) {
    return panelId === 'compaction';
  }

  async function ensureModelCatalogsLoaded() {
    if (modelCatalogsLoaded || modelCatalogsLoading) {
      return;
    }

    modelCatalogsLoading = true;

    try {
      const [modelsResult, connectionsResult] = await Promise.all([
        rpc('model.list'),
        rpc('connection.list'),
      ]);

      availableModels = Array.isArray(modelsResult?.models)
        ? modelsResult.models
        : [];
      availableConnections = Array.isArray(connectionsResult?.connections)
        ? connectionsResult.connections
        : [];
      modelCatalogsLoaded = true;
    } catch (error) {
      saveError = `${t('settings.models.loadError', 'Model catalog could not be loaded.')} ${error.message}`;
    } finally {
      modelCatalogsLoading = false;
    }
  }

  function applySettings(nextSettings) {
    settings = nextSettings;

    const language = nextSettings?.appearance?.language ?? 'en';
    compactionSettings = getCompactionSettings(nextSettings);
    taskModelBindings = normalizeTaskModelSettings(nextSettings);
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

  async function saveCompactionSettings() {
    if (compactionSettingsSaveDisabled) {
      return;
    }

    saving = true;
    saveError = '';

    try {
      const nextSettings = await rpc(
        'settings.update',
        buildCompactionSettingsPayload(compactionSettings),
      );
      commitSettings(nextSettings);
      showSettingsToast(
        t('settings.compaction.saved', 'Compaction settings saved.'),
        'success',
      );
    } catch (error) {
      saveError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      saving = false;
    }
  }

  async function ensureTaskModelPanelLoaded() {
    if (taskModelPanelLoaded || taskModelLoading) {
      return;
    }

    taskModelLoading = true;
    taskModelError = '';

    try {
      const targetEntries = await Promise.all(
        TASK_MODEL_ROWS.map(async (row) => {
          const result = await listTaskModelTargets(row.taskType);
          return [row.taskType, normalizeTargets(result)];
        }),
      );
      taskModelTargetsByType = Object.fromEntries(targetEntries);
      taskModelPanelLoaded = true;

      for (const row of TASK_MODEL_ROWS) {
        const target = taskModelBindings[row.taskType]?.target ?? '';
        if (target) {
          await loadTaskModelSchema(row.taskType, target);
        }
      }
    } catch (error) {
      taskModelError = `${t('settings.specializedModels.loadError', 'Specialized model targets could not be loaded.')} ${error.message}`;
    } finally {
      taskModelLoading = false;
    }
  }

  async function loadTaskModelSchema(taskType, target) {
    if (!target) {
      taskModelSchemasByType = {
        ...taskModelSchemasByType,
        [taskType]: [],
      };
      return;
    }

    const result = await getTaskModelOptions(taskType, target);
    const fields = normalizeOptionSchema(result);
    taskModelSchemasByType = {
      ...taskModelSchemasByType,
      [taskType]: fields,
    };
    taskModelBindings = {
      ...taskModelBindings,
      [taskType]: applyOptionDefaults(taskModelBindings[taskType], fields),
    };
  }

  async function saveTaskModelBindings() {
    if (taskModelSaveDisabled) {
      return;
    }

    taskModelSaving = true;
    taskModelError = '';
    saveError = '';

    try {
      const result = await updateTaskModelSettings(
        createTaskModelUpdatePayload(taskModelBindings),
      );
      const nextSettings = {
        ...settings,
        model_tasks: result.model_tasks ?? {},
      };
      commitSettings(nextSettings);
      taskModelBindings = normalizeTaskModelSettings(nextSettings);
      showSettingsToast(
        t(
          'settings.specializedModels.saveSuccess',
          'Specialized model bindings updated.',
        ),
        'success',
      );
    } catch (error) {
      taskModelError = `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`;
    } finally {
      taskModelSaving = false;
    }
  }

  function clearCompactionSettingsAutoSaveTimer() {
    if (compactionSettingsAutoSaveTimer !== null) {
      clearTimeout(compactionSettingsAutoSaveTimer);
      compactionSettingsAutoSaveTimer = null;
    }
  }

  function showAlreadySavedToast() {
    showSettingsToast(t('common.alreadySaved', 'Already saved'), 'success');
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

  function handleManualTaskModelSave() {
    if (saving || taskModelSaving) {
      return;
    }

    if (taskModelSaveDisabled) {
      showAlreadySavedToast();
      return;
    }

    void saveTaskModelBindings();
  }

  function handleCompactionSettingChange(key, value) {
    compactionSettings = {
      ...compactionSettings,
      [key]: value,
    };
    saveError = '';
  }

  async function handleTaskModelTargetChange(taskType, event) {
    const target = event.currentTarget.value;
    taskModelError = '';
    taskModelBindings = {
      ...taskModelBindings,
      [taskType]: {
        target,
        options: {},
      },
    };

    try {
      await loadTaskModelSchema(taskType, target);
    } catch (error) {
      taskModelError = `${t('settings.specializedModels.optionsLoadError', 'Model options could not be loaded.')} ${error.message}`;
    }
  }

  function handleTaskModelOptionChange(taskType, field, event) {
    const currentBinding = taskModelBindings[taskType] ?? {
      target: '',
      options: {},
    };
    const value = valueFromTaskModelOptionField(field, event);
    taskModelBindings = {
      ...taskModelBindings,
      [taskType]: {
        ...currentBinding,
        options: {
          ...(currentBinding.options ?? {}),
          [field.name]: value,
        },
      },
    };
    taskModelError = '';
  }

  function valueFromTaskModelOptionField(field, event) {
    if (field.type === 'boolean') {
      return event.currentTarget.checked === true;
    }
    if (field.type === 'number') {
      const value = event.currentTarget.value;
      if (value === '') {
        return '';
      }
      const numberValue = Number(value);
      return Number.isFinite(numberValue) ? numberValue : value;
    }
    return event.currentTarget.value;
  }

  function taskModelTargets(taskType) {
    return taskModelTargetsByType[taskType] ?? [];
  }

  function taskModelFields(taskType) {
    return taskModelSchemasByType[taskType] ?? [];
  }

  function taskModelOptionValue(taskType, field) {
    const options = taskModelBindings[taskType]?.options ?? {};
    const value = options[field.name];
    if (value === undefined || value === null) {
      return field.default ?? '';
    }
    return value;
  }

  function updateCompactionSummaryModelSelection(selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    handleCompactionSettingChange(
      'summary_model',
      modelSelectionValue(selection.model, selection.connectionLocalId),
    );
  }

  function selectModelOptions(selectedModelValue, emptyLabel) {
    return buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue,
      emptyLabel,
      translate: t,
    });
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

  function providerDisplayName(provider) {
    return provider?.name ?? provider?.id ?? 'Provider';
  }

  function providerTranslationValues(provider) {
    return { provider: providerDisplayName(provider) };
  }

  async function startOAuthConnect(provider, connection) {
    const connectionId = getPublicConnectionId(connection);

    saveError = '';
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

  async function completeOAuthFlow(connectionId, provider) {
    copiedDeviceFlowConnectionId = '';
    updateOAuthState(connectionId, {
      flowActive: false,
      showDialog: false,
      dialogData: null,
    });
    showSettingsToast(
      t(
        'settings.providers.device_flow.success_toast',
        '{provider} connected successfully',
        providerTranslationValues(provider),
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
    showSettingsToast(
      t(
        'settings.providers.device_flow.error_toast',
        'Authorization failed or timed out',
      ),
      'error',
    );
  }

  function showSettingsToast(message, variant = 'success') {
    onToast?.({ title: message, variant });
  }

  async function copyDeviceFlowUserCode(connection, userCode) {
    if (!userCode) {
      return;
    }

    if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) {
      showSettingsToast(
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
      showSettingsToast(
        t('settings.providers.device_flow.copy_success', 'Device code copied.'),
        'success',
      );
    } catch {
      showSettingsToast(
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
    const connectionContext = findConnectionContext(
      payload.provider_id,
      payload.connection_id,
    );
    const connectionStateId = connectionContext.connectionStateId;

    if (!connectionStateId || !getOAuthState(connectionStateId).flowActive) {
      return;
    }

    if (payload.success === true) {
      completeOAuthFlow(connectionStateId, connectionContext.provider);
      return;
    }

    failOAuthFlow(connectionStateId);
  }

  function findConnectionContext(providerId, connectionId) {
    const provider = providerItems.find((item) => item.id === providerId);
    const connections = Array.isArray(provider?.connections)
      ? provider.connections
      : [];
    const connection = connections.find(
      (item) => getPublicConnectionId(item) === connectionId,
    );

    return {
      provider,
      connection,
      connectionStateId: connection?.id ?? '',
    };
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
      const modelsResult = await rpc('model.list');
      availableModels = Array.isArray(modelsResult?.models)
        ? modelsResult.models
        : [];
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
        {/if}

        {#if activePanelId === 'providers' && modelRefreshError}
          <div class="s-feedback s-feedback--error">{modelRefreshError}</div>
        {:else if activePanelId === 'providers' && modelRefreshMessage}
          <div class="s-feedback s-feedback--success">
            {modelRefreshMessage}
          </div>
        {/if}

        {#if activePanelId === 'general'}
          <SettingsGeneralPanel {settings} />
        {:else if activePanelId === 'defaults'}
          <SettingsDefaultsPanel
            {settings}
            onCommit={commitSettings}
            {onToast}
            onError={(message) => (saveError = message)}
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
            <div class="s-row-control s-row-control--model">
              <SearchableDropdown
                id="settings-compaction-summary-model"
                value={compactionSummaryModelSelectValue}
                options={compactionSummaryModelOptions}
                placeholder={t(
                  'settings.compaction.summaryModelPlaceholder',
                  'Active agent model',
                )}
                searchPlaceholder={t(
                  'agents.form.modelSearchPlaceholder',
                  'Filter models…',
                )}
                emptyLabel={t(
                  'agents.form.modelSearchEmpty',
                  'No models match',
                )}
                ariaLabel={t(
                  'settings.compaction.summaryModel',
                  'Summary model',
                )}
                triggerClass="settings-view__dropdown"
                panelClass="settings-view__model-panel"
                onValueChange={updateCompactionSummaryModelSelection}
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
          {#if taskModelLoading}
            <div class="s-feedback s-feedback--neutral">
              {t(
                'settings.specializedModels.loading',
                'Loading specialized model targets…',
              )}
            </div>
          {/if}

          {#if taskModelError}
            <div class="s-feedback s-feedback--error">{taskModelError}</div>
          {/if}

          <div class="s-task-model-list">
            {#each TASK_MODEL_ROWS as row (row.taskType)}
              {@const binding = taskModelBindings[row.taskType] ?? {
                target: '',
                options: {},
              }}
              {@const targets = taskModelTargets(row.taskType)}
              {@const fields = taskModelFields(row.taskType)}
              <div class="s-row s-row--stacked s-task-model-row">
                <div class="s-task-model-head">
                  <div class="s-row-info">
                    <div class="s-row-label">
                      {t(row.titleKey, row.titleFallback)}
                    </div>
                    <div class="s-row-desc">
                      {t(row.descriptionKey, row.descriptionFallback)}
                    </div>
                  </div>
                  <div class="s-row-control s-row-control--task-model">
                    <select
                      class="s-select"
                      value={binding.target}
                      aria-label={t(row.titleKey, row.titleFallback)}
                      disabled={taskModelLoading || taskModelSaving}
                      onchange={(event) =>
                        handleTaskModelTargetChange(row.taskType, event)}
                    >
                      <option value="">
                        {t(
                          'settings.specializedModels.noTarget',
                          'Not configured',
                        )}
                      </option>
                      {#each targets as target (target.id)}
                        <option value={target.id}>{target.label}</option>
                      {/each}
                      {#if binding.target && !targets.some((target) => target.id === binding.target)}
                        <option value={binding.target}>
                          {t(
                            'settings.specializedModels.customTarget',
                            'Custom target: {target}',
                            { target: binding.target },
                          )}
                        </option>
                      {/if}
                    </select>
                  </div>
                </div>

                {#if binding.target && fields.length > 0}
                  <div class="s-task-model-options">
                    {#each fields as field (field.name)}
                      <label class="s-field">
                        <span class="s-field-label">{field.label}</span>
                        {#if field.type === 'select'}
                          <select
                            class="s-select"
                            value={taskModelOptionValue(row.taskType, field)}
                            disabled={taskModelSaving}
                            onchange={(event) =>
                              handleTaskModelOptionChange(
                                row.taskType,
                                field,
                                event,
                              )}
                          >
                            {#each field.options as option (option.value)}
                              <option value={option.value}>
                                {option.label}
                              </option>
                            {/each}
                          </select>
                        {:else if field.type === 'textarea'}
                          <textarea
                            class="s-input s-textarea"
                            rows="3"
                            value={taskModelOptionValue(row.taskType, field)}
                            disabled={taskModelSaving}
                            oninput={(event) =>
                              handleTaskModelOptionChange(
                                row.taskType,
                                field,
                                event,
                              )}
                          ></textarea>
                        {:else if field.type === 'number'}
                          <input
                            class="s-input"
                            type="number"
                            min={field.min ?? undefined}
                            max={field.max ?? undefined}
                            step={field.step ?? 'any'}
                            value={taskModelOptionValue(row.taskType, field)}
                            disabled={taskModelSaving}
                            oninput={(event) =>
                              handleTaskModelOptionChange(
                                row.taskType,
                                field,
                                event,
                              )}
                          />
                        {:else if field.type === 'boolean'}
                          <input
                            class="s-checkbox"
                            type="checkbox"
                            checked={taskModelOptionValue(
                              row.taskType,
                              field,
                            ) === true}
                            disabled={taskModelSaving}
                            onchange={(event) =>
                              handleTaskModelOptionChange(
                                row.taskType,
                                field,
                                event,
                              )}
                          />
                        {:else}
                          <input
                            class="s-input"
                            type="text"
                            value={taskModelOptionValue(row.taskType, field)}
                            disabled={taskModelSaving}
                            oninput={(event) =>
                              handleTaskModelOptionChange(
                                row.taskType,
                                field,
                                event,
                              )}
                          />
                        {/if}
                        {#if field.description}
                          <span class="s-field-help">{field.description}</span>
                        {/if}
                      </label>
                    {/each}
                  </div>
                {:else if binding.target}
                  <div class="s-row-desc">
                    {t(
                      'settings.specializedModels.noOptions',
                      'This target has no configurable options.',
                    )}
                  </div>
                {/if}
              </div>
            {/each}
          </div>

          <div class="s-sticky-footer">
            <button
              class="btn-primary s-save-button s-save-button--inline"
              type="button"
              onclick={handleManualTaskModelSave}
            >
              {taskModelSaving
                ? t('common.saving', 'Saving…')
                : t('common.save', 'Save')}
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
                            {isOAuthDeviceFlowConnection(connection)
                              ? t(
                                  'settings.providers.oauthDescription',
                                  'OAuth device authorization managed by the provider.',
                                )
                              : isOAuthConnection(connection)
                                ? t(
                                    'settings.providers.oauthTokenDescription',
                                    'OAuth token configured from environment or data directory.',
                                  )
                                : t(
                                    'settings.providers.apiKeyDescription',
                                    'Static credential configured from environment or data directory.',
                                  )}
                          </div>
                        </div>

                        <div class="s-row-control">
                          {#if isOAuthDeviceFlowConnection(connection)}
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
                                    'Waiting for {provider} authorization…',
                                    providerTranslationValues(provider),
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
                                      'Connect {provider}',
                                      providerTranslationValues(provider),
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
                                      'Waiting for {provider} authorization…',
                                      providerTranslationValues(provider),
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
          <SettingsChannelsPanel />
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
