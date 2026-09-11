<script>
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import FormField from '../ui/FormField.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { t } from '$lib/i18n.js';
  import { normalizeCompactionPolicy } from '$lib/compactionPolicy.js';

  let {
    value,
    onChange = () => {},
    disabled = false,
    idPrefix = 'compaction-policy',
    summaryModelOptions = null,
    summaryModelSelectValue = '',
    onSummaryModelSelect = null,
    onSummaryModelOpenChange = () => {},
  } = $props();

  let policy = $derived(normalizeCompactionPolicy(value));
  const triggerOptions = $derived([
    {
      value: 'context_ratio',
      label: t('compaction.trigger.contextRatio', 'Context window ratio'),
    },
    {
      value: 'input_tokens',
      label: t('compaction.trigger.inputTokens', 'Absolute input tokens'),
    },
  ]);
  const strategyOptions = $derived([
    {
      value: 'continuation',
      label: t('compaction.strategy.continuation', 'Classic'),
      description: t(
        'compaction.strategy.continuationDescription',
        'Summarize the conversation with the active Model. Continue from the summary.',
      ),
    },
    {
      value: 'summary_tail',
      label: t('compaction.strategy.summaryTail', 'With tail'),
      description: t(
        'compaction.strategy.summaryTailDescription',
        'Summarize older messages and keep the most recent messages unchanged.',
      ),
    },
  ]);

  function changeEnabled(enabled) {
    onChange({ ...policy, enabled });
  }

  function changeTriggerType(type) {
    onChange({
      ...policy,
      trigger:
        type === 'input_tokens'
          ? { type, tokens: policy.trigger.tokens ?? 100000 }
          : {
              type,
              threshold: 0.8,
              ...(policy.trigger.tokens
                ? { tokens: policy.trigger.tokens }
                : {}),
            },
    });
  }

  function changeTriggerField(field, next) {
    onChange({ ...policy, trigger: { ...policy.trigger, [field]: next } });
  }

  function changeStrategyType(type) {
    if (type === policy.strategy.type) return;
    onChange({
      ...policy,
      strategy:
        type === 'continuation'
          ? { type }
          : { type, tail_tokens: 15000, summary_model: null },
    });
  }

  function changeStrategyField(field, next) {
    onChange({ ...policy, strategy: { ...policy.strategy, [field]: next } });
  }
</script>

