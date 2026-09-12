<script>
  import { t } from '$lib/i18n.js';
  import StatusChip from '../ui/StatusChip.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import {
    retryWakewordModelCalibration,
    restartWakewordCalibration,
    stopWakewordCalibration,
    startWakewordCalibration,
  } from '$lib/desktopBridge.js';
  import { applyRuntimeStatus } from '$lib/wakewordSettings.js';
  let {
    onToast,
    voiceState = $bindable(),
    loaded,
    wakewordModels,
    modelActionBusy,
    enableActionBusy,
    calibrationBaselineSensitivities = $bindable(),
    calibrationActionState = $bindable(),
    calibrationActionBusy,
    calibrationSessionActive,
    voiceConfigHasChanges,
    saveConfig,
    restoreCalibrationDraft,
  } = $props();

  let calibrationDiscardConfirm = $state(false);

  let calibrationRetryModelId = $state(null);

  let calibrationActive = $derived(Boolean(voiceState.calibration?.active));

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
      !enableActionBusy &&
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
</script>

<div class="s-row s-row--stacked">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.voice.calibrationTitle', 'Wakeword calibration')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.voice.calibrationDescription',
        'Measure room noise and five natural repetitions per phrase to calculate a reliable sensitivity automatically.',
      )}
    </div>
  </div>
  <div class="s-row-control voice-calibration-control">
    {#if calibrationSessionActive}
      <div class="voice-calibration-panel">
        <div class="voice-calibration-header">
          <div>
            <div class="voice-calibration-heading">
              {t('settings.voice.calibrationAnalyzer', 'Guided calibration')}
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
              ? t('settings.voice.calibrationReadyToApply', 'Ready to apply')
              : calibrationActive
                ? t('settings.voice.calibrationListening', 'Commands paused')
                : t('settings.voice.calibrationStopped', 'Calibration stopped')}
          </StatusChip>
        </div>

        {#if calibrationNoiseHigh}
          <div class="voice-calibration-noise-warning" role="alert">
            {t(
              'settings.voice.calibrationNoiseHighWarning',
              'Room noise is high ({level}). Consider moving to a quieter environment or reducing background noise for better results.',
              {
                level: Math.max(
                  ...Object.values(voiceState.calibration?.noise_levels || {}),
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
          {#each wakewordModels.filter( (model) => voiceState.active_model_ids.includes(model.id) ) as model (model.id)}
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
                      {t('settings.voice.calibrationSayNow', 'Say this now')}
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
                  <span class="voice-calibration-meter__peak" aria-hidden="true"
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
              {t('settings.voice.calibrationApply', 'Apply calibrated values')}
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
                'Calibration takes about 30–60 seconds. Wakeword commands are paused while it runs.',
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
