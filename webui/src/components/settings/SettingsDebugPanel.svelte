<script>
  import { onDestroy, untrack } from 'svelte';

  import InfoHint from '../ui/InfoHint.svelte';
  import SaveButton from '../ui/SaveButton.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { updateSettings } from '$lib/api.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';

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

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
    onDebugEnabledChange = noop,
  } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let debugSettings = $state(untrack(() => getDebugSettings(settings)));
  let saving = $state(false);

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
    const normalizedLeft = getDebugSettings({ debug: left });
    const normalizedRight = getDebugSettings({ debug: right });

    return (
      normalizedLeft.enabled === normalizedRight.enabled &&
      normalizedLeft.trace_limit === normalizedRight.trace_limit
    );
  }

  function handleManualDebugSettingsSave() {
    if (saving) {
      return;
    }

    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved'),
        variant: 'success',
      });
      return;
    }

    debugAutosave.cancelPendingTimer();
    void debugAutosave.participant.runSave('manual');
  }

  async function saveDebugSettings(reason) {
    if (debugSettingsMatch(debugSettings, getDebugSettings(settings))) {
      return true;
    }

    const submitted = JSON.stringify(debugSettings);
    const nextEnabled = debugSettings.enabled === true;
    saving = true;
    onError('');

    try {
      const nextSettings = await updateSettings({
        debug: getDebugSettings({ debug: debugSettings }),
      });
      onCommit(nextSettings);
      if (JSON.stringify(debugSettings) === submitted)
        debugSettings = getDebugSettings(nextSettings);
      onDebugEnabledChange(nextEnabled);
      if (reason === 'manual')
        onToast({ title: t('debug.settings'), variant: 'success' });
      return true;
    } catch (error) {
      onError(`${t('settings.saveError')} ${error.message}`);
      return false;
    } finally {
      saving = false;
    }
  }
</script>

<!-- The description states what debug mode captures; the Trace limit only
     applies while it records, so it appears with it. Its draft stays in the
     form while the row is hidden. -->
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

  <div class="s-row s-row--compact" hidden={debugSettings.enabled !== true}>
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
          const rawValue = next;
          if (rawValue === '') {
            debugSettings = {
              ...debugSettings,
              trace_limit: rawValue,
            };
            onError('');
            return;
          }
          const numberValue = Number(rawValue);
          if (
            Number.isInteger(numberValue) &&
            numberValue >= 1 &&
            numberValue <= 500
          ) {
            debugSettings = {
              ...debugSettings,
              trace_limit: numberValue,
            };
            onError('');
          }
        }}
      />
    </div>
  </div>
</div>

<div class="s-footer">
  <SaveButton
    class="s-save-button s-save-button--inline"
    {saving}
    pending={debugAutosave.participant.hasChanges()}
    onClick={handleManualDebugSettingsSave}
  />
</div>
