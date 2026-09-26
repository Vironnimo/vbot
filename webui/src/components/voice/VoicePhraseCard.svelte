<script>
  // One wake phrase of the catalog: whether it is listened for and, while
  // active, its sensitivity and what a detection does. The Voice panel owns
  // the draft and the saving; this card only renders and reports edits.
  import { t } from '$lib/i18n.js';
  import Toggle from '../ui/Toggle.svelte';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Dropdown from '../Dropdown.svelte';
  import { errorMessage } from './voiceLabels.js';
  import {
    ACTION_CHOICE_COMMAND,
    ACTION_CHOICE_LIVE_START,
    ACTION_CHOICE_LIVE_TOGGLE,
    voiceActionChoice,
    voiceActionForChoice,
  } from '$lib/wakewordSettings.js';

  const noop = () => {};

  let {
    // Catalog descriptor: {id, label, source, removable}.
    model,
    active = false,
    // Draft sensitivity; null until the Desktop reports one.
    sensitivity = null,
    // `{min_sensitivity, max_sensitivity}` from the status; null values
    // disable the slider.
    limits = null,
    // Draft action of this phrase.
    action = null,
    agentOptions = [],
    // Problem code the Desktop reports for this phrase.
    problem = null,
    // Labels of active phrases that can fire on the same words but do
    // something else.
    conflicts = [],
    toggleDisabled = false,
    sensitivityDisabled = false,
    routingDisabled = false,
    // Calibration: `null` hides the button (the phrase is not calibratable
    // now), otherwise whether it is disabled.
    calibrateDisabled = null,
    removeDisabled = false,
    onToggle = noop,
    onSensitivityInput = noop,
    onSensitivityCommit = noop,
    onActionChange = noop,
    onCalibrate = noop,
    onRemove = noop,
    // Snippet rendering the running calibration of this phrase.
    calibration = null,
  } = $props();

  const ACTION_OPTIONS = [
    {
      value: ACTION_CHOICE_COMMAND,
      label: t('settings.voice.actionCommand', 'Send a command'),
    },
    {
      value: ACTION_CHOICE_LIVE_TOGGLE,
      label: t('settings.voice.actionLiveToggle', 'Start or end Live voice'),
    },
    {
      value: ACTION_CHOICE_LIVE_START,
      label: t('settings.voice.actionLiveStart', 'Start Live voice'),
    },
  ];
  const SESSION_OPTIONS = [
    {
      value: '',
      label: t('settings.voice.sessionDefault', 'Default Session behavior'),
    },
    {
      value: 'active',
      label: t('settings.voice.sessionBehaviorActive', 'Use active Session'),
    },
    {
      value: 'new',
      label: t('settings.voice.sessionBehaviorNew', 'New Session each time'),
    },
  ];

  let sliderId = $derived(`voice-sensitivity-${model.id}`);
  let minSensitivity = $derived(limits?.min_sensitivity ?? null);
  let maxSensitivity = $derived(limits?.max_sensitivity ?? null);
  let sliderAvailable = $derived(
    minSensitivity !== null && maxSensitivity !== null && sensitivity !== null,
  );
  let sliderValue = $derived(
    sliderAvailable
      ? Math.min(maxSensitivity, Math.max(minSensitivity, sensitivity))
      : 0.5,
  );
  let actionChoice = $derived(voiceActionChoice(action));
  let command = $derived(actionChoice === ACTION_CHOICE_COMMAND);
  let agentChoices = $derived([
    {
      value: '',
      label: t('settings.voice.agentDefault', 'Default Agent'),
    },
    ...agentOptions,
    ...(action?.agent_id &&
    !agentOptions.some((option) => option.value === action.agent_id)
      ? [
          {
            value: action.agent_id,
            label: action.agent_id,
            secondaryLabel: t(
              'settings.voice.agentUnavailable',
              'Not on this server',
            ),
            disabled: true,
          },
        ]
      : []),
  ]);

  function changeChoice(choice) {
    onActionChange(voiceActionForChoice(choice, action));
  }

  function changeAgent(agentId) {
    onActionChange({ ...action, type: 'command', agent_id: agentId || null });
  }

  function changeSession(behavior) {
    onActionChange({
      ...action,
      type: 'command',
      session_behavior: behavior || null,
    });
  }

  function handleSensitivityInput(event) {
    const value = Number.parseFloat(event.currentTarget.value);
    if (Number.isFinite(value)) onSensitivityInput(value);
  }
</script>

<div
  class="voice-model-card"
  class:voice-model-card--active={active}
  class:voice-model-card--calibrating={Boolean(active && calibration)}
  data-model-id={model.id}
