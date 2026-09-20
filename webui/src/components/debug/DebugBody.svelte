<script>
  import { tick } from 'svelte';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import {
    rawBodyText,
    formattedBodyText,
    hasParseableBody,
    bodyMatchOffsets,
  } from '$lib/debugView.js';
  import Toggle from '../ui/Toggle.svelte';
  import TabList from '../ui/TabList.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import Button from '../ui/Button.svelte';
  import DebugJsonValue from './DebugJsonValue.svelte';

  let { body = '', idPrefix = 'debug-body' } = $props();
  let view = $state('readable');
  let wrap = $state(true);
  let query = $state('');
  let matchIndex = $state(0);
  let activeMatch = $state(null);
  let visibleCount = $state(50);
  let raw = $derived(rawBodyText(body));
  let parseable = $derived(hasParseableBody(raw));
  let parsed = $derived(parseable ? JSON.parse(raw) : null);
  let entries = $derived(
    parsed !== null && typeof parsed === 'object'
      ? Object.entries(parsed)
      : null,
  );
  let effectiveView = $derived(parseable ? view : 'raw');
  let text = $derived(
    effectiveView === 'formatted' ? formattedBodyText(raw) : raw,
  );
  let matches = $derived(bodyMatchOffsets(text, query));
  let match = $derived(matches.length ? matchIndex % matches.length : 0);
  let offset = $derived(matches[match] ?? -1);
  let bytes = $derived(new TextEncoder().encode(raw).length);
  let tabs = $derived([
    { id: 'readable', label: t('debug.readable', 'Readable') },
    { id: 'formatted', label: t('debug.formatted', 'Formatted JSON') },
    { id: 'raw', label: t('debug.streamRaw', 'Raw') },
  ]);

  async function find(event) {
    query = event.currentTarget.value;
    matchIndex = 0;
    if (view === 'readable') view = 'raw';
    await revealMatch();
  }
  async function revealMatch() {
    await tick();
    activeMatch?.scrollIntoView?.({ block: 'center', inline: 'nearest' });
  }
  function nextMatch(direction) {
    if (!matches.length) return;
    matchIndex = (match + direction + matches.length) % matches.length;
    void revealMatch();
  }
  function changeView(value) {
    view = value;
    query = '';
    matchIndex = 0;
  }
</script>

