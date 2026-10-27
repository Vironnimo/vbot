<script>
  import AutocompletePopup from './composer/AutocompletePopup.svelte';
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

  let popup = $state(null);

  export function matchCount() {
    return candidates.length;
  }

  export function hasMatches() {
    return candidates.length > 0;
  }

  export function selectActive() {
    return popup?.selectActive() ?? false;
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

<AutocompletePopup
  bind:this={popup}
  kind="file"
  items={candidates}
  keyOf={(candidate) => candidate.path}
  {activeIndex}
  {loading}
  ariaLabel={t('fileAutocomplete.label')}
  eyebrow={t('fileAutocomplete.eyebrow')}
  {onSelect}
  {onHover}
>
  {#snippet headerDetail()}
    {#if truncated}
      <span class="file-autocomplete__truncated"
        >{t('fileAutocomplete.truncated')}</span
      >
    {/if}
  {/snippet}
  {#snippet row(candidate)}
    {@const parts = splitPath(candidate.path)}
    <!-- Adjacent spans: no space between the folder and the name. -->
    <span class="file-autocomplete__directory">{parts.directory}</span><span
      class="file-autocomplete__filename"
      class:ignored={candidate.ignored}
      >{parts.filename}{candidate.kind === 'directory' ? '/' : ''}</span
    >
    {#if candidate.ignored}
      <span class="file-autocomplete__ignored">
        {t('fileAutocomplete.ignored')}
      </span>
    {/if}
  {/snippet}
</AutocompletePopup>

<style>
  .file-autocomplete__truncated {
    font-weight: 400;
    text-transform: none;
  }

  .file-autocomplete__directory {
    color: var(--text-lo);
  }

  .file-autocomplete__filename {
    color: var(--text-hi);
  }

  .file-autocomplete__ignored {
    margin-left: 8px;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: var(--fs-label-sm);
    font-style: italic;
  }

  .file-autocomplete__filename.ignored {
    color: var(--text-lo);
  }
</style>
