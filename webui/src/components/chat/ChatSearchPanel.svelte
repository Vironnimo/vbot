<script>
  import { tick } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { isImeComposing } from '$lib/keyboard.js';
  import { formatAbsoluteTime } from '$lib/timeText.js';
  import Button from '../ui/Button.svelte';

  let { search, agentName, narrow, onChoose, onClose } = $props();
  let input = $state(null);
  let results = $state(null);
  let back = $state(null);
  let compact = $state(false);
  const slim = $derived(narrow && compact && !!search.selected);
  const index = $derived(
    search.hits.findIndex((hit) => hit.sequence === search.selected?.sequence),
  );
  const count = $derived(search.hits.length);
  const countText = $derived(
    !search.complete
      ? t('chat.search.firstMatches', { count })
      : count === 1
        ? t('chat.search.oneMatch')
        : t('chat.search.matches', { count }),
  );

  export function fieldFocused() {
    return !!input && document.activeElement === input;
  }
  export async function focusField() {
    compact = false;
    await tick();
    input?.focus({ preventScroll: true });
  }
  $effect(() => {
    input?.focus({ preventScroll: true });
  });
  $effect(() => {
    if (index >= 0)
      results?.children[index]?.scrollIntoView?.({ block: 'nearest' });
  });

  async function choose(hit) {
    if (hit && (await onChoose(hit)) && narrow) {
      compact = true;
      await tick();
      back?.focus({ preventScroll: true });
    }
  }
  function move(delta) {
    if (!count) return;
    const next =
      index < 0 ? (delta > 0 ? 0 : count - 1) : (index + delta + count) % count;
    void choose(search.hits[next]);
  }
  function keydown(event) {
    if (isImeComposing(event)) return;
    if (event.key === 'Enter') {
      event.preventDefault();
      void search.refresh(true);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      move(event.key === 'ArrowDown' ? 1 : -1);
    }
  }
  function segments(hit) {
    const chars = Array.from(hit.snippet);
    const parts = [];
    let end = 0;
    for (const [start, after] of hit.ranges) {
      if (end < start) parts.push({ text: chars.slice(end, start).join('') });
      parts.push({ text: chars.slice(start, after).join(''), marked: true });
      end = after;
    }
    if (end < chars.length) parts.push({ text: chars.slice(end).join('') });
    return parts;
  }
</script>

<aside class="chat-search" class:slim aria-label={t('chat.search.title')}>
  {#if slim}
    <strong class="query">{search.query}</strong>
    <span class="position"
      >{t('chat.search.position', { position: index + 1, count })}</span
    >
    <Button
      variant="tertiary"
      icon
      ariaLabel={t('chat.search.previous')}
      onClick={() => move(-1)}>↑</Button
    >
    <Button
      variant="tertiary"
      icon
      ariaLabel={t('chat.search.next')}
      onClick={() => move(1)}>↓</Button
    >
    <button class="back" bind:this={back} onclick={focusField}
      >{t('chat.search.back')}</button
    >
  {:else}
    <h3>{t('chat.search.title')}</h3>
  {/if}
  <Button
    variant="tertiary"
    icon
    class="search-close"
    ariaLabel={t('common.close')}
    onClick={onClose}>×</Button
  >
  {#if !slim}
    <input
      bind:this={input}
      type="search"
      maxlength="256"
      aria-label={t('chat.search.title')}
      placeholder={t('common.search')}
      value={search.query}
      oncompositionend={(event) => search.setQuery(event.currentTarget.value)}
      oninput={(event) => {
        if (!event.isComposing) search.setQuery(event.currentTarget.value);
      }}
      onkeydown={keydown}
    />
  {/if}
  <div class="status" role="status" aria-live="polite">
    {search.error ||
      (search.jumping
        ? t('chat.search.loading')
        : search.searching
          ? t('chat.search.searching')
          : !slim && search.query.trim()
            ? countText
            : '')}
  </div>
  {#if !slim}
    <ul class="results" bind:this={results}>
      {#each search.hits as hit (hit.sequence)}
        <li>
          <button
            class="result"
            aria-current={hit.sequence === search.selected?.sequence
              ? 'true'
              : undefined}
            onkeydown={(event) => {
              if (event.key === 'Enter' && isImeComposing(event))
                event.preventDefault();
            }}
            onclick={() => choose(hit)}
          >
            <span class="who"
              >{hit.role === 'user'
                ? t('chat.role.user')
                : agentName || t('chat.role.assistant')}</span
            >
            <time datetime={hit.timestamp}
              >{formatAbsoluteTime(hit.timestamp)}</time
            >
            <span class="snippet"
              >{#each segments(hit) as part, index (index)}{#if part.marked}<mark
                    >{part.text}</mark
                  >{:else}{part.text}{/if}{/each}</span
            >
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</aside>

<style>
  .chat-search {
    position: absolute;
    z-index: 31;
    inset: 0 var(--chat-scrollbar-width, 0px) 0 auto;
    width: var(--chat-activity-width);
    background: var(--bg);
    border-left: 1px solid var(--border);
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-rows: auto auto auto minmax(0, 1fr);
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-sm);
    color: var(--text-hi);
  }
  h3 {
    font-size: var(--fs-heading-sm);
    margin: 0;
  }
  input,
  .results,
  .status {
    grid-column: 1/-1;
  }
  input {
    width: 100%;
    min-width: 0;
    padding: 7px 9px;
    background: var(--surface);
    color: inherit;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    font: inherit;
  }
  .status,
  time,
  .position {
    font-size: var(--fs-label-sm);
    color: var(--text-med);
  }
  .results {
    margin: 0;
    padding: 0;
    list-style: none;
    align-self: stretch;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .result {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    text-align: start;
    width: 100%;
    gap: 4px;
    padding: 9px;
    border: 0;
    border-bottom: 1px solid var(--border);
    background: transparent;
    color: inherit;
    font: inherit;
    cursor: pointer;
  }
  .result:hover,
  .result[aria-current] {
    background: var(--surface-2);
  }
  .result[aria-current] {
    box-shadow: inset 2px 0 var(--accent);
  }
  .who,
  .query {
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .snippet {
    grid-column: 1/-1;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    overflow-wrap: anywhere;
  }
  mark {
    background: var(--amber);
    color: var(--bg);
  }
  .back {
    background: none;
    border: 0;
    color: var(--accent);
    cursor: pointer;
    font: inherit;
  }
  @container chat-surface (max-width:739.99px) {
    .chat-search {
      inset: 0;
      width: auto;
      border: 0;
    }
    .slim {
      bottom: auto;
      grid-template-columns: minmax(0, 1fr) repeat(5, auto);
      grid-template-rows: auto;
      gap: 3px;
      border-bottom: 1px solid var(--border);
    }
    .slim .status:empty {
      display: none;
    }
  }
</style>