<section class="debug-body debug-view__detail-section">
  <div class="body-toolbar">
    <h4 class="debug-view__detail-heading">{t('debug.requestBody', 'Body')}</h4>
    <span class="body-size"
      >{t('debug.bodyBytes', '{count} bytes', {
        count: new Intl.NumberFormat(activeLocaleTag()).format(bytes),
      })}</span
    >
    {#if parseable}
      <TabList
        items={tabs}
        value={effectiveView}
        ariaLabel={t('debug.bodyView', 'Body view')}
        appearance="segmented"
        density="compact"
        {idPrefix}
        class="debug-view__body-tab-list"
        onChange={changeView}
      />
    {/if}
    <CopyButton text={raw} label={t('debug.copyRawBody', 'Copy raw body')} />
  </div>
  <div class="body-search">
    <input
      type="search"
      value={query}
      oninput={find}
      onkeydown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          nextMatch(event.shiftKey ? -1 : 1);
        }
      }}
      aria-label={t('debug.findBody', 'Find in body (case-sensitive)')}
      placeholder={t('debug.findBody', 'Find in body (case-sensitive)')}
    />
    {#if query}
      <span aria-live="polite"
        >{t('debug.matchCount', '{current} / {total}', {
          current: matches.length ? match + 1 : 0,
          total: matches.length,
        })}</span
      >
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('debug.previousMatch', 'Previous match')}
        disabled={!matches.length}
        onClick={() => nextMatch(-1)}>↑</Button
      >
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('debug.nextMatch', 'Next match')}
        disabled={!matches.length}
        onClick={() => nextMatch(1)}>↓</Button
      >
    {/if}
    {#if effectiveView !== 'readable'}
      <label class="body-wrap"
        ><Toggle
          size="sm"
          checked={wrap}
          onChange={(value) => (wrap = value)}
          ariaLabel={t('debug.wrapLines', 'Wrap lines')}
        />{t('debug.wrapLines', 'Wrap lines')}</label
      >
    {/if}
  </div>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (The scrollable payload must be keyboard reachable, including non-JSON bodies.) -->
  <div
    aria-label={parseable ? undefined : t('debug.requestBody', 'Body')}
    class="body-content"
    role={parseable ? 'tabpanel' : 'region'}
    id={parseable ? `${idPrefix}-panel-${effectiveView}` : undefined}
    aria-labelledby={parseable ? `${idPrefix}-tab-${effectiveView}` : undefined}
    tabindex="0"
  >
    {#if !raw}
      <p class="body-note">{t('debug.emptyBody', 'No body captured.')}</p>
    {:else if effectiveView === 'readable'}
      <p class="body-note">
        {t(
          'debug.readableHint',
          'JSON reading view. Strings show their line breaks; Raw preserves the captured text.',
        )}
      </p>
      {#if entries}
        {#each entries.slice(0, visibleCount) as [key, value] (key)}
          <DebugJsonValue {value} name={key} />
        {/each}
        {#if entries.length > visibleCount}
          <Button variant="tertiary" onClick={() => (visibleCount += 50)}
            >{t(
              'debug.showMoreFields',
              'Show next fields ({count} remaining)',
              { count: entries.length - visibleCount },
            )}</Button
          >
        {/if}
      {:else}
        <DebugJsonValue value={parsed} />
      {/if}
    {:else}
      <pre
        class="debug-view__code-block"
        class:debug-view__code-block--raw={effectiveView === 'raw'}
        class:debug-view__code-block--formatted={effectiveView === 'formatted'}
        class:body-wrapped={wrap}>{#if offset >= 0}{text.slice(0, offset)}<mark
            bind:this={activeMatch}
            >{text.slice(offset, offset + query.length)}</mark
          >{text.slice(offset + query.length)}{:else}{text}{/if}</pre>
    {/if}
  </div>
</section>

<style>
  .debug-body {
    display: flex;
    flex-direction: column;
    min-height: 0;
    flex: 1;
  }
  .body-toolbar,
  .body-search {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 18px;
    flex-wrap: wrap;
    border-bottom: 1px solid var(--border);
  }
  .body-toolbar h4 {
    font-size: var(--fs-body-md);
    font-weight: 500;
  }
  .body-size {
    color: var(--text-med);
    font: var(--fs-mono-xs) var(--font-mono);
    margin-right: auto;
  }
  .body-search {
    gap: 6px;
    padding-block: 8px;
  }
  .body-search > input {
    min-width: 120px;
    flex: 1;
    padding: 6px 0;
    border: 0;
    background: transparent;
    color: var(--text-hi);
    font: var(--fs-body-sm) var(--font-ui);
  }
  .body-search input::placeholder {
    color: var(--text-med);
  }
  .body-search > span,
  .body-wrap {
    color: var(--text-med);
    font-size: var(--fs-label-sm);
  }
  .body-wrap {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .body-content {
    min-height: 0;
    flex: 1;
    overflow: auto;
    padding: 16px 20px 28px;
    background: var(--bg);
    user-select: text;
  }
  .body-note {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.6;
    margin: 0 0 14px;
  }
  pre {
    margin: 0;
    color: var(--text-hi);
    font: var(--fs-body-sm)/1.75 var(--font-mono);
    white-space: pre;
    tab-size: 2;
  }
  pre.body-wrapped {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  mark {
    color: var(--bg);
    background: var(--accent);
    outline: 2px solid var(--accent);
  }
  input:focus-visible,
  .body-content:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
</style>
