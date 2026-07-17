<script>
  import { onDestroy } from 'svelte';
  import Dropdown from './Dropdown.svelte';
  import Button from './ui/Button.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import Toggle from './ui/Toggle.svelte';
  import { t } from '$lib/i18n.js';
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

  let { agents = [], onToast = () => {} } = $props();

  let voiceState = $state(createVoiceSettingsState());
  let lastSaved = $state(null);
  let loaded = $state(false);
  let cleanupStatusPoll = null;
  let microphones = $state([]);
  let wakewordModels = $state([]);
  let saveState = $state('idle');
  let saveChain = Promise.resolve();
  let modelFileInput = $state();
  let modelActionState = $state('idle');
  let deleteConfirmModel = $state(null);

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
  let wakewordModelOptions = $derived(
    wakewordModels.map((model) => ({
      value: model.id,
      label: model.label,
      secondaryLabel:
        model.source === 'built_in'
          ? t('settings.voice.modelBuiltIn', 'Built-in')
          : t('settings.voice.modelImported', 'Imported ONNX'),
    })),
  );
  let selectedWakewordModel = $derived(
    wakewordModels.find((model) => model.id === voiceState.model_id) ?? null,
  );
  let modelActionBusy = $derived(modelActionState !== 'idle');

  let liveStateLabel = $derived(liveStateText(voiceState.liveState));
  let liveStateDotClass = $derived(liveStateDotColor(voiceState.liveState));

  let dirty = $derived(voiceSettingsDirty(voiceState, lastSaved));
  let sensitivityPercent = $derived(Math.round(voiceState.sensitivity * 100));
  let enableToggleDisabled = $derived(
    !loaded ||
      (!voiceState.enabled &&
        (!voiceState.target_agent_id || voiceState.mode === 'unavailable')),
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
        'The wakeword model is not a compatible openWakeWord ONNX model.',
      ),
      microphone_unavailable: t(
        'settings.voice.error.microphone',
        'The selected microphone cannot provide compatible audio. Choose another input device or check microphone permissions.',
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
        return 'voice-dot--warning';
      case 'error':
        return 'voice-dot--error';
      default:
        return 'voice-dot--off';
    }
  }

  onDestroy(() => {
    if (cleanupStatusPoll) {
      cleanupStatusPoll();
      cleanupStatusPoll = null;
    }
  });

  async function loadStatus() {
    try {
      const [status, availableMicrophones, availableModels] = await Promise.all(
        [getWakewordStatus(), listMicrophones(), listWakewordModels()],
      );
      voiceState = applyWakewordStatus(voiceState, status);
      microphones = availableMicrophones;
      wakewordModels = availableModels;
      lastSaved = snapshotVoiceSettings(voiceState);
    } catch {
      // Bridge not available; keep defaults
    }
    loaded = true;
    // The poll only carries observed runtime fields (live state, mock flag) into
    // state — never the editable config — so a poll firing during the autosave
    // debounce cannot revert an unsaved edit.
    cleanupStatusPoll = onWakewordStatusChange((status) => {
      voiceState = applyRuntimeStatus(voiceState, status);
    });
  }

  async function handleEnabledChange() {
    const enabled = !voiceState.enabled;
    voiceState = { ...voiceState, enabled };
    try {
      await setWakewordEnabled(enabled);
      voiceState = applyWakewordStatus(voiceState, {
        enabled,
        state: enabled ? 'starting' : 'off',
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

  async function persistCurrentConfig() {
    const payload = buildVoiceSettingsPayload(voiceState, lastSaved);
    if (Object.keys(payload).length === 0) return;
    const savedSnapshot = snapshotVoiceSettings(voiceState);
    saveState = 'saving';
    try {
      await setWakewordConfig(payload);
      lastSaved = savedSnapshot;
      saveState = 'saved';
    } catch (error) {
      saveState = 'error';
      onToast({
        title: t('errors.generic', 'Something went wrong. Try again.'),
        message: error?.message || '',
        variant: 'error',
      });
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
    voiceState = {
      ...voiceState,
      microphone: Number.isInteger(parsed) ? parsed : null,
    };
    void saveConfig();
  }

  async function handleWakewordModelChange(value) {
    if (!value || value === voiceState.model_id) return;
    voiceState = { ...voiceState, model_id: value };
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
      await setWakewordConfig({ model_id: imported.id });
      wakewordModels = await listWakewordModels();
      await refreshEditableStatus();
      onToast({
        title: t(
          'settings.voice.importSuccess',
          'Wakeword model imported and selected.',
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
      await setWakewordConfig({ model_id: 'builtin/hey_jarvis' });
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

  function handleSensitivityInput(event) {
    const value = parseFloat(event.target.value);
    if (Number.isFinite(value)) {
      voiceState = { ...voiceState, sensitivity: value };
    }
  }

  async function handleRetry() {
    try {
      voiceState = { ...voiceState, liveState: 'starting', errorCode: null };
      await retryWakeword();
    } catch (error) {
      onToast({
        title: t('settings.voice.retryFailed', 'Voice could not restart.'),
        message: error?.message || '',
        variant: 'error',
      });
    }
  }

  // Load status on component init
  loadStatus();

  let desktopMode = $derived(isDesktop());
</script>

<div class="voice-settings">
  {#if !desktopMode}
    <div class="s-row">
      <div class="s-row-info" style="max-width: 100%">
        <div class="s-row-label">
          {t('settings.voice.title', 'Voice')}
        </div>
        <div class="s-row-desc">
          {t(
            'settings.voice.desktopOnly',
            'Voice settings are only available in the vBot Desktop app. Open the Desktop app to configure wakeword detection and voice commands.',
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

    {#if voiceState.liveState === 'error' || voiceState.mode === 'unavailable'}
      <div class="voice-error" role="alert">
        <div>
          <strong
            >{t('settings.voice.errorTitle', 'Voice needs attention')}</strong
          >
          <p>
            {errorMessage(
              voiceState.mode === 'unavailable'
                ? 'voice_stack_unavailable'
                : voiceState.errorCode,
            )}
          </p>
        </div>
        {#if voiceState.errorCode !== 'missing_target_agent' && voiceState.errorCode !== 'target_agent_unavailable' && voiceState.mode !== 'unavailable'}
          <Button variant="secondary" class="voice-retry" onClick={handleRetry}>
            {t('settings.voice.retry', 'Retry listening')}
          </Button>
        {/if}
      </div>
    {/if}

    <!-- Active wakeword model and local model management -->
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.voice.model', 'Wakeword model')}
        </div>
        <div class="s-row-desc">
          {t(
            'settings.voice.modelDescription',
            'Exactly one model listens at a time. Import finished custom ONNX models trained outside vBot.',
          )}
        </div>
      </div>
      <div class="s-row-control voice-model-control">
        <Dropdown
          value={voiceState.model_id}
          options={wakewordModelOptions}
          ariaLabel={t('settings.voice.model', 'Wakeword model')}
          triggerClass="voice-model-dropdown"
          onValueChange={handleWakewordModelChange}
          disabled={!loaded || modelActionBusy || wakewordModels.length === 0}
        />
        <div class="voice-model-actions">
          <input
            bind:this={modelFileInput}
            class="voice-model-file"
            type="file"
            accept=".onnx,application/octet-stream"
            onchange={handleWakewordModelFile}
          />
          <Button
            variant="secondary"
            loading={modelActionState === 'importing'}
            disabled={!loaded || modelActionBusy}
            onClick={chooseWakewordModelFile}
          >
            {t('settings.voice.importModel', 'Import ONNX model')}
          </Button>
          {#if selectedWakewordModel?.removable}
            <Button
              variant="tertiary"
              disabled={!loaded || modelActionBusy}
              onClick={() => (deleteConfirmModel = selectedWakewordModel)}
            >
              {t('settings.voice.removeModel', 'Remove imported model')}
            </Button>
          {/if}
        </div>
      </div>
    </div>

    <!-- Sensitivity is stored independently for each model -->
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.voice.sensitivity', 'Sensitivity')}
        </div>
        <div class="s-row-desc">
          {sensitivityPercent}%
        </div>
      </div>
      <div class="s-row-control">
        <div class="voice-slider">
          <label class="voice-slider-label" for="voice-sensitivity">
            {t('settings.voice.sensitivity', 'Sensitivity')}
          </label>
          <input
            id="voice-sensitivity"
            type="range"
            min="0.05"
            max="0.95"
            step="0.05"
            value={voiceState.sensitivity}
            oninput={handleSensitivityInput}
            onchange={() => void saveConfig()}
            disabled={!loaded}
          />
          <div class="voice-slider-labels">
            <span>{t('settings.voice.lessSensitive', 'Less sensitive')}</span>
            <span>{t('settings.voice.moreSensitive', 'More sensitive')}</span>
          </div>
        </div>
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
          disabled={!loaded || agentOptions.length === 0}
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
          disabled={!loaded}
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
          disabled={!loaded || microphones.length === 0}
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
      'Remove “{name}” permanently from this Desktop? The ONNX file stored by vBot will be deleted.',
      { name: deleteConfirmModel.label },
    )}
    confirmLabel={t('common.delete', 'Delete')}
    onConfirm={confirmDeleteWakewordModel}
    onCancel={() => (deleteConfirmModel = null)}
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

  /* Sensitivity slider */
  .voice-slider {
    display: flex;
    flex-direction: column;
    gap: 6px;
    width: 100%;
  }
  .voice-slider-label {
    font-size: var(--fs-body-sm);
    color: var(--text-lo);
  }
  .voice-slider input[type='range'] {
    width: 100%;
    accent-color: var(--accent);
  }
  .voice-slider-labels {
    display: flex;
    justify-content: space-between;
    font-size: var(--fs-body-sm);
    color: var(--text-lo);
  }

  .voice-microphone-control,
  .voice-model-control,
  .voice-settings :global(.voice-model-dropdown.dropdown),
  .voice-settings :global(.voice-microphone-dropdown.dropdown) {
    min-width: 300px;
  }

  .voice-model-control {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }
  .voice-model-actions {
    display: flex;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 8px;
  }
  .voice-model-file {
    display: none;
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

  .voice-error {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    margin: 8px 0 12px;
    padding: 12px 16px;
    border: 1px solid color-mix(in srgb, var(--red) 35%, var(--border));
    border-radius: var(--r-md);
    background: color-mix(in srgb, var(--red) 9%, var(--surface-2));
    color: var(--text-hi);
  }
  .voice-error p {
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
  }
</style>
