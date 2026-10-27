<script>
  import { t } from '$lib/i18n.js';

  let {
    kind,
    items = [],
    keyOf,
    activeIndex = 0,
    loading = false,
    ariaLabel,
    eyebrow,
    row,
    headerDetail = undefined,
    footerLabel = '',
    onFooterAction = () => {},
    onSelect = () => {},
    onHover = () => {},
  } = $props();

  let container = $state(null);

  // Only the popup scrolls when keyboard navigation moves its active row.
  $effect(() => {
    activeIndex;
    items.length;
    const activeOption = container?.querySelector('[role="option"].active');
    if (!activeOption) return;
    const containerRect = container.getBoundingClientRect();
    const optionRect = activeOption.getBoundingClientRect();
    if (optionRect.top < containerRect.top) {
      container.scrollTop -= containerRect.top - optionRect.top;
    } else if (optionRect.bottom > containerRect.bottom) {
      container.scrollTop += optionRect.bottom - containerRect.bottom;
    }
  });

  export function selectActive() {
    const item = items[activeIndex];
    if (!item) return false;
    onSelect(item);
    return true;
  }
</script>

{#if items.length > 0 || loading}
  <div
    bind:this={container}
    class={`autocomplete-popup ${kind}-autocomplete`}
    role="listbox"
    aria-label={ariaLabel}
  >
    <div class={`autocomplete-popup__eyebrow ${kind}-autocomplete__eyebrow`}>
      {eyebrow}
      {@render headerDetail?.()}
    </div>
    {#if loading && items.length === 0}
      <div class={`autocomplete-popup__loading ${kind}-autocomplete__loading`}>
        {t('common.loading')}
      </div>
    {/if}
    {#each items as item, index (keyOf(item))}
      <button
        type="button"
        class={`autocomplete-popup__option autocomplete-popup__option--${kind} ${kind}-autocomplete__option`}
        class:active={index === activeIndex}
        class:ignored={kind === 'file' && item.ignored}
        role="option"
        aria-selected={index === activeIndex}
        onmouseenter={() => onHover(index)}
        onmousedown={(event) => event.preventDefault()}
        onclick={() => onSelect(item)}
      >
        {@render row(item)}
      </button>
    {/each}
    {#if items.length > 0 && footerLabel}
      <button
        type="button"
        class={`autocomplete-popup__footer ${kind}-autocomplete__footer`}
        onclick={() => onFooterAction()}
      >
        {footerLabel}
      </button>
    {/if}
  </div>
{/if}

<style>
  .autocomplete-popup {
    position: absolute;
    right: var(--composer-pad-x, 0px);
    bottom: calc(100% - 8px);
    left: var(--composer-pad-x, 0px);
    z-index: 20;
    display: flex;
    flex-direction: column;
    max-width: var(--chat-measure);
    max-height: min(320px, 45vh);
    margin-inline: auto;
    overflow-y: auto;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  }

  /* Rows keep their full height in the capped, scrolling column. */
  .autocomplete-popup > * {
    flex-shrink: 0;
  }

  .autocomplete-popup__eyebrow {
    display: flex;
    justify-content: space-between;
    padding: 8px 10px 6px;
    border-bottom: 1px solid var(--border);
    color: var(--text-lo);
    background: var(--surface);
    font-size: var(--fs-label-sm);
    font-weight: 600;
    line-height: 1;
  }

  .autocomplete-popup__loading {
    padding: 9px 10px;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    font-style: italic;
  }

  .autocomplete-popup__option {
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

  .autocomplete-popup__option:last-of-type {
    border-bottom: 0;
  }

  .autocomplete-popup__option:hover,
  .autocomplete-popup__option.active {
    color: var(--text-hi);
    background: var(--surface-3);
  }

  .autocomplete-popup__option.active {
    box-shadow: inset 2px 0 0 var(--accent);
  }

  .autocomplete-popup__option--model {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .autocomplete-popup__option--skill {
    display: grid;
    grid-template-columns: minmax(14ch, 0.2fr) minmax(0, 1fr);
    gap: var(--space-sm);
    padding: 9px 10px;
  }

  .autocomplete-popup__option--file {
    display: block;
    overflow: hidden;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .autocomplete-popup__footer {
    padding: 7px 10px;
    border: 0;
    border-top: 1px solid var(--border);
    color: var(--text-lo);
    background: var(--surface);
    font-family: var(--font-ui);
    font-size: var(--fs-label-sm);
    text-align: left;
    transition: background-color 120ms ease;
  }

  .autocomplete-popup__footer:hover {
    color: var(--text-hi);
    background: var(--surface-3);
  }

  @media (max-width: 640px) {
    .autocomplete-popup {
      right: 14px;
      left: 14px;
    }

    .autocomplete-popup__option--skill {
      grid-template-columns: 1fr;
      gap: 3px;
    }
  }
</style>
