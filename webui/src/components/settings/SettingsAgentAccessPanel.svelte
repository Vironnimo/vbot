<script>
  import {
    getSettings as reloadDraftSettings,
    updateSettings as submitDraftSettings,
  } from '$lib/api.js';

  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import ToolAccessEditor from '../tools/ToolAccessEditor.svelte';
  import FormField from '../ui/FormField.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import { t } from '$lib/i18n.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { createSettingsEditorDraft } from '$lib/settingsEditorDraft.svelte.js';
  import {
    buildAgentAccessDefaultPayload,
    normalizeAgentAccessDefaultSettings,
  } from '$lib/settingsView.js';

  const noop = () => {};
  const ALL_SKILLS = 'all';
  const NO_SKILLS = 'none';
  const SELECTED_SKILLS = 'selected';

  // The shared Tool access and Skills (`defaults.agent.tool_access`,
  // `.allowed_skills`): every Agent, Project Agent, Factory member and Swarm
  // Agent without its own follows them, also after a change.
  let {
    settings = null,
    tools = [],
    onCommit = noop,
    onError = noop,
  } = $props();

  let form = $state(
    untrack(() => normalizeAgentAccessDefaultSettings(settings)),
  );
  let saving = $state(false);
  const draft = createSettingsEditorDraft({
    reload: reloadDraftSettings,
    submit: submitDraftSettings,
    getSettings: () => settings,
    onError: (message) => onError(message),
    fromSettings: normalizeAgentAccessDefaultSettings,
    read: () => form,
    write: (next) => (form = next),
    toPayload: buildAgentAccessDefaultPayload,
    // A Tool selection or Skill list only means something as a whole.
    leafDepth: 1,
  });

  let saveDisabled = $derived(saving || !hasChanges());
  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => JSON.parse(JSON.stringify(form)),
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

  let skillsChoice = $derived(
    form.allowed_skills.includes('*')
      ? ALL_SKILLS
      : form.allowed_skills.length
        ? SELECTED_SKILLS
        : NO_SKILLS,
  );
  let skillOptions = $derived([
    { value: ALL_SKILLS, label: t('agents.shared.skillsAll') },
    { value: NO_SKILLS, label: t('agents.shared.skillsNone') },
    // A list of Skill names set elsewhere (CLI, settings.json) stays
    // selectable; this panel does not edit the names themselves.
    ...(skillsChoice === SELECTED_SKILLS
      ? [
          {
            value: SELECTED_SKILLS,
            label: t('agents.shared.skillsSelected', {
              count: form.allowed_skills.length,
            }),
          },
        ]
      : []),
  ]);

  function hasChanges() {
    return (
      JSON.stringify(normalizeAgentAccessDefaultSettings(form)) !==
      JSON.stringify(normalizeAgentAccessDefaultSettings(settings))
    );
  }

  function update(changes) {
    form = { ...form, ...changes };
    onError('');
  }

  function selectSkills(choice) {
    if (choice === ALL_SKILLS) update({ allowed_skills: ['*'] });
    else if (choice === NO_SKILLS) update({ allowed_skills: [] });
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

<div class="s-group agent-access-defaults">
  <ToolAccessEditor
    value={form.tool_access}
    {tools}
    onChange={(next) => update({ tool_access: next })}
  />
  <FormField
    label={t('agents.shared.skills')}
    controlId="agent-access-defaults-skills"
  >
    <Dropdown
      id="agent-access-defaults-skills"
      ariaLabel={t('agents.shared.skills')}
      value={skillsChoice}
      options={skillOptions}
      onValueChange={selectSkills}
    />
  </FormField>
  <div class="s-footer">
    <SaveStatus
      {saving}
      pending={autosave.participant.hasChanges()}
      onClick={() => autosave.participant.runSave('manual')}
    />
  </div>
</div>

<style>
  .agent-access-defaults {
    display: grid;
    gap: var(--space-md);
  }
</style>
