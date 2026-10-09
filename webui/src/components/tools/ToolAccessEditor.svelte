<script>
  import Button from '../ui/Button.svelte';
  import ToolCatalogEditor from './ToolCatalogEditor.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    TOOL_ACCESS_MODE_ALL,
    TOOL_ACCESS_MODE_NONE,
    groupToolCatalog,
    isFileEditTool,
    normalizeToolAccess,
    normalizeToolLoading,
    policyNamesNotInCatalog,
    resetAlwaysLoadedTools,
    setAnalyzeImageAlwaysAvailable,
    setToolAccessPreference,
    setToolAlwaysLoaded,
    setToolFamilyPreference,
    setToolsOnDemand,
    toolAccessPreferenceEnabled,
    toolCatalogForEditor,
    toolIsAlwaysLoaded,
    toolIsConfigurable,
    toolLoadingUsesDefaultSet,
  } from '$lib/toolAccess.js';
  import { tick } from 'svelte';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    value = { mode: TOOL_ACCESS_MODE_ALL },
    tools = [],
    ceiling = null,
    disabled = false,
    // Edits a Live voice Agent: lists the Live call Tools and only
    // configurable Tools (`toolCatalogForEditor`).
    liveCall = false,
    memoryPromptMode = 'agent_user',
    showReset = false,
    resetLabel = '',
    // On-demand Tools: with `toolLoadingEditable` the editor shows the "Load
    // Tools on demand" switch for `toolLoading` (`tool_loading`, null while
    // off) and, while it is on, an "Always loaded" pin on every allowed Tool
    // row. Editors that leave it out are unchanged.
    toolLoadingEditable = false,
    toolLoading = null,
    toolLoadingDisabled = false,
    onChange = noop,
    onToolLoadingChange = noop,
    onReset = noop,
    onOpenExtensions = noop,
  } = $props();

  let policy = $derived(normalizeToolAccess(value));
  let loading = $derived(normalizeToolLoading(toolLoading));
  let onDemand = $derived(toolLoadingEditable && loading?.on_demand === true);
  let loadingLocked = $derived(disabled || toolLoadingDisabled);
  let completeCatalog = $derived(catalogWithStoredTools());
  let catalogItems = $derived(
    groupToolCatalog(completeCatalog, ceiling)
      .flatMap((group) => group.members)
      .map((tool) => ({
        ...tool,
        allowed: preferenceEnabled(tool),
        automatic: !toolIsConfigurable(tool),
        notes: toolNotes(tool),
      })),
  );

  function catalogWithStoredTools() {
    const catalog = toolCatalogForEditor(tools, { liveCall });
    // A hidden catalog Tool is known, not a stored name to list as missing.
    const unknown = policyNamesNotInCatalog(policy, tools);
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

  function selectAllTools() {
    updateGroup(catalogItems, true);
  }

  function preferenceEnabled(tool) {
    return toolAccessPreferenceEnabled(policy, tool);
  }

  function updateTool(tool) {
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

  function updateGroup(members, enabled) {
    onChange(
      setToolFamilyPreference(
        policy,
        members,
        enabled,
        completeCatalog,
        ceiling,
      ),
    );
  }

  // Session-granted Tools are always sent while the Session grants them, so
  // they have no "Always loaded" choice.
  function offersLoadingChoice(tool) {
    return tool.allowed && tool.activation !== 'session_grant';
  }

  function alwaysLoaded(tool) {
    return toolIsAlwaysLoaded(loading, tool, completeCatalog);
  }

  let loadingCounts = $derived.by(() => {
    const choices = catalogItems.filter(offersLoadingChoice);
    const always = choices.filter(alwaysLoaded).length;
    return { alwaysLoaded: always, onDemand: choices.length - always };
  });

  function toggleAlwaysLoaded(tool) {
    onToolLoadingChange(
      setToolAlwaysLoaded(loading, tool, !alwaysLoaded(tool), completeCatalog),
    );
  }

  function pinHint(tool) {
    const state = alwaysLoaded(tool)
      ? t('toolAccess.alwaysLoaded.pinnedHint')
      : t('toolAccess.alwaysLoaded.unpinnedHint');
    return isFileEditTool(tool.name)
      ? `${state} ${t('toolAccess.alwaysLoaded.fileEditUnit')}`
      : state;
  }

  let editorRoot = $state();

  // The reset button disappears once the default set applies again, so focus
  // moves to the switch instead of dropping to the page.
  async function resetAlwaysLoaded() {
    onToolLoadingChange(resetAlwaysLoadedTools(loading));
    await tick();
    editorRoot?.querySelector('[data-tool-loading-switch]')?.focus();
  }

  function toolNotes(tool) {
    const notes = [];
    if (tool.activation === 'follows') {
      notes.push(
        t('toolAccess.activation.follows', {
          source: tool.activation_source,
        }),
      );
    } else if (tool.activation === 'memory_mode') {
      notes.push(
        memoryPromptMode === 'off'
          ? t('toolAccess.activation.memoryOff')
          : t('toolAccess.activation.memoryOn'),
      );
    } else if (tool.activation === 'session_grant') {
      notes.push(t('toolAccess.activation.session'));
    }
    if ((tool.constraints ?? []).includes('identity_agent')) {
      notes.push(t('toolAccess.constraint.identity'));
    }
    if ((tool.constraints ?? []).includes('image_fallback_route')) {
      notes.push(t('toolAccess.constraint.imageFallback'));
    }
    if (tool.registered === false) {
      notes.push(t('toolAccess.readiness.unregistered'));
    }
    return notes;
  }
</script>

<div class="tool-access-editor" bind:this={editorRoot}>
  {#if toolLoadingEditable}
    <div class="s-group tool-loading-group">
      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('toolAccess.onDemand.label')}
            <InfoHint text={t('toolAccess.onDemand.help')} />
          </div>
          <div class="s-row-desc">{t('toolAccess.onDemand.description')}</div>
        </div>
        <div class="s-row-control">
          <Toggle
            checked={onDemand}
            disabled={loadingLocked}
            ariaLabel={t('toolAccess.onDemand.label')}
            data-tool-loading-switch
            onChange={(next) =>
              onToolLoadingChange(setToolsOnDemand(loading, next))}
          />
        </div>
      </div>
      {#if onDemand}
        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <div class="s-row-label">{t('toolAccess.alwaysLoaded.title')}</div>
            <div class="s-row-desc" data-tool-loading-summary>
              {t('toolAccess.alwaysLoaded.summary', {
                alwaysLoaded: loadingCounts.alwaysLoaded,
                onDemand: loadingCounts.onDemand,
              })}
            </div>
          </div>
          <div class="s-row-control">
            {#if !toolLoadingUsesDefaultSet(loading)}
              <Button
                variant="tertiary"
                disabled={loadingLocked}
                tooltip={t('toolAccess.alwaysLoaded.resetHint')}
                onClick={resetAlwaysLoaded}
                >{t('toolAccess.alwaysLoaded.reset')}</Button
              >
            {/if}
          </div>
        </div>
      {/if}
    </div>
  {/if}
  <ToolCatalogEditor
    items={catalogItems}
    {disabled}
    onToggle={updateTool}
    onToggleGroup={updateGroup}
    rowAction={onDemand ? alwaysLoadedPin : undefined}
    {onOpenExtensions}
  >
    {#snippet toolbar()}
      <Button variant="tertiary" {disabled} onClick={selectAllTools}
        >{t('toolAccess.selectAll')}</Button
      >
      <Button
        variant="tertiary"
        {disabled}
        onClick={() => onChange({ mode: TOOL_ACCESS_MODE_NONE })}
        >{t('toolAccess.deselectAll')}</Button
      >
      {#if showReset}
        <Button variant="tertiary" {disabled} onClick={onReset}
          >{resetLabel || t('toolAccess.resetOverride')}</Button
        >
      {/if}
    {/snippet}
    {#snippet details(tool)}
      {#if tool.name === 'analyze_image'}
        <FormField label={t('toolAccess.imageAlwaysAvailable')}>
          <Toggle
            size="sm"
            checked={(policy.granted ?? []).includes(tool.name)}
            disabled={disabled || !preferenceEnabled(tool)}
            ariaLabel={t('toolAccess.imageAlwaysAvailable')}
            onChange={(next) =>
              onChange(setAnalyzeImageAlwaysAvailable(policy, next))}
          />
        </FormField>
      {/if}
    {/snippet}
  </ToolCatalogEditor>
</div>

<!-- The "Always loaded" pin at the end of an allowed Tool row; other rows
     keep its place so the row states stay aligned. -->
{#snippet alwaysLoadedPin(tool)}
  {#if offersLoadingChoice(tool)}
    {@const pinned = alwaysLoaded(tool)}
    <Button
      variant="tertiary"
      icon
      class="tool-loading-pin"
      aria-pressed={pinned ? 'true' : 'false'}
      ariaLabel={t('toolAccess.alwaysLoaded.toggle', { name: tool.name })}
      tooltip={pinHint(tool)}
      disabled={loadingLocked}
      data-tool-always-loaded={tool.name}
      onClick={() => toggleAlwaysLoaded(tool)}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        stroke-width="1.3"
        stroke-linejoin="round"
        stroke-linecap="round"
        aria-hidden="true"
        ><path
          class="tool-loading-pin__head"
          d="M10 1.8 14.2 6l-2 .9-2.6 2.6-.5 3.1L3.4 6.9l3.1-.5 2.6-2.6z"
        /><path d="M6.3 9.7 2 14" /></svg
      >
    </Button>
  {:else}
    <span class="tool-loading-pin-space" aria-hidden="true"></span>
  {/if}
{/snippet}

<style>
  .tool-access-editor {
    display: grid;
    gap: 14px;
    min-width: 0;
  }
  /* The pin shares the row's "⋯" button geometry. An on-demand Tool's pin
     shows while the row is hovered or focused (always on touch screens), so
     the pinned Tools stand out; a pinned one is filled with the accent like
     a pinned Skill. */
  .tool-access-editor :global(.btn-tertiary.btn-icon.tool-loading-pin),
  .tool-loading-pin-space {
    flex-shrink: 0;
    width: 28px;
    height: 28px;
    min-height: 28px;
    margin-right: 4px;
  }
  .tool-access-editor :global(.tool-loading-pin[aria-pressed='false']) {
    opacity: 0;
  }
  .tool-access-editor :global(.s-check-item:hover .tool-loading-pin),
  .tool-access-editor :global(.s-check-item:focus-within .tool-loading-pin) {
    opacity: 1;
  }
  .tool-access-editor :global(.tool-loading-pin[aria-pressed='true']) {
    color: var(--accent);
  }
  .tool-access-editor
    :global(.tool-loading-pin[aria-pressed='true'] .tool-loading-pin__head) {
    fill: currentColor;
  }
  @media (hover: none), (pointer: coarse) {
    .tool-access-editor :global(.tool-loading-pin[aria-pressed='false']) {
      opacity: 1;
    }
  }
</style>
