<script>
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import ToolReadinessNotice from '../ui/ToolReadinessNotice.svelte';
  import {
    TOOL_ACCESS_MODE_ALL,
    TOOL_ACCESS_MODE_NONE,
    TOOL_ACCESS_MODE_SELECTED,
    changeToolAccessMode,
    groupToolCatalog,
    normalizeToolAccess,
    policyNamesNotInCatalog,
    setToolAccessPreference,
    setToolFamilyPreference,
    toolAccessPreferenceEnabled,
    toolIsConfigurable,
  } from '$lib/toolAccess.js';
  import { t } from '$lib/i18n.js';
  import { floatingHoverCard } from '$lib/tooltip.js';

  const noop = () => {};
  const FAMILY_ORDER = [
    'files',
    'execution',
    'web',
    'sessions',
    'skills',
    'media',
  ];

  let {
    value = { mode: TOOL_ACCESS_MODE_ALL },
    tools = [],
    ceiling = null,
    disabled = false,
    memoryPromptMode = 'agent_user',
    showReset = false,
    resetLabel = '',
    onChange = noop,
    onReset = noop,
    onOpenExtensions = noop,
  } = $props();

  let search = $state('');
  let policy = $derived(normalizeToolAccess(value));
  let completeCatalog = $derived(catalogWithStoredTools());
  let groups = $derived(filteredGroups());
  let visibleCount = $derived(
    groups.reduce((total, group) => total + group.members.length, 0),
  );

  const modeOptions = [
    {
      mode: TOOL_ACCESS_MODE_ALL,
      label: () => t('toolAccess.mode.all', 'All'),
    },
    {
      mode: TOOL_ACCESS_MODE_SELECTED,
      label: () => t('toolAccess.mode.selected', 'Choose'),
    },
    {
      mode: TOOL_ACCESS_MODE_NONE,
      label: () => t('toolAccess.mode.none', 'None'),
    },
  ];

  function catalogWithStoredTools() {
    const catalog = Array.isArray(tools) ? [...tools] : [];
    const unknown = policyNamesNotInCatalog(policy, catalog);
    for (const name of unknown) {
      catalog.push({
        name,
        family: null,
        activation: 'configurable',
        ready: false,
        registered: false,
        requires_opt_in: (policy.granted ?? []).includes(name),
      });
    }
    return catalog;
  }

  function filteredGroups() {
    const needle = search.trim().toLocaleLowerCase();
    return groupToolCatalog(completeCatalog, ceiling)
      .map((group) => ({
        ...group,
        members: needle
          ? group.members.filter((tool) =>
              tool.name.toLocaleLowerCase().includes(needle),
            )
          : group.members,
      }))
      .filter((group) => group.members.length > 0)
      .sort((left, right) => familyOrder(left.id) - familyOrder(right.id));
  }

  function updateMode(mode) {
    onChange(changeToolAccessMode(policy, mode, completeCatalog, ceiling));
  }

  function preferenceEnabled(tool) {
    return toolAccessPreferenceEnabled(policy, tool);
  }

  function updateTool(tool) {
    if (policy.mode === TOOL_ACCESS_MODE_NONE) return;
    onChange(
      setToolAccessPreference(
        policy,
        tool,
        !preferenceEnabled(tool),
        completeCatalog,
        ceiling,
      ),
    );
  }

  function groupState(group) {
    const configurable = group.members.filter(toolIsConfigurable);
    const controlledMembers =
      configurable.length > 0 ? configurable : group.members;
    const enabled = controlledMembers.filter(preferenceEnabled).length;
    if (enabled === 0) return 'off';
    if (enabled === controlledMembers.length) return 'on';
    return 'mixed';
  }

  function updateGroup(group) {
    if (policy.mode === TOOL_ACCESS_MODE_NONE) return;
    onChange(
      setToolFamilyPreference(
        policy,
        group.members,
        groupState(group) !== 'on',
        completeCatalog,
        ceiling,
      ),
    );
  }

  function familyLabel(group) {
    if (!group.family) {
      return t('toolAccess.family.individual', 'Individual Tools');
    }
    const labels = {
      files: t('toolAccess.family.files', 'Files'),
      execution: t('toolAccess.family.execution', 'Execution'),
      web: t('toolAccess.family.web', 'Web'),
      sessions: t('toolAccess.family.sessions', 'Sessions'),
      skills: t('toolAccess.family.skills', 'Skills'),
      media: t('toolAccess.family.media', 'Media'),
    };
    return (
      labels[group.id] ??
      group.members.find((tool) => tool.family_label)?.family_label ??
      humanize(group.id)
    );
  }

  function groupToggleLabel(group) {
    return groupState(group) === 'on'
      ? t('toolAccess.family.disable', 'Turn off {family}', {
          family: familyLabel(group),
        })
      : t('toolAccess.family.enable', 'Turn on {family}', {
          family: familyLabel(group),
        });
  }

  function toolToggleLabel(tool) {
    return preferenceEnabled(tool)
      ? t('toolAccess.disableTool', 'Turn off {name}', { name: tool.name })
      : t('toolAccess.enableTool', 'Turn on {name}', { name: tool.name });
  }

  function toolNotes(tool) {
    const notes = [];
    if (tool.requires_opt_in) {
      notes.push(
        t(
          'toolAccess.requiresOptIn',
          'Requires explicit permission, including in All mode. Turn this Tool on to grant it.',
        ),
      );
    }
    if (tool.activation === 'follows') {
      notes.push(
        t('toolAccess.activation.follows', 'Automatic with {source}', {
          source: tool.activation_source,
        }),
      );
    } else if (tool.activation === 'memory_mode') {
      notes.push(
        memoryPromptMode === 'off'
          ? t('toolAccess.activation.memoryOff', 'Memory is currently off')
          : t('toolAccess.activation.memoryOn', 'Automatic while Memory is on'),
      );
    } else if (tool.activation === 'session_grant') {
      notes.push(
        t(
          'toolAccess.activation.session',
          'Available automatically when the Session grants it',
        ),
      );
    }
    if ((tool.constraints ?? []).includes('identity_agent')) {
      notes.push(t('toolAccess.constraint.identity', 'Identity Agents only'));
    }
    if ((tool.constraints ?? []).includes('image_fallback_route')) {
      notes.push(
        t(
          'toolAccess.constraint.imageFallback',
          'Used only when the main Model cannot analyze images directly',
        ),
      );
    }
    if (tool.registered === false) {
      notes.push(
        t('toolAccess.readiness.unregistered', 'Not registered right now'),
      );
    }
    return notes;
  }

  function hasToolDetails(tool) {
    return (
      Boolean(tool.description) ||
      toolNotes(tool).length > 0 ||
      tool.ready === false
    );
  }

  function humanize(value) {
    return String(value ?? '')
      .replaceAll('_', ' ')
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function familyOrder(family) {
    if (!family) return FAMILY_ORDER.length + 1;
    const index = FAMILY_ORDER.indexOf(family);
    return index === -1 ? FAMILY_ORDER.length : index;
  }
</script>

<div class="tool-access-editor">
  <div class="tool-access-toolbar">
    <div
      class="tool-access-modes"
      role="radiogroup"
      aria-label={t('toolAccess.modeLabel', 'Tool access')}
    >
      {#each modeOptions as option (option.mode)}
        <button
          type="button"
          class:is-selected={policy.mode === option.mode}
          role="radio"
          aria-checked={policy.mode === option.mode}
          {disabled}
          onclick={() => updateMode(option.mode)}
        >
          {option.label()}
        </button>
      {/each}
    </div>

    <label class="tool-access-search">
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
        <circle
          cx="7"
          cy="7"
          r="4.25"
          fill="none"
          stroke="currentColor"
          stroke-width="1.25"
        />
        <path
          d="m10.25 10.25 3 3"
          fill="none"
          stroke="currentColor"
          stroke-width="1.25"
          stroke-linecap="round"
        />
      </svg>
      <input
        type="search"
        value={search}
        placeholder={t('toolAccess.searchPlaceholder', 'Filter Tools…')}
        aria-label={t('toolAccess.searchLabel', 'Filter Tools')}
        oninput={(event) => (search = event.currentTarget.value)}
      />
    </label>

    {#if showReset}
      <Button variant="tertiary" {disabled} onClick={onReset}>
        {resetLabel ||
          t('toolAccess.resetOverride', 'Reset to repository policy')}
      </Button>
    {/if}
  </div>

  {#if visibleCount === 0}
    <EmptyState
      density="compact"
      title={t('toolAccess.empty', 'No matching Tools.')}
    />
  {:else}
    <div class="tool-access-groups">
      {#each groups as group (group.id ?? 'individual')}
        <section
          class="tool-access-group"
          class:tool-access-group--individual={!group.family}
        >
          <header class="tool-access-group-header">
            <div class="tool-access-group-title">
              <h4>{familyLabel(group)}</h4>
            </div>
            {#if group.family}
              <button
                type="button"
                class="tool-access-family-toggle"
                class:is-on={groupState(group) === 'on'}
                class:is-mixed={groupState(group) === 'mixed'}
                role="checkbox"
                aria-checked={groupState(group) === 'mixed'
                  ? 'mixed'
                  : groupState(group) === 'on'}
                aria-label={groupToggleLabel(group)}
                data-tool-family={group.id}
                disabled={disabled || policy.mode === TOOL_ACCESS_MODE_NONE}
                onclick={() => updateGroup(group)}
              >
                <span aria-hidden="true"></span>
              </button>
            {/if}
          </header>

          <div class="tool-access-cloud">
            {#each group.members as tool (tool.name)}
              <div class="tool-access-chip-wrap">
                <button
                  type="button"
                  class="tool-access-chip"
                  class:is-on={preferenceEnabled(tool)}
                  class:is-automatic={!toolIsConfigurable(tool)}
                  class:is-unavailable={tool.ready === false}
                  role="switch"
                  aria-checked={preferenceEnabled(tool)}
                  aria-label={toolToggleLabel(tool)}
                  data-tool-name={tool.name}
                  data-tool-access-toggle
                  disabled={disabled || policy.mode === TOOL_ACCESS_MODE_NONE}
                  onclick={() => updateTool(tool)}
                >
                  <span>{tool.name}</span>
                </button>

                {#if hasToolDetails(tool)}
                  <div
                    class="tool-access-tip"
                    role="tooltip"
                    use:floatingHoverCard
                  >
                    {#if tool.description}
                      <p class="tool-access-description">
                        {tool.description}
                      </p>
                    {/if}
                    {#each toolNotes(tool) as note (`${tool.name}-${note}`)}
                      <p>{note}</p>
                    {/each}
                    <ToolReadinessNotice
                      ready={tool.ready}
                      readinessHint={tool.readiness_hint}
                      extension={tool.extension}
                      {onOpenExtensions}
                    />
                  </div>
                {/if}
              </div>
            {/each}
          </div>
        </section>
      {/each}
    </div>
  {/if}
</div>

<style>
  .tool-access-editor {
    display: grid;
    gap: 10px;
  }

  .tool-access-toolbar {
    display: grid;
    grid-template-columns: auto minmax(150px, 1fr) auto;
    align-items: center;
    gap: 8px;
  }

  .tool-access-modes {
    display: inline-flex;
    overflow: hidden;
    width: max-content;
    border: 1px solid var(--border-2);
    border-radius: 6px;
    background: var(--surface-2);
  }

  .tool-access-modes button {
    padding: 7px 11px;
    color: var(--text-lo);
    border: 0;
    border-left: 1px solid var(--border);
    background: transparent;
    cursor: pointer;
    font: inherit;
    font-size: var(--fs-mono-xs);
  }

  .tool-access-modes button:first-child {
    border-left: 0;
  }

  .tool-access-modes button:hover:not(:disabled) {
    color: var(--text-hi);
    background: var(--surface-3);
  }

  .tool-access-modes button.is-selected {
    color: var(--accent);
    background: var(--accent-dim);
  }

  .tool-access-search {
    display: flex;
    justify-self: end;
    align-items: center;
    width: min(230px, 100%);
    gap: 6px;
    padding: 0 8px;
    color: var(--text-lo);
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--surface-2);
  }

  .tool-access-search:focus-within {
    border-color: var(--accent-40);
    box-shadow: var(--focus-ring);
  }

  .tool-access-search input {
    width: 100%;
    min-width: 0;
    padding: 7px 0;
    color: var(--text-hi);
    border: 0;
    outline: 0;
    background: transparent;
    font: inherit;
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .tool-access-groups {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 270px), 1fr));
    align-items: start;
    gap: 8px;
  }

  .tool-access-group {
    min-width: 0;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface);
  }

  .tool-access-group--individual {
    grid-column: 1 / -1;
  }

  .tool-access-group-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-height: 32px;
    padding: 6px 9px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
  }

  .tool-access-group-title {
    display: flex;
    align-items: baseline;
    gap: 7px;
    min-width: 0;
  }

  .tool-access-group-title h4 {
    margin: 0;
    color: var(--text-hi);
    font-size: var(--fs-label-sm);
  }

  .tool-access-family-toggle {
    position: relative;
    flex: 0 0 auto;
    width: 30px;
    height: 17px;
    padding: 0;
    border: 1px solid var(--border-2);
    border-radius: 999px;
    background: var(--surface-3);
    cursor: pointer;
  }

  .tool-access-family-toggle span {
    position: absolute;
    top: 3px;
    left: 3px;
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--text-lo);
    transition:
      left 0.12s ease,
      background 0.12s ease;
  }

  .tool-access-family-toggle.is-on {
    border-color: var(--accent-40);
    background: var(--accent-dim);
  }

  .tool-access-family-toggle.is-on span {
    left: 16px;
    background: var(--accent);
  }

  .tool-access-family-toggle.is-mixed span {
    left: 9px;
    width: 10px;
    border-radius: 3px;
    background: var(--amber);
  }

  .tool-access-cloud {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 9px;
  }

  .tool-access-chip-wrap {
    position: relative;
  }

  .tool-access-chip {
    display: inline-flex;
    align-items: center;
    max-width: 100%;
    padding: 5px 10px;
    color: var(--text-lo);
    border: 1px solid var(--border);
    border-radius: 12px;
    background: transparent;
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: 11.5px;
    line-height: 1;
  }

  .tool-access-chip:hover:not(:disabled) {
    color: var(--text-med);
    border-color: var(--border-2);
  }

  .tool-access-chip.is-on {
    color: var(--accent);
    border-color: var(--accent-30);
    background: var(--accent-10);
  }

  .tool-access-chip.is-on:hover:not(:disabled) {
    color: var(--accent);
    border-color: var(--accent-40);
    background: var(--accent-16);
  }

  .tool-access-chip.is-automatic {
    border-style: dashed;
  }

  .tool-access-chip.is-automatic.is-on {
    color: var(--text-med);
    border-color: var(--border-2);
    background: var(--surface-2);
  }

  .tool-access-chip.is-automatic.is-on:hover:not(:disabled) {
    color: var(--text-hi);
    border-color: var(--border-2);
    background: var(--surface-3);
  }

  .tool-access-chip.is-unavailable {
    opacity: 0.6;
  }

  .tool-access-chip:disabled,
  .tool-access-family-toggle:disabled,
  .tool-access-modes button:disabled {
    cursor: default;
    opacity: 0.45;
  }

  .tool-access-chip:focus-visible,
  .tool-access-family-toggle:focus-visible,
  .tool-access-modes button:focus-visible {
    outline: 0;
    box-shadow: var(--focus-ring);
  }

  .tool-access-tip {
    position: fixed;
    z-index: var(--z-floating);
    width: max-content;
    max-width: min(300px, calc(100vw - 16px));
    padding: 8px 10px;
    color: var(--text-med);
    border: 1px solid var(--border-2);
    border-radius: 6px;
    background: var(--surface-3);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
    font-size: 11px;
    line-height: 1.4;
  }

  .tool-access-tip:global([data-floating-open='true']) {
    opacity: 1;
    visibility: visible;
    pointer-events: auto;
  }

  .tool-access-tip p {
    margin: 0;
  }

  .tool-access-tip p + p {
    margin-top: 4px;
  }

  .tool-access-description {
    color: var(--text-hi);
  }

  @media (max-width: 900px) {
    .tool-access-toolbar {
      grid-template-columns: 1fr auto;
    }

    .tool-access-search {
      grid-column: 1 / -1;
      grid-row: 2;
      justify-self: stretch;
      width: 100%;
    }

    .tool-access-groups {
      grid-template-columns: 1fr;
    }
  }
</style>
