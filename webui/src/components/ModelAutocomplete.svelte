<script>
  import AutocompletePopup from './composer/AutocompletePopup.svelte';
  import { modelAutocompleteRows } from './composer/autocomplete.js';
  import { t } from '$lib/i18n.js';
  import { holdModelFavoriteOrder } from '$lib/modelFavorites.svelte.js';
  import { tooltip } from '$lib/tooltip.js';
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

  // A changed favorite moves its row once the popup has closed.
  $effect(() => holdModelFavoriteOrder());

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
  {#snippet rowAction(option)}
    {#if option.favorite}
      <button
        type="button"
        class="model-autocomplete__favorite"
        class:model-autocomplete__favorite--active={option.favorite.active}
        aria-pressed={option.favorite.active}
        aria-label={option.favorite.active
          ? t('dropdown.removeFavorite')
          : t('dropdown.addFavorite')}
        use:tooltip={option.favorite.active
          ? t('dropdown.removeFavorite')
          : t('dropdown.addFavorite')}
        tabindex="-1"
        onmousedown={(event) => event.preventDefault()}
        onclick={() => option.favorite.toggle()}
      >
        <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
          <path
            d="M6 1.2 7.5 4.3l3.4.5-2.5 2.4.6 3.4L6 9l-3 1.6.6-3.4L1.1 4.8l3.4-.5Z"
          />
        </svg>
      </button>
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

  .model-autocomplete__favorite {
    position: absolute;
    top: 7px;
    right: 6px;
    display: grid;
    width: 22px;
    height: 22px;
    place-items: center;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    color: var(--text-lo);
    background: transparent;
    opacity: 0;
    transition: opacity 100ms ease;
  }

  .model-autocomplete__favorite svg {
    fill: none;
    stroke: currentcolor;
    stroke-linejoin: round;
    stroke-width: 1.1;
  }

  :global(.autocomplete-popup__row:hover) .model-autocomplete__favorite {
    opacity: 1;
  }

  .model-autocomplete__favorite--active {
    color: var(--accent);
    opacity: 1;
  }

  .model-autocomplete__favorite--active svg {
    fill: currentcolor;
  }

  @media (hover: none) {
    .model-autocomplete__favorite {
      opacity: 0.6;
    }
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
