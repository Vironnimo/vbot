<script>
  import { onDestroy, untrack } from 'svelte';

  import Button from '../ui/Button.svelte';
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

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
  } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let subAgentSettings = $state(
    untrack(() => normalizeSubAgentSettings(settings)),
  );
  let saving = $state(false);

  let saveDisabled = $derived(
    saving ||
      subAgentSettingsMatch(
        subAgentSettings,
        normalizeSubAgentSettings(settings),
      ),
  );
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

  function subAgentDraftHasChanges() {
    const persisted = normalizeSubAgentSettings(settings);
    return [
      'max_subagent_depth',
      'max_subagents_per_turn',
      'subagent_timeout_minutes',
    ].some((key) => String(subAgentSettings[key]) !== String(persisted[key]));
  }

  function handleSubAgentSettingChange(key, event) {
    subAgentSettings = {
      ...subAgentSettings,
      [key]: event.currentTarget.value,
    };
    onError('');
  }

  function handleManualSubAgentSettingsSave() {
    if (saving) {
      return;
    }

    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved', 'Already saved'),
        variant: 'success',
      });
      return;
    }

    subAgentsAutosave.cancelPendingTimer();
    void subAgentsAutosave.participant.runSave('manual');
  }

  async function saveSubAgentSettings() {
    if (!subAgentDraftHasChanges()) {
      return true;
    }

    return runSettingsSave({
      onCommit,
      onToast,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => buildSubAgentSettingsPayload(subAgentSettings),
      successKey: 'settings.subagents.saveSuccess',
      successFallback: 'Sub-agent settings updated.',
    });
  }
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.subagents.maxDepth', 'Max sub-agent depth')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.subagents.maxDepthDescription',
        'Maximum nesting level allowed when sub-agents spawn their own sub-agents.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--number">
    <TextField
      type="number"
      min="1"
      step="1"
      value={subAgentSettings.max_subagent_depth}
      ariaLabel={t('settings.subagents.maxDepth', 'Max sub-agent depth')}
      onInput={(_next, event) =>
        handleSubAgentSettingChange('max_subagent_depth', event)}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.subagents.maxPerTurn', 'Max sub-agents per turn')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.subagents.maxPerTurnDescription',
        'Maximum number of sub-agent sessions one parent run may spawn.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--number">
    <TextField
      type="number"
      min="1"
      step="1"
      value={subAgentSettings.max_subagents_per_turn}
      ariaLabel={t('settings.subagents.maxPerTurn', 'Max sub-agents per turn')}
      onInput={(_next, event) =>
        handleSubAgentSettingChange('max_subagents_per_turn', event)}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.subagents.timeoutMinutes', 'Timeout minutes')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.subagents.timeoutMinutesDescription',
        'Maximum wait time for foreground sub-agent calls before they fail.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--number">
    <TextField
      type="number"
      min="1"
      step="1"
      value={subAgentSettings.subagent_timeout_minutes}
      ariaLabel={t('settings.subagents.timeoutMinutes', 'Timeout minutes')}
      onInput={(_next, event) =>
        handleSubAgentSettingChange('subagent_timeout_minutes', event)}
    />
  </div>
</div>

<div class="s-footer">
  <Button
    variant="primary"
    class="s-save-button s-save-button--inline"
    onClick={handleManualSubAgentSettingsSave}
  >
    {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </Button>
</div>
