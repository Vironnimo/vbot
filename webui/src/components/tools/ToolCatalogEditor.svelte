<script>
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
    toggleLabel = (tool) =>
      tool.allowed
        ? t('toolAccess.disableTool', 'Turn off {name}', { name: tool.name })
        : t('toolAccess.enableTool', 'Turn on {name}', { name: tool.name }),
    toolbar,
    details,
    onOpenExtensions,
  } = $props();
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
    <label class="tool-catalog-search">
      <input
        type="search"
        bind:value={search}
        placeholder={t('toolAccess.searchPlaceholder', 'Filter Tools…')}
        aria-label={t('toolAccess.searchLabel', 'Filter Tools')}
      />
    </label>
  </div>
  <div class="tool-catalog-summary">
    {t('toolAccess.selectionCount', '{enabled} of {total} allowed', {
      enabled: enabledCount,
      total: items.length,
    })}
  </div>
  {#if groups.length === 0}
    <EmptyState
      density="compact"
      title={t('toolAccess.empty', 'No matching Tools.')}
    />
  {/if}
  <div class="tool-access-groups">
    {#each groups as group (group.id ?? 'individual')}
      <section class="tool-access-group">
        <header class="tool-access-group-header">
          <h4>{familyLabel(group)}</h4>
          <span class="tool-catalog-count"
            >{group.members.filter((tool) => tool.allowed).length}/{group
              .members.length}</span
          >
          {#if group.family && onToggleGroup}
            <button
              type="button"
              class="tool-access-family-toggle"
              class:is-on={familyState(group) === 'on'}
              class:is-mixed={familyState(group) === 'mixed'}
              role="checkbox"
              aria-checked={familyState(group) === 'mixed'
                ? 'mixed'
                : familyState(group) === 'on'}
              aria-label={familyState(group) === 'on'
                ? t('toolAccess.family.disable', 'Turn off {family}', {
                    family: familyLabel(group),
                  })
                : t('toolAccess.family.enable', 'Turn on {family}', {
                    family: familyLabel(group),
                  })}
              data-tool-family={group.id}
              {disabled}
              onclick={() =>
                onToggleGroup(group.members, familyState(group) !== 'on')}
              ><span></span></button
            >
          {/if}
        </header>
        <div class="tool-catalog-rows">
          {#each group.members as tool (tool.name)}
            <div
              class="tool-access-chip-wrap"
              class:is-unavailable={tool.ready === false}
            >
              <button
                type="button"
                class="tool-access-chip"
                class:is-on={tool.allowed}
                class:is-automatic={tool.automatic}
                role="switch"
                aria-checked={tool.allowed}
                aria-label={toggleLabel(tool)}
                data-tool-name={tool.name}
                data-tool-access-toggle
                {disabled}
                onclick={() => onToggle(tool, !tool.allowed)}
              >
                <span>{tool.name}</span>
              </button>
              {#if tool.automatic || tool.ready === false || tool.requires_opt_in}
                <div class="tool-catalog-status">
                  {#if tool.automatic}<span
                      >{t('toolAccess.automatic', 'Automatic')}</span
                    >{/if}
                  {#if tool.ready === false}<span
                      class="tool-catalog-unavailable"
                      >{t(
                        'toolAccess.unavailable',
                        'Currently unavailable',
                      )}</span
                    >{/if}
                  {#if tool.requires_opt_in && !tool.allowed}<span
                      >{t(
                        'toolAccess.explicitPermission',
                        'Explicit permission required',
                      )}</span
                    >{/if}
                </div>
              {/if}
              <div class="tool-access-tip" role="tooltip" use:floatingHoverCard>
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
    gap: 16px;
    min-width: 0;
  }
  .tool-catalog-toolbar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 12px;
  }
  .tool-catalog-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .tool-catalog-search {
    margin-left: auto;
    flex: 1 1 180px;
    max-width: 280px;
  }
  .tool-catalog-search input {
    width: 100%;
    box-sizing: border-box;
    min-height: 36px;
    padding: 7px 12px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--field-surface);
    color: var(--text-hi);
    font: inherit;
    font-size: var(--fs-body-sm);
  }
  .tool-catalog-search input:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }
  .tool-catalog-summary {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .tool-access-groups {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 24px 32px;
    align-items: start;
  }
  .tool-access-group {
    min-width: 0;
  }
  .tool-access-group-header {
    display: flex;
    align-items: center;
    gap: 10px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }
  .tool-access-group-header h4 {
    margin: 0;
    color: var(--text-hi);
    font: 600 var(--fs-label-md) var(--font-ui);
  }
  .tool-catalog-count {
    color: var(--text-lo);
    font: var(--fs-label-sm) var(--font-ui);
    font-variant-numeric: tabular-nums;
  }
  /* Family and Tool switches follow the shared toggle: off is a dark track
     with a muted knob, on is a light track with a dark knob. */
  .tool-access-family-toggle {
    position: relative;
    margin-left: auto;
    flex: 0 0 auto;
    width: 32px;
    height: 20px;
    border: 1px solid var(--border-2);
    border-radius: 12px;
    background: var(--surface-3);
    cursor: pointer;
  }
  .tool-access-family-toggle span {
    position: absolute;
    top: 4px;
    left: 4px;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--text-lo);
  }
  .tool-access-family-toggle.is-on {
    background: var(--text-hi);
    border-color: var(--text-hi);
  }
  .tool-access-family-toggle.is-on span {
    left: 16px;
    background: var(--bg);
  }
  /* Partly on: a centered square knob on the off track. */
  .tool-access-family-toggle.is-mixed span {
    left: 10px;
    border-radius: 2px;
    background: var(--text-hi);
  }
  .tool-catalog-rows {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }
  .tool-access-chip-wrap {
    min-width: 0;
    padding: 5px 0;
    border-bottom: 1px solid var(--border);
  }
  .tool-access-chip {
    display: flex;
    position: relative;
    align-items: center;
    width: 100%;
    min-height: 28px;
    padding: 2px 44px 2px 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-hi);
    cursor: pointer;
    text-align: left;
    /* Tool names are identifiers. */
    font: var(--fs-mono-xs) var(--font-mono);
  }
  .tool-access-chip span {
    overflow-wrap: anywhere;
  }
  .tool-access-chip::before {
    content: '';
    position: absolute;
    right: 0;
    width: 28px;
    height: 16px;
    border: 1px solid var(--border-2);
    border-radius: 12px;
    background: var(--surface-3);
  }
  .tool-access-chip::after {
    content: '';
    position: absolute;
    right: 18px;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-lo);
  }
  .tool-access-chip.is-on::before {
    background: var(--text-hi);
    border-color: var(--text-hi);
  }
  .tool-access-chip.is-on::after {
    right: 4px;
    background: var(--bg);
  }
  .tool-access-chip.is-automatic::before {
    border-style: dashed;
  }
  /* Keep the dashed automatic marker visible on the light on-track. */
  .tool-access-chip.is-automatic.is-on::before {
    background: var(--text-med);
    border-color: var(--text-hi);
  }
  .tool-access-chip:focus-visible,
  .tool-access-family-toggle:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }
  .tool-access-chip:not(.is-on):hover:not(:disabled)::before {
    border-color: var(--text-faint);
  }
  .tool-access-chip:disabled,
  .tool-access-family-toggle:disabled {
    cursor: default;
    opacity: 0.5;
  }
  .tool-catalog-status {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    margin-top: 3px;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }
  .tool-catalog-unavailable {
    color: var(--amber);
  }
  .tool-access-tip {
    position: fixed;
    z-index: var(--z-floating);
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
    width: max-content;
    max-width: min(420px, calc(100vw - 24px));
    padding: 14px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.5;
    box-shadow: var(--floating-elevation);
  }
  .tool-access-tip:global([data-floating-open='true']) {
    opacity: 1;
    visibility: visible;
    pointer-events: auto;
  }
  .tool-access-tip p {
    margin: 6px 0;
    white-space: pre-wrap;
  }
  @media (max-width: 640px) {
    .tool-access-groups {
      grid-template-columns: minmax(0, 1fr);
    }
    .tool-access-chip {
      min-height: 36px;
    }
    .tool-catalog-search {
      max-width: none;
    }
  }
</style>
