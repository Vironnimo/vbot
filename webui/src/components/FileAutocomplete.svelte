<script>
  import { fuzzyFilterFiles } from '$lib/fileMentions.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  // Rendered matches are capped: with local fuzzy ranking, everything relevant
  // sits at the top and a longer tail only costs scrolling.
  const MAX_RENDERED_MATCHES = 50;

  let {
    files = [],
    query = '',
    truncated = false,
    loading = false,
    activeIndex = 0,
    onSelect = noop,
    onHover = noop,
  } = $props();

  let matchingFiles = $derived(
    fuzzyFilterFiles(files, query, MAX_RENDERED_MATCHES),
  );

  export function matchCount() {
    return matchingFiles.length;
  }

  export function hasMatches() {
    return matchingFiles.length > 0;
  }

  export function selectActive() {
    const file = matchingFiles[activeIndex] ?? matchingFiles[0];

    if (file) {
      onSelect(file);
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

{#if matchingFiles.length > 0 || loading}
  <div
    class="file-autocomplete"
    role="listbox"
    aria-label={t('fileAutocomplete.label', 'File suggestions')}
  >
    <div class="file-autocomplete__eyebrow">
      {t('fileAutocomplete.eyebrow', 'files')}
      {#if truncated}
        <span class="file-autocomplete__truncated">
          {t('fileAutocomplete.truncated', 'list truncated — keep typing')}
        </span>
      {/if}
    </div>
    {#if loading && matchingFiles.length === 0}
      <div class="file-autocomplete__loading">
        {t('common.loading', 'Loading…')}
      </div>
    {/if}
    {#each matchingFiles as file, index (file)}
      {@const parts = splitPath(file)}
      <button
        type="button"
        class="file-autocomplete__option"
        class:active={index === activeIndex}
        role="option"
        aria-selected={index === activeIndex}
        onmouseenter={() => onHover(index)}
        onmousedown={(event) => event.preventDefault()}
        onclick={() => onSelect(file)}
      >
        {#if parts.directory}
          <span class="file-autocomplete__directory">{parts.directory}</span>
        {/if}
        <span class="file-autocomplete__filename">{parts.filename}</span>
      </button>
    {/each}
  </div>
{/if}

<style>
  .file-autocomplete {
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

  .file-autocomplete__eyebrow {
    display: flex;
    justify-content: space-between;
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

  .file-autocomplete__truncated {
    text-transform: none;
    letter-spacing: normal;
  }

  .file-autocomplete__loading {
    padding: 9px 10px;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: 12.5px;
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
    font-size: 12px;
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

  .file-autocomplete__option.active .file-autocomplete__filename {
    color: var(--accent);
  }

  @media (max-width: 640px) {
    .file-autocomplete {
      right: 14px;
      left: 14px;
    }
  }
</style>
