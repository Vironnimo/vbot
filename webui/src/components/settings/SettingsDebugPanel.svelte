<script>
  import { onDestroy, untrack } from 'svelte';

  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { createSettingsDraft } from '$lib/settingsSave.js';

  const noop = () => {};

  const DEBUG_SETTING_DEFAULTS = Object.freeze({
    enabled: false,
    trace_limit: 50,
  });

  function getDebugSettings(rawSettings) {
    const debug = rawSettings?.debug ?? {};
    const traceLimit = Number(debug.trace_limit);

    return {
      enabled:
        typeof debug.enabled === 'boolean'
          ? debug.enabled
          : DEBUG_SETTING_DEFAULTS.enabled,
      trace_limit:
        Number.isInteger(traceLimit) && traceLimit >= 1 && traceLimit <= 500
          ? traceLimit
          : DEBUG_SETTING_DEFAULTS.trace_limit,
    };
  }

  let { settings = null, onCommit = noop, onError = noop } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let debugSettings = $state(untrack(() => getDebugSettings(settings)));
  let saving = $state(false);
  const debugDraft = createSettingsDraft({
    settings: untrack(() => settings),
    fromSettings: getDebugSettings,
    read: () => debugSettings,
    write: (next) => (debugSettings = next),
    toPayload: (values) => ({
      debug: {
        enabled: values.enabled,
        trace_limit: Number(values.trace_limit),
      },
    }),
  });

  let saveDisabled = $derived(
    saving || debugSettingsMatch(debugSettings, getDebugSettings(settings)),
  );
  const autosaveContext = useAutosaveContext();
  const debugAutosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...debugSettings }),
    hasChanges: () =>
      !debugSettingsMatch(debugSettings, getDebugSettings(settings)),
    save: saveDebugSettings,
  });
  const unregisterDebugAutosave = autosaveContext.register(
    debugAutosave.participant,
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    debugAutosave.scheduleRun();

    return () => {
      debugAutosave.cancelPendingTimer();
    };
  });

  onDestroy(() => {
    unregisterDebugAutosave();
    debugAutosave.cancelPendingTimer();
  });

  function debugSettingsMatch(left, right) {
    return (
      left.enabled === right.enabled &&
      Number(left.trace_limit) === Number(right.trace_limit)
    );
  }

  async function saveDebugSettings() {
    if (debugSettingsMatch(debugSettings, getDebugSettings(settings))) {
      return true;
    }

    const limit = Number(debugSettings.trace_limit);
    if (!Number.isInteger(limit) || limit < 1 || limit > 500) {
      onError(t('debug.limitInvalid'));
      return false;
    }

    return debugDraft.save({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
    });
  }
</script>

<!-- Retention can be configured before capture is enabled. -->
<div class="s-group">
  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('debug.enabled')}
        <InfoHint text={t('debug.enabledHelp')} />
      </div>
      <div class="s-row-desc">
        {t('debug.enabledDescription')}
      </div>
    </div>
    <div class="s-row-control">
      <Toggle
        checked={debugSettings.enabled === true}
        ariaLabel={t('debug.enabled')}
        onChange={(next) => {
          debugSettings = {
            ...debugSettings,
            enabled: next,
          };
          onError('');
        }}
      />
    </div>
  </div>

  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('debug.traceLimit')}
        <InfoHint text={t('debug.traceLimitHelp')} />
      </div>
    </div>
    <div class="s-row-control s-row-control--number">
      <TextField
        id="settings-debug-trace-limit"
        type="number"
        min="1"
        max="500"
        step="1"
        value={debugSettings.trace_limit}
        ariaLabel={t('debug.traceLimit')}
        onInput={(next) => {
          debugSettings = { ...debugSettings, trace_limit: next };
          onError('');
        }}
      />
    </div>
  </div>
</div>

<div class="s-footer">
  <SaveStatus
    {saving}
    pending={debugAutosave.participant.hasChanges()}
    onClick={() => debugAutosave.participant.runSave('manual')}
  />
</div>
