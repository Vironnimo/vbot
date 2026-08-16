<script>
  import { onDestroy, onMount, untrack } from 'svelte';
  import Dropdown from './Dropdown.svelte';
  import Button from './ui/Button.svelte';
  import Badge from './ui/Badge.svelte';
  import Banner from './ui/Banner.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import Toggle from './ui/Toggle.svelte';
  import { useAutosaveContext } from '$lib/autosave.js';
  import { updateSettings } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    TRANSCRIPTION_AUDIO_FORMATS,
    TRANSCRIPTION_AUDIO_PROFILES,
    TRANSCRIPTION_AUDIO_SAMPLE_RATES,
    buildTranscriptionAudioSettingsPayload,
    normalizeTranscriptionAudio,
  } from '$lib/settingsView.js';
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
    startWakewordCalibration,
    stopWakewordCalibration,
    restartWakewordCalibration,
    retryWakewordModelCalibration,
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

  const MAX_VOICE_FLUSH_PASSES = 10;
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
  let microphones = $state([]);
  let wakewordModels = $state([]);
  let saveState = $state('idle');
  let saveChain = Promise.resolve();
  let modelFileInput = $state();
  let modelActionState = $state('idle');
  let deleteConfirmModel = $state(null);
  let calibrationBaselineSensitivities = $state(null);
  let calibrationActionState = $state('idle');
  let calibrationDiscardConfirm = $state(false);
  let calibrationRetryModelId = $state(null);
  let transcriptionAudio = $state(
    untrack(() => normalizeTranscriptionAudio(settings)),
  );
  let lastSavedTranscriptionAudio = $state(
    untrack(() => normalizeTranscriptionAudio(settings)),
  );
  let transcriptionSaveState = $state('idle');
  let transcriptionSaveChain = Promise.resolve();
  let desktopMode = $derived(isDesktop() && wakewordAvailable);

  let agentOptions = $derived(
    agents.map((agent) => ({
      value: agent.id,
      label: agent.name || agent.id,
    })),
  );
  let selectedAgentValue = $derived(voiceState.target_agent_id || '');
  let microphoneOptions = $derived([
    {
      value: '',
      label: t('settings.voice.systemAutomaticMic', 'Automatic selection'),
      secondaryLabel: voiceState.activeMicrophone?.name || '',
    },
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
    Number.isInteger(voiceState.microphone)
      ? String(voiceState.microphone)
      : '',
  );
  let modelActionBusy = $derived(modelActionState !== 'idle');
  let calibrationSessionActive = $derived(
    calibrationBaselineSensitivities !== null,
  );
  let calibrationActive = $derived(Boolean(voiceState.calibration?.active));
  let calibrationActionBusy = $derived(calibrationActionState !== 'idle');
  let calibrationReady = $derived(
    voiceState.calibration?.phase === 'ready' &&
      voiceState.active_model_ids.every((modelId) =>
        Number.isFinite(
          voiceState.calibration?.recommended_sensitivities?.[modelId],
        ),
      ),
  );
  let calibrationCanStart = $derived(
    loaded &&
      voiceState.enabled &&
      voiceState.liveState === 'listening' &&
      voiceState.mode === 'real' &&
      !modelActionBusy &&
      !calibrationSessionActive,
  );
  let calibrationNoiseHigh = $derived(
    Boolean(voiceState.calibration?.noise_high),
  );
  let calibrationModelProgress = $derived(() => {
    const activeIds = voiceState.active_model_ids || [];
    const targetIndex = activeIds.indexOf(
      voiceState.calibration?.target_model_id,
    );
    if (targetIndex < 0) return null;
    return { index: targetIndex + 1, total: activeIds.length };
  });
  let transcriptionProfileOptions = $derived(
    TRANSCRIPTION_AUDIO_PROFILES.map((profile) => ({
      value: profile,
      label:
        profile === 'compatibility'
          ? t(
              'settings.voice.transcriptionProfileCompatibility',
              'Maximum compatibility (recommended)',
            )
          : profile === 'high_quality'
            ? t(
                'settings.voice.transcriptionProfileHighQuality',
                'High fidelity',
              )
            : t('settings.voice.transcriptionProfileCustom', 'Custom'),
    })),
  );
  let transcriptionFormatOptions = $derived(
    TRANSCRIPTION_AUDIO_FORMATS.map((format) => ({
      value: format,
      label:
        format === 'wav'
          ? t('settings.voice.transcriptionFormatWav', 'WAV (PCM16)')
          : t(
              'settings.voice.transcriptionFormatFlac',
              'FLAC (lossless PCM16)',
            ),
    })),
  );
  let transcriptionSampleRateOptions = $derived(
    TRANSCRIPTION_AUDIO_SAMPLE_RATES.map((sampleRate) => ({
      value: String(sampleRate),
      label:
        sampleRate === 16000
          ? t(
              'settings.voice.transcriptionSampleRate16',
              '16 kHz (recommended for speech)',
            )
          : `${sampleRate / 1000} kHz`,
    })),
  );

  let liveStateLabel = $derived(liveStateText(voiceState.liveState));
  let liveStateDotClass = $derived(liveStateDotColor(voiceState.liveState));

  let dirty = $derived(
    !calibrationSessionActive && voiceSettingsDirty(voiceState, lastSaved),
  );
  let enableToggleDisabled = $derived(
    !loaded ||
      calibrationSessionActive ||
      (!voiceState.enabled &&
        (!voiceState.target_agent_id || voiceState.mode === 'unavailable')),
  );
  const autosaveContext = useAutosaveContext();
  const voiceAutosaveParticipant = {
    flush: flushVoiceAutosave,
    hasPending: () =>
      saveState === 'saving' ||
      transcriptionSaveState === 'saving' ||
      calibrationActionBusy ||
      voiceConfigHasChanges() ||
      transcriptionAudioHasChanges(),
  };
  const unregisterVoiceAutosave = autosaveContext.register(
    voiceAutosaveParticipant,
  );

  function liveStateText(state) {
    if (state === 'wakeword_detected') {
      return t('voice.state.wakewordDetected', 'Wakeword detected');
    }
    const key = `voice.state.${state}`;
    return t(key, state);
  }

  function errorMessage(code) {
    const messages = {
      no_server: t(
        'settings.voice.error.noServer',
        'Voice has no active server. Connect the Desktop app to a server and try again.',
      ),
      server_unreachable: t(
        'settings.voice.error.serverUnreachable',
        'Voice could not reach the active server. Check the Desktop connection and try again.',
      ),
      speech_to_text_unconfigured: t(
        'settings.voice.error.speechToTextUnconfigured',
        'Configure a Speech-to-text Model under Settings → Models before enabling wakeword listening.',
      ),
      speech_to_text_unavailable: t(
        'settings.voice.error.speechToTextUnavailable',
        'The configured Speech-to-text Model is not currently usable. Check its Provider connection or choose another Model under Settings → Models.',
      ),
      speech_to_text_readiness_failed: t(
        'settings.voice.error.speechToTextReadiness',
        'Voice could not verify the Speech-to-text configuration. Check the Desktop log and try again.',
      ),
      missing_target_agent: t(
        'settings.voice.error.missingTarget',
        'Choose a Personal Agent for this server before enabling wakeword listening.',
      ),
      target_agent_unavailable: t(
        'settings.voice.error.targetUnavailable',
        'The selected Personal Agent no longer exists on this server. Choose another Agent.',
      ),
      engine_start_failed: t(
        'settings.voice.error.engine',
        'The on-device wakeword model could not start. Restart the Desktop app and try again.',
      ),
      wakeword_model_unavailable: t(
        'settings.voice.error.modelUnavailable',
        'The selected wakeword model is no longer available. Choose another model or import it again.',
      ),
      wakeword_model_invalid: t(
        'settings.voice.error.modelInvalid',
        'The wakeword model is not a compatible pyopen-wakeword TFLite model.',
      ),
      microphone_unavailable: t(
        'settings.voice.error.microphone',
        'No compatible microphone is available. Connect a microphone or choose another input device, then retry.',
      ),
      microphone_read_failed: t(
        'settings.voice.error.microphoneRead',
        'The microphone stopped responding. Check the device connection and retry.',
      ),
      detection_failed: t(
        'settings.voice.error.detection',
        'Wakeword detection stopped unexpectedly. Retry listening.',
      ),
      session_resolution_failed: t(
        'settings.voice.error.session',
        'vBot could not open the target Agent session. Check the server connection and retry.',
      ),
      send_failed: t(
        'settings.voice.error.send',
        'The spoken command could not be sent. Check the server connection and retry.',
      ),
      voice_stack_unavailable: t(
        'settings.voice.error.stackUnavailable',
        'The Desktop Voice components are unavailable. Install the desktop Voice dependencies and restart vBot.',
      ),
    };
    return (
      messages[code] ||
      t(
        'settings.voice.error.unknown',
        'Voice stopped unexpectedly. Retry listening or restart the Desktop app.',
      )
    );
  }

  function liveStateDotColor(state) {
    switch (state) {
      case 'listening':
        return 'voice-dot--listening';
      case 'starting':
      case 'wakeword_detected':
        return 'voice-dot--detected';
      case 'recording':
        return 'voice-dot--recording';
      case 'transcribing':
      case 'sending':
        return 'voice-dot--processing';
      case 'sent':
        return 'voice-dot--listening';
      case 'cancelled':
      case 'no_speech':
      case 'transcription_failed':
      case 'microphone_disconnected':
        return 'voice-dot--warning';
      case 'error':
        return 'voice-dot--error';
      default:
        return 'voice-dot--off';
    }
  }

  onDestroy(() => {
    destroyed = true;
    unregisterVoiceAutosave();
    if (cleanupStatusPoll) {
      cleanupStatusPoll();
      cleanupStatusPoll = null;
    }
    if (voiceState.calibration?.active) {
      void stopWakewordCalibration();
    }
  });

  async function loadStatus() {
    try {
      const [status, availableMicrophones, availableModels] = await Promise.all(
        [getWakewordStatus(), listMicrophones(), listWakewordModels()],
      );
      if (destroyed) {
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
    } catch {
      // Bridge not available; keep defaults
    }
    if (destroyed) {
      return;
    }
    loaded = true;
    // The poll only carries observed runtime fields (live state, mock flag) into
    // state — never the editable config — so a poll firing during the autosave
    // debounce cannot revert an unsaved edit.
    cleanupStatusPoll = onWakewordStatusChange((status) => {
      const wasCalibrating = Boolean(voiceState.calibration?.active);
      voiceState = applyRuntimeStatus(voiceState, status);
      if (
        calibrationBaselineSensitivities &&
        wasCalibrating &&
        !voiceState.calibration.active &&
        calibrationActionState === 'idle'
      ) {
        restoreCalibrationDraft();
      }
    });
  }

  async function handleEnabledChange() {
    const enabled = !voiceState.enabled;
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
      lastSaved = snapshotVoiceSettings(voiceState);
    } catch (error) {
      voiceState = { ...voiceState, enabled: !enabled };
      onToast({
        title: t('errors.generic', 'Something went wrong. Try again.'),
        message: error?.message || '',
        variant: 'error',
      });
    }
  }

  function saveConfig() {
    saveChain = saveChain.then(persistCurrentConfig);
    return saveChain;
  }

  function voiceConfigHasChanges() {
    if (!desktopMode || calibrationSessionActive) {
      return false;
    }
    return (
      Object.keys(buildVoiceSettingsPayload(voiceState, lastSaved)).length > 0
    );
  }

  function transcriptionAudioHasChanges() {
    return (
      transcriptionAudio.profile !== lastSavedTranscriptionAudio.profile ||
      transcriptionAudio.format !== lastSavedTranscriptionAudio.format ||
      transcriptionAudio.sample_rate_hz !==
        lastSavedTranscriptionAudio.sample_rate_hz
    );
  }

  async function flushVoiceAutosave() {
    for (let pass = 0; pass < MAX_VOICE_FLUSH_PASSES; pass += 1) {
      const observedChain = saveChain;
      const observedTranscriptionChain = transcriptionSaveChain;
      const [saved, transcriptionSaved] = await Promise.all([
        observedChain,
        observedTranscriptionChain,
      ]);
      if (
        observedChain !== saveChain ||
        observedTranscriptionChain !== transcriptionSaveChain
      ) {
        continue;
      }
      if (saved === false || transcriptionSaved === false) {
        return false;
      }
      if (!voiceConfigHasChanges() && !transcriptionAudioHasChanges()) {
        return true;
      }
      const saves = [];
      if (voiceConfigHasChanges()) {
        saves.push(saveConfig());
      }
      if (transcriptionAudioHasChanges()) {
        saves.push(saveTranscriptionAudio());
      }
      if ((await Promise.all(saves)).some((result) => result === false)) {
        return false;
      }
    }
    return false;
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

  function saveTranscriptionAudio() {
    transcriptionSaveChain = transcriptionSaveChain.then(
      persistCurrentTranscriptionAudio,
    );
    return transcriptionSaveChain;
  }

  async function persistCurrentTranscriptionAudio() {
    if (!transcriptionAudioHasChanges()) return true;
    const savedSnapshot = { ...transcriptionAudio };
    transcriptionSaveState = 'saving';
    onError('');
    try {
      const nextSettings = await updateSettings(
        buildTranscriptionAudioSettingsPayload(savedSnapshot),
      );
      lastSavedTranscriptionAudio = normalizeTranscriptionAudio(nextSettings);
      onCommit(nextSettings);
      transcriptionSaveState = 'saved';
      return true;
    } catch (error) {
      transcriptionSaveState = 'error';
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
      return false;
    }
  }

  function handleTranscriptionProfileChange(profile) {
    transcriptionAudio = normalizeTranscriptionAudio({
      speech: {
        transcription_audio: {
          ...transcriptionAudio,
          profile,
        },
      },
    });
    void saveTranscriptionAudio();
  }

  function handleTranscriptionFormatChange(format) {
    transcriptionAudio = {
      ...transcriptionAudio,
      format,
    };
    void saveTranscriptionAudio();
  }

  function handleTranscriptionSampleRateChange(value) {
    const sampleRate = Number.parseInt(value, 10);
    if (!TRANSCRIPTION_AUDIO_SAMPLE_RATES.includes(sampleRate)) return;
    transcriptionAudio = {
      ...transcriptionAudio,
      sample_rate_hz: sampleRate,
    };
    void saveTranscriptionAudio();
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
    voiceState = {
      ...voiceState,
      microphone: Number.isInteger(parsed) ? parsed : null,
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

    modelActionState = 'importing';
    try {
      await saveChain;
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
      await saveChain;
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

  function calibrationScore(modelId) {
    return clampScore(voiceState.calibration?.scores?.[modelId]);
  }

  function calibrationPeak(modelId) {
    return clampScore(voiceState.calibration?.peaks?.[modelId]);
  }

  function calibrationNoiseLevel(modelId) {
    return clampScore(voiceState.calibration?.noise_levels?.[modelId]);
  }

  function calibrationSampleCount(modelId) {
    const count = Number(voiceState.calibration?.sample_counts?.[modelId]);
    return Number.isInteger(count) ? Math.max(0, count) : 0;
  }

  function calibrationRequiredSamples() {
    const required = Number(voiceState.calibration?.required_samples);
    return Number.isInteger(required) && required > 0 ? required : 3;
  }

  function calibrationRecommendation(modelId) {
    const recommendation = Number(
      voiceState.calibration?.recommended_sensitivities?.[modelId],
    );
    return Number.isFinite(recommendation) ? recommendation : null;
  }

  function calibrationThreshold(modelId) {
    const sensitivity =
      calibrationRecommendation(modelId) ??
      voiceState.model_sensitivities[modelId] ??
      0.5;
    return clampScore(1 - sensitivity);
  }

  function calibrationModelLabel(modelId) {
    return (
      wakewordModels.find((model) => model.id === modelId)?.label || modelId
    );
  }

  function calibrationInstruction() {
    const calibration = voiceState.calibration;
    if (calibration?.phase === 'noise') {
      return t(
        'settings.voice.calibrationNoiseInstruction',
        'Stay quiet for {seconds} seconds while vBot measures the room.',
        { seconds: calibration.noise_seconds_remaining || 1 },
      );
    }
    if (calibration?.phase === 'phrases') {
      const targetModelId = calibration.target_model_id;
      return t(
        'settings.voice.calibrationPhraseInstruction',
        'Say “{name}” naturally — {count} of {required} repetitions captured. Pause briefly between repetitions.',
        {
          name: calibrationModelLabel(targetModelId),
          count: calibrationSampleCount(targetModelId),
          required: calibrationRequiredSamples(),
        },
      );
    }
    if (calibration?.phase === 'ready') {
      return t(
        'settings.voice.calibrationReviewInstruction',
        'Measurement complete. Review the automatically calculated sensitivities, then apply them.',
      );
    }
    return t(
      'settings.voice.calibrationStopped',
      'Calibration stopped before a result was ready.',
    );
  }

  function calibrationStepState(step) {
    const phase = voiceState.calibration?.phase;
    const order = { noise: 0, phrases: 1, ready: 2 };
    const current = order[phase] ?? -1;
    if (step < current) return 'complete';
    if (step === current) return 'current';
    return 'pending';
  }

  function scorePercent(value) {
    return `${Math.round(clampScore(value) * 1000) / 10}%`;
  }

  function clampScore(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 0;
    return Math.max(0, Math.min(1, numeric));
  }

  async function handleStartCalibration() {
    calibrationActionState = 'starting';
    try {
      if (voiceConfigHasChanges() && !(await saveConfig())) return;
      calibrationBaselineSensitivities = {
        ...voiceState.model_sensitivities,
      };
      const status = await startWakewordCalibration();
      voiceState = applyRuntimeStatus(voiceState, status);
    } catch (error) {
      calibrationBaselineSensitivities = null;
      onToast({
        title: t(
          'settings.voice.calibrationStartFailed',
          'Calibration could not start.',
        ),
        message: error?.message || '',
        variant: 'error',
      });
    } finally {
      calibrationActionState = 'idle';
    }
  }

  async function handleRestartCalibration() {
    calibrationActionState = 'resetting';
    try {
      const status = await restartWakewordCalibration();
      voiceState = applyRuntimeStatus(voiceState, status);
    } catch (error) {
      onToast({
        title: t(
          'settings.voice.calibrationResetFailed',
          'Calibration could not restart.',
        ),
        message: error?.message || '',
        variant: 'error',
      });
    } finally {
      calibrationActionState = 'idle';
    }
  }

  async function handleRetryModelCalibration(modelId) {
    calibrationRetryModelId = modelId;
    try {
      const status = await retryWakewordModelCalibration(modelId);
      voiceState = applyRuntimeStatus(voiceState, status);
    } catch (error) {
      onToast({
        title: t(
          'settings.voice.calibrationRetryModelFailed',
          'Could not retry this phrase.',
        ),
        message: error?.message || '',
        variant: 'error',
      });
    } finally {
      calibrationRetryModelId = null;
    }
  }

  async function handleDiscardCalibration() {
    calibrationDiscardConfirm = false;
    calibrationActionState = 'discarding';
    try {
      const status = await stopWakewordCalibration();
      voiceState = applyRuntimeStatus(voiceState, status);
      restoreCalibrationDraft();
    } catch (error) {
      onToast({
        title: t(
          'settings.voice.calibrationStopFailed',
          'Calibration could not stop.',
        ),
        message: error?.message || '',
        variant: 'error',
      });
    } finally {
      calibrationActionState = 'idle';
    }
  }

  async function handleApplyCalibration() {
    if (!calibrationReady) return;
    calibrationActionState = 'applying';
    try {
      voiceState = {
        ...voiceState,
        model_sensitivities: {
          ...voiceState.model_sensitivities,
          ...voiceState.calibration.recommended_sensitivities,
        },
      };
      if (!(await saveConfig())) return;
      const status = await stopWakewordCalibration();
      voiceState = applyRuntimeStatus(voiceState, status);
      calibrationBaselineSensitivities = null;
      onToast({
        title: t(
          'settings.voice.calibrationApplied',
          'Wakeword sensitivity applied.',
        ),
        variant: 'success',
      });
    } catch (error) {
      onToast({
        title: t(
          'settings.voice.calibrationApplyFailed',
          'Calibration could not be applied.',
        ),
        message: error?.message || '',
        variant: 'error',
      });
    } finally {
      calibrationActionState = 'idle';
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

  onMount(() => {
    if (isDesktop() && wakewordAvailable) {
      void loadStatus();
    } else {
      loaded = true;
    }
  });
</script>

<div class="voice-settings">
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.voice.transcriptionProfile', 'Transcription audio')}
      </div>
      <div class="s-row-desc">
        {t(
          'settings.voice.transcriptionProfileDescription',
          'The audio sent to the Speech-to-text Model from both the Chat microphone and a command recorded after a wake phrase. Local wakeword detection keeps its optimized 16 kHz stream.',
        )}
      </div>
    </div>
    <div class="s-row-control">
      <Dropdown
        value={transcriptionAudio.profile}
        options={transcriptionProfileOptions}
        ariaLabel={t(
          'settings.voice.transcriptionProfile',
          'Transcription audio',
        )}
        onValueChange={handleTranscriptionProfileChange}
        disabled={transcriptionSaveState === 'saving'}
      />
    </div>
  </div>

  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.voice.transcriptionFormat', 'Format')}
      </div>
      <div class="s-row-desc">
        {t(
          'settings.voice.transcriptionFormatDescription',
          'Mono, signed 16-bit audio. WAV has the broadest Provider support; FLAC is lossless and smaller.',
        )}
      </div>
    </div>
    <div class="s-row-control">
      <Dropdown
        value={transcriptionAudio.format}
        options={transcriptionFormatOptions}
        ariaLabel={t('settings.voice.transcriptionFormat', 'Format')}
        onValueChange={handleTranscriptionFormatChange}
        disabled={transcriptionAudio.profile !== 'custom' ||
          transcriptionSaveState === 'saving'}
      />
    </div>
  </div>

  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.voice.transcriptionSampleRate', 'Sample rate')}
      </div>
      <div class="s-row-desc">
        {t(
          'settings.voice.transcriptionSampleRateDescription',
          '16 kHz is the speech-focused default. Higher rates retain more source detail but create larger uploads.',
        )}
      </div>
    </div>
    <div class="s-row-control">
      <Dropdown
        value={String(transcriptionAudio.sample_rate_hz)}
        options={transcriptionSampleRateOptions}
        ariaLabel={t('settings.voice.transcriptionSampleRate', 'Sample rate')}
        onValueChange={handleTranscriptionSampleRateChange}
        disabled={transcriptionAudio.profile !== 'custom' ||
          transcriptionSaveState === 'saving'}
      />
    </div>
  </div>

  <div class="voice-save-state" aria-live="polite">
    {#if transcriptionSaveState === 'saving'}
      {t('common.saving', 'Saving…')}
    {:else if transcriptionSaveState === 'saved' && !transcriptionAudioHasChanges()}
      {t('common.saved', 'Saved')}
    {:else if transcriptionSaveState === 'error'}
      {t('common.saveFailed', 'Not saved')}
    {/if}
  </div>

  {#if !desktopMode}
    <div class="s-row">
      <div class="s-row-info" style="max-width: 100%">
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
  {:else}
    <!-- Enable/disable toggle -->
    <div class="s-row">
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
      <div class="voice-mock-warning" role="alert">
        {t(
          'settings.voice.mockWarning',
          'Voice is running in demo mode. State changes are simulated; no microphone is heard and no command is sent. Restart Desktop without --mock-wakeword for real detection.',
        )}
      </div>
    {/if}

    <!-- Live state indicator -->
    <div class="s-row">
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
          <Button variant="secondary" class="voice-retry" onClick={handleRetry}>
            {t('settings.voice.retry', 'Retry listening')}
          </Button>
        {/if}
      </Banner>
    {/if}

    <!-- Active wakeword models and local model management -->
    <div class="s-row">
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
                    oninput={(event) => handleSensitivityInput(model.id, event)}
                    onchange={handleSensitivityChange}
                    disabled={!loaded ||
                      modelActionBusy ||
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
            disabled={!loaded || modelActionBusy || calibrationSessionActive}
            onClick={chooseWakewordModelFile}
          >
            {t('settings.voice.importModel', 'Import TFLite model')}
          </Button>
        </div>
        <div class="voice-model-limit">
          {t(
            'settings.voice.modelLimit',
            '{count} of 2 wakeword models active',
            { count: voiceState.active_model_ids.length },
          )}
        </div>
      </div>
    </div>

    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.voice.calibrationTitle', 'Wakeword calibration')}
        </div>
        <div class="s-row-desc">
          {t(
            'settings.voice.calibrationDescription',
            'Measure room noise and three natural repetitions per phrase to calculate a reliable sensitivity automatically.',
          )}
        </div>
      </div>
      <div class="s-row-control voice-calibration-control">
        {#if calibrationSessionActive}
          <div class="voice-calibration-panel">
            <div class="voice-calibration-header">
              <div>
                <div class="voice-calibration-heading">
                  {t(
                    'settings.voice.calibrationAnalyzer',
                    'Guided calibration',
                  )}
                </div>
                <div class="voice-calibration-instruction">
                  {calibrationInstruction()}
                </div>
                {#if calibrationModelProgress() && voiceState.calibration?.phase === 'phrases'}
                  <div class="voice-calibration-model-progress">
                    {t(
                      'settings.voice.calibrationOverallProgress',
                      'Model {index} of {total}',
                      calibrationModelProgress(),
                    )}
                  </div>
                {/if}
              </div>
              <StatusChip
                variant={calibrationReady
                  ? 'success'
                  : calibrationActive
                    ? 'warn'
                    : 'neutral'}
              >
                {calibrationReady
                  ? t(
                      'settings.voice.calibrationReadyToApply',
                      'Ready to apply',
                    )
                  : calibrationActive
                    ? t(
                        'settings.voice.calibrationListening',
                        'Commands paused',
                      )
                    : t(
                        'settings.voice.calibrationStopped',
                        'Calibration stopped',
                      )}
              </StatusChip>
            </div>

            {#if calibrationNoiseHigh}
              <div class="voice-calibration-noise-warning" role="alert">
                {t(
                  'settings.voice.calibrationNoiseHighWarning',
                  'Room noise is high ({level}). Consider moving to a quieter environment or reducing background noise for better results.',
                  {
                    level: Math.max(
                      ...Object.values(
                        voiceState.calibration?.noise_levels || {},
                      ),
                    ).toFixed(2),
                  },
                )}
              </div>
            {/if}

            <ol
              class="voice-calibration-steps"
              aria-label={t(
                'settings.voice.calibrationProgressAria',
                'Calibration progress',
              )}
            >
              <li data-state={calibrationStepState(0)}>
                <span>1</span>
                {t('settings.voice.calibrationStepNoise', 'Room noise')}
              </li>
              <li data-state={calibrationStepState(1)}>
                <span>2</span>
                {t('settings.voice.calibrationStepPhrases', 'Wakeword samples')}
              </li>
              <li data-state={calibrationStepState(2)}>
                <span>3</span>
                {t('settings.voice.calibrationStepReview', 'Review')}
              </li>
            </ol>

            <div class="voice-calibration-models">
              {#each wakewordModels.filter( (model) => voiceState.active_model_ids.includes(model.id), ) as model (model.id)}
                {@const score = calibrationScore(model.id)}
                {@const peak = calibrationPeak(model.id)}
                {@const noise = calibrationNoiseLevel(model.id)}
                {@const sampleCount = calibrationSampleCount(model.id)}
                {@const requiredSamples = calibrationRequiredSamples()}
                {@const recommendation = calibrationRecommendation(model.id)}
                {@const threshold = calibrationThreshold(model.id)}
                {@const currentSensitivity =
                  calibrationBaselineSensitivities?.[model.id] ?? 0.5}
                {@const isTarget =
                  voiceState.calibration?.target_model_id === model.id}
                {@const isCompleted = recommendation !== null}
                <div
                  class:voice-calibration-model--target={isTarget}
                  class:voice-calibration-model--complete={isCompleted}
                  class="voice-calibration-model"
                >
                  <div class="voice-calibration-model__header">
                    <span class="voice-calibration-model__identity">
                      {model.label}
                      {#if isTarget}
                        <span class="voice-calibration-model__target">
                          {t(
                            'settings.voice.calibrationSayNow',
                            'Say this now',
                          )}
                        </span>
                      {/if}
                      {#if isCompleted}
                        <span
                          class="voice-calibration-model__check"
                          aria-hidden="true">✓</span
                        >
                      {/if}
                    </span>
                    <span class="voice-calibration-model__values">
                      {t('settings.voice.calibrationScore', 'Score')}
                      {score.toFixed(2)}
                      · {t('settings.voice.calibrationNoise', 'Noise')}
                      {noise.toFixed(2)}
                      · {t('settings.voice.calibrationThreshold', 'Threshold')}
                      {threshold.toFixed(2)}
                    </span>
                  </div>
                  <div
                    class="voice-calibration-meter"
                    role="meter"
                    aria-label={t(
                      'settings.voice.calibrationMeterAria',
                      '{name} detector score',
                      { name: model.label },
                    )}
                    aria-valuemin="0"
                    aria-valuemax="1"
                    aria-valuenow={score}
                    style={`--score: ${scorePercent(score)}; --peak: ${scorePercent(peak)}; --threshold: ${scorePercent(threshold)};`}
                  >
                    <span
                      class:voice-calibration-meter__score--match={score >=
                        threshold}
                      class="voice-calibration-meter__score"
                    ></span>
                    {#if peak > 0}
                      <span
                        class="voice-calibration-meter__peak"
                        aria-hidden="true"
                      ></span>
                    {/if}
                    <span
                      class="voice-calibration-meter__threshold"
                      aria-hidden="true"
                    ></span>
                  </div>
                  <div class="voice-calibration-model__result">
                    <span>
                      {t(
                        'settings.voice.calibrationSamples',
                        '{count} / {required} samples',
                        { count: sampleCount, required: requiredSamples },
                      )}
                    </span>
                    {#if isCompleted}
                      <strong>
                        {t(
                          'settings.voice.calibrationCurrentSensitivity',
                          'Current',
                        )}
                        {Math.round(currentSensitivity * 100)}% →
                        {t(
                          'settings.voice.calibrationRecommendation',
                          'Recommended sensitivity {value}%',
                          { value: Math.round(recommendation * 100) },
                        )}
                      </strong>
                    {:else if peak > 0}
                      <span>
                        {t('settings.voice.calibrationPeak', 'Peak')}
                        {peak.toFixed(2)}
                      </span>
                    {/if}
                  </div>
                  {#if !isCompleted && sampleCount > 0}
                    <div class="voice-calibration-model__retry">
                      <Button
                        variant="tertiary"
                        disabled={calibrationActionBusy ||
                          calibrationRetryModelId === model.id}
                        loading={calibrationRetryModelId === model.id}
                        onClick={() => handleRetryModelCalibration(model.id)}
                      >
                        {t(
                          'settings.voice.calibrationRetryModel',
                          'Retry this phrase',
                        )}
                      </Button>
                    </div>
                  {/if}
                </div>
              {/each}
            </div>

            <div class="voice-calibration-legend" aria-hidden="true">
              <span
                ><i class="voice-calibration-legend__peak"></i>
                {t('settings.voice.calibrationPeak', 'Session peak')}</span
              >
              <span
                ><i class="voice-calibration-legend__threshold"></i>
                {t('settings.voice.calibrationThreshold', 'Threshold')}</span
              >
            </div>

            <div class="voice-calibration-actions">
              <Button
                variant="tertiary"
                disabled={!calibrationActive || calibrationActionBusy}
                onClick={handleRestartCalibration}
              >
                {t('settings.voice.calibrationReset', 'Restart calibration')}
              </Button>
              <div class="voice-calibration-actions__decision">
                <Button
                  variant="secondary"
                  disabled={calibrationActionBusy}
                  onClick={() => (calibrationDiscardConfirm = true)}
                >
                  {t('settings.voice.calibrationDiscard', 'Discard and stop')}
                </Button>
                <Button
                  variant="primary"
                  loading={calibrationActionState === 'applying'}
                  disabled={calibrationActionBusy || !calibrationReady}
                  onClick={handleApplyCalibration}
                >
                  {t(
                    'settings.voice.calibrationApply',
                    'Apply calibrated values',
                  )}
                </Button>
              </div>
            </div>
          </div>
        {:else}
          <div class="voice-calibration-entry">
            <span>
              {voiceState.enabled
                ? t(
                    'settings.voice.calibrationReady',
                    'Calibration takes about 15 seconds. Wakeword commands are paused while it runs.',
                  )
                : t(
                    'settings.voice.calibrationEnableFirst',
                    'Enable wakeword listening before starting calibration.',
                  )}
            </span>
            <Button
              variant="secondary"
              disabled={!calibrationCanStart}
              loading={calibrationActionState === 'starting'}
              onClick={handleStartCalibration}
            >
              {t('settings.voice.calibrationStart', 'Start calibration')}
            </Button>
          </div>
        {/if}
      </div>
    </div>

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
          disabled={!loaded || calibrationSessionActive}
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
      <div class="s-row-control voice-microphone-control">
        <Dropdown
          value={selectedMicrophoneValue}
          options={microphoneOptions}
          ariaLabel={t('settings.voice.microphone', 'Microphone')}
          triggerClass="voice-microphone-dropdown"
          onValueChange={handleMicrophoneChange}
          disabled={!loaded ||
            microphones.length === 0 ||
            calibrationSessionActive}
        />
      </div>
    </div>

    <!-- Privacy note -->
    <div class="voice-privacy-note">
      {t(
        'settings.voice.privacyNote',
        'While listening is enabled, microphone audio is analyzed continuously on this device. Nothing is saved or sent before the wake phrase matches; the following command recording is sent to your configured vBot speech backend for transcription.',
      )}
      <p>
        {t(
          'settings.voice.cancelPhrases',
          'Say “abbrechen” or “vergiss es” at the end of the same recording to discard the entire command before it starts a Run.',
        )}
      </p>
    </div>

    <div class="voice-save-state" aria-live="polite">
      {#if saveState === 'saving'}
        {t('common.saving', 'Saving…')}
      {:else if saveState === 'saved' && !dirty}
        {t('common.saved', 'Saved')}
      {:else if saveState === 'error'}
        {t('common.saveFailed', 'Not saved')}
      {/if}
    </div>
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

{#if calibrationDiscardConfirm}
  <ConfirmDialog
    title={t(
      'settings.voice.calibrationDiscardConfirmTitle',
      'Discard calibration?',
    )}
    body={t(
      'settings.voice.calibrationDiscardConfirm',
      'All measurements will be discarded. You will need to start calibration again from the beginning.',
    )}
    confirmLabel={t('settings.voice.calibrationDiscard', 'Discard and stop')}
    onConfirm={handleDiscardCalibration}
    onCancel={() => (calibrationDiscardConfirm = false)}
  />
{/if}

<style>
  .voice-settings {
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  /* Live state dot */
  .voice-state {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }
  .voice-state-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--text-lo);
  }
  .voice-dot--off {
    background: var(--text-lo);
  }
  .voice-dot--listening {
    background: var(--green);
    animation: voice-pulse 1.6s ease-in-out infinite;
  }
  .voice-dot--detected {
    background: var(--green);
    animation: voice-pulse 0.6s ease-in-out infinite;
  }
  .voice-dot--recording {
    background: var(--amber);
  }
  .voice-dot--processing {
    background: var(--accent);
    animation: voice-spin 1s linear infinite;
  }
  .voice-dot--warning {
    background: var(--amber);
  }
  .voice-dot--error {
    background: var(--red);
  }
  .voice-state-label {
    font-size: var(--fs-body-md);
    color: var(--text-hi);
  }

  @keyframes voice-pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.35;
    }
  }
  @keyframes voice-spin {
    0% {
      opacity: 1;
    }
    25% {
      opacity: 0.5;
    }
    50% {
      opacity: 0.2;
    }
    75% {
      opacity: 0.5;
    }
    100% {
      opacity: 1;
    }
  }

  .voice-slider-labels {
    display: flex;
    justify-content: space-between;
    font-size: var(--fs-body-sm);
    color: var(--text-lo);
  }

  .voice-microphone-control,
  .voice-model-control,
  .voice-calibration-control,
  .voice-settings :global(.voice-microphone-dropdown.dropdown) {
    min-width: 300px;
  }

  .voice-model-control {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }
  .voice-model-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .voice-model-card {
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-2);
    transition:
      border-color 140ms ease,
      background 140ms ease;
  }
  .voice-model-card--active {
    border-color: color-mix(in srgb, var(--accent) 42%, var(--border));
    background: color-mix(in srgb, var(--accent) 6%, var(--surface-2));
  }
  .voice-model-card__header,
  .voice-model-card__identity,
  .voice-model-card__sensitivity,
  .voice-model-card__actions {
    display: flex;
    align-items: center;
  }
  .voice-model-card__header,
  .voice-model-card__sensitivity {
    justify-content: space-between;
    gap: 12px;
  }
  .voice-model-card__identity {
    min-width: 0;
    flex-wrap: wrap;
    gap: 7px;
  }
  .voice-model-card__name {
    color: var(--text-hi);
    font-size: var(--fs-body-md);
    font-weight: 600;
  }
  .voice-model-card__tuning {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding-top: 10px;
  }
  .voice-model-card__sensitivity,
  .voice-slider-labels,
  .voice-model-limit {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }
  .voice-model-card__tuning input[type='range'] {
    width: 100%;
    accent-color: var(--accent);
  }
  .voice-model-card__actions {
    justify-content: flex-end;
    padding-top: 8px;
  }
  .voice-model-actions {
    display: flex;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 8px;
  }
  .voice-model-limit {
    text-align: right;
  }
  .voice-model-file {
    display: none;
  }

  .voice-calibration-entry {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .voice-calibration-panel {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding: 16px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-lg);
    background: var(--surface-2);
  }
  .voice-calibration-header,
  .voice-calibration-model__header,
  .voice-calibration-actions,
  .voice-calibration-actions__decision,
  .voice-calibration-legend {
    display: flex;
    align-items: center;
  }
  .voice-calibration-header,
  .voice-calibration-model__header,
  .voice-calibration-actions {
    justify-content: space-between;
  }
  .voice-calibration-header {
    align-items: flex-start;
    gap: 12px;
  }
  .voice-calibration-heading {
    color: var(--text-hi);
    font-size: var(--fs-heading-sm);
    font-weight: 600;
  }
  .voice-calibration-instruction {
    max-width: 58ch;
    margin-top: 4px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.4;
  }
  .voice-calibration-steps {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .voice-calibration-steps li {
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
    padding: 7px 9px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .voice-calibration-steps li span {
    display: grid;
    flex: 0 0 18px;
    height: 18px;
    place-items: center;
    border: 1px solid currentColor;
    border-radius: 50%;
    font-size: 10px;
  }
  .voice-calibration-steps li[data-state='current'] {
    border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
    background: color-mix(in srgb, var(--accent) 7%, transparent);
    color: var(--accent);
  }
  .voice-calibration-steps li[data-state='complete'] {
    color: var(--green);
  }
  .voice-calibration-models {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .voice-calibration-model {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 10px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--bg);
  }
  .voice-calibration-model--target {
    border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  }
  .voice-calibration-model--complete {
    border-color: color-mix(in srgb, var(--green) 45%, var(--border));
  }
  .voice-calibration-model__header {
    gap: 12px;
    color: var(--text-hi);
    font-size: var(--fs-label-md);
    font-weight: 500;
  }
  .voice-calibration-model__identity,
  .voice-calibration-model__result {
    display: flex;
    align-items: center;
  }
  .voice-calibration-model__identity {
    min-width: 0;
    gap: 7px;
  }
  .voice-calibration-model__target {
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    font-weight: 500;
  }
  .voice-calibration-model__check {
    color: var(--green);
    font-size: var(--fs-body-md);
    font-weight: 600;
  }
  .voice-calibration-model__retry {
    display: flex;
    justify-content: flex-end;
    padding-top: 4px;
  }
  .voice-calibration-model-progress {
    margin-top: 4px;
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .voice-calibration-noise-warning {
    padding: 10px 12px;
    border: 1px solid rgba(245, 158, 11, 0.22);
    border-radius: var(--r-md);
    background: rgba(245, 158, 11, 0.12);
    font-size: var(--fs-body-sm);
    color: var(--text-hi);
    line-height: 1.4;
  }
  .voice-calibration-model__values {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    font-weight: 400;
    white-space: nowrap;
  }
  .voice-calibration-model__result {
    justify-content: space-between;
    gap: 12px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .voice-calibration-model__result strong {
    color: var(--green);
    font-weight: 500;
  }
  .voice-calibration-meter {
    position: relative;
    height: 12px;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    background:
      repeating-linear-gradient(
        to right,
        transparent 0 calc(25% - 1px),
        var(--border) calc(25% - 1px) 25%
      ),
      var(--bg);
  }
  .voice-calibration-meter__score {
    position: absolute;
    inset: 0 auto 0 0;
    width: var(--score);
    background: var(--accent);
    transition: width 120ms linear;
  }
  .voice-calibration-meter__score--match {
    background: var(--green);
  }
  .voice-calibration-meter__peak,
  .voice-calibration-meter__threshold {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
  }
  .voice-calibration-meter__peak {
    left: calc(var(--peak) - 1px);
    background: var(--green);
  }
  .voice-calibration-meter__threshold {
    left: calc(var(--threshold) - 1px);
    background: var(--text-hi);
  }
  .voice-calibration-legend {
    justify-content: flex-end;
    gap: 14px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }
  .voice-calibration-legend span {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  .voice-calibration-legend i {
    display: inline-block;
    width: 2px;
    height: 10px;
  }
  .voice-calibration-legend__peak {
    background: var(--green);
  }
  .voice-calibration-legend__threshold {
    background: var(--text-hi);
  }
  .voice-calibration-actions,
  .voice-calibration-actions__decision {
    gap: 8px;
  }

  /* Privacy note */
  .voice-privacy-note {
    margin-top: 16px;
    padding: 12px 16px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-2);
    font-size: var(--fs-body-sm);
    color: var(--text-med);
    line-height: 1.5;
  }
  .voice-privacy-note p {
    margin: 8px 0 0;
  }

  :global(.voice-attention-banner) {
    margin: 8px 0 12px;
  }
  .voice-attention-copy {
    color: var(--text-hi);
  }
  .voice-attention-copy p {
    margin: 4px 0 0;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.45;
  }
  :global(.voice-retry) {
    white-space: nowrap;
  }

  .voice-save-state {
    min-height: 20px;
    padding: 10px 0 0;
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
    text-align: right;
  }

  /* Mock-mode warning */
  .voice-mock-warning {
    margin: 8px 0 12px;
    padding: 12px 16px;
    border: 1px solid rgba(245, 158, 11, 0.22);
    border-radius: var(--r-md);
    background: rgba(245, 158, 11, 0.12);
    font-size: var(--fs-body-sm);
    color: var(--text-hi);
    line-height: 1.5;
  }

  @media (prefers-reduced-motion: reduce) {
    .voice-dot--listening,
    .voice-dot--detected,
    .voice-dot--processing {
      animation: none;
    }
    .voice-calibration-meter__score {
      transition: none;
    }
  }

  @media (max-width: 640px) {
    .voice-calibration-entry,
    .voice-calibration-header,
    .voice-calibration-model__header,
    .voice-calibration-actions {
      align-items: stretch;
      flex-direction: column;
    }
    .voice-calibration-steps {
      grid-template-columns: 1fr;
    }
    .voice-calibration-actions__decision {
      justify-content: flex-end;
      flex-wrap: wrap;
    }
    .voice-calibration-model__values {
      white-space: normal;
    }
  }
</style>
