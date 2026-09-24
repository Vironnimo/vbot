<script>
  import {
    liveStateText,
    liveStateDotColor,
    errorMessage,
  } from './voice/voiceLabels.js';
  import './voice/voice.css';
  import { t } from '$lib/i18n.js';
  import Toggle from './ui/Toggle.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import Badge from './ui/Badge.svelte';
  import Dropdown from './Dropdown.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import { onDestroy, untrack } from 'svelte';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import {
    getWakewordStatus,
    setWakewordEnabled,
    setWakewordConfig,
    listMicrophones,
    listWakewordModels,
    importWakewordModel,
    deleteWakewordModel,
    onWakewordStatusChange,
    retryWakeword,
    stopWakewordCalibration,
    isDesktop,
  } from '$lib/desktopBridge.js';
  import {
    createVoiceSettingsState,
    applyWakewordStatus,
    applyRuntimeStatus,
    buildVoiceSettingsPayload,
    voiceSettingsDirty,
    snapshotVoiceSettings,
  } from '$lib/wakewordSettings.js';
  import TranscriptionAudioSettings from './voice/TranscriptionAudioSettings.svelte';
  import WakewordCalibration from './voice/WakewordCalibration.svelte';

  const MAX_CUSTOM_WAKEWORD_MODEL_BYTES = 20 * 1024 * 1024;
  const VOICE_STATUS_RETRY_MS = 3000;
  const UNAVAILABLE_MICROPHONE_VALUE = '__configured_unavailable__';
  const SESSION_BEHAVIOR_OPTIONS = Object.freeze([
    {
      value: 'active',
      label: t('settings.voice.sessionBehaviorActive', 'Use active session'),
    },
    {
      value: 'new',
      label: t('settings.voice.sessionBehaviorNew', 'New session each time'),
    },
  ]);

  let {
    agents = [],
    settings = null,
    wakewordAvailable = true,
    onCommit = () => {},
    onToast = () => {},
    onError = () => {},
  } = $props();

  let voiceState = $state(createVoiceSettingsState());
  let lastSaved = $state(null);
  let loaded = $state(false);
  let cleanupStatusPoll = null;
  let destroyed = false;
  let voiceRuntimeInitialized = false;
  let voiceLoadInFlight = false;
  let voiceLoadRetryTimer = null;
  let previousDesktopMode = null;
  let runtimeLoadError = $state(false);
  let microphones = $state([]);
  let wakewordModels = $state([]);
  let saveState = $state('idle');
  let modelFileInput = $state();
  let modelActionState = $state('idle');
  let enableActionState = $state('idle');
  let deleteConfirmModel = $state(null);
  let calibrationBaselineSensitivities = $state(null);
  let calibrationActionState = $state('idle');

  let desktopMode = $derived(isDesktop() && wakewordAvailable);

  let agentOptions = $derived(
    agents.map((agent) => ({
      value: agent.id,
      label: agent.name || agent.id,
    })),
  );
  let selectedAgentValue = $derived(voiceState.target_agent_id || '');
  let configuredMicrophoneDevice = $derived(
    voiceState.microphone
      ? microphones.find((device) =>
          sameMicrophoneIdentity(device, voiceState.microphone),
        ) || null
      : null,
  );
  let microphoneOptions = $derived([
    {
      value: '',
      label: t('settings.voice.systemAutomaticMic', 'Automatic selection'),
      secondaryLabel: voiceState.activeMicrophone?.name || '',
    },
    ...(voiceState.microphone && !configuredMicrophoneDevice
      ? [
          {
            value: UNAVAILABLE_MICROPHONE_VALUE,
            label: voiceState.microphone.name,
            secondaryLabel: t(
              'settings.voice.configuredMicUnavailable',
              'Configured device unavailable',
            ),
            disabled: true,
          },
        ]
      : []),
    ...microphones.map((device) => ({
      value: String(device.index),
      label: device.name,
      secondaryLabel: device.supported
        ? t('settings.voice.compatibleMic', 'Compatible')
        : t('settings.voice.incompatibleMic', 'Unsupported format'),
      disabled: !device.supported,
    })),
  ]);
  let selectedMicrophoneValue = $derived(
    configuredMicrophoneDevice
      ? String(configuredMicrophoneDevice.index)
      : voiceState.microphone
        ? UNAVAILABLE_MICROPHONE_VALUE
        : '',
  );
  let modelActionBusy = $derived(modelActionState !== 'idle');
  let enableActionBusy = $derived(enableActionState !== 'idle');
  let calibrationSessionActive = $derived(
    calibrationBaselineSensitivities !== null,
  );

  let calibrationActionBusy = $derived(calibrationActionState !== 'idle');

  let liveStateLabel = $derived(liveStateText(voiceState.liveState));
  let liveStateDotClass = $derived(liveStateDotColor(voiceState.liveState));

  let dirty = $derived(
    !calibrationSessionActive && voiceSettingsDirty(voiceState, lastSaved),
  );
  let transcriptionSaveStatus = $state('idle');
  let wakewordSaveStatus = $derived(
    saveState === 'saved' && dirty ? 'idle' : saveState,
  );
  // The section shows one save state for its two autosaved parts; a write in
  // flight wins over a failure, and a failure over a confirmation.
  let voiceSaveStatus = $derived.by(() => {
    const states = [transcriptionSaveStatus, wakewordSaveStatus];
    for (const candidate of ['saving', 'error', 'saved']) {
      if (states.includes(candidate)) return candidate;
    }
    return 'idle';
  });
  let enableToggleDisabled = $derived(
    !loaded ||
      enableActionBusy ||
      modelActionBusy ||
      calibrationSessionActive ||
      (!voiceState.enabled &&
        (!voiceState.target_agent_id || voiceState.mode === 'unavailable')),
  );
  const autosaveContext = useAutosaveContext();
  const voiceAutosave = createAutosaveParticipant({
    getSnapshot: () => snapshotVoiceSettings(voiceState),
    hasChanges: voiceConfigHasChanges,
    save: persistCurrentConfig,
  });

  const unregisterVoiceAutosave = autosaveContext.register(voiceAutosave);

  function sameMicrophoneIdentity(left, right) {
    return left?.name === right?.name && left?.host_api === right?.host_api;
  }

  onDestroy(() => {
    destroyed = true;
    unregisterVoiceAutosave();

    if (voiceLoadRetryTimer !== null) {
      clearTimeout(voiceLoadRetryTimer);
      voiceLoadRetryTimer = null;
    }
    if (cleanupStatusPoll) {
      cleanupStatusPoll();
      cleanupStatusPoll = null;
    }
    if (voiceState.calibration?.active) {
      void stopWakewordCalibration().catch(() => {});
    }
  });

  function scheduleVoiceStatusRetry() {
    if (destroyed || !desktopMode || voiceLoadRetryTimer !== null) return;
    voiceLoadRetryTimer = setTimeout(() => {
      voiceLoadRetryTimer = null;
      void loadStatus();
    }, VOICE_STATUS_RETRY_MS);
  }

  async function loadStatus() {
    if (
      destroyed ||
      !desktopMode ||
      voiceLoadInFlight ||
      voiceRuntimeInitialized
    ) {
      return;
    }
    voiceLoadInFlight = true;
    try {
      const [status, availableMicrophones, availableModels] = await Promise.all(
        [getWakewordStatus(), listMicrophones(), listWakewordModels()],
      );
      if (destroyed || !desktopMode) {
        return;
      }
      voiceState = applyWakewordStatus(voiceState, status);
      if (status?.calibration?.active) {
        calibrationBaselineSensitivities = {
          ...voiceState.model_sensitivities,
        };
      }
      microphones = availableMicrophones;
      wakewordModels = availableModels;
      lastSaved = snapshotVoiceSettings(voiceState);
      runtimeLoadError = false;
      voiceRuntimeInitialized = true;
      loaded = true;
      // The poll only carries observed runtime fields (live state, mock flag)
      // into state — never editable config — so a poll firing during autosave
      // cannot revert an unsaved edit.
      cleanupStatusPoll = onWakewordStatusChange((nextStatus) => {
        const wasCalibrating = Boolean(voiceState.calibration?.active);
        voiceState = applyRuntimeStatus(voiceState, nextStatus);
        if (
          calibrationBaselineSensitivities &&
          wasCalibrating &&
          !voiceState.calibration.active &&
          calibrationActionState === 'idle'
        ) {
          restoreCalibrationDraft();
        }
      });
    } catch {
      if (!destroyed && desktopMode) {
        runtimeLoadError = true;
        scheduleVoiceStatusRetry();
      }
    } finally {
      voiceLoadInFlight = false;
    }
  }

  $effect(() => {
    const active = desktopMode;
    untrack(() => {
      if (active === previousDesktopMode) return;
      previousDesktopMode = active;
      if (active) {
        loaded = false;
        runtimeLoadError = false;
        voiceRuntimeInitialized = false;
        void loadStatus();
        return;
      }
      if (voiceLoadRetryTimer !== null) {
        clearTimeout(voiceLoadRetryTimer);
        voiceLoadRetryTimer = null;
      }
      if (cleanupStatusPoll) {
        cleanupStatusPoll();
        cleanupStatusPoll = null;
      }
      voiceRuntimeInitialized = false;
      runtimeLoadError = false;
      loaded = true;
    });
  });

  async function handleEnabledChange() {
    if (enableActionBusy) return;
    const enabled = !voiceState.enabled;
    enableActionState = enabled ? 'enabling' : 'disabling';
    voiceState = { ...voiceState, enabled };
    try {
      const result = await setWakewordEnabled(enabled);
      const acceptedEnabled =
        typeof result?.enabled === 'boolean' ? result.enabled : enabled;
      const errorCode =
        typeof result?.error_code === 'string' ? result.error_code : null;
      voiceState = applyWakewordStatus(voiceState, {
        enabled: acceptedEnabled,
        state: errorCode ? 'error' : acceptedEnabled ? 'starting' : 'off',
        error_code: errorCode,
      });
      lastSaved = lastSaved
        ? { ...lastSaved, enabled: acceptedEnabled }
        : snapshotVoiceSettings(voiceState);
    } catch (error) {
      voiceState = { ...voiceState, enabled: !enabled };
      onToast({
        title: t('errors.generic', 'Something went wrong. Try again.'),
        message: error?.message || '',
        variant: 'error',
      });
    } finally {
      enableActionState = 'idle';
    }
  }

  function saveConfig() {
    return voiceAutosave.runSave('manual', { force: true });
  }

  function voiceConfigHasChanges() {
    if (!desktopMode || calibrationSessionActive) {
      return false;
    }
    return (
      Object.keys(buildVoiceSettingsPayload(voiceState, lastSaved)).length > 0
    );
  }

  async function persistCurrentConfig() {
    const payload = buildVoiceSettingsPayload(voiceState, lastSaved);
    if (Object.keys(payload).length === 0) return true;
    const savedSnapshot = snapshotVoiceSettings(voiceState);
    saveState = 'saving';
    try {
      await setWakewordConfig(payload);
      lastSaved = savedSnapshot;
      saveState = 'saved';
      return true;
    } catch (error) {
      saveState = 'error';
      onToast({
        title: t('errors.generic', 'Something went wrong. Try again.'),
        message: error?.message || '',
        variant: 'error',
      });
      return false;
    }
  }

  function handleAgentChange(value) {
    voiceState = { ...voiceState, target_agent_id: value || null };
    void saveConfig();
  }

  function handleSessionBehaviorChange(value) {
    voiceState = { ...voiceState, session_behavior: value };
    void saveConfig();
  }

  function handleMicrophoneChange(value) {
    const parsed = Number.parseInt(value, 10);
    const device = Number.isInteger(parsed)
      ? microphones.find((candidate) => candidate.index === parsed)
      : null;
    voiceState = {
      ...voiceState,
      microphone: device
        ? {
            index: device.index,
            name: device.name,
            host_api: device.host_api || '',
          }
        : null,
    };
    void saveConfig();
  }

  async function handleWakewordModelToggle(model, checked) {
    const isActive = voiceState.active_model_ids.includes(model.id);
    if (checked === isActive) return;
    if (checked && voiceState.active_model_ids.length >= 2) return;
    if (!checked && voiceState.active_model_ids.length <= 1) return;
    voiceState = {
      ...voiceState,
      active_model_ids: checked
        ? [...voiceState.active_model_ids, model.id]
        : voiceState.active_model_ids.filter((modelId) => modelId !== model.id),
    };
    await saveConfig();
    if (saveState !== 'saved') return;
    await refreshEditableStatus();
  }

  function chooseWakewordModelFile() {
    modelFileInput?.click();
  }

  async function handleWakewordModelFile(event) {
    const input = event.currentTarget;
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;

    if (file.size > MAX_CUSTOM_WAKEWORD_MODEL_BYTES) {
      onToast({
        title: t(
          'settings.voice.importTooLargeTitle',
          'Wakeword model is too large.',
        ),
        message: t(
          'settings.voice.importTooLargeMessage',
          'Choose a TFLite model no larger than 20 MiB.',
        ),
        variant: 'error',
      });
      return;
    }

    modelActionState = 'importing';
    try {
      if (!(await voiceAutosave.flush())) return;
      const contentBase64 = await readFileAsBase64(file);
      const imported = await importWakewordModel(file.name, contentBase64);
      wakewordModels = await listWakewordModels();
      await refreshEditableStatus();
      onToast({
        title: imported.activated
          ? t(
              'settings.voice.importSuccessActive',
              'Wakeword model imported and activated.',
            )
          : t(
              'settings.voice.importSuccessInactive',
              'Wakeword model imported. Deactivate another model to use it.',
            ),
        variant: 'success',
      });
    } catch (error) {
      onToast({
        title: t('errors.generic', 'Something went wrong. Try again.'),
        message: error?.message || '',
        variant: 'error',
      });
    } finally {
      modelActionState = 'idle';
    }
  }

  async function refreshEditableStatus() {
    const status = await getWakewordStatus();
    voiceState = applyWakewordStatus(voiceState, status);
    lastSaved = snapshotVoiceSettings(voiceState);
  }

  async function confirmDeleteWakewordModel() {
    const model = deleteConfirmModel;
    deleteConfirmModel = null;
    if (!model?.removable) return;

    modelActionState = 'deleting';
    try {
      if (!(await voiceAutosave.flush())) return;
      await deleteWakewordModel(model.id);
      wakewordModels = await listWakewordModels();
      await refreshEditableStatus();
      onToast({
        title: t('settings.voice.deleteSuccess', 'Wakeword model removed.'),
        variant: 'success',
      });
    } catch (error) {
      wakewordModels = await listWakewordModels();
      await refreshEditableStatus();
      onToast({
        title: t('errors.generic', 'Something went wrong. Try again.'),
        message: error?.message || '',
        variant: 'error',
      });
    } finally {
      modelActionState = 'idle';
    }
  }

  function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || '');
        const separator = result.indexOf(',');
        if (separator < 0) {
          reject(new Error('The wakeword model could not be read.'));
          return;
        }
        resolve(result.slice(separator + 1));
      };
      reader.onerror = () =>
        reject(
          reader.error || new Error('The wakeword model could not be read.'),
        );
      reader.readAsDataURL(file);
    });
  }

  function handleSensitivityInput(modelId, event) {
    const value = parseFloat(event.target.value);
    if (Number.isFinite(value)) {
      voiceState = {
        ...voiceState,
        model_sensitivities: {
          ...voiceState.model_sensitivities,
          [modelId]: value,
        },
      };
    }
  }

  function handleSensitivityChange() {
    if (!calibrationSessionActive) {
      void saveConfig();
    }
  }

  function restoreCalibrationDraft() {
    if (!calibrationBaselineSensitivities) return;
    voiceState = {
      ...voiceState,
      model_sensitivities: {
        ...calibrationBaselineSensitivities,
      },
    };
    calibrationBaselineSensitivities = null;
  }

  async function handleRetry() {
    try {
      voiceState = { ...voiceState, liveState: 'starting', errorCode: null };
      await retryWakeword();
      microphones = await listMicrophones();
    } catch (error) {
      onToast({
        title: t('settings.voice.retryFailed', 'Voice could not restart.'),
        message: error?.message || '',
        variant: 'error',
      });
    }
  }
