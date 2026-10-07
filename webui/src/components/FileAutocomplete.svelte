<script>
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  // `candidates` are the rows to show, each `{ path, kind, ignored }`, already
  // ranked and capped by the composer's picker.
  let {
    candidates = [],
    truncated = false,
    loading = false,
    activeIndex = 0,
    onSelect = noop,
    onHover = noop,
  } = $props();

  let containerElement = $state(null);

  // Keep the active option visible inside the scrollable popup when keyboard
  // navigation or list changes move it out of the visible area. Only the
  // container is scrolled — the page and timeline stay put.
  $effect(() => {
    activeIndex;
    candidates.length;

    const container = containerElement;
    if (!container) {
      return;
    }

    const activeOption = container.querySelector(
      '.file-autocomplete__option.active',
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

  export function matchCount() {
    return candidates.length;
  }

  export function hasMatches() {
    return candidates.length > 0;
  }

  export function selectActive() {
    const candidate = candidates[activeIndex];

    if (candidate) {
      onSelect(candidate);
      return true;
    }

    return false;
  }

  function splitPath(path) {
    const separatorIndex = path.lastIndexOf('/');
    if (separatorIndex === -1) {
      return { directory: '', filename: path };
    }
    return {
      directory: path.slice(0, separatorIndex + 1),
      filename: path.slice(separatorIndex + 1),
    };
  }
</script>

{#if candidates.length > 0 || loading}
  <div
    bind:this={containerElement}
    class="file-autocomplete"
    role="listbox"
    aria-label={t('fileAutocomplete.label')}
  >
    <div class="file-autocomplete__eyebrow">
      {t('fileAutocomplete.eyebrow')}
      {#if truncated}
        <span class="file-autocomplete__truncated">
          {t('fileAutocomplete.truncated')}
        </span>
      {/if}
    </div>
    {#if loading && candidates.length === 0}
      <div class="file-autocomplete__loading">
        {t('common.loading')}
      </div>
    {/if}
    {#each candidates as candidate, index (candidate.path)}
      {@const parts = splitPath(candidate.path)}
      <button
        type="button"
        class="file-autocomplete__option"
        class:active={index === activeIndex}
        class:ignored={candidate.ignored}
        role="option"
        aria-selected={index === activeIndex}
        onmouseenter={() => onHover(index)}
        onmousedown={(event) => event.preventDefault()}
        onclick={() => onSelect(candidate)}
      >
        <!-- Adjacent spans: no space between the folder and the name. -->
        <span class="file-autocomplete__directory">{parts.directory}</span><span
          class="file-autocomplete__filename"
          >{parts.filename}{candidate.kind === 'directory' ? '/' : ''}</span
        >
        {#if candidate.ignored}
          <span class="file-autocomplete__ignored">
            {t('fileAutocomplete.ignored')}
          </span>
        {/if}
      </button>
    {/each}
  </div>
{/if}

<style>
  .file-autocomplete {
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

  /* The popup scrolls; its rows must keep their full height instead of
     shrinking to fit the capped column (rows with overflow: hidden would
     otherwise collapse onto their padding and clip their text). */
  .file-autocomplete > * {
    flex-shrink: 0;
  }

  .file-autocomplete__eyebrow {
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

  .file-autocomplete__truncated {
    font-weight: 400;
    text-transform: none;
  }

  .file-autocomplete__loading {
    padding: 9px 10px;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    font-style: italic;
  }

  .file-autocomplete__option {
    display: block;
    overflow: hidden;
    width: 100%;
    padding: 7px 10px;
    border: 0;
    border-bottom: 1px solid var(--border);
    color: var(--text-med);
    background: transparent;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    line-height: 1.4;
    text-align: left;
    text-overflow: ellipsis;
    white-space: nowrap;
    transition:
      background-color 120ms ease,
      color 120ms ease;
  }

  .file-autocomplete__option:last-child {
    border-bottom: 0;
  }

  .file-autocomplete__option:hover,
  .file-autocomplete__option.active {
    color: var(--text-hi);
    background: var(--surface-3);
  }

  .file-autocomplete__directory {
    color: var(--text-lo);
  }

  .file-autocomplete__filename {
    color: var(--text-hi);
  }

  .file-autocomplete__option.active {
    box-shadow: inset 2px 0 0 var(--accent);
  }

  /* Ignored entries (reached by browsing a folder) stay choosable but recede. */
  .file-autocomplete__option.ignored .file-autocomplete__filename {
    color: var(--text-lo);
  }

  .file-autocomplete__ignored {
    margin-left: 8px;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: var(--fs-label-sm);
    font-style: italic;
  }

  @media (max-width: 640px) {
    .file-autocomplete {
      right: 14px;
      left: 14px;
    }
  }
</style>