>
  <div class="voice-model-card__header">
    <div class="voice-model-card__identity">
      <span class="voice-model-card__name">{model.label}</span>
      <Badge variant={model.source === 'built_in' ? 'info' : 'neutral'}>
        {model.source === 'built_in'
          ? t('settings.voice.modelBuiltIn', 'Built-in')
          : t('settings.voice.modelImported', 'Imported TFLite')}
      </Badge>
      {#if active && problem}
        <Badge variant="warn" class="voice-model-card__problem-badge">
          {t('settings.voice.phraseNotReady', 'Not ready')}
        </Badge>
      {/if}
    </div>
    <Toggle
      size="sm"
      checked={active}
      onChange={onToggle}
      disabled={toggleDisabled}
      ariaLabel={t('settings.voice.modelToggleAria', 'Listen for {name}', {
        name: model.label,
      })}
    />
  </div>

  {#if active}
    {#if problem}
      <p class="voice-model-card__notice" role="status">
        {errorMessage(problem)}
      </p>
    {/if}
    {#if conflicts.length > 0}
      <p
        class="voice-model-card__notice voice-model-card__notice--warn"
        role="note"
      >
        {t(
          'settings.voice.overlapWarning',
          '“{name}” can also be heard as {others}, which does something else. Give them the same action or keep only one of them active.',
          {
            name: model.label,
            others: conflicts.map((label) => `“${label}”`).join(', '),
          },
        )}
      </p>
    {/if}

    <div class="voice-model-card__tuning">
      <div class="voice-model-card__sensitivity">
        <label for={sliderId}>
          {t('settings.voice.sensitivity', 'Sensitivity')}
        </label>
        <span
          >{sliderAvailable ? `${Math.round(sliderValue * 100)}%` : '–'}</span
        >
      </div>
      <input
        id={sliderId}
        type="range"
        min={minSensitivity ?? 0}
        max={maxSensitivity ?? 1}
        step="0.01"
        value={sliderValue}
        oninput={handleSensitivityInput}
        onchange={onSensitivityCommit}
        disabled={sensitivityDisabled || !sliderAvailable}
      />
      <div class="voice-slider-labels">
        <span>{t('settings.voice.lessSensitive', 'Less sensitive')}</span>
        <span>{t('settings.voice.moreSensitive', 'More sensitive')}</span>
      </div>

      <div class="voice-model-card__action">
        <span aria-hidden="true">
          {t('settings.voice.modelAction', 'When heard')}
        </span>
        <Dropdown
          value={actionChoice}
          options={ACTION_OPTIONS}
          ariaLabel={t(
            'settings.voice.modelActionAria',
            'When {name} is heard',
            {
              name: model.label,
            },
          )}
          onValueChange={changeChoice}
          disabled={routingDisabled}
        />
      </div>
      {#if command}
        <div class="voice-model-card__action">
          <span aria-hidden="true">
            {t('settings.voice.phraseAgent', 'Agent')}
          </span>
          <Dropdown
            value={action?.agent_id ?? ''}
            options={agentChoices}
            ariaLabel={t('settings.voice.phraseAgentAria', 'Agent for {name}', {
              name: model.label,
            })}
            onValueChange={changeAgent}
            disabled={routingDisabled}
          />
        </div>
        <div class="voice-model-card__action">
          <span aria-hidden="true">
            {t('settings.voice.phraseSession', 'Session')}
          </span>
          <Dropdown
            value={action?.session_behavior ?? ''}
            options={SESSION_OPTIONS}
            ariaLabel={t(
              'settings.voice.phraseSessionAria',
              'Session for {name}',
              { name: model.label },
            )}
            onValueChange={changeSession}
            disabled={routingDisabled}
          />
        </div>
      {/if}
    </div>

    {#if calibration}
      {@render calibration()}
    {:else if calibrateDisabled !== null}
      <div class="voice-model-card__actions">
        <Button
          variant="tertiary"
          disabled={calibrateDisabled}
          ariaLabel={t('settings.voice.calibrateAria', 'Calibrate {name}', {
            name: model.label,
          })}
          onClick={onCalibrate}
        >
          {t('settings.voice.calibrate', 'Calibrate')}
        </Button>
      </div>
    {/if}
  {:else if model.removable}
    <div class="voice-model-card__actions">
      <Button variant="tertiary" disabled={removeDisabled} onClick={onRemove}>
        {t('settings.voice.removeModel', 'Remove imported model')}
      </Button>
    </div>
  {/if}
</div>
