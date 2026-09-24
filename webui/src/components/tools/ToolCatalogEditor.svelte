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
  <div class="s-check-groups">
    {#each groups as group (group.id ?? 'individual')}
      {@const state = familyState(group)}
      <section class="s-group s-check-group">
        <header class="s-check-group__head">
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
          <h4 class="s-check-group__title">{familyLabel(group)}</h4>
          {#if familyUnavailable(group)}
            <span class="tool-catalog-unavailable tool-catalog-family-status"
              >{t('toolAccess.unavailable', 'Currently unavailable')}</span
            >
          {/if}
          <span class="s-check-group__count"
            >{group.members.filter((tool) => tool.allowed).length}/{group
              .members.length}</span
          >
        </header>
        <div class="s-check-group__rows">
          {#each group.members as tool (tool.name)}
            {@const status = toolStatus(tool, group)}
            <div
              class="tool-access-chip-wrap"
              class:is-unavailable={tool.ready === false}
            >
              <Checkbox
                class="s-check-row tool-access-chip"
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
                <span class="s-check-row__name">{tool.name}</span>
                {#if status}
                  <span
                    id="{catalogId}-{tool.name}-status"
                    class="s-check-row__state"
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
  /* Each family is a Checkbox group (styles/settings/sections.css); the
     wrapper anchors the row's hover card. */
  .tool-access-chip-wrap {
    min-width: 0;
  }
  .tool-access-chip-wrap.is-unavailable .s-check-row__name {
    color: var(--text-med);
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
  @media (max-width: 640px) {
    .tool-catalog-search {
      max-width: none;
      margin-left: 0;
    }
  }
</style>
