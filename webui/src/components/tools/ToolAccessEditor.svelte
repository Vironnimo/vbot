<script>
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import {
    TOOL_ACCESS_MODE_ALL,
    TOOL_ACCESS_MODE_NONE,
    TOOL_ACCESS_MODE_SELECTED,
    changeToolAccessMode,
    groupToolCatalog,
    normalizeToolAccess,
    policyNamesNotInCatalog,
    setToolAccessState,
    setToolFamilyState,
    toolAccessState,
    toolIsConfigurable,
  } from '$lib/toolAccess.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

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
      label: () => t('toolAccess.mode.all', 'All Tools'),
      description: () =>
        t(
          'toolAccess.mode.allHelp',
          'Includes current and future Tools. Individual Tools can still be blocked.',
        ),
    },
    {
      mode: TOOL_ACCESS_MODE_SELECTED,
      label: () => t('toolAccess.mode.selected', 'Selected Tools'),
      description: () =>
        t(
          'toolAccess.mode.selectedHelp',
          'Only the Tools you choose are included. Automatic companion Tools follow their source.',
        ),
    },
    {
      mode: TOOL_ACCESS_MODE_NONE,
      label: () => t('toolAccess.mode.none', 'No Tools'),
      description: () =>
        t('toolAccess.mode.noneHelp', 'Turns off every Tool for this Agent.'),
    },
  ];

  function catalogWithStoredTools() {
    const catalog = Array.isArray(tools) ? [...tools] : [];
    const unknown = policyNamesNotInCatalog(policy, catalog);
    for (const name of unknown) {
      catalog.push({
        name,
        description: t(
          'toolAccess.unregisteredDescription',
          'Stored in this policy, but not registered right now.',
        ),
        family: null,
        activation: 'configurable',
        ready: false,
        registered: false,
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
              `${tool.name} ${tool.description ?? ''}`
                .toLocaleLowerCase()
                .includes(needle),
            )
          : group.members,
      }))
      .filter((group) => group.members.length > 0);
  }

  function updateMode(mode) {
    onChange(changeToolAccessMode(policy, mode, completeCatalog, ceiling));
  }

  function updateTool(tool, state) {
    onChange(
      setToolAccessState(policy, tool.name, state, completeCatalog, ceiling),
    );
  }

  function updateGroup(group, state) {
    onChange(
      setToolFamilyState(
        policy,
        group.members,
        state,
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
    return labels[group.id] ?? humanize(group.id);
  }

  function groupSummary(group) {
    const states = group.members.map((tool) => accessState(tool));
    const denied = states.filter((state) => state === 'denied').length;
    const included = states.filter(
      (state) =>
        state === 'included' || state === 'enabled' || state === 'automatic',
    ).length;
    if (denied > 0) {
      return t(
        'toolAccess.family.summaryBlocked',
        '{included} included · {denied} blocked',
        { included, denied },
      );
    }
    return t('toolAccess.family.summary', '{included} of {total} included', {
      included,
      total: states.length,
    });
  }

  function accessState(tool) {
    return toolAccessState(policy, tool, completeCatalog, {
      memoryPromptMode,
    });
  }

  function stateLabel(tool) {
    const state = accessState(tool);
    if (state === 'denied') {
      return t('toolAccess.state.denied', 'Blocked');
    }
    if (state === 'enabled') {
      return t('toolAccess.state.enabled', 'On');
    }
    if (state === 'included') {
      return t('toolAccess.state.included', 'Included');
    }
    if (state === 'automatic') {
      return t('toolAccess.state.automatic', 'Automatic');
    }
    if (state === 'inactive') {
      return t('toolAccess.state.inactive', 'Inactive');
    }
    return t('toolAccess.state.off', 'Off');
  }

  function stateVariant(tool) {
    const state = accessState(tool);
    if (state === 'denied') return 'error';
    if (state === 'enabled') return 'success';
    if (state === 'included' || state === 'automatic') return 'info';
    return 'neutral';
  }

  function automaticNote(tool) {
    if (tool.activation === 'follows') {
      if (accessState(tool) === 'inactive') {
        return t(
          'toolAccess.activation.followsInactive',
          'Inactive · waiting for {source}',
          { source: tool.activation_source },
        );
      }
      return t('toolAccess.activation.follows', 'Follows {source}', {
        source: tool.activation_source,
      });
    }
    if (tool.activation === 'memory_mode') {
      return memoryPromptMode === 'off'
        ? t('toolAccess.activation.memoryOff', 'Inactive · Memory is off')
        : t('toolAccess.activation.memoryOn', 'Automatic · Memory is on');
    }
    if (tool.activation === 'session_grant') {
      return t(
        'toolAccess.activation.session',
        'Appears automatically when the Session grants it',
      );
    }
    if ((tool.constraints ?? []).includes('identity_agent')) {
      return t('toolAccess.constraint.identity', 'Identity Agents only');
    }
    if ((tool.constraints ?? []).includes('image_fallback_route')) {
      return t(
        'toolAccess.constraint.imageFallback',
        'Used only when the main Model cannot analyze images directly',
      );
    }
    return '';
  }

  function readinessNote(tool) {
    if (tool.registered === false) {
      return t('toolAccess.readiness.unregistered', 'Not registered right now');
    }
    if (tool.ready === false) {
      return (
        tool.readiness_hint ??
        t('toolAccess.readiness.unavailable', 'Currently unavailable')
      );
    }
    return '';
  }

  function defaultStateSelected(tool) {
    const state = accessState(tool);
    return (
      state === 'included' ||
      state === 'automatic' ||
      state === 'inactive' ||
      state === 'off'
    );
  }

  function defaultChoiceLabel(tool) {
    if (!toolIsConfigurable(tool)) {
      return t('toolAccess.choice.auto', 'Auto');
    }
    return policy.mode === TOOL_ACCESS_MODE_ALL
      ? t('toolAccess.choice.included', 'Included')
      : t('toolAccess.choice.off', 'Off');
  }

  function familyActionLabel(group, action) {
    return t('toolAccess.family.actionFor', '{action} {family}', {
      action,
      family: familyLabel(group),
    });
  }

  function humanize(value) {
    return String(value ?? '')
      .replaceAll('_', ' ')
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }
</script>

<div class="tool-access-editor">
  <div
    class="tool-access-intent"
    role="radiogroup"
    aria-label={t('toolAccess.intentLabel', 'Tool access')}
  >
    {#each modeOptions as option (option.mode)}
      <button
        type="button"
        class:tool-access-intent-card--selected={policy.mode === option.mode}
        class="tool-access-intent-card"
        role="radio"
        aria-checked={policy.mode === option.mode}
        {disabled}
        onclick={() => updateMode(option.mode)}
      >
        <span class="tool-access-intent-radio" aria-hidden="true"></span>
        <span class="tool-access-intent-copy">
          <strong>{option.label()}</strong>
          <span>{option.description()}</span>
        </span>
      </button>
    {/each}
  </div>

  <div class="tool-access-toolbar">
    <label class="tool-access-search">
      <span class="sr-only">{t('toolAccess.searchLabel', 'Search Tools')}</span>
      <svg
        class="tool-access-search-icon"
        viewBox="0 0 16 16"
        width="16"
        height="16"
        aria-hidden="true"
      >
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
        placeholder={t('toolAccess.searchPlaceholder', 'Find a Tool…')}
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
        <section class="tool-access-group">
          <header class="tool-access-group-header">
            <div>
              <h4>{familyLabel(group)}</h4>
              <p>{groupSummary(group)}</p>
            </div>
            <div
              class="tool-access-family-actions"
              aria-label={t('toolAccess.family.actions', 'Family actions')}
            >
              <button
                type="button"
                {disabled}
                aria-label={familyActionLabel(
                  group,
                  t('toolAccess.family.allow', 'Allow current'),
                )}
                onclick={() => updateGroup(group, 'enabled')}
              >
                {t('toolAccess.family.allow', 'Allow current')}
              </button>
              <button
                type="button"
                {disabled}
                aria-label={familyActionLabel(
                  group,
                  t('toolAccess.family.block', 'Block current'),
                )}
                onclick={() => updateGroup(group, 'denied')}
              >
                {t('toolAccess.family.block', 'Block current')}
              </button>
              <button
                type="button"
                {disabled}
                aria-label={familyActionLabel(
                  group,
                  t('toolAccess.family.reset', 'Reset current'),
                )}
                onclick={() => updateGroup(group, 'default')}
              >
                {t('toolAccess.family.reset', 'Reset current')}
              </button>
            </div>
          </header>

          <div class="tool-access-rows">
            {#each group.members as tool (tool.name)}
              <div
                class:tool-access-row--unavailable={Boolean(
                  readinessNote(tool),
                )}
                class="tool-access-row"
                data-tool-name={tool.name}
              >
                <div class="tool-access-tool-copy">
                  <div class="tool-access-tool-heading">
                    <code>{tool.name}</code>
                    <StatusChip variant={stateVariant(tool)}>
                      {stateLabel(tool)}
                    </StatusChip>
                  </div>
                  {#if tool.description}
                    <p>{tool.description}</p>
                  {/if}
                  {#if automaticNote(tool) || readinessNote(tool)}
                    <div class="tool-access-notes">
                      {#if automaticNote(tool)}<span>{automaticNote(tool)}</span
                        >{/if}
                      {#if readinessNote(tool)}
                        <span class="tool-access-unavailable-note"
                          >{readinessNote(tool)}</span
                        >
                        {#if tool.extension}
                          <button
                            type="button"
                            onclick={() => onOpenExtensions(tool.extension)}
                          >
                            {t('toolAccess.configureExtension', 'Configure')}
                          </button>
                        {/if}
                      {/if}
                    </div>
                  {/if}
                </div>

                <div
                  class="tool-access-state-control"
                  aria-label={t('toolAccess.stateFor', 'Access for {name}', {
                    name: tool.name,
                  })}
                >
                  <button
                    type="button"
                    class:tool-access-choice--selected={defaultStateSelected(
                      tool,
                    )}
                    data-tool-access-state="default"
                    aria-pressed={defaultStateSelected(tool)}
                    aria-label={t(
                      'toolAccess.setDefaultFor',
                      'Use default access for {name}',
                      { name: tool.name },
                    )}
                    {disabled}
                    onclick={() => updateTool(tool, 'default')}
                  >
                    {defaultChoiceLabel(tool)}
                  </button>
                  {#if toolIsConfigurable(tool)}
                    <button
                      type="button"
                      class:tool-access-choice--selected={accessState(tool) ===
                        'enabled'}
                      data-tool-access-state="enabled"
                      aria-pressed={accessState(tool) === 'enabled'}
                      aria-label={t('toolAccess.enableFor', 'Enable {name}', {
                        name: tool.name,
                      })}
                      disabled={disabled ||
                        policy.mode === TOOL_ACCESS_MODE_ALL}
                      onclick={() => updateTool(tool, 'enabled')}
                    >
                      {t('toolAccess.choice.on', 'On')}
                    </button>
                  {/if}
                  <button
                    type="button"
                    class:tool-access-choice--danger={accessState(tool) ===
                      'denied'}
                    data-tool-access-state="denied"
                    aria-pressed={accessState(tool) === 'denied'}
                    aria-label={t('toolAccess.denyFor', 'Block {name}', {
                      name: tool.name,
                    })}
                    {disabled}
                    onclick={() => updateTool(tool, 'denied')}
                  >
                    {t('toolAccess.choice.block', 'Block')}
                  </button>
                </div>
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
    gap: 14px;
  }

  .tool-access-intent {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
  }

  .tool-access-intent-card {
    display: flex;
    gap: 10px;
    min-width: 0;
    padding: 12px;
    text-align: left;
    color: var(--text-med);
    border: 1px solid var(--border);
    border-radius: 9px;
    background: var(--surface);
    cursor: pointer;
  }

  .tool-access-intent-card:hover:not(:disabled) {
    border-color: var(--border-2);
    background: var(--surface-2);
  }

  .tool-access-intent-card:focus-visible,
  .tool-access-family-actions button:focus-visible,
  .tool-access-notes button:focus-visible,
  .tool-access-state-control button:focus-visible {
    outline: 0;
    box-shadow: var(--focus-ring);
  }

  .tool-access-intent-card--selected {
    border-color: var(--accent);
    box-shadow: inset 0 0 0 1px var(--accent-40);
    background: var(--accent-dim);
  }

  .tool-access-intent-radio {
    flex: 0 0 auto;
    width: 14px;
    height: 14px;
    margin-top: 2px;
    border: 1px solid var(--border-2);
    border-radius: 50%;
    box-shadow: inset 0 0 0 3px var(--surface);
    background: transparent;
  }

  .tool-access-intent-card--selected .tool-access-intent-radio {
    border-color: var(--accent);
    background: var(--accent);
  }

  .tool-access-intent-copy {
    display: grid;
    gap: 3px;
    min-width: 0;
  }

  .tool-access-intent-copy strong {
    color: var(--text-hi);
    font-size: var(--fs-label-md);
  }

  .tool-access-intent-copy span {
    font-size: var(--fs-body-sm);
    line-height: 1.4;
  }

  .tool-access-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .tool-access-search {
    display: flex;
    align-items: center;
    width: min(330px, 100%);
    padding: 0 10px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
  }

  .tool-access-search:focus-within {
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }

  .tool-access-search-icon {
    color: var(--text-lo);
  }

  .tool-access-search input {
    width: 100%;
    min-width: 0;
    padding: 8px;
    color: var(--text-hi);
    border: 0;
    outline: 0;
    background: transparent;
    font: inherit;
    font-size: var(--fs-mono-body);
  }

  .tool-access-groups {
    display: grid;
    gap: 10px;
  }

  .tool-access-group {
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: 9px;
    background: var(--surface);
  }

  .tool-access-group-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
  }

  .tool-access-group-header h4,
  .tool-access-group-header p,
  .tool-access-tool-copy p {
    margin: 0;
  }

  .tool-access-group-header h4 {
    color: var(--text-hi);
    font-size: var(--fs-label-sm);
    letter-spacing: 0.02em;
  }

  .tool-access-group-header p {
    margin-top: 2px;
    color: var(--text-lo);
    font-size: var(--fs-mono-xs);
  }

  .tool-access-family-actions {
    display: flex;
    gap: 2px;
  }

  .tool-access-family-actions button,
  .tool-access-notes button {
    padding: 3px 6px;
    color: var(--text-med);
    border: 0;
    border-radius: 4px;
    background: transparent;
    cursor: pointer;
    font: inherit;
    font-size: var(--fs-mono-xs);
  }

  .tool-access-family-actions button:hover:not(:disabled),
  .tool-access-notes button:hover:not(:disabled) {
    color: var(--text-hi);
    background: var(--surface);
  }

  .tool-access-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 14px;
    min-height: 60px;
    padding: 10px 12px;
    border-top: 1px solid var(--border);
  }

  .tool-access-row:first-child {
    border-top: 0;
  }

  .tool-access-row--unavailable {
    background: color-mix(in srgb, var(--surface-2) 45%, transparent);
  }

  .tool-access-tool-copy {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .tool-access-tool-heading,
  .tool-access-notes {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
  }

  .tool-access-tool-heading code {
    color: var(--text-hi);
    font-size: var(--fs-mono-body);
  }

  .tool-access-tool-copy > p {
    overflow: hidden;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.35;
    text-overflow: ellipsis;
  }

  .tool-access-notes {
    color: var(--text-lo);
    font-size: var(--fs-mono-xs);
  }

  .tool-access-unavailable-note {
    color: var(--amber);
  }

  .tool-access-state-control {
    display: inline-flex;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--surface-2);
  }

  .tool-access-state-control button {
    min-width: 45px;
    padding: 6px 7px;
    color: var(--text-lo);
    border: 0;
    border-left: 1px solid var(--border);
    background: transparent;
    cursor: pointer;
    font: inherit;
    font-size: var(--fs-mono-xs);
  }

  .tool-access-state-control button:first-child {
    border-left: 0;
  }

  .tool-access-state-control button:hover:not(:disabled) {
    color: var(--text-hi);
    background: var(--surface);
  }

  .tool-access-state-control button:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  .tool-access-state-control .tool-access-choice--selected {
    color: var(--accent);
    background: var(--accent-dim);
  }

  .tool-access-state-control .tool-access-choice--danger {
    color: var(--red);
    background: color-mix(in srgb, var(--red) 8%, transparent);
  }

  .sr-only {
    position: absolute;
    overflow: hidden;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  @media (max-width: 760px) {
    .tool-access-intent {
      grid-template-columns: 1fr;
    }

    .tool-access-toolbar,
    .tool-access-group-header {
      align-items: stretch;
      flex-direction: column;
    }

    .tool-access-search {
      width: 100%;
    }

    .tool-access-row {
      grid-template-columns: 1fr;
    }

    .tool-access-state-control {
      width: 100%;
    }

    .tool-access-state-control button {
      flex: 1;
    }
  }
</style>
