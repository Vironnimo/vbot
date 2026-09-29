<script>
  import Button from '../ui/Button.svelte';
  import ToolCatalogEditor from './ToolCatalogEditor.svelte';
  import FormField from '../ui/FormField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    TOOL_ACCESS_MODE_ALL,
    TOOL_ACCESS_MODE_NONE,
    groupToolCatalog,
    normalizeToolAccess,
    policyNamesNotInCatalog,
    setAnalyzeImageAlwaysAvailable,
    setToolAccessPreference,
    setToolFamilyPreference,
    toolAccessPreferenceEnabled,
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

  let policy = $derived(normalizeToolAccess(value));
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

<div class="tool-access-editor">
  <ToolCatalogEditor
    items={catalogItems}
    {disabled}
    onToggle={updateTool}
    onToggleGroup={updateGroup}
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
