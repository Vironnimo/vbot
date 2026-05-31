<script>
  import { onDestroy } from 'svelte';
  import Dropdown from './Dropdown.svelte';
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
    cleanupStatusPoll = onWakewordStatusChange((status) => {
      voiceState = applyWakewordStatus(voiceState, status);
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
        <label class="voice-toggle">
          <input
            type="checkbox"
            checked={voiceState.enabled}
            onchange={handleEnabledChange}
            disabled={!loaded}
          />
          <span class="voice-toggle__slider"></span>
        </label>
      </div>
    </div>

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
          {t('settings.voice.targetAgent', 'Target Agent')}
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
        <div class="s-value-box">{voiceState.engine}</div>
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
        <div class="s-value-box">{microphoneLabel()}</div>
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
        <div class="s-value-box">{voiceState.wake_phrase}</div>
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
      <button
        class="btn btn--primary"
        type="button"
        disabled={!loaded}
        onclick={() => saveConfig()}
      >
        {t('common.save', 'Save')}
      </button>
    </div>
  {/if}
</div>

<style>
  .voice-settings {
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  /* Toggle switch */
  .voice-toggle {
    position: relative;
    display: inline-block;
    width: 44px;
    height: 24px;
    cursor: pointer;
  }
  .voice-toggle input {
    opacity: 0;
    width: 0;
    height: 0;
    position: absolute;
  }
  .voice-toggle__slider {
    position: absolute;
    inset: 0;
    border-radius: 12px;
    background: var(--bg-subtle, #2d271f);
    border: 1px solid var(--border, #3d3528);
    transition:
      background 0.2s,
      border-color 0.2s;
  }
  .voice-toggle__slider::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: var(--text-lo, #847762);
    transition:
      transform 0.2s,
      background 0.2s;
  }
  .voice-toggle input:checked + .voice-toggle__slider {
    background: var(--green, #4ade80);
    border-color: var(--green, #4ade80);
  }
  .voice-toggle input:checked + .voice-toggle__slider::after {
    transform: translateX(20px);
    background: #fff;
  }
  .voice-toggle input:disabled + .voice-toggle__slider {
    opacity: 0.4;
    cursor: not-allowed;
  }

  /* Live state dot */
  .voice-state {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
  }
  .voice-state-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--text-lo, #5e4c38);
  }
  .voice-dot--off {
    background: var(--text-lo, #5e4c38);
  }
  .voice-dot--listening {
    background: var(--green, #4ade80);
    animation: voice-pulse 1.6s ease-in-out infinite;
  }
  .voice-dot--detected {
    background: var(--green, #4ade80);
    animation: voice-pulse 0.6s ease-in-out infinite;
  }
  .voice-dot--recording {
    background: var(--amber, #f59e0b);
  }
  .voice-dot--processing {
    background: var(--accent, #e8870a);
    animation: voice-spin 1s linear infinite;
  }
  .voice-dot--error {
    background: var(--red, #fc8181);
  }
  .voice-state-label {
    font-size: 0.85rem;
    color: var(--text, #f1eadf);
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
    gap: 0.4rem;
    width: 100%;
  }
  .voice-slider-label {
    font-size: 0.8rem;
    color: var(--text-lo, #847762);
  }
  .voice-slider input[type='range'] {
    width: 100%;
    accent-color: var(--accent, #e8870a);
  }
  .voice-slider-labels {
    display: flex;
    justify-content: space-between;
    font-size: 0.7rem;
    color: var(--text-lo, #847762);
  }

  /* Privacy note */
  .voice-privacy-note {
    margin-top: 1rem;
    padding: 0.8rem 1rem;
    border-radius: 0.5rem;
    background: var(--bg-subtle, #2d271f);
    font-size: 0.8rem;
    color: var(--text-lo, #847762);
    line-height: 1.5;
  }
</style>
