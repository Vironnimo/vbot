<script>
  import DebugJsonValue from './DebugJsonValue.svelte';
  import { t } from '$lib/i18n.js';
  let { value, name = '', depth = 0 } = $props();
  let open = $state(false);
  let visibleCount = $state(50);
  let entries = $derived(
    value !== null && typeof value === 'object' ? Object.entries(value) : null,
  );
  let summary = $derived(
    entries
      ? Array.isArray(value)
        ? `[${entries.length}]`
        : `{${entries.length}}`
      : '',
  );
  let label = $derived(
    entries && !Array.isArray(value)
      ? [value.role, value.type, value.name ?? value.function?.name]
          .filter((item) => typeof item === 'string')
          .join(' · ')
      : '',
  );
</script>

{#if entries && depth < 30}
  <details bind:open class="json-node">
    <summary
      ><span class="json-key">{name}</span>
      <span class="json-count">{summary}</span><span class="json-label"
        >{label}</span
      ></summary
    >
    {#if open}
      <div class="json-children">
        {#each entries.slice(0, visibleCount) as [key, child] (key)}
          <DebugJsonValue value={child} name={key} depth={depth + 1} />
        {/each}
        {#if entries.length > visibleCount}
          <button type="button" onclick={() => (visibleCount += 50)}
            >{t(
              'debug.showMoreFields',
              'Show next fields ({count} remaining)',
              { count: entries.length - visibleCount },
            )}</button
          >
        {/if}
      </div>
    {/if}
  </details>
{:else}
  <div class="json-leaf">
    <span class="json-key">{name}</span>
    <pre class:json-string={typeof value === 'string'}>{typeof value ===
      'string'
        ? value || '""'
        : JSON.stringify(value, null, 2)}</pre>
  </div>
{/if}

<style>
  .json-node,
  .json-leaf {
    border-bottom: 1px solid var(--border);
  }
  summary {
    display: list-item;
    cursor: pointer;
    padding: 10px 4px;
    overflow-wrap: anywhere;
    color: var(--text-med);
  }
  summary:focus-visible,
  button:focus-visible {
    outline: 2px solid var(--accent);
  }
  .json-key {
    color: var(--text-hi);
    font: var(--fs-mono-body) var(--font-mono);
  }
  .json-count {
    color: var(--text-lo);
    margin-left: 8px;
    font: var(--fs-mono-body) var(--font-mono);
  }
  .json-label {
    margin-left: 12px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .json-children {
    margin-left: 10px;
    padding-left: 14px;
    border-left: 1px solid var(--border-2);
  }
  .json-leaf {
    padding: 10px 4px;
  }
  pre {
    margin: 6px 0 2px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--text-med);
    font: var(--fs-mono-body)/1.7 var(--font-mono);
  }
  pre.json-string {
    color: var(--text-hi);
    font: var(--fs-body-lg)/1.7 var(--font-ui);
  }
  button {
    min-height: 32px;
    margin: 12px 0;
    padding: 6px 12px;
    background: var(--surface-2);
    color: var(--text-hi);
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    font-size: var(--fs-body-sm);
    font-weight: 500;
    cursor: pointer;
  }
  button:hover {
    background: var(--surface-3);
  }
</style>
