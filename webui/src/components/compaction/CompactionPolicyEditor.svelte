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
      value: 'summary_tail',
      label: t('compaction.strategy.summaryTail', 'Summary + verbatim tail'),
    },
    {
      value: 'continuation',
      label: t(
        'compaction.strategy.continuation',
        'Cache-preserving continuation',
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
  <div class="compaction-policy-editor__enabled">
    <div>
      <div class="compaction-policy-editor__label">
        {t('compaction.enabled', 'Automatic compaction')}
      </div>
      <div class="compaction-policy-editor__description">
        {t(
          'compaction.enabledDescription',
          'Compact before a Model request or after complete Tool Results when the configured ratio or token limit is reached.',
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

    <FormField label={t('compaction.strategy.label', 'Strategy')} full>
      <Dropdown
        id={`${idPrefix}-strategy`}
        value={policy.strategy.type}
        options={strategyOptions}
        {disabled}
        ariaLabel={t('compaction.strategy.label', 'Strategy')}
        onValueChange={changeStrategyType}
      />
    </FormField>

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
    {:else}
      <p class="compaction-policy-editor__note">
        {t(
          'compaction.strategy.continuationDescription',
          'Reuses the active Model request prefix and turns one text response directly into the next checkpoint.',
        )}
      </p>
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
    padding: 12px 14px;
    border: 1px solid var(--border-2);
    border-radius: 8px;
    background: var(--surface-2);
  }

  .compaction-policy-editor__label {
    color: var(--text-hi);
    font: 500 13px var(--font-ui);
  }

  .compaction-policy-editor__description,
  .compaction-policy-editor__note {
    margin: 3px 0 0;
    color: var(--text-med);
    font: 12px/1.45 var(--font-ui);
  }

  .compaction-policy-editor__grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
  }

  .compaction-policy-editor__note {
    grid-column: 1 / -1;
    padding: 10px 12px;
    border-left: 2px solid var(--accent);
    background: var(--accent-08);
  }

  @media (max-width: 760px) {
    .compaction-policy-editor__grid {
      grid-template-columns: 1fr;
    }
  }
</style>
