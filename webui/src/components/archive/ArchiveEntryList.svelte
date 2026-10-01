<script>
  // The archive entries as rows: a selection Checkbox (only for entries that
  // can be deleted now), the label opening the entry, what it is and where it
  // belonged, and what happens next to it (its automatic deletion date, that
  // vBot never deletes it automatically, or an operation in progress).
  import Badge from '../ui/Badge.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import { archiveRow } from '$lib/archiveView.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  const noop = () => {};

  let {
    entries = [],
    retentionDays = undefined,
    agentNames = new Map(),
    projectNames = new Map(),
    selectedIds = new Set(),
    onToggle = noop,
    onOpen = noop,
  } = $props();

  let rows = $derived(
    entries.map((entry) =>
      archiveRow(entry, { retentionDays, agentNames, projectNames }),
    ),
  );
</script>

<ul class="archive-list" aria-label={t('archive.listLabel')}>
  {#each rows as row (row.id)}
    <li
      class="archive-row"
      class:archive-row--selected={selectedIds.has(row.id)}
      data-entry-id={row.id}
    >
      <Checkbox
        class="archive-row__select"
        checked={selectedIds.has(row.id)}
        disabled={!row.purgeable}
        ariaLabel={t('archive.row.select', { name: row.label })}
        onChange={(next) => onToggle(row.id, next)}
      />
      <button
        type="button"
        class="archive-row__main"
        onclick={() => onOpen(row.id)}
      >
        <span class="archive-row__title">
          <span class="archive-row__label">{row.label}</span>
          <Badge>{row.kind}</Badge>
        </span>
        <span class="archive-row__meta">
          {#if row.scope}<span>{row.scope}</span>{/if}
          {#if row.sessions}<span>{row.sessions}</span>{/if}
          <span>{row.archived}</span>
        </span>
      </button>
      <span class="archive-row__facts">
        {#if row.restoreHint}
          <span
            class="archive-row__fact tooltip-anchor"
            use:tooltip={row.restoreHint}
            ><Badge variant="warn">{t('archive.row.deleteOnly')}</Badge></span
          >
        {/if}
        {#if row.status}
          <span
            class="archive-row__fact archive-row__status archive-row__status--{row.statusTone}"
            class:tooltip-anchor={Boolean(row.statusHint)}
            use:tooltip={row.statusHint}>{row.status}</span
          >
        {/if}
      </span>
    </li>
  {/each}
</ul>

<style>
  .archive-list {
    display: grid;
    margin: 0;
    padding: 0;
    overflow: hidden;
    list-style: none;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--surface);
  }

  .archive-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 12px;
    padding: 6px 18px;
  }

  .archive-row + .archive-row {
    border-top: 1px solid var(--border);
  }

  .archive-row--selected {
    background: var(--surface-2);
  }

  .archive-row__main {
    display: grid;
    gap: 3px;
    min-width: 0;
    padding: 6px 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  /* Focus scrolls a row clear of the panel's sticky list head. */
  .archive-row__main,
  .archive-row :global(.archive-row__select) {
    scroll-margin-top: 64px;
  }

  .archive-row__main:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  .archive-row__main:hover .archive-row__label {
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .archive-row__title {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .archive-row__label {
    overflow: hidden;
    color: var(--text-hi);
    font-size: var(--fs-body-md);
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .archive-row__meta {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 14px;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }

  .archive-row__facts {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 6px 10px;
  }

  .archive-row__status {
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    white-space: nowrap;
  }

  .archive-row__status--info {
    color: var(--blue);
  }

  .archive-row__status--warn {
    color: var(--amber);
  }

  .tooltip-anchor {
    cursor: default;
  }

  @media (max-width: 640px) {
    .archive-row {
      grid-template-columns: auto minmax(0, 1fr);
      gap: 4px 12px;
      padding: 6px 12px;
    }

    .archive-row__facts {
      grid-column: 2;
      justify-content: flex-start;
      padding-bottom: 6px;
    }
  }
</style>
