<script>
  // The running calibration of one wake phrase: the Desktop measures room
  // noise, then repetitions of the phrase, and recommends a sensitivity.
  // Commands pause meanwhile. Restart and Discard act on the Desktop directly;
  // Apply hands the recommendation to the Voice panel, which saves it.
  import { t } from '$lib/i18n.js';
  import StatusChip from '../ui/StatusChip.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import {
    restartVoiceCalibration,
    stopVoiceCalibration,
  } from '$lib/desktopBridge.js';
  import { bridgeErrorMessage } from './voiceLabels.js';

  let {
    // `status.calibration` of this phrase.
    calibration,
    label,
    // The phrase's sensitivity before calibration.
    currentSensitivity = null,
    // Receives each status snapshot a calibration call returns.
    onStatus = () => {},
    // Saves the recommended sensitivity; resolves whether it was saved.
    onApply = async () => false,
    onToast = () => {},
  } = $props();

  let actionState = $state('idle');
  let discardConfirm = $state(false);

  let busy = $derived(actionState !== 'idle');
  let recommendation = $derived(calibration?.recommended_sensitivity ?? null);
  let ready = $derived(
    calibration?.phase === 'ready' && recommendation !== null,
  );
  let requiredSamples = $derived(calibration?.required_samples ?? null);
  let score = $derived(clampScore(calibration?.score));
  let peak = $derived(clampScore(calibration?.peak));
  let threshold = $derived(
    clampScore(1 - (recommendation ?? currentSensitivity ?? 0.5)),
  );
  let instruction = $derived.by(() => {
    if (calibration?.phase === 'noise') {
      return t('settings.voice.calibrationNoiseInstruction', {
        seconds: Math.max(1, Math.ceil(calibration.noise_seconds_remaining)),
      });
    }
    if (calibration?.phase === 'phrases') {
      return t('settings.voice.calibrationPhraseInstruction', {
        name: label,
        count: calibration.sample_count,
        required: requiredSamples ?? '–',
      });
    }
    return t('settings.voice.calibrationReviewInstruction');
  });

  function stepState(step) {
    const order = { noise: 0, phrases: 1, ready: 2 };
    const current = order[calibration?.phase] ?? -1;
    if (step < current) return 'complete';
    if (step === current) return 'current';
    return 'pending';
  }

  function clampScore(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 0;
    return Math.max(0, Math.min(1, numeric));
  }

  const percent = (value) => `${Math.round(clampScore(value) * 1000) / 10}%`;

  async function run(state, action, failureTitle) {
    if (busy) return;
    actionState = state;
    try {
      await action();
    } catch (error) {
      onToast({
        title: failureTitle,
        message: bridgeErrorMessage(error),
        variant: 'error',
      });
    } finally {
      actionState = 'idle';
    }
  }

  function restart() {
    return run(
      'restarting',
      async () => onStatus(await restartVoiceCalibration()),
      t('settings.voice.calibrationResetFailed'),
    );
  }

  function discard() {
    discardConfirm = false;
    return run(
      'discarding',
      async () => onStatus(await stopVoiceCalibration()),
      t('settings.voice.calibrationStopFailed'),
    );
  }

  function apply() {
    if (!ready) return;
    const value = recommendation;
    return run(
      'applying',
      async () => {
        if (!(await onApply(value))) return;
        onToast({
          title: t('settings.voice.calibrationApplied'),
          variant: 'success',
        });
      },
      t('settings.voice.calibrationApplyFailed'),
    );
  }
</script>

