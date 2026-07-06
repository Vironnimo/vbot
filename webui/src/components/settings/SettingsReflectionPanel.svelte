<script>
  import { onDestroy, untrack } from 'svelte';

  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';

  const AUTO_SAVE_DEBOUNCE_MS = 800;
  const noop = () => {};

  const REFLECTION_SETTING_DEFAULTS = Object.freeze({
    enabled: false,
    memory_turn_interval: 10,
    skill_tool_call_interval: 25,
  });

  function positiveIntegerOr(value, fallback) {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed >= 1 ? parsed : fallback;
  }

  function getReflectionSettings(rawSettings) {
    const reflection = rawSettings?.reflection ?? {};

    return {
      enabled:
        typeof reflection.enabled === 'boolean'
          ? reflection.enabled
          : REFLECTION_SETTING_DEFAULTS.enabled,
      memory_turn_interval: positiveIntegerOr(
        reflection.memory_turn_interval,
        REFLECTION_SETTING_DEFAULTS.memory_turn_interval,
      ),
      skill_tool_call_interval: positiveIntegerOr(
        reflection.skill_tool_call_interval,
        REFLECTION_SETTING_DEFAULTS.skill_tool_call_interval,
      ),
    };
  }

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
  } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let reflectionSettings = $state(
    untrack(() => getReflectionSettings(settings)),
  );
  let saving = $state(false);
  let autoSaveTimer = null;

  let saveDisabled = $derived(
    saving ||
      reflectionSettingsMatch(
        reflectionSettings,
        getReflectionSettings(settings),
      ),
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveReflectionSettings();
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

  function reflectionSettingsMatch(left, right) {
    const normalizedLeft = getReflectionSettings({ reflection: left });
    const normalizedRight = getReflectionSettings({ reflection: right });

    return (
      normalizedLeft.enabled === normalizedRight.enabled &&
      normalizedLeft.memory_turn_interval ===
        normalizedRight.memory_turn_interval &&
      normalizedLeft.skill_tool_call_interval ===
        normalizedRight.skill_tool_call_interval
    );
  }

  function handleIntervalInput(key, next) {
    if (next === '') {
      reflectionSettings = {
        ...reflectionSettings,
        [key]: next,
      };
      onError('');
      return;
    }
    const numberValue = Number(next);
    if (Number.isInteger(numberValue) && numberValue >= 1) {
      reflectionSettings = {
        ...reflectionSettings,
        [key]: numberValue,
      };
      onError('');
    }
  }

  function handleManualReflectionSettingsSave() {
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
    void saveReflectionSettings();
  }

  async function saveReflectionSettings() {
    if (saveDisabled) {
      return;
    }

    saving = true;
    onError('');

    try {
      const nextSettings = await rpc('settings.update', {
        reflection: getReflectionSettings({ reflection: reflectionSettings }),
      });
      onCommit(nextSettings);
      reflectionSettings = getReflectionSettings(nextSettings);
      onToast({
        title: t(
          'settings.reflection.saveSuccess',
          'Reflection settings updated.',
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
      {t('settings.reflection.enabled', 'Enable background reflection')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.reflection.enabledDescription',
        'After a run finishes, the agent periodically reviews the conversation in a forked session and saves durable memory and skill updates. The original conversation is never touched.',
      )}
    </div>
  </div>
  <div class="s-row-control">
    <Toggle
      checked={reflectionSettings.enabled === true}
      ariaLabel={t(
        'settings.reflection.enabled',
        'Enable background reflection',
      )}
      onChange={(next) => {
        reflectionSettings = {
          ...reflectionSettings,
          enabled: next,
        };
        onError('');
      }}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t(
        'settings.reflection.memoryInterval',
        'Memory review interval (turns)',
      )}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.reflection.memoryIntervalDescription',
        'A memory review becomes due after this many of your messages in a conversation.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--number">
    <TextField
      id="settings-reflection-memory-interval"
      type="number"
      min="1"
      step="1"
      value={reflectionSettings.memory_turn_interval}
      ariaLabel={t(
        'settings.reflection.memoryInterval',
        'Memory review interval (turns)',
      )}
      onInput={(next) => handleIntervalInput('memory_turn_interval', next)}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t(
        'settings.reflection.skillInterval',
        'Skill review interval (tool calls)',
      )}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.reflection.skillIntervalDescription',
        'A skill review becomes due after this many tool calls in a conversation.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--number">
    <TextField
      id="settings-reflection-skill-interval"
      type="number"
      min="1"
      step="1"
      value={reflectionSettings.skill_tool_call_interval}
      ariaLabel={t(
        'settings.reflection.skillInterval',
        'Skill review interval (tool calls)',
      )}
      onInput={(next) => handleIntervalInput('skill_tool_call_interval', next)}
    />
  </div>
</div>

<div class="s-footer">
  <Button
    variant="primary"
    class="s-save-button s-save-button--inline"
    onClick={handleManualReflectionSettingsSave}
  >
    {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </Button>
</div>
