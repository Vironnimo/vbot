<script>
  import { activeLocaleTag, t } from '../../../../webui/src/lib/i18n.js';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import { limitFields } from './limits.js';

  let {
    value = $bindable(),
    prefix,
    label,
    disabled = false,
    usage = null,
  } = $props();
</script>

<fieldset class="factory-limits">
  <legend>{label}</legend>
  <div class="factory-fields">
    {#each limitFields as field (field.key)}
      <div class="factory-field">
        <label for={`${prefix}-${field.key}`}>{field.label()}</label>
        <TextField
          id={`${prefix}-${field.key}`}
          type="number"
          min="0"
          step={field.key === 'cost_usd' ? 'any' : '1'}
          value={value?.[field.key] ?? ''}
          placeholder={t('factory.limit.unlimited')}
          {disabled}
          onInput={(amount) => (value = { ...value, [field.key]: amount })}
        />
        {#if usage}
          <span class="factory-help"
            >{t('factory.limit.used', {
              value:
                usage[field.key] == null ||
                (field.key === 'tokens' && usage.unreported_calls > 0) ||
                (field.key === 'cost_usd' && usage.unpriced_calls > 0)
                  ? t('factory.limit.unknown')
                  : usage[field.key].toLocaleString(activeLocaleTag(), {
                      maximumFractionDigits: 6,
                    }),
            })}</span
          >
        {/if}
      </div>
    {/each}
  </div>
</fieldset>

<style>
  .factory-limits {
    border: 0;
    padding: 0;
    margin: 1rem 0;
    min-width: 0;
  }
  legend {
    font-weight: 600;
    margin-bottom: 0.75rem;
  }
</style>
