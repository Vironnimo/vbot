<script>
  import Checkbox from '../ui/Checkbox.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import ToolReadinessNotice from '../ui/ToolReadinessNotice.svelte';
  import { groupToolCatalog } from '$lib/toolAccess.js';
  import { t } from '$lib/i18n.js';
  import { floatingHoverCard } from '$lib/tooltip.js';

  // Presentation only: each owner supplies its policy projection and mutations.
  let {
    items = [],
    disabled = false,
    onToggle,
    onToggleGroup,
    toggleLabel = (tool) => tool.name,
    toolbar,
    details,
    onOpenExtensions,
  } = $props();
  const catalogId = $props.id();
  let search = $state('');
  const order = ['files', 'execution', 'web', 'sessions', 'skills', 'media'];
  let allGroups = $derived(
    groupToolCatalog(items).sort((a, b) => rank(a.id) - rank(b.id)),
  );
  let groups = $derived(
    allGroups
      .map((group) => ({
        ...group,
        members: group.members.filter((tool) =>
          [tool.name, tool.description, familyLabel(group)]
            .join(' ')
            .toLocaleLowerCase()
            .includes(search.trim().toLocaleLowerCase()),
        ),
      }))
      .filter((group) => group.members.length),
  );
  let enabledCount = $derived(items.filter((tool) => tool.allowed).length);

  function rank(id) {
    const i = order.indexOf(id);
    return i < 0 ? (id ? 6 : 7) : i;
  }
  function familyLabel(group) {
    const labels = {
      files: t('toolAccess.family.files', 'Files'),
      execution: t('toolAccess.family.execution', 'Execution'),
      web: t('toolAccess.family.web', 'Web'),
      sessions: t('toolAccess.family.sessions', 'Sessions'),
      skills: t('toolAccess.family.skills', 'Skills'),
      media: t('toolAccess.family.media', 'Media'),
    };
    return !group.family
      ? t('toolAccess.family.individual', 'Individual Tools')
      : (labels[group.id] ??
          group.members.find((tool) => tool.family_label)?.family_label ??
          group.id);
  }
  // One short state per row, most important first; the full story lives in
  // the hover card. A family whose Tools are all unavailable says so once in
  // its header instead.
  function toolStatus(tool, group) {
    if (tool.ready === false && !familyUnavailable(group))
      return {
        text: t('toolAccess.unavailable', 'Currently unavailable'),
        unavailable: true,
      };
    if (tool.requires_opt_in && !tool.allowed)
      return {
        text: t(
          'toolAccess.explicitPermission',
          'Explicit permission required',
        ),
      };
    if (tool.automatic) return { text: t('toolAccess.automatic', 'Automatic') };
    return null;
  }
  function familyUnavailable(group) {
    return group.members.every((tool) => tool.ready === false);
  }
  function familyState(group) {
    const ordinary = group.members.filter((tool) => !tool.automatic);
    const members = ordinary.length ? ordinary : group.members;
    const count = members.filter((tool) => tool.allowed).length;
    return count === members.length ? 'on' : count ? 'mixed' : 'off';
  }
</script>

