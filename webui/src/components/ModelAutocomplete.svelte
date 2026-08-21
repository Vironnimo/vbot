<script>
  import { t } from '$lib/i18n.js';

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

  let normalizedOptions = $derived(normalizeOptions(options));
  let matchingOptions = $derived(matchOptions(normalizedOptions, query));
  let containerElement = $state(null);

  // Keep the active option visible inside the scrollable popup when keyboard
  // navigation or list changes move it out of the visible area. Only the
  // container is scrolled — the page and timeline stay put.
  $effect(() => {
    // Track reactive dependencies so the effect re-runs on navigation and
    // list changes.
    activeIndex;
    matchingOptions.length;

    const container = containerElement;
    if (!container) {
      return;
    }

    const activeOption = container.querySelector(
      '.model-autocomplete__option.active',
    );
    if (!activeOption) {
      return;
    }

    const containerRect = container.getBoundingClientRect();
    const optionRect = activeOption.getBoundingClientRect();
    if (optionRect.top < containerRect.top) {
      container.scrollTop -= containerRect.top - optionRect.top;
    } else if (optionRect.bottom > containerRect.bottom) {
      container.scrollTop += optionRect.bottom - containerRect.bottom;
    }
  });

  export function hasMatches() {
    return matchingOptions.length > 0;
  }

  export function selectActive() {
    const option = matchingOptions[activeIndex] ?? matchingOptions[0];

    if (option) {
      onSelect(option);
      return true;
    }

    return false;
  }

  function normalizeOptions(items) {
    return items
      .filter(
        (option) =>
          typeof option?.value === 'string' && option.value.length > 0,
      )
      .map((option) => ({
        value: option.value,
        label: option.label ?? option.value,
        secondaryLabel: option.secondaryLabel ?? '',
        searchText:
          `${option.label ?? option.value} ${option.secondaryLabel ?? ''}`
            .trim()
            .toLowerCase(),
      }));
  }

  function matchOptions(items, value) {
    const normalizedQuery = value.trim().toLowerCase();

    if (!normalizedQuery) {
      return items;
    }

    return items.filter((option) =>
      option.searchText.includes(normalizedQuery),
    );
  }
</script>

{#if matchingOptions.length > 0 || loading}
  <div
    bind:this={containerElement}
    class="model-autocomplete"
    role="listbox"
    aria-label={t('modelAutocomplete.label', 'Model suggestions')}
  >
    <div class="model-autocomplete__eyebrow">
      {t('modelAutocomplete.eyebrow', 'models')}
    </div>
    {#if loading && matchingOptions.length === 0}
      <div class="model-autocomplete__loading">
        {t('common.loading', 'Loading…')}
      </div>
    {/if}
    {#each matchingOptions as option, index (option.value)}
      <button
        type="button"
        class="model-autocomplete__option"
        class:active={index === activeIndex}
        role="option"
        aria-selected={index === activeIndex}
        onmouseenter={() => onHover(index)}
        onmousedown={(event) => event.preventDefault()}
        onclick={() => onSelect(option)}
      >
        <span class="model-autocomplete__label">{option.label}</span>
        {#if option.secondaryLabel}
          <span class="model-autocomplete__meta">{option.secondaryLabel}</span>
        {/if}
      </button>
    {/each}
    {#if matchingOptions.length > 0 && footerLabel}
      <button
        type="button"
        class="model-autocomplete__footer"
        onclick={() => onFooterAction()}
      >
        {footerLabel}
      </button>
    {/if}
  </div>
{/if}

<style>
  .model-autocomplete {
    position: absolute;
    right: 20px;
    bottom: calc(100% - 8px);
    left: 20px;
    z-index: 20;
    display: flex;
    flex-direction: column;
    max-width: var(--chat-measure);
    max-height: min(320px, 45vh);
    margin-inline: auto;
    overflow-y: auto;
    border: 1px solid var(--accent-30);
    border-radius: var(--r-md);
    background: var(--surface-2);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  }

  .model-autocomplete__eyebrow {
    padding: 8px 10px 6px;
    border-bottom: 1px solid var(--border);
    color: var(--text-lo);
    background: var(--surface);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    line-height: 1;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .model-autocomplete__loading {
    padding: 9px 10px;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: 12.5px;
    font-style: italic;
  }

  .model-autocomplete__option {
    display: flex;
    flex-direction: column;
    gap: 2px;
    width: 100%;
    padding: 7px 10px;
    border: 0;
    border-bottom: 1px solid var(--border);
    color: var(--text-med);
    background: transparent;
    text-align: left;
    transition:
      background-color 120ms ease,
      color 120ms ease;
  }

  .model-autocomplete__option:last-of-type {
    border-bottom: 0;
  }

  .model-autocomplete__option:hover,
  .model-autocomplete__option.active {
    color: var(--text-hi);
    background: var(--surface-3);
  }

  .model-autocomplete__option.active .model-autocomplete__label {
    color: var(--accent);
  }

  .model-autocomplete__label {
    overflow: hidden;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .model-autocomplete__meta {
    overflow: hidden;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: 11.5px;
    line-height: 1.3;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .model-autocomplete__footer {
    flex-shrink: 0;
    padding: 7px 10px;
    border: 0;
    border-top: 1px solid var(--border);
    color: var(--text-lo);
    background: var(--surface);
    font-family: var(--font-ui);
    font-size: 12px;
    text-align: left;
    transition: background-color 120ms ease;
  }

  .model-autocomplete__footer:hover {
    color: var(--text-hi);
    background: var(--surface-3);
  }

  @media (max-width: 640px) {
    .model-autocomplete {
      right: 14px;
      left: 14px;
    }
  }
</style>
