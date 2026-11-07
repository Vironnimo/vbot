<script>
  import { onDestroy, untrack } from 'svelte';
  import { updateSettings } from '$lib/api.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import Banner from '../../ui/Banner.svelte';
  import FormField from '../../ui/FormField.svelte';
  import PathField from '../../ui/PathField.svelte';
  import SaveStatus from '../../ui/SaveStatus.svelte';

  let { provider } = $props();
  let draft = $state(untrack(() => provider.executable_path ?? ''));
  let baseline = $state(untrack(() => provider.executable_path ?? ''));
  let saving = $state(false);
  let error = $state('');
  let observedPath = untrack(() => provider.executable_path ?? '');
  let pendingServerPath = null;
  const controlId = $props.id();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => draft,
    hasChanges: () => draft.trim() !== baseline,
    save,
  });
  const unregister = useAutosaveContext().register(autosave.participant);
  $effect(() => {
    const current = provider.executable_path ?? '';
    const canReconcile = !saving && draft.trim() === baseline;
    untrack(() => {
      if (current !== observedPath) {
        observedPath = current;
        pendingServerPath = current;
      }
      if (pendingServerPath === baseline) pendingServerPath = null;
      if (canReconcile && pendingServerPath !== null) {
        draft = baseline = pendingServerPath;
        pendingServerPath = null;
      }
    });
  });
  $effect(() => {
    if (saving || !autosave.participant.hasChanges()) return;
    autosave.scheduleRun();
    return autosave.cancelPendingTimer;
  });
  onDestroy(() => {
    unregister();
    autosave.cancelPendingTimer();
  });

  async function save() {
    const path = draft.trim();
    const origin = baseline;
    saving = true;
    error = '';
    try {
      await updateSettings({
        providers: { claude_agent: { executable_path: path } },
        base: { providers: { claude_agent: { executable_path: origin } } },
      });
      baseline = path;
      return true;
    } catch (failure) {
      error = failure.message;
      return false;
    } finally {
      saving = false;
    }
  }
</script>

<div>
  <FormField
    {controlId}
    label={t('settings.providers.claudeAgent.executable')}
    help={t('settings.providers.claudeAgent.executableHelp')}
  >
    {#snippet children(field)}
      <PathField
        id={field.controlId}
        aria-describedby={field.describedBy}
        mode="file"
        value={draft}
        onInput={(value) => (draft = value)}
        ariaLabel={t('settings.providers.claudeAgent.executable')}
        placeholder={t('settings.providers.claudeAgent.executablePlaceholder')}
      />
    {/snippet}
  </FormField>
  {#if error}<Banner variant="error">{error}</Banner>{/if}
  <SaveStatus
    {saving}
    pending={autosave.participant.hasChanges()}
    onClick={() => autosave.participant.runSave('manual')}
  />
</div>