</script>

<div class="voice-settings">
  <TranscriptionAudioSettings
    {settings}
    {onCommit}
    {onError}
    onSaveStatusChange={(status) => (transcriptionSaveStatus = status)}
  />

  {#if !desktopMode}
    <div class="s-group">
      <div class="s-row">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.enabled', 'Wakeword listening')}
          </div>
          <div class="s-row-desc">
            {t(
              'settings.voice.desktopOnly',
              'Wakeword listening is configured in the vBot Desktop app. The transcription audio settings above are server-wide.',
            )}
          </div>
        </div>
      </div>
    </div>
  {:else}
    {#if runtimeLoadError && !loaded}
      <Banner variant="error" class="voice-attention-banner" role="alert">
        <div class="voice-attention-copy">
          <strong>
            {t(
              'settings.voice.statusUnavailableTitle',
              'Desktop Voice status unavailable',
            )}
          </strong>
          <p>
            {t(
              'settings.voice.statusUnavailableMessage',
              'The Desktop bridge did not return Voice settings. Retrying automatically…',
            )}
          </p>
        </div>
      </Banner>
    {/if}

    <!-- Wakeword listening: detection, the phrases it listens for, and where
         a recognised command goes. -->
    <div class="s-group">
      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.enabled', 'Wakeword listening')}
          </div>
          <div class="s-row-desc">
            {t(
              'settings.voice.subtitle',
              'Wakeword detection and voice command settings.',
            )}
          </div>
        </div>
        <div class="s-row-control">
          <Toggle
            checked={voiceState.enabled}
            onChange={handleEnabledChange}
            disabled={enableToggleDisabled}
            ariaLabel={t(
              'settings.voice.enabledAria',
              'Enable wakeword listening',
            )}
          />
        </div>
      </div>

      {#if voiceState.mock}
        <div class="s-group__block s-group__block--attached">
          <div class="voice-mock-warning" role="alert">
            {t(
              'settings.voice.mockWarning',
              'Voice is running in demo mode. State changes are simulated; no microphone is heard and no command is sent. Restart Desktop without --mock-wakeword for real detection.',
            )}
          </div>
        </div>
      {/if}

      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.state', 'Status')}
          </div>
        </div>
        <div class="s-row-control">
          <span class="voice-state" aria-live="polite">
            <span class="voice-state-dot {liveStateDotClass}" aria-hidden="true"
            ></span>
            <span class="voice-state-label">{liveStateLabel}</span>
          </span>
        </div>
      </div>

      {#if voiceState.liveState === 'error' || voiceState.liveState === 'microphone_disconnected' || voiceState.mode === 'unavailable'}
        {@const microphoneDisconnected =
          voiceState.liveState === 'microphone_disconnected'}
        <div class="s-group__block s-group__block--attached">
          <Banner
            variant={microphoneDisconnected ? 'warn' : 'error'}
            class="voice-attention-banner"
            role={microphoneDisconnected ? 'status' : 'alert'}
          >
            <div class="voice-attention-copy">
              <strong>
                {microphoneDisconnected
                  ? t(
                      'settings.voice.microphoneDisconnectedTitle',
                      'Microphone disconnected',
                    )
                  : t('settings.voice.errorTitle', 'Voice needs attention')}
              </strong>
              <p>
                {errorMessage(
                  voiceState.mode === 'unavailable'
                    ? 'voice_stack_unavailable'
                    : voiceState.errorCode,
                )}
              </p>
            </div>
            {#if voiceState.errorCode !== 'missing_target_agent' && voiceState.errorCode !== 'target_agent_unavailable' && voiceState.errorCode !== 'speech_to_text_unconfigured' && voiceState.mode !== 'unavailable'}
              <Button
                variant="secondary"
                class="voice-retry"
                onClick={handleRetry}
              >
                {t('settings.voice.retry', 'Retry listening')}
              </Button>
            {/if}
          </Banner>
        </div>
      {/if}

      <!-- Active wakeword models and local model management -->
      <div class="s-row s-row--stacked">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.models', 'Wakeword phrases')}
          </div>
          <div class="s-row-desc">
            {t(
              'settings.voice.modelDescription',
              'Choose one or two phrases to listen for at the same time. Each model keeps its own sensitivity.',
            )}
          </div>
        </div>
        <div class="s-row-control voice-model-control">
          <div class="voice-model-list">
            {#each wakewordModels as model (model.id)}
              {@const active = voiceState.active_model_ids.includes(model.id)}
              {@const sensitivity =
                voiceState.model_sensitivities[model.id] ?? 0.5}
              <div
                class:voice-model-card--active={active}
                class="voice-model-card"
              >
                <div class="voice-model-card__header">
                  <div class="voice-model-card__identity">
                    <span class="voice-model-card__name">{model.label}</span>
                    <Badge
                      variant={model.source === 'built_in' ? 'info' : 'neutral'}
                    >
                      {model.source === 'built_in'
                        ? t('settings.voice.modelBuiltIn', 'Built-in')
                        : t('settings.voice.modelImported', 'Imported TFLite')}
                    </Badge>
                  </div>
                  <Toggle
                    size="sm"
                    checked={active}
                    onChange={(checked) =>
                      handleWakewordModelToggle(model, checked)}
                    disabled={!loaded ||
                      modelActionBusy ||
                      enableActionBusy ||
                      calibrationSessionActive ||
                      (active && voiceState.active_model_ids.length === 1) ||
                      (!active && voiceState.active_model_ids.length === 2)}
                    ariaLabel={t(
                      'settings.voice.modelToggleAria',
                      'Listen for {name}',
                      { name: model.label },
                    )}
                  />
                </div>
                {#if active}
                  <div class="voice-model-card__tuning">
                    <div class="voice-model-card__sensitivity">
                      <label for={`voice-sensitivity-${model.id}`}>
                        {t('settings.voice.sensitivity', 'Sensitivity')}
                      </label>
                      <span>{Math.round(sensitivity * 100)}%</span>
                    </div>
                    <input
                      id={`voice-sensitivity-${model.id}`}
                      type="range"
                      min="0.05"
                      max="0.95"
                      step="0.05"
                      value={sensitivity}
                      oninput={(event) =>
                        handleSensitivityInput(model.id, event)}
                      onchange={handleSensitivityChange}
                      disabled={!loaded ||
                        modelActionBusy ||
                        enableActionBusy ||
                        calibrationActionBusy ||
                        calibrationSessionActive}
                    />
                    <div class="voice-slider-labels">
                      <span
                        >{t(
                          'settings.voice.lessSensitive',
                          'Less sensitive',
                        )}</span
                      >
                      <span
                        >{t(
                          'settings.voice.moreSensitive',
                          'More sensitive',
                        )}</span
                      >
                    </div>
                  </div>
                {/if}
                {#if model.removable && !active}
                  <div class="voice-model-card__actions">
                    <Button
                      variant="tertiary"
                      disabled={!loaded ||
                        modelActionBusy ||
                        enableActionBusy ||
                        calibrationSessionActive}
                      onClick={() => (deleteConfirmModel = model)}
                    >
                      {t('settings.voice.removeModel', 'Remove imported model')}
                    </Button>
                  </div>
                {/if}
              </div>
            {/each}
          </div>
          <div class="voice-model-actions">
            <span class="voice-model-limit">
              {t(
                'settings.voice.modelLimit',
                '{count} of 2 wakeword models active',
                { count: voiceState.active_model_ids.length },
              )}
            </span>
            <input
              bind:this={modelFileInput}
              class="voice-model-file"
              type="file"
              accept=".tflite,application/octet-stream"
              onchange={handleWakewordModelFile}
            />
            <Button
              variant="secondary"
              loading={modelActionState === 'importing'}
              disabled={!loaded ||
                modelActionBusy ||
                enableActionBusy ||
                calibrationSessionActive}
              onClick={chooseWakewordModelFile}
            >
              {t('settings.voice.importModel', 'Import TFLite model')}
            </Button>
          </div>
        </div>
      </div>

      <WakewordCalibration
        {onToast}
        bind:voiceState
        {loaded}
        {wakewordModels}
        {modelActionBusy}
        {enableActionBusy}
        bind:calibrationBaselineSensitivities
        bind:calibrationActionState
        {calibrationActionBusy}
        {calibrationSessionActive}
        {voiceConfigHasChanges}
        {saveConfig}
        {restoreCalibrationDraft}
      />

      <!-- Target Agent dropdown -->
      <div class="s-row">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.targetAgent', 'Personal Agent')}
          </div>
          <div class="s-row-desc">
            {t(
              'settings.voice.targetAgentDescription',
              'The Personal Agent that receives spoken commands on this server. Project Agents and other servers use separate routing.',
            )}
          </div>
        </div>
        <div class="s-row-control">
          <Dropdown
            value={selectedAgentValue}
            options={[
              { value: '', label: t('settings.voice.noAgent', '— (none)') },
              ...agentOptions,
            ]}
            placeholder={t('settings.voice.noAgent', '— (none)')}
            onValueChange={handleAgentChange}
            disabled={!loaded ||
              agentOptions.length === 0 ||
              enableActionBusy ||
              calibrationSessionActive}
          />
        </div>
      </div>

      <!-- Session behavior -->
      <div class="s-row">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.sessionBehavior', 'Session')}
          </div>
        </div>
        <div class="s-row-control">
          <Dropdown
            value={voiceState.session_behavior}
            options={SESSION_BEHAVIOR_OPTIONS}
            onValueChange={handleSessionBehaviorChange}
            disabled={!loaded || enableActionBusy || calibrationSessionActive}
          />
        </div>
      </div>

      <!-- Microphone picker -->
      <div class="s-row">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.microphone', 'Microphone')}
          </div>
        </div>
        <div class="s-row-control">
          <Dropdown
            value={selectedMicrophoneValue}
            options={microphoneOptions}
            ariaLabel={t('settings.voice.microphone', 'Microphone')}
            triggerClass="voice-microphone-dropdown"
            onValueChange={handleMicrophoneChange}
            disabled={!loaded ||
              microphones.length === 0 ||
              enableActionBusy ||
              calibrationSessionActive}
          />
        </div>
      </div>

      <!-- Privacy note -->
      <div class="s-group__block s-group__note">
        <p>
          {t(
            'settings.voice.privacyNote',
            'While listening is enabled, microphone audio is analyzed continuously on this device. Nothing is sent unless a wake phrase matches. After a match, the command recording—including up to 320 ms of locally buffered audio immediately before detection—is sent to your configured vBot speech backend for transcription.',
          )}
        </p>
        <p>
          {t(
            'settings.voice.cancelPhrases',
            'Say “abbrechen” or “vergiss es” at the end of the same recording to discard the entire command before it starts a Run.',
          )}
        </p>
      </div>
    </div>
  {/if}
</div>

<!-- One save state for the whole section: transcription audio and the
     Desktop wakeword configuration both save as they change. -->
<div class="s-footer voice-save-state" aria-live="polite">
  {#if voiceSaveStatus === 'saving'}
    {t('common.saving', 'Saving…')}
  {:else if voiceSaveStatus === 'error'}
    {t('common.saveFailed', 'Not saved')}
  {:else if voiceSaveStatus === 'saved'}
    {t('common.saved', 'Saved')}
  {/if}
</div>

{#if deleteConfirmModel}
  <ConfirmDialog
    title={t('settings.voice.deleteConfirmTitle', 'Remove wakeword model')}
    body={t(
      'settings.voice.deleteConfirm',
      'Remove “{name}” permanently from this Desktop? The TFLite file stored by vBot will be deleted.',
      { name: deleteConfirmModel.label },
    )}
    confirmLabel={t('common.delete', 'Delete')}
    onConfirm={confirmDeleteWakewordModel}
    onCancel={() => (deleteConfirmModel = null)}
  />
{/if}
