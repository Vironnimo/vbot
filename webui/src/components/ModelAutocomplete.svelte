<script>
  import AutocompletePopup from './composer/AutocompletePopup.svelte';
  import { modelAutocompleteRows } from './composer/autocomplete.js';
  import { t } from '$lib/i18n.js';
  import OptionMarker from './OptionMarker.svelte';

  const noop = () => {};

  let {
    options = [],
    query = '',
    activeIndex = 0,
    loading = false,
    footerLabel = '',
    onFooterAction = noop,
    onSelect = noop,
    onHover = noop,
  } = $props();

  let matchingOptions = $derived(modelAutocompleteRows(options, query));
  let popup = $state(null);

  export function hasMatches() {
    return matchingOptions.length > 0;
  }

  export function selectActive() {
    return popup?.selectActive() ?? false;
  }
</script>

<AutocompletePopup
  bind:this={popup}
  kind="model"
  items={matchingOptions}
  keyOf={(option) => option.value}
  {activeIndex}
  {loading}
  {footerLabel}
  {onFooterAction}
  ariaLabel={t('modelAutocomplete.label')}
  eyebrow={t('modelAutocomplete.eyebrow')}
  {onSelect}
  {onHover}
>
  {#snippet row(option)}
    <span class="model-autocomplete__head">
      <span class="model-autocomplete__label"
        >{#if option.labelLead}<span class="model-autocomplete__lead"
            >{option.labelLead}</span
          >{option.label.slice(
            option.labelLead.length,
          )}{:else}{option.label}{/if}</span
      >
      {#if option.marker}
        <OptionMarker
          label={option.marker.label}
          tooltip={option.marker.tooltip}
        />
      {/if}
    </span>
    {#if option.secondaryLabel}
      <span class="model-autocomplete__meta">{option.secondaryLabel}</span>
    {/if}
  {/snippet}
</AutocompletePopup>

<style>
  .model-autocomplete__head {
    display: flex;
    align-items: center;
    gap: 4px;
    min-width: 0;
  }

  .model-autocomplete__label {
    overflow: hidden;
    color: var(--text-hi);
    font-size: var(--fs-body-sm);
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .model-autocomplete__lead {
    color: var(--text-lo);
  }

  .model-autocomplete__meta {
    overflow: hidden;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: var(--fs-label-sm);
    line-height: 1.3;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
