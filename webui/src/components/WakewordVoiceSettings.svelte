<script>
  import {
    bridgeErrorMessage,
    errorMessage,
    voiceIndicator,
  } from './voice/voiceLabels.js';
  import './voice/voice.css';
  import { t } from '$lib/i18n.js';
  import Toggle from './ui/Toggle.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import Dropdown from './Dropdown.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import { onDestroy, untrack } from 'svelte';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import {
    deleteWakewordModel,
    importWakewordModel,
    isDesktopAccessor,
    listMicrophones,
    listWakewordModels,
    retryVoice,
    setVoiceEnabled,
    startVoiceCalibration,
    stopVoiceCalibration,
    updateVoiceConfig,
  } from '$lib/desktopBridge.js';
  import {
    buildVoiceConfigChanges,
    cloneVoiceConfig,
    overlappingPhraseConflicts,
    rebaseVoiceConfig,
    voiceConfigFromStatus,
  } from '$lib/wakewordSettings.js';
  import TranscriptionAudioSettings from './voice/TranscriptionAudioSettings.svelte';
  import VoicePhraseCard from './voice/VoicePhraseCard.svelte';
  import WakewordCalibration from './voice/WakewordCalibration.svelte';

  const MAX_CUSTOM_WAKEWORD_MODEL_BYTES = 20 * 1024 * 1024;
  const VOICE_LIST_RETRY_MS = 3000;
  const UNAVAILABLE_MICROPHONE_VALUE = '__configured_unavailable__';
  const SESSION_BEHAVIOR_OPTIONS = Object.freeze([
    {
      value: 'active',
      label: t('settings.voice.sessionBehaviorActive', 'Use active Session'),
    },
    {
      value: 'new',
      label: t('settings.voice.sessionBehaviorNew', 'New Session each time'),
    },
  ]);

  let {
    agents = [],
    settings = null,
    // The Desktop advertises wakeword support (any Voice bridge version).
    wakewordAvailable = false,
    // The app-level Desktop Voice owner: `{available, status, adopt,
    // refresh}`; null outside the Desktop app.
    desktopVoice = null,
    onCommit = () => {},
    onToast = () => {},
    onError = () => {},
  } = $props();

  // The Desktop status is authoritative. `baseline` is the configuration of
  // the last applied snapshot, `draft` the edited one; `appliedSequence`
  // keeps an older snapshot from replacing a newer one.
  let draft = $state(null);
  let baseline = $state(null);
  let appliedSequence = -1;
  let microphones = $state([]);
  let wakewordModels = $state([]);
  let listsLoaded = $state(false);
  let listsError = $state(false);
  let listsLoading = false;
  let listsRetryTimer = null;
  let destroyed = false;
  let saveState = $state('idle');
  let transcriptionSaveStatus = $state('idle');
  let modelFileInput = $state();
  let modelActionState = $state('idle');
  let enablePending = $state(null);
  let enableError = $state(null);
  let calibrationStarting = $state(null);
  let deleteConfirmModel = $state(null);

  let voiceReady = $derived(
    isDesktopAccessor() && desktopVoice?.available === true,
  );
  let updateRequired = $derived(
    !voiceReady && isDesktopAccessor() && wakewordAvailable,
  );
  let status = $derived(voiceReady ? desktopVoice.status : null);
  let loaded = $derived(status !== null && draft !== null && listsLoaded);
  let limits = $derived(status?.limits ?? null);
  let maxActivePhrases = $derived(limits?.max_active_phrases ?? null);
  let calibration = $derived(status?.calibration ?? null);
  let calibrating = $derived(calibration !== null);
  let modelActionBusy = $derived(modelActionState !== 'idle');
  let enableBusy = $derived(enablePending !== null);
  let enabled = $derived(enablePending ?? status?.enabled ?? false);
  let indicator = $derived(voiceIndicator(status));
  let savedActiveIds = $derived(
    new Set((status?.phrases ?? []).map((phrase) => phrase.model_id)),
  );
  let phraseProblems = $derived(
    new Map(
      (status?.phrases ?? []).map((phrase) => [
        phrase.model_id,
        phrase.problem,
      ]),
    ),
  );
  let modelLabels = $derived(
    new Map(wakewordModels.map((model) => [model.id, model.label])),
  );
  let conflicts = $derived(
    draft ? overlappingPhraseConflicts(draft, wakewordModels) : new Map(),
  );
  let agentOptions = $derived(
    agents.map((agent) => ({
      value: agent.id,
      label: agent.name || agent.id,
    })),
  );
  let defaultAgentOptions = $derived([
    { value: '', label: t('settings.voice.noDefaultAgent', 'None') },
    ...agentOptions,
    ...(draft?.default_agent_id &&
    !agentOptions.some((option) => option.value === draft.default_agent_id)
      ? [
          {
            value: draft.default_agent_id,
            label: draft.default_agent_id,
            secondaryLabel: t(
              'settings.voice.agentUnavailable',
              'Not on this server',
            ),
            disabled: true,
          },
        ]
      : []),
  ]);
  let configuredMicrophoneDevice = $derived(
    draft?.microphone
      ? microphones.find((device) =>
          sameMicrophoneIdentity(device, draft.microphone),
        ) || null
      : null,
  );
  let microphoneOptions = $derived([
    {
      value: '',
      label: t('settings.voice.systemAutomaticMic', 'Automatic selection'),
      secondaryLabel: status?.active_microphone?.name || '',
    },
    ...(draft?.microphone && !configuredMicrophoneDevice
      ? [
          {
            value: UNAVAILABLE_MICROPHONE_VALUE,
            label: draft.microphone.name,
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
      : draft?.microphone
        ? UNAVAILABLE_MICROPHONE_VALUE
        : '',
  );
  let attention = $derived.by(() => {
    if (!status) return null;
    if (status.mode === 'unavailable')
      return { warn: false, code: 'voice_stack_unavailable', retry: false };
    if (status.state === 'error')
      return { warn: false, code: status.error_code, retry: true };
    if (status.state === 'microphone_disconnected')
      return {
        warn: true,
        code: status.error_code ?? 'microphone_unavailable',
        retry: true,
      };
    // A refused enable stays visible until the next toggle or until Voice
    // is enabled.
    if (enableError && !status.enabled)
      return { warn: false, code: enableError, retry: false };
    return null;
  });
  let echo = $derived.by(() => {
    if (!draft || !status) return null;
    if (!draft.echo_cancellation)
      return {
        variant: 'neutral',
        label: t('settings.voice.echoOff', 'Off'),
        detail: t(
          'settings.voice.echoOffDetail',
          'Speaker output can trigger wake phrases and end up in command recordings.',
        ),
      };
    // The saved setting is on; the state describes the running capture.
    if (!status.echo_cancellation.enabled) return null;
    switch (status.echo_cancellation.state) {
      case 'starting':
        return {
          variant: 'neutral',
          label: t('settings.voice.echoStarting', 'Starting'),
          detail: t(
            'settings.voice.echoStartingDetail',
            'Echo cancellation is still loading. Until it is ready, the microphone signal is used unprocessed.',
          ),
        };
      case 'active':
        return {
          variant: 'success',
          label: t('settings.voice.echoActive', 'Active'),
          detail: t(
            'settings.voice.echoActiveDetail',
            'Speaker output is removed from the microphone signal before phrases are detected and commands are recorded.',
          ),
        };
      case 'no_reference':
        return {
          variant: 'warn',
          label: t('settings.voice.echoNoReference', 'No speaker signal'),
          detail: t(
            'settings.voice.echoNoReferenceDetail',
            'The Desktop cannot capture the speaker output, so the microphone signal is used unprocessed.',
          ),
        };
      case 'unavailable':
        return {
          variant: 'warn',
          label: t('settings.voice.echoUnavailable', 'Unavailable'),
          detail: t(
            'settings.voice.echoUnavailableDetail',
            'Echo cancellation is not installed in this Desktop app, so the microphone signal is used unprocessed.',
          ),
        };
      default:
        return null;
    }
  });

  let dirty = $derived(
    draft !== null &&
      baseline !== null &&
      Object.keys(buildVoiceConfigChanges(draft, baseline)).length > 0,
  );
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
  // Changes that restart listening wait while a calibration or a model
  // action runs.
  let captureLocked = $derived(
    !loaded || calibrating || modelActionBusy || enableBusy,
  );
  let routingLocked = $derived(!loaded || modelActionBusy);

  const autosaveContext = useAutosaveContext();
  const voiceAutosave = createAutosaveParticipant({
    getSnapshot: () => (draft ? cloneVoiceConfig(draft) : null),
    hasChanges: () => voiceReady && dirty,
    save: persistDraft,
  });
  const unregisterVoiceAutosave = autosaveContext.register(voiceAutosave);

  function sameMicrophoneIdentity(left, right) {
    return left?.name === right?.name && left?.host_api === right?.host_api;
  }

  // Apply a status snapshot to the draft: unedited values follow it, edits
  // stay. Idempotent per sequence.
  function syncFromStatus(snapshot) {
    if (!snapshot || (baseline && snapshot.sequence <= appliedSequence)) return;
    const next = voiceConfigFromStatus(snapshot);
    draft =
      draft && baseline
        ? rebaseVoiceConfig(draft, baseline, next)
        : cloneVoiceConfig(next);
    baseline = next;
    appliedSequence = snapshot.sequence;
  }

  // A snapshot a bridge call returned: hand it to the app-level owner, which
  // keeps the newest one, and apply whatever is current.
  function adoptStatus(snapshot) {
    desktopVoice?.adopt(snapshot);
    syncFromStatus(desktopVoice?.status ?? snapshot);
  }

  $effect(() => {
    const snapshot = status;
    untrack(() => syncFromStatus(snapshot));
  });

  $effect(() => {
    if (!voiceReady) return;
    untrack(() => {
      if (!listsLoaded) void loadLists();
    });
  });

  onDestroy(() => {
    destroyed = true;
    unregisterVoiceAutosave();
    if (listsRetryTimer !== null) {
      clearTimeout(listsRetryTimer);
      listsRetryTimer = null;
    }
    // Commands stay paused while a calibration runs; leaving the panel ends it.
    if (untrack(() => calibrating)) void stopVoiceCalibration().catch(() => {});
  });

  async function loadLists() {
    if (destroyed || listsLoading) return;
    listsLoading = true;
    try {
      const [availableMicrophones, availableModels] = await Promise.all([
        listMicrophones(),
        listWakewordModels(),
      ]);
      if (destroyed) return;
      microphones = availableMicrophones;
      wakewordModels = availableModels;
      listsLoaded = true;
      listsError = false;
    } catch {
      if (destroyed) return;
      listsError = true;
      if (listsRetryTimer === null) {
        listsRetryTimer = setTimeout(() => {
          listsRetryTimer = null;
          void loadLists();
        }, VOICE_LIST_RETRY_MS);
      }
    } finally {
      listsLoading = false;
    }
  }

  function errorToast(error, title = null) {
    onToast({
      title: title ?? t('errors.generic', 'Something went wrong. Try again.'),
      message: bridgeErrorMessage(error),
      variant: 'error',
    });
  }

  async function handleEnabledChange() {
    if (enableBusy || !status) return;
    const next = !enabled;
    enablePending = next;
    enableError = null;
    try {
      const result = await setVoiceEnabled(next);
      enableError = result.error_code;
      await desktopVoice?.refresh();
    } catch (error) {
      errorToast(error);
    } finally {
      enablePending = null;
    }
  }

  async function persistDraft() {
    if (!draft || !baseline) return true;
    const changes = buildVoiceConfigChanges(draft, baseline);
    if (Object.keys(changes).length === 0) return true;
    const submitted = cloneVoiceConfig(draft);
    saveState = 'saving';
    try {
      const snapshot = await updateVoiceConfig(changes);
      if (destroyed) return true;
      desktopVoice?.adopt(snapshot);
      const current = desktopVoice?.status;
      const latest =
        current && current.sequence >= snapshot.sequence ? current : snapshot;
      // Everything submitted is saved as the Desktop reports it; edits made
      // while the save ran stay in the draft.
      const saved = voiceConfigFromStatus(latest);
      draft = rebaseVoiceConfig(draft, submitted, saved);
      baseline = saved;
      appliedSequence = latest.sequence;
      saveState = 'saved';
      return true;
    } catch (error) {
      if (!destroyed) {
        saveState = 'error';
        errorToast(error);
      }
      return false;
    }
  }

  function saveConfig() {
    return voiceAutosave.runSave('manual', { force: true });
  }

  function editDraft(changes) {
    draft = { ...draft, ...changes };
    void saveConfig();
  }

  function handlePhraseToggle(model, checked) {
    const ids = draft.active_model_ids;
    const isActive = ids.includes(model.id);
    if (checked === isActive) return;
    if (
      checked &&
      (maxActivePhrases === null || ids.length >= maxActivePhrases)
    )
      return;
    if (!checked && ids.length <= 1) return;
    editDraft({
      active_model_ids: checked
        ? [...ids, model.id]
        : ids.filter((modelId) => modelId !== model.id),
    });
  }

  function handleSensitivityInput(modelId, value) {
    draft = {
      ...draft,
      model_sensitivities: { ...draft.model_sensitivities, [modelId]: value },
    };
  }

  function handlePhraseActionChange(modelId, action) {
    editDraft({
      phrase_actions: { ...draft.phrase_actions, [modelId]: action },
    });
  }

  function handleMicrophoneChange(value) {
    const parsed = Number.parseInt(value, 10);
    const device = Number.isInteger(parsed)
      ? microphones.find((candidate) => candidate.index === parsed)
      : null;
    editDraft({
      microphone: device
        ? {
            index: device.index,
            name: device.name,
            host_api: device.host_api || '',
          }
        : null,
    });
  }

  async function handleCalibrate(modelId) {
    if (calibrationStarting) return;
    calibrationStarting = modelId;
    try {
      if (!(await voiceAutosave.flush())) return;
      adoptStatus(await startVoiceCalibration(modelId));
    } catch (error) {
      errorToast(
        error,
        t(
          'settings.voice.calibrationStartFailed',
          'Calibration could not start.',
        ),
      );
    } finally {
      calibrationStarting = null;
    }
  }

  // Calibration stops first: a new sensitivity may restart listening.
  async function applyCalibration(modelId, value) {
    adoptStatus(await stopVoiceCalibration());
    draft = {
      ...draft,
      model_sensitivities: { ...draft.model_sensitivities, [modelId]: value },
    };
    return saveConfig();
  }

  function calibrateDisabled(modelId) {
    if (!savedActiveIds.has(modelId)) return null;
    return (
      !loaded ||
      calibrating ||
      calibrationStarting !== null ||
      modelActionBusy ||
      enableBusy ||
      status.mode !== 'real' ||
      !status.enabled ||
      status.state !== 'listening'
    );
  }

  async function refreshAfterModelChange() {
    wakewordModels = await listWakewordModels();
    syncFromStatus(await desktopVoice?.refresh());
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
      await refreshAfterModelChange();
      onToast({
        title: imported?.activated
          ? t(
              'settings.voice.importSuccessActive',
              'Wakeword model imported and activated.',
            )
          : t(
              'settings.voice.importSuccessInactive',
              'Wakeword model imported. Activate it to listen for it.',
            ),
        variant: 'success',
      });
    } catch (error) {
      errorToast(error);
    } finally {
      modelActionState = 'idle';
    }
  }

  async function confirmDeleteWakewordModel() {
    const model = deleteConfirmModel;
    deleteConfirmModel = null;
    if (!model?.removable) return;

    modelActionState = 'deleting';
    try {
      if (!(await voiceAutosave.flush())) return;
      await deleteWakewordModel(model.id);
      await refreshAfterModelChange();
      onToast({
        title: t('settings.voice.deleteSuccess', 'Wakeword model removed.'),
        variant: 'success',
      });
    } catch (error) {
      errorToast(error);
      await refreshAfterModelChange().catch(() => {});
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

  async function handleRetry() {
    try {
      await retryVoice();
      microphones = await listMicrophones();
      await desktopVoice?.refresh();
    } catch (error) {
      errorToast(
        error,
        t('settings.voice.retryFailed', 'Voice could not restart.'),
      );
    }
  }
</script>

<div class="voice-settings">
  <TranscriptionAudioSettings
    {settings}
    {onCommit}
    {onError}
    onSaveStatusChange={(next) => (transcriptionSaveStatus = next)}
  />

  {#if !voiceReady}
    <div class="s-group">
      <div class="s-row">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.enabled', 'Wakeword listening')}
          </div>
          <div class="s-row-desc">
            {updateRequired
              ? t(
                  'settings.voice.desktopUpdateRequired',
                  'Update the vBot Desktop app to use Voice with this server.',
                )
              : t(
                  'settings.voice.desktopOnly',
                  'Wakeword listening is configured in the vBot Desktop app. The transcription audio settings above are server-wide.',
                )}
          </div>
        </div>
      </div>
    </div>
  {:else}
    {#if status === null || listsError}
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

    <!-- Wakeword listening: the phrases, what each one does, and the
         microphone they are heard through. -->
    <div class="s-group">
      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.enabled', 'Wakeword listening')}
          </div>
          <div class="s-row-desc">
            {t(
              'settings.voice.enabledDescription',
              'Listen on this device for wake phrases that send a spoken command to an Agent or start Live voice.',
            )}
          </div>
        </div>
        <div class="s-row-control">
          <Toggle
            checked={enabled}
            onChange={handleEnabledChange}
            disabled={!status ||
              enableBusy ||
              modelActionBusy ||
              calibrating ||
              (!status.enabled && status.mode === 'unavailable')}
            ariaLabel={t(
              'settings.voice.enabledAria',
              'Enable wakeword listening',
            )}
          />
        </div>
      </div>

      {#if status?.mode === 'mock'}
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
            <span
              class="voice-state-dot voice-dot--{indicator.tone}"
              aria-hidden="true"
            ></span>
            <span class="voice-state-label">{indicator.label}</span>
          </span>
        </div>
      </div>

      {#if attention}
        <div class="s-group__block s-group__block--attached">
          <Banner
            variant={attention.warn ? 'warn' : 'error'}
            class="voice-attention-banner"
            role={attention.warn ? 'status' : 'alert'}
          >
            <div class="voice-attention-copy">
              <strong>
                {attention.warn
                  ? t(
                      'settings.voice.microphoneDisconnectedTitle',
                      'Microphone disconnected',
                    )
                  : t('settings.voice.errorTitle', 'Voice needs attention')}
              </strong>
              <p>{errorMessage(attention.code)}</p>
            </div>
            {#if attention.retry}
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

      <!-- The phrase catalog: activation, sensitivity, action, calibration,
           and imported model management. -->
      <div class="s-row s-row--stacked">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.models', 'Wake phrases')}
          </div>
          <div class="s-row-desc">
            {t(
              'settings.voice.modelDescription',
              'Choose the phrases to listen for. Each active phrase has its own sensitivity and action; Calibrate measures the room and your voice to set its sensitivity while listening is on.',
            )}
          </div>
        </div>
        <div class="s-row-control voice-model-control">
          <div class="voice-model-list">
            {#each wakewordModels as model (model.id)}
              {@const active =
                draft?.active_model_ids.includes(model.id) ?? false}
              {@const phraseCalibration =
                calibration?.model_id === model.id ? calibration : null}
              <VoicePhraseCard
                {model}
                {active}
                sensitivity={draft?.model_sensitivities[model.id] ?? null}
                {limits}
                action={draft?.phrase_actions[model.id] ?? null}
                {agentOptions}
                problem={savedActiveIds.has(model.id)
                  ? (phraseProblems.get(model.id) ?? null)
                  : null}
                conflicts={(conflicts.get(model.id) ?? []).map(
                  (modelId) => modelLabels.get(modelId) ?? modelId,
                )}
                toggleDisabled={captureLocked ||
                  (active && draft.active_model_ids.length <= 1) ||
                  (!active &&
                    (maxActivePhrases === null ||
                      draft.active_model_ids.length >= maxActivePhrases))}
                sensitivityDisabled={captureLocked}
                routingDisabled={routingLocked}
                calibrateDisabled={calibrateDisabled(model.id)}
                removeDisabled={captureLocked}
                onToggle={(checked) => handlePhraseToggle(model, checked)}
                onSensitivityInput={(value) =>
                  handleSensitivityInput(model.id, value)}
                onSensitivityCommit={() => void saveConfig()}
                onActionChange={(action) =>
                  handlePhraseActionChange(model.id, action)}
                onCalibrate={() => handleCalibrate(model.id)}
                onRemove={() => (deleteConfirmModel = model)}
                calibration={phraseCalibration ? calibrationPanel : null}
              />
              {#snippet calibrationPanel()}
                <WakewordCalibration
                  calibration={phraseCalibration}
                  label={model.label}
                  currentSensitivity={baseline?.model_sensitivities[model.id] ??
                    null}
                  onStatus={adoptStatus}
                  onApply={(value) => applyCalibration(model.id, value)}
                  {onToast}
                />
              {/snippet}
            {/each}
          </div>
          <div class="voice-model-actions">
            <span class="voice-model-limit">
              {#if maxActivePhrases !== null}
                {t(
                  'settings.voice.phraseLimit',
                  '{count} of {max} phrases active',
                  {
                    count: draft?.active_model_ids.length ?? 0,
                    max: maxActivePhrases,
                  },
                )}
              {/if}
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
              disabled={captureLocked}
              onClick={chooseWakewordModelFile}
            >
              {t('settings.voice.importModel', 'Import TFLite model')}
            </Button>
          </div>
        </div>
      </div>

      <!-- Where commands go when a phrase names no Agent of its own. -->
      <div class="s-row">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.defaultAgent', 'Default Agent')}
          </div>
          <div class="s-row-desc">
            {t(
              'settings.voice.defaultAgentDescription',
              'Receives the spoken commands of phrases without their own Agent. Applies to the server this Desktop app is connected to.',
            )}
          </div>
        </div>
        <div class="s-row-control">
          <Dropdown
            value={draft?.default_agent_id ?? ''}
            options={defaultAgentOptions}
            ariaLabel={t('settings.voice.defaultAgent', 'Default Agent')}
            onValueChange={(value) =>
              editDraft({ default_agent_id: value || null })}
            disabled={routingLocked}
          />
        </div>
      </div>

      <div class="s-row">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.defaultSession', 'Default Session behavior')}
          </div>
          <div class="s-row-desc">
            {t(
              'settings.voice.defaultSessionDescription',
              'Whether commands continue the Agent’s active Session or start a new one, unless a phrase chooses otherwise.',
            )}
          </div>
        </div>
        <div class="s-row-control">
          <Dropdown
            value={draft?.default_session_behavior ?? 'active'}
            options={SESSION_BEHAVIOR_OPTIONS}
            ariaLabel={t(
              'settings.voice.defaultSession',
              'Default Session behavior',
            )}
            onValueChange={(value) =>
              editDraft({ default_session_behavior: value })}
            disabled={routingLocked}
          />
        </div>
      </div>

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
            disabled={captureLocked || microphones.length === 0}
          />
        </div>
      </div>

      <div class="s-row">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.voice.echoCancellation', 'Echo cancellation')}
          </div>
          <div class="s-row-desc">
            {echo?.detail ??
              t(
                'settings.voice.echoCancellationDescription',
                'Removes speaker output such as Live voice or read-aloud replies from the microphone signal.',
              )}
          </div>
        </div>
        <div class="s-row-control voice-echo-control">
          {#if echo}
            <StatusChip variant={echo.variant}>{echo.label}</StatusChip>
          {/if}
          <Toggle
            checked={draft?.echo_cancellation ?? true}
            onChange={(checked) => editDraft({ echo_cancellation: checked })}
            disabled={captureLocked}
            ariaLabel={t(
              'settings.voice.echoCancellationAria',
              'Use echo cancellation',
            )}
          />
        </div>
      </div>

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
     Desktop Voice configuration both save as they change. -->
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
