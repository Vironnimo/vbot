<script>
  // Advanced sampling: Temperature and Top P behind one collapsed disclosure
  // row, rendered as items of the caller's `.s-group`. Every surface that
  // configures sampling (Agent, global defaults, Project defaults, Team
  // overrides) uses it, so wording and behavior stay identical.
  //
  // `fields` carries one entry per sampling field, `temperature` and `top_p`:
  //   value       – the field's text as the caller stores it ('' = not set)
  //   hint        – inherit hint shown while the field is empty
  //   error       – translated validation message; any error opens the block
  //   recommended – the selected Model's recommended value, or null
  //   explicit    – whether the field holds its own value (default: non-empty);
  //                 explicit fields appear in the collapsed summary and offer
  //                 the clear action
  //   clearLabel  – tooltip and accessible name of the clear action
  // `onChange(field, value)` receives the new text; `onClear(field)` (optional)
  // adds the clear action. A Model recommendation is only offered: its "Use"
  // action is the one way it fills a field.
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';

  let { idPrefix, fields, onChange, onClear = null } = $props();

  const CLEAR_GLYPH = '—';

  let open = $state(false);

  let rows = $derived([
    sampleRow('temperature', 'temperature', {
      label: t('sampling.temperature'),
      description: t('sampling.temperatureDescription'),
      summary: (value) => t('sampling.summaryTemperature', { value }),
    }),
    sampleRow('top_p', 'top-p', {
      label: t('sampling.topP'),
      description: t('sampling.topPDescription'),
      summary: (value) => t('sampling.summaryTopP', { value }),
    }),
  ]);

  let summary = $derived(
    rows
      .filter((row) => row.explicit)
      .map((row) => row.summary(row.value))
      .join(' · '),
  );

  // A validation error must never hide behind the collapsed row.
  $effect(() => {
    if (rows.some((row) => row.error)) open = true;
  });

  function sampleRow(key, slug, text) {
    const field = fields?.[key] ?? {};
    const value = String(field.value ?? '').trim();
    const recommended =
      typeof field.recommended === 'number' &&
      Number.isFinite(field.recommended)
        ? field.recommended
        : null;
    const id = `${idPrefix}-${slug}`;
    return {
      ...text,
      key,
      id,
      value,
      hint: value === '' ? (field.hint ?? '') : '',
      error: field.error ?? '',
      recommended,
      offerRecommendation:
        recommended !== null && parseSampleValue(value) !== recommended,
      explicit: field.explicit ?? value !== '',
      clearLabel: field.clearLabel || t('inherit.resetToInherit'),
    };
  }

  // Comma-tolerant, like the forms that store these values.
  function parseSampleValue(value) {
    if (!value) return null;
    const parsed = Number(value.replace(',', '.'));
    return Number.isFinite(parsed) ? parsed : null;
  }

  function describedBy(row) {
    return [
      `${row.id}-desc`,
      row.hint ? `${row.id}-help` : '',
      row.recommended !== null ? `${row.id}-recommendation` : '',
      row.error ? `${row.id}-error` : '',
    ]
      .filter(Boolean)
      .join(' ');
  }
</script>

<button
  type="button"
  class="s-row s-row--compact s-disclosure-row"
  id={`${idPrefix}-sampling-toggle`}
  aria-expanded={open}
  aria-controls={`${idPrefix}-sampling`}
  onclick={() => (open = !open)}
>
  <span class="s-row-label" id={`${idPrefix}-sampling-label`}>
    <span
      class="disclosure-chevron"
      class:disclosure-chevron--open={open}
      aria-hidden="true"
    ></span>
    {t('sampling.title')}
  </span>
  {#if !open && summary}
    <span class="s-disclosure__meta">
      {summary}
    </span>
  {/if}
</button>
<div
  class="s-group__rows"
  id={`${idPrefix}-sampling`}
  role="group"
  aria-labelledby={`${idPrefix}-sampling-label`}
  aria-describedby={`${idPrefix}-sampling-note`}
  hidden={!open}
>
  <p class="sampling-settings__note" id={`${idPrefix}-sampling-note`}>
    {t('sampling.note')}
  </p>
  {#each rows as row (row.key)}
    <div class="s-row">
      <div class="s-row-info">
        <label class="s-row-label" id={`${row.id}-label`} for={row.id}>
          {row.label}
        </label>
        <div class="s-row-desc" id={`${row.id}-desc`}>
          {row.description}
        </div>
        {#if row.hint}
          <div class="s-row-desc sampling-settings__hint" id={`${row.id}-help`}>
            {row.hint}
          </div>
        {/if}
        {#if row.recommended !== null}
          <div class="s-row-desc sampling-settings__recommendation">
            <span id={`${row.id}-recommendation`}>
              {t('sampling.modelRecommends', { value: row.recommended })}
            </span>
            {#if row.offerRecommendation}
              <Button
                variant="tertiary"
                class="sampling-settings__use"
                aria-describedby={`${row.id}-label`}
                onClick={() => onChange(row.key, String(row.recommended))}
              >
                {t('sampling.useRecommendation', { value: row.recommended })}
              </Button>
            {/if}
          </div>
        {/if}
        {#if row.error}
          <p
            class="sampling-settings__error"
            id={`${row.id}-error`}
            role="alert"
          >
            {row.error}
          </p>
        {/if}
      </div>
      <div class="s-row-control sampling-settings__control">
        {#if onClear && row.explicit}
          <Button
            variant="tertiary"
            class="sampling-settings__clear"
            tooltip={row.clearLabel}
            ariaLabel={row.clearLabel}
            onClick={() => onClear(row.key)}
          >
            {CLEAR_GLYPH}
          </Button>
        {/if}
        <TextField
          id={row.id}
          inputmode="decimal"
          invalid={Boolean(row.error)}
          aria-describedby={describedBy(row)}
          value={fields?.[row.key]?.value ?? ''}
          onInput={(next) => onChange(row.key, next)}
        />
      </div>
    </div>
  {/each}
</div>

<style>
  .sampling-settings__note {
    margin: 0;
    padding: 14px 18px 0 33px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.5;
  }

  .sampling-settings__hint {
    color: var(--text-med);
  }

  .sampling-settings__recommendation {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 2px 8px;
  }

  .sampling-settings__recommendation > :global(.sampling-settings__use) {
    min-height: 0;
    margin-left: -8px;
    padding: 2px 8px;
    font-size: var(--fs-label-sm);
  }

  .sampling-settings__error {
    margin: 4px 0 0;
    color: var(--red);
    font-size: var(--fs-body-sm);
    line-height: 1.4;
  }

  /* The field keeps the shared number width; the clear action sits in front
     of the input instead of widening the control column. */
  .s-row > .sampling-settings__control {
    width: auto;
    justify-self: end;
    gap: 6px;
  }

  .sampling-settings__control > :global(.s-input) {
    width: var(--s-number-width, 148px);
  }

  .sampling-settings__control > :global(.sampling-settings__clear) {
    flex-shrink: 0;
    padding: 4px 10px;
  }

  @media (max-width: 640px) {
    .sampling-settings__note {
      padding: 14px 14px 0 29px;
    }

    .s-row > .sampling-settings__control {
      justify-self: start;
    }
  }
</style>
