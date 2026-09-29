<script>
  import { onDestroy, untrack } from 'svelte';

  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import TextField from '../ui/TextField.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { runSettingsSave } from '$lib/settingsSave.js';
  import {
    buildSubAgentSettingsPayload,
    normalizeSubAgentSettings,
  } from '$lib/settingsView.js';

  const noop = () => {};

  let { settings = null, onCommit = noop, onError = noop } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let subAgentSettings = $state(
    untrack(() => normalizeSubAgentSettings(settings)),
  );
  let saving = $state(false);

  let saveDisabled = $derived(saving || !subAgentDraftHasChanges());
  const autosaveContext = useAutosaveContext();
  const subAgentsAutosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...subAgentSettings }),
    hasChanges: subAgentDraftHasChanges,
    save: saveSubAgentSettings,
  });
  const unregisterSubAgentsAutosave = autosaveContext.register(
    subAgentsAutosave.participant,
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    subAgentsAutosave.scheduleRun();

    return () => {
      subAgentsAutosave.cancelPendingTimer();
    };
  });

  onDestroy(() => {
    unregisterSubAgentsAutosave();
    subAgentsAutosave.cancelPendingTimer();
  });

  function subAgentSettingsMatch(left, right) {
    const normalizedLeft = normalizeSubAgentSettings({ subagents: left });
    const normalizedRight = normalizeSubAgentSettings({ subagents: right });

    return (
      normalizedLeft.max_subagent_depth ===
        normalizedRight.max_subagent_depth &&
      normalizedLeft.max_subagents_per_turn ===
        normalizedRight.max_subagents_per_turn &&
      normalizedLeft.subagent_timeout_minutes ===
        normalizedRight.subagent_timeout_minutes
    );
  }

  // Dirty state, scheduling and saving all compare the normalized draft (the
  // payload that would be sent) with the persisted values, so an incomplete
  // field that normalizes to the stored value is not a pending change.
  function subAgentDraftHasChanges() {
    return !subAgentSettingsMatch(
      subAgentSettings,
      normalizeSubAgentSettings(settings),
    );
  }

  function handleSubAgentSettingChange(key, event) {
    subAgentSettings = {
      ...subAgentSettings,
      [key]: event.currentTarget.value,
    };
    onError('');
  }

  async function saveSubAgentSettings() {
    if (!subAgentDraftHasChanges()) {
      return true;
    }

    return runSettingsSave({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => buildSubAgentSettingsPayload(subAgentSettings),
      // Show the saved values (e.g. the default a cleared field saved) unless
      // the user kept editing while the request was in flight.
      getDraftSnapshot: () => subAgentSettings,
      applyResult: (next) =>
        (subAgentSettings = normalizeSubAgentSettings(next)),
    });
  }
</script>

<div class="s-group">
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.subagents.maxDepth')}
        <InfoHint text={t('settings.subagents.maxDepthHelp')} />
      </div>
    </div>
    <div class="s-row-control s-row-control--number">
      <TextField
        id="settings-subagents-max-depth"
        type="number"
        min="1"
        step="1"
        value={subAgentSettings.max_subagent_depth}
        ariaLabel={t('settings.subagents.maxDepth')}
        onInput={(_next, event) =>
          handleSubAgentSettingChange('max_subagent_depth', event)}
      />
    </div>
  </div>

  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.subagents.maxPerTurn')}
        <InfoHint text={t('settings.subagents.maxPerTurnHelp')} />
      </div>
    </div>
    <div class="s-row-control s-row-control--number">
      <TextField
        id="settings-subagents-max-per-run"
        type="number"
        min="1"
        step="1"
        value={subAgentSettings.max_subagents_per_turn}
        ariaLabel={t('settings.subagents.maxPerTurn')}
        onInput={(_next, event) =>
          handleSubAgentSettingChange('max_subagents_per_turn', event)}
      />
    </div>
  </div>

  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.subagents.timeoutMinutes')}
        <InfoHint text={t('settings.subagents.timeoutMinutesHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.subagents.timeoutMinutesDescription')}
      </div>
    </div>
    <div class="s-row-control s-row-control--number">
      <TextField
        id="settings-subagents-timeout"
        type="number"
        min="1"
        step="1"
        value={subAgentSettings.subagent_timeout_minutes}
        ariaLabel={t('settings.subagents.timeoutMinutes')}
        onInput={(_next, event) =>
          handleSubAgentSettingChange('subagent_timeout_minutes', event)}
      />
    </div>
  </div>
</div>

<div class="s-footer">
  <SaveStatus
    {saving}
    pending={subAgentsAutosave.participant.hasChanges()}
    onClick={() => subAgentsAutosave.participant.runSave('manual')}
  />
</div>