<div class="voice-calibration-panel">
  <div class="voice-calibration-header">
    <div>
      <div class="voice-calibration-heading">
        {t('settings.voice.calibrationHeading', {
          name: label,
        })}
      </div>
      <div class="voice-calibration-instruction">{instruction}</div>
    </div>
    <StatusChip variant={ready ? 'success' : 'warn'}>
      {ready
        ? t('settings.voice.calibrationReadyToApply')
        : t('settings.voice.calibrationListening')}
    </StatusChip>
  </div>

  {#if calibration?.noise_high}
    <div class="voice-calibration-noise-warning" role="alert">
      {t('settings.voice.calibrationNoiseHighWarning', {
        level: clampScore(calibration.noise_level).toFixed(2),
      })}
    </div>
  {/if}

  <ol
    class="voice-calibration-steps"
    aria-label={t('settings.voice.calibrationProgressAria')}
  >
    <li data-state={stepState(0)}>
      <span>1</span>
      {t('settings.voice.calibrationStepNoise')}
    </li>
    <li data-state={stepState(1)}>
      <span>2</span>
      {t('settings.voice.calibrationStepPhrases')}
    </li>
    <li data-state={stepState(2)}>
      <span>3</span>
      {t('settings.voice.calibrationStepReview')}
    </li>
  </ol>

  <div class="voice-calibration-model">
    <div class="voice-calibration-model__header">
      <span class="voice-calibration-model__identity">{label}</span>
      <span class="voice-calibration-model__values">
        {t('settings.voice.calibrationScore')}
        {score.toFixed(2)}
        · {t('settings.voice.calibrationNoise')}
        {clampScore(calibration?.noise_level).toFixed(2)}
        · {t('settings.voice.calibrationThreshold')}
        {threshold.toFixed(2)}
      </span>
    </div>
    <div
      class="voice-calibration-meter"
      role="meter"
      aria-label={t('settings.voice.calibrationMeterAria', { name: label })}
      aria-valuemin="0"
      aria-valuemax="1"
      aria-valuenow={score}
      style={`--score: ${percent(score)}; --peak: ${percent(peak)}; --threshold: ${percent(threshold)};`}
    >
      <span
        class="voice-calibration-meter__score"
        class:voice-calibration-meter__score--match={score >= threshold}
      ></span>
      {#if peak > 0}
        <span class="voice-calibration-meter__peak" aria-hidden="true"></span>
      {/if}
      <span class="voice-calibration-meter__threshold" aria-hidden="true"
      ></span>
    </div>
    <div class="voice-calibration-model__result">
      <span>
        {t('settings.voice.calibrationSamples', {
          count: calibration?.sample_count ?? 0,
          required: requiredSamples ?? '–',
        })}
      </span>
      {#if ready}
        <strong>
          {#if currentSensitivity !== null}
            {t('settings.voice.calibrationCurrentSensitivity')}
            {Math.round(currentSensitivity * 100)}% →
          {/if}
          {t('settings.voice.calibrationRecommendation', {
            value: Math.round(recommendation * 100),
          })}
        </strong>
      {:else if peak > 0}
        <span>
          {t('settings.voice.calibrationPeak')}
          {peak.toFixed(2)}
        </span>
      {/if}
    </div>
  </div>

  <div class="voice-calibration-legend" aria-hidden="true">
    <span>
      <i class="voice-calibration-legend__peak"></i>
      {t('settings.voice.calibrationPeak')}
    </span>
    <span>
      <i class="voice-calibration-legend__threshold"></i>
      {t('settings.voice.calibrationThreshold')}
    </span>
  </div>

  <div class="voice-calibration-actions">
    <Button
      variant="tertiary"
      disabled={busy}
      loading={actionState === 'restarting'}
      onClick={restart}
    >
      {t('settings.voice.calibrationReset')}
    </Button>
    <div class="voice-calibration-actions__decision">
      <Button
        variant="secondary"
        disabled={busy}
        loading={actionState === 'discarding'}
        onClick={() => (discardConfirm = true)}
      >
        {t('settings.voice.calibrationDiscard')}
      </Button>
      <Button
        variant="primary"
        loading={actionState === 'applying'}
        disabled={busy || !ready}
        onClick={apply}
      >
        {t('settings.voice.calibrationApply')}
      </Button>
    </div>
  </div>

  {#if discardConfirm}
    <ConfirmDialog
      title={t('settings.voice.calibrationDiscardConfirmTitle')}
      body={t('settings.voice.calibrationDiscardConfirm')}
      confirmLabel={t('settings.voice.calibrationDiscard')}
      onConfirm={discard}
      onCancel={() => (discardConfirm = false)}
    />
  {/if}
</div>
