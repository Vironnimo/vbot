<script>
  import { onDestroy, untrack } from 'svelte';

  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    buildSubAgentSettingsPayload,
    normalizeSubAgentSettings,
  } from '$lib/settingsView.js';

  const AUTO_SAVE_DEBOUNCE_MS = 800;
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
  let autoSaveTimer = null;

  let saveDisabled = $derived(
    saving ||
      subAgentSettingsMatch(
        subAgentSettings,
        normalizeSubAgentSettings(settings),
      ),
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveSubAgentSettings();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

  onDestroy(() => {
    clearAutoSaveTimer();
  });

  function clearAutoSaveTimer() {
    if (autoSaveTimer !== null) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
  }

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

    clearAutoSaveTimer();
    void saveSubAgentSettings();
  }

  async function saveSubAgentSettings() {
    if (saveDisabled) {
      return;
    }

    saving = true;
    onError('');

    try {
      const nextSettings = await rpc(
        'settings.update',
        buildSubAgentSettingsPayload(subAgentSettings),
      );
      onCommit(nextSettings);
      onToast({
        title: t(
          'settings.subagents.saveSuccess',
          'Sub-agent settings updated.',
        ),
        variant: 'success',
      });
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
    } finally {
      saving = false;
    }
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
        'Maximum wait time for blocking sub-agent calls before they fail.',
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
