<script>
  import { onDestroy, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { activationSources, toolCatalogForEditor } from '$lib/toolAccess.js';
  import { t } from '$lib/i18n.js';
  import Dropdown from '../../Dropdown.svelte';
  import ToolCatalogEditor from '../../tools/ToolCatalogEditor.svelte';
  import SaveStatus from '../../ui/SaveStatus.svelte';

  let {
    channelId,
    scopeId = null,
    value,
    inherited,
    tools = [],
    onSave,
    onError,
  } = $props();
  const id = $props.id();
  const fingerprint = (policy) => JSON.stringify(policy);
  let draft = $state(untrack(() => value));
  let baseline = $state(untrack(() => fingerprint(value)));
  let lastServer = $state(untrack(() => baseline));
  let saving = $state(false);
  const participant = createAutosaveParticipant({
    getSnapshot: () => ({ channelId, scopeId, policy: draft }),
    hasChanges: () => fingerprint(draft) !== baseline,
    save: persist,
  });
  const unregister = useAutosaveContext().register(participant);
  onDestroy(unregister);

  $effect(() => {
    const received = fingerprint(value);
    if (saving || received === lastServer) return;
    if (fingerprint(draft) === baseline) draft = value;
    baseline = received;
    lastServer = received;
  });

  let effective = $derived(draft ?? inherited);
  let mode = $derived(draft?.mode ?? 'inherit');
  let options = $derived([
    ...(scopeId === null
      ? []
      : [
          {
            value: 'inherit',
            label: t('settings.channels.memberTools.inherit'),
          },
        ]),
    { value: 'none', label: t('settings.channels.memberTools.none') },
    { value: 'selected', label: t('settings.channels.memberTools.selected') },
    { value: 'all', label: t('settings.channels.memberTools.all') },
  ]);
  let catalog = $derived.by(() => {
    const visible = toolCatalogForEditor(tools).filter(
      (tool) => tool.activation !== 'follows' && tool.name !== 'load_tools',
    );
    for (const name of draft?.allowed ?? []) {
      if (!visible.some((tool) => tool.name === name)) {
        visible.push(
          tools.find((tool) => tool.name === name) ?? {
            name,
            ready: false,
            registered: false,
          },
        );
      }
    }
    return visible.map((tool) => ({
      ...tool,
      allowed: (draft?.allowed ?? []).includes(tool.name),
    }));
  });
  let effectiveNames = $derived.by(() => {
    const names = new SvelteSet(effective?.allowed ?? []);
    let changed = true;
    while (changed) {
      changed = false;
      for (const tool of tools) {
        if (
          tool.activation === 'follows' &&
          !names.has(tool.name) &&
          activationSources(tool).some((name) => names.has(name))
        ) {
          names.add(tool.name);
          changed = true;
        }
      }
    }
    return [...names].sort().join(', ');
  });

  function update(policy) {
    draft = policy;
    void participant.runSave();
  }
  function changeMode(next) {
    update(
      next === 'inherit'
        ? null
        : next === 'selected'
          ? { mode: next, allowed: [...(effective?.allowed ?? [])] }
          : { mode: next },
    );
  }
  function toggleTools(items, enabled) {
    const names = new SvelteSet(draft?.allowed ?? []);
    for (const tool of items) {
      if (enabled) names.add(tool.name);
      else names.delete(tool.name);
    }
    update({ mode: 'selected', allowed: [...names].sort() });
  }
  async function persist() {
    const submitted = fingerprint(draft);
    saving = true;
    try {
      const saved = await onSave(channelId, scopeId, JSON.parse(submitted));
      const policy =
        scopeId === null
          ? saved.member_tools
          : (saved.groups.find((group) => group.access_scope_id === scopeId)
              ?.member_tools ?? null);
      baseline = fingerprint(policy);
      lastServer = baseline;
      if (fingerprint(draft) === submitted) draft = policy;
      return true;
    } catch (error) {
      onError(`${t('settings.saveError')} ${error.message}`);
      return false;
    } finally {
      saving = false;
    }
  }
</script>

<div
  class="s-channel-member-tools"
  data-member-tools-scope={scopeId ?? 'default'}
>
  <div class="s-row">
    <div class="s-row-info">
      <label class="s-row-label" for={id}
        >{scopeId === null
          ? t('settings.channels.memberTools.default')
          : t('settings.channels.memberTools.title')}</label
      >
      <div class="s-row-desc">
        {draft === null
          ? t('settings.channels.memberTools.inherited')
          : t('settings.channels.memberTools.custom')}
      </div>
    </div>
    <div class="s-row-control">
      <Dropdown
        {id}
        value={mode}
        {options}
        ariaLabel={scopeId === null
          ? t('settings.channels.memberTools.default')
          : t('settings.channels.memberTools.title')}
        onValueChange={changeMode}
      />
    </div>
  </div>
  <p class="s-row-desc">
    {t('settings.channels.memberTools.effective', {
      tools:
        effective?.mode === 'all'
          ? t('settings.channels.memberTools.all')
          : effective?.mode === 'none' || !effectiveNames
            ? t('settings.channels.memberTools.none')
            : effectiveNames,
    })}
  </p>
  {#if mode === 'selected'}
    <ToolCatalogEditor
      items={catalog}
      showAccessFact={false}
      onToggle={(tool, enabled) => toggleTools([tool], enabled)}
      onToggleGroup={toggleTools}
    />
  {/if}
  <SaveStatus
    {saving}
    pending={participant.hasChanges()}
    onClick={() => participant.runSave('manual')}
  />
</div>
