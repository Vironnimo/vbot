<script>
  import { onDestroy } from 'svelte';
  import Dropdown from './Dropdown.svelte';
  import Button from './ui/Button.svelte';
  import TextField from './ui/TextField.svelte';
  import Toggle from './ui/Toggle.svelte';
  import { t } from '$lib/i18n.js';
  import {
    getWakewordStatus,
    setWakewordEnabled,
    setWakewordConfig,
    onWakewordStatusChange,
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

  const AUTO_SAVE_DEBOUNCE_MS = 800;
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
  let autoSaveTimer = null;
  let cleanupStatusPoll = null;

  let agentOptions = $derived(
    agents.map((agent) => ({
      value: agent.id,
      label: agent.name || agent.id,
    })),
  );
  let selectedAgentValue = $derived(voiceState.target_agent_id || '');

  let liveStateLabel = $derived(liveStateText(voiceState.liveState));
  let liveStateDotClass = $derived(liveStateDotColor(voiceState.liveState));

  let dirty = $derived(voiceSettingsDirty(voiceState, lastSaved));
  let sensitivityPercent = $derived(Math.round(voiceState.sensitivity * 100));

  function liveStateText(state) {
    if (state === 'wakeword_detected') {
      return t('voice.state.wakewordDetected', 'Wakeword detected');
    }
    const key = `voice.state.${state}`;
    return t(key, state);
  }

  function liveStateDotColor(state) {
    switch (state) {
      case 'listening':
        return 'voice-dot--listening';
      case 'wakeword_detected':
        return 'voice-dot--detected';
      case 'recording':
        return 'voice-dot--recording';
      case 'transcribing':
      case 'sending':
        return 'voice-dot--processing';
      case 'error':
        return 'voice-dot--error';
      default:
        return 'voice-dot--off';
    }
  }

  onDestroy(() => {
    if (autoSaveTimer) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
    if (cleanupStatusPoll) {
      cleanupStatusPoll();
      cleanupStatusPoll = null;
    }
  });

  async function loadStatus() {
    try {
      const status = await getWakewordStatus();
      voiceState = applyWakewordStatus(voiceState, status);
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
        state: enabled ? 'listening' : 'off',
      });
      lastSaved = snapshotVoiceSettings(voiceState);
    } catch {
      voiceState = { ...voiceState, enabled: !enabled };
    }
  }

  function handleConfigChange() {
    if (autoSaveTimer) {
      clearTimeout(autoSaveTimer);
    }
    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveConfig();
    }, AUTO_SAVE_DEBOUNCE_MS);
  }

  async function saveConfig() {
    if (!dirty) {
      onToast({
        title: t('common.alreadySaved', 'Already saved'),
        variant: 'info',
      });
      return;
    }
    const payload = buildVoiceSettingsPayload(voiceState, lastSaved);
    try {
      await setWakewordConfig(payload);
      lastSaved = snapshotVoiceSettings(voiceState);
      onToast({
        title: t('settings.voice.saveSuccess', 'Voice settings updated.'),
        variant: 'success',
      });
    } catch {
      onToast({
        title: t('errors.generic', 'Something went wrong. Try again.'),
        variant: 'error',
      });
    }
  }

  function handleAgentChange(value) {
    voiceState = { ...voiceState, target_agent_id: value || null };
    handleConfigChange();
  }

  function handleSessionBehaviorChange(value) {
    voiceState = { ...voiceState, session_behavior: value };
    handleConfigChange();
  }

  function handleSensitivityInput(event) {
    const value = parseFloat(event.target.value);
    if (Number.isFinite(value)) {
      voiceState = { ...voiceState, sensitivity: value };
      handleConfigChange();
    }
  }

  function microphoneLabel() {
    if (!voiceState.microphone) {
      return t('settings.voice.systemDefaultMic', 'System default');
    }
    return String(voiceState.microphone);
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
          disabled={!loaded}
          ariaLabel={t('settings.voice.enabled', 'Wakeword listening')}
        />
      </div>
    </div>

    {#if voiceState.mock}
      <div class="voice-mock-warning" role="alert">
        {t(
          'settings.voice.mockWarning',
          'Wakeword detection is running in mock mode — the on-device speech engine could not be loaded, so nothing is actually being heard. Install the desktop voice dependencies and restart to enable real detection.',
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
        <span class="voice-state">
          <span class="voice-state-dot {liveStateDotClass}" aria-hidden="true"
          ></span>
          <span class="voice-state-label">{liveStateLabel}</span>
        </span>
      </div>
    </div>

    <!-- Sensitivity slider -->
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
          <span class="voice-slider-label">
            {t('settings.voice.sensitivity', 'Sensitivity')}
          </span>
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            value={voiceState.sensitivity}
            oninput={handleSensitivityInput}
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
          {t('settings.voice.targetAgent', 'Target Agent')}
        </div>
        <div class="s-row-desc">
          {t(
            'settings.voice.targetAgentDescription',
            'The agent that receives your spoken command after the wake phrase. Voice commands go nowhere until a target agent is selected.',
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

    <!-- Engine (read-only) -->
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.voice.engine', 'Engine')}
        </div>
      </div>
      <div class="s-row-control s-row-control--input">
        <TextField readonly value={voiceState.engine} />
      </div>
    </div>

    <!-- Microphone (read-only) -->
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.voice.microphone', 'Microphone')}
        </div>
      </div>
      <div class="s-row-control s-row-control--input">
        <TextField readonly value={microphoneLabel()} />
      </div>
    </div>

    <!-- Wake phrase (read-only) -->
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.voice.wakePhrase', 'Wake phrase')}
        </div>
      </div>
      <div class="s-row-control s-row-control--input">
        <TextField readonly value={voiceState.wake_phrase} />
      </div>
    </div>

    <!-- Privacy note -->
    <div class="voice-privacy-note">
      {t(
        'settings.voice.privacyNote',
        'Wakeword detection runs locally on your device. Audio is only recorded after the wake phrase is detected. Transcription uses your configured vBot speech backend.',
      )}
    </div>

    <!-- Save button -->
    <div class="s-footer">
      <Button
        variant="primary"
        class="s-save-button s-save-button--inline"
        disabled={!loaded}
        onClick={() => saveConfig()}
      >
        {t('common.save', 'Save')}
      </Button>
    </div>
  {/if}
</div>

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
</style>