<div class="compaction-policy-editor" data-testid={`${idPrefix}-editor`}>
  <fieldset class="compaction-policy-editor__mode" {disabled}>
    <legend>{t('compaction.strategy.label', 'Compaction mode')}</legend>
    <div class="compaction-policy-editor__choices">
      {#each strategyOptions as option (option.value)}
        <label
          class="compaction-policy-editor__choice"
          class:compaction-policy-editor__choice--selected={policy.strategy
            .type === option.value}
        >
          <input
            type="radio"
            name={`${idPrefix}-strategy`}
            value={option.value}
            checked={policy.strategy.type === option.value}
            onchange={() => changeStrategyType(option.value)}
          />
          <span>
            <strong>{option.label}</strong>
            <span class="compaction-policy-editor__description"
              >{option.description}</span
            >
          </span>
        </label>
      {/each}
    </div>
  </fieldset>
  <div
    class="compaction-policy-editor__grid"
    hidden={policy.strategy.type !== 'summary_tail'}
  >
    {#if policy.strategy.type === 'summary_tail'}
      <FormField
        label={t('compaction.strategy.tailTokens', 'Verbatim tail tokens')}
      >
        <TextField
          type="number"
          value={policy.strategy.tail_tokens}
          {disabled}
          ariaLabel={t(
            'compaction.strategy.tailTokens',
            'Verbatim tail tokens',
          )}
          onInput={(next) => changeStrategyField('tail_tokens', next)}
        />
      </FormField>
      <FormField label={t('compaction.strategy.summaryModel', 'Summary model')}>
        {#if Array.isArray(summaryModelOptions)}
          <SearchableDropdown
            id={`${idPrefix}-summary-model`}
            value={summaryModelSelectValue}
            options={summaryModelOptions}
            {disabled}
            placeholder={t(
              'settings.compaction.summaryModelPlaceholder',
              'Active agent model',
            )}
            searchPlaceholder={t(
              'agents.form.modelSearchPlaceholder',
              'Filter models…',
            )}
            emptyLabel={t('agents.form.modelSearchEmpty', 'No models match')}
            ariaLabel={t('compaction.strategy.summaryModel', 'Summary model')}
            onOpenChange={onSummaryModelOpenChange}
            onValueChange={onSummaryModelSelect}
          />
        {:else}
          <TextField
            value={policy.strategy.summary_model ?? ''}
            {disabled}
            placeholder={t('compaction.strategy.activeModel', 'Active Model')}
            ariaLabel={t('compaction.strategy.summaryModel', 'Summary model')}
            onInput={(next) => changeStrategyField('summary_model', next)}
          />
        {/if}
      </FormField>
    {/if}
  </div>

  <div class="compaction-policy-editor__enabled">
    <div>
      <div class="compaction-policy-editor__label">
        {t('compaction.enabled', 'Automatic compaction')}
      </div>
      <div class="compaction-policy-editor__description">
        {t(
          'compaction.enabledDescription',
          'Compact automatically when a limit is reached. Manual Compaction remains available when this is off.',
        )}
      </div>
    </div>
    <Toggle
      checked={policy.enabled}
      {disabled}
      ariaLabel={t('compaction.enabled', 'Automatic compaction')}
      onChange={changeEnabled}
    />
  </div>

  <div class="compaction-policy-editor__grid">
    <FormField label={t('compaction.trigger.label', 'Trigger')}>
      <Dropdown
        id={`${idPrefix}-trigger`}
        value={policy.trigger.type}
        options={triggerOptions}
        {disabled}
        ariaLabel={t('compaction.trigger.label', 'Trigger')}
        onValueChange={changeTriggerType}
      />
    </FormField>

    {#if policy.trigger.type === 'input_tokens'}
      <FormField label={t('compaction.trigger.tokens', 'Input tokens')}>
        <TextField
          type="number"
          value={policy.trigger.tokens}
          {disabled}
          ariaLabel={t('compaction.trigger.tokens', 'Input tokens')}
          onInput={(next) => changeTriggerField('tokens', next)}
        />
      </FormField>
    {:else}
      <FormField label={t('compaction.trigger.threshold', 'Context ratio')}>
        <TextField
          inputmode="decimal"
          value={policy.trigger.threshold}
          {disabled}
          ariaLabel={t('compaction.trigger.threshold', 'Context ratio')}
          onInput={(next) => changeTriggerField('threshold', next)}
        />
      </FormField>
      <FormField
        label={t(
          'compaction.trigger.maxTokens',
          'Maximum input tokens (optional)',
        )}
      >
        <TextField
          type="number"
          value={policy.trigger.tokens ?? ''}
          {disabled}
          placeholder={t('compaction.trigger.noTokenCap', 'No token cap')}
          ariaLabel={t(
            'compaction.trigger.maxTokens',
            'Maximum input tokens (optional)',
          )}
          onInput={(next) => changeTriggerField('tokens', next)}
        />
      </FormField>
    {/if}
  </div>
</div>

<style>
  .compaction-policy-editor {
    display: grid;
    gap: 16px;
    width: 100%;
  }

  .compaction-policy-editor__enabled {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    padding-top: 20px;
    border-top: 1px solid var(--border);
  }

  .compaction-policy-editor__label {
    color: var(--text-hi);
    font: 500 var(--fs-label-md) var(--font-ui);
  }

  .compaction-policy-editor__description {
    display: block;
    margin: 3px 0 0;
    color: var(--text-med);
    font: var(--fs-body-sm)/1.45 var(--font-ui);
  }

  .compaction-policy-editor__grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
  }

  .compaction-policy-editor__grid[hidden] {
    display: none;
  }

  .compaction-policy-editor__mode {
    margin: 0;
    padding: 0;
    border: 0;
    min-width: 0;
  }

  .compaction-policy-editor__mode legend {
    padding: 0;
    margin-bottom: 10px;
    color: var(--text-hi);
    font: 600 var(--fs-body-md) var(--font-ui);
  }

  .compaction-policy-editor__choices {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  .compaction-policy-editor__choice {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 14px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    cursor: pointer;
  }

  .compaction-policy-editor__choice--selected {
    border-color: var(--accent-40);
    background: var(--accent-08);
  }

  .compaction-policy-editor__choice:focus-within {
    outline: 1px solid var(--accent);
    outline-offset: 2px;
  }

  .compaction-policy-editor__choice input {
    margin: 3px 0 0;
    accent-color: var(--accent);
  }

  .compaction-policy-editor__choice strong {
    font: 600 var(--fs-body-md) var(--font-ui);
    color: var(--text-hi);
  }

  .compaction-policy-editor__mode:disabled .compaction-policy-editor__choice {
    cursor: default;
    opacity: 0.6;
  }

  @media (max-width: 760px) {
    .compaction-policy-editor__grid,
    .compaction-policy-editor__choices {
      grid-template-columns: 1fr;
    }
  }
</style>