<div class="tool-catalog">
  <div class="tool-catalog-toolbar">
    <div class="tool-catalog-actions">{@render toolbar?.()}</div>
    <span class="tool-catalog-summary">
      {t('toolAccess.selectionCount', '{enabled} of {total} allowed', {
        enabled: enabledCount,
        total: items.length,
      })}
    </span>
    <label class="tool-catalog-search">
      <input
        type="search"
        bind:value={search}
        placeholder={t('toolAccess.searchPlaceholder', 'Filter Tools…')}
        aria-label={t('toolAccess.searchLabel', 'Filter Tools')}
      />
    </label>
  </div>
  {#if groups.length === 0}
    <EmptyState
      density="compact"
      title={t('toolAccess.empty', 'No matching Tools.')}
    />
  {/if}
  <div class="tool-access-groups">
    {#each groups as group (group.id ?? 'individual')}
      {@const state = familyState(group)}
      <section class="tool-access-group">
        <header class="tool-access-group-header">
          {#if group.family && onToggleGroup}
            <Checkbox
              class="tool-access-family-toggle"
              checked={state === 'on'}
              indeterminate={state === 'mixed'}
              ariaLabel={t('toolAccess.family.all', 'All {family} Tools', {
                family: familyLabel(group),
              })}
              data-tool-family={group.id}
              {disabled}
              onChange={(next) => onToggleGroup(group.members, next)}
            />
          {/if}
          <h4>{familyLabel(group)}</h4>
          {#if familyUnavailable(group)}
            <span class="tool-catalog-unavailable tool-catalog-family-status"
              >{t('toolAccess.unavailable', 'Currently unavailable')}</span
            >
          {/if}
          <span class="tool-catalog-count"
            >{group.members.filter((tool) => tool.allowed).length}/{group
              .members.length}</span
          >
        </header>
        <div class="tool-catalog-rows">
          {#each group.members as tool (tool.name)}
            {@const status = toolStatus(tool, group)}
            <div
              class="tool-access-chip-wrap"
              class:is-unavailable={tool.ready === false}
            >
              <Checkbox
                class="tool-access-chip"
                checked={tool.allowed}
                ariaLabel={toggleLabel(tool)}
                aria-describedby={status
                  ? `${catalogId}-${tool.name}-status`
                  : undefined}
                data-tool-name={tool.name}
                data-tool-access-toggle
                {disabled}
                onChange={(next) => onToggle(tool, next)}
              >
                <span class="tool-access-name">{tool.name}</span>
                {#if status}
                  <span
                    id="{catalogId}-{tool.name}-status"
                    class="tool-catalog-status"
                    class:tool-catalog-unavailable={status.unavailable}
                    >{status.text}</span
                  >
                {/if}
              </Checkbox>
              <div class="floating-card tool-access-tip" use:floatingHoverCard>
                <strong>{tool.name}</strong>
                {#if tool.description}<p>{tool.description}</p>{/if}
                {#each tool.notes ?? [] as note, index (`${tool.name}-${index}`)}<p
                  >
                    {note}
                  </p>{/each}
                {@render details?.(tool)}
                <ToolReadinessNotice
                  ready={tool.ready}
                  readinessHint={tool.readiness_hint}
                  extension={tool.extension}
                  {onOpenExtensions}
                />
              </div>
            </div>
          {/each}
        </div>
      </section>
    {/each}
  </div>
</div>

<style>
  .tool-catalog {
    display: grid;
    gap: 14px;
    min-width: 0;
  }
  .tool-catalog-toolbar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px 12px;
  }
  .tool-catalog-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
  }
  .tool-catalog-actions:empty {
    display: none;
  }
  .tool-catalog-summary {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
    font-variant-numeric: tabular-nums;
  }
  .tool-catalog-search {
    margin-left: auto;
    flex: 1 1 180px;
    max-width: 260px;
  }
  .tool-catalog-search input {
    width: 100%;
    box-sizing: border-box;
    min-height: 32px;
    padding: 6px 12px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--field-surface);
    color: var(--text-hi);
    font: inherit;
    font-size: var(--fs-body-sm);
  }
  .tool-catalog-search input::placeholder {
    color: var(--text-lo);
  }
  .tool-catalog-search input:focus-visible {
    border-color: var(--accent);
    outline: none;
    box-shadow: var(--field-focus-ring);
  }
  /* Each family is its own panel; panels flow through two balanced columns
     so a short family never leaves a hole beside a long one, and a family
     never splits across columns. */
  .tool-access-groups {
    columns: 2;
    column-gap: 16px;
  }
  .tool-access-group {
    display: block;
    min-width: 0;
    margin-bottom: 16px;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--surface);
    break-inside: avoid;
    overflow: hidden;
  }
  .tool-access-group-header {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 44px;
    padding: 8px 14px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
  }
  .tool-access-group-header h4 {
    margin: 0;
    color: var(--text-hi);
    font: 600 var(--fs-body-md) / 1.4 var(--font-ui);
  }
  .tool-catalog-count {
    margin-left: auto;
    color: var(--text-lo);
    font: var(--fs-label-sm) var(--font-ui);
    font-variant-numeric: tabular-nums;
  }
  .tool-catalog-rows {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    padding: 4px 0;
  }
  .tool-access-chip-wrap {
    min-width: 0;
  }
  /* The whole row is the checkbox. */
  .tool-access-groups :global(.tool-access-chip) {
    min-height: 34px;
    padding: 6px 14px;
    border-radius: 0;
  }
  .tool-access-groups :global(.tool-access-chip:hover:not(:disabled)) {
    background: var(--surface-2);
  }
  .tool-access-groups :global(.tool-access-chip:focus-visible) {
    box-shadow: inset 0 0 0 2px var(--accent);
  }
  .tool-access-name {
    min-width: 0;
    overflow-wrap: anywhere;
    color: var(--text-hi);
    /* Tool names are identifiers. */
    font: var(--fs-mono-sm) var(--font-mono);
  }
  .tool-access-chip-wrap.is-unavailable .tool-access-name {
    color: var(--text-med);
  }
  .tool-catalog-status {
    flex-shrink: 0;
    margin-left: auto;
    padding-left: 8px;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    text-align: right;
  }
  .tool-catalog-unavailable {
    color: var(--amber);
  }
  .tool-catalog-family-status {
    font-size: var(--fs-label-sm);
  }
  /* Full Tool details use the shared floating card (styles/app/hints.css);
     long descriptions get a wider measure. */
  .tool-access-tip {
    max-width: min(400px, calc(100vw - 16px));
    color: var(--text-med);
  }
  .tool-access-tip strong {
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    font-weight: 600;
  }
  .tool-access-tip p {
    margin: 6px 0 0;
    white-space: pre-wrap;
  }
  @media (max-width: 760px) {
    .tool-access-groups {
      columns: 1;
    }
  }
  @media (max-width: 640px) {
    .tool-access-groups :global(.tool-access-chip) {
      min-height: 40px;
    }
    .tool-catalog-search {
      max-width: none;
      margin-left: 0;
    }
  }
</style>
