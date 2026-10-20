<script>
  import { onDestroy, untrack } from 'svelte';

  import ToolCatalogEditor from '../tools/ToolCatalogEditor.svelte';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { createSettingsDraft } from '$lib/settingsSave.js';
  import {
    buildToolLoadingDefaultPayload,
    normalizeToolLoadingDefaultSettings,
  } from '$lib/settingsView.js';
  import {
    completeToolLoading,
    isFileEditTool,
    resetAlwaysLoadedTools,
    setToolAlwaysLoaded,
    setToolsOnDemand,
    toolCatalogForEditor,
    toolIsAlwaysLoaded,
    toolLoadingInheritsList,
  } from '$lib/toolAccess.js';

  const noop = () => {};

  // The shared On-demand Tools default (`defaults.agent.tool_loading`):
  // every Agent and Project Agent inherits each part it does not set itself.
  // Without a shared list the built-in always-loaded Tools apply.
  let {
    settings = null,
    tools = [],
    onCommit = noop,
    onError = noop,
  } = $props();

  let form = $state(
    untrack(() => normalizeToolLoadingDefaultSettings(settings)),
  );
  let saving = $state(false);
  const draft = createSettingsDraft({
    settings: untrack(() => settings),
    fromSettings: normalizeToolLoadingDefaultSettings,
    read: () => form,
    write: (next) => (form = next),
    toPayload: buildToolLoadingDefaultPayload,
  });

  // Session-granted Tools are always sent while the Session grants them, so
  // they have no "Always loaded" choice; the Live call Tools are never on
  // demand.
  let catalog = $derived(
    toolCatalogForEditor(tools).filter(
      (tool) => tool.activation !== 'session_grant',
    ),
  );
  let builtIn = $derived(completeToolLoading(null, catalog));
  let items = $derived(
    catalog.map((tool) => ({
      ...tool,
      allowed: alwaysLoaded(tool),
      automatic: false,
      requires_opt_in: false,
      notes: isFileEditTool(tool.name)
        ? [t('toolAccess.alwaysLoaded.fileEditUnit')]
        : [],
    })),
  );
  let onDemand = $derived(form.tool_loading?.on_demand === true);
  let inheritsList = $derived(toolLoadingInheritsList(form.tool_loading));

  let saveDisabled = $derived(saving || !hasChanges());
  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...form }),
    hasChanges,
    save,
  });
  const unregisterAutosave = autosaveContext.register(autosave.participant);

  onDestroy(() => {
    unregisterAutosave();
    autosave.cancelPendingTimer();
  });

  $effect(() => {
    if (saveDisabled) return;
    autosave.scheduleRun();
    return () => autosave.cancelPendingTimer();
  });

  function hasChanges() {
    return (
      JSON.stringify(normalizeToolLoadingDefaultSettings(form)) !==
      JSON.stringify(normalizeToolLoadingDefaultSettings(settings))
    );
  }

  function alwaysLoaded(tool) {
    return toolIsAlwaysLoaded(form.tool_loading, tool, builtIn, catalog);
  }

  function change(toolLoading) {
    form = { tool_loading: toolLoading };
    onError('');
  }

  // Off is the built-in default, so switching off leaves only a chosen list.
  function setOnDemand(enabled) {
    change(setToolsOnDemand(form.tool_loading, enabled ? true : null));
  }

  function toggleTool(tool) {
    change(
      setToolAlwaysLoaded(
        form.tool_loading,
        tool,
        !alwaysLoaded(tool),
        builtIn,
        catalog,
      ),
    );
  }

  function toggleGroup(members, enabled) {
    let next = form.tool_loading;
    for (const tool of members) {
      next = setToolAlwaysLoaded(next, tool, enabled, builtIn, catalog);
    }
    change(next);
  }

  async function save() {
    if (!hasChanges()) return true;
    return draft.save({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
    });
  }
</script>

<div class="s-group">
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('toolAccess.onDemand.label')}
        <InfoHint text={t('settings.toolLoading.help')} />
      </div>
      <div class="s-row-desc">{t('settings.toolLoading.description')}</div>
    </div>
    <div class="s-row-control">
      <Toggle
        checked={onDemand}
        ariaLabel={t('toolAccess.onDemand.label')}
        onChange={setOnDemand}
      />
    </div>
  </div>
  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">{t('toolAccess.alwaysLoaded.title')}</div>
      <div class="s-row-desc">
        {inheritsList
          ? t('settings.toolLoading.builtInList')
          : t('settings.toolLoading.ownList')}
      </div>
    </div>
    <div class="s-row-control">
      {#if !inheritsList}
        <Button
          variant="tertiary"
          tooltip={t('settings.toolLoading.resetHint')}
          onClick={() => change(resetAlwaysLoadedTools(form.tool_loading))}
          >{t('toolAccess.alwaysLoaded.reset')}</Button
        >
      {/if}
    </div>
  </div>
</div>
<ToolCatalogEditor
  {items}
  onToggle={toggleTool}
  onToggleGroup={toggleGroup}
  toggleLabel={(tool) =>
    t('toolAccess.alwaysLoaded.toggle', { name: tool.name })}
  summaryLabel={(enabled, total) =>
    t('settings.toolLoading.count', { enabled, total })}
  showAccessFact={false}
/>
<div class="s-footer">
  <SaveStatus
    {saving}
    pending={autosave.participant.hasChanges()}
    onClick={() => autosave.participant.runSave('manual')}
  />
</div>
