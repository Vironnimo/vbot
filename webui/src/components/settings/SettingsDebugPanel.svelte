<script>
  import { onDestroy, untrack } from 'svelte';

  import Button from '../ui/Button.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';

  const AUTO_SAVE_DEBOUNCE_MS = 800;
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
  let autoSaveTimer = null;

  let saveDisabled = $derived(
    saving || debugSettingsMatch(debugSettings, getDebugSettings(settings)),
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveDebugSettings();
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
        title: t('common.alreadySaved', 'Already saved'),
        variant: 'success',
      });
      return;
    }

    clearAutoSaveTimer();
    void saveDebugSettings();
  }

  async function saveDebugSettings() {
    if (saveDisabled) {
      return;
    }

    const nextEnabled = debugSettings.enabled === true;
    saving = true;
    onError('');

    try {
      const nextSettings = await rpc('settings.update', {
        debug: getDebugSettings({ debug: debugSettings }),
      });
      onCommit(nextSettings);
      debugSettings = getDebugSettings(nextSettings);
      onDebugEnabledChange(nextEnabled);
      onToast({ title: t('debug.settings', 'Debug'), variant: 'success' });
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
      {t('debug.enabled', 'Enable debug mode')}
    </div>
    <div class="s-row-desc">
      {t(
        'debug.enabledDescription',
        'Capture provider requests and responses for inspection.',
      )}
    </div>
  </div>
  <div class="s-row-control">
    <Toggle
      checked={debugSettings.enabled === true}
      ariaLabel={t('debug.enabled', 'Enable debug mode')}
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

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('debug.traceLimit', 'Trace limit')}
    </div>
    <div class="s-row-desc">
      {t(
        'debug.traceLimitDescription',
        'Maximum number of traces to keep. Older traces are removed when the limit is reached.',
      )}
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
      ariaLabel={t('debug.traceLimit', 'Trace limit')}
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

<div class="s-debug-warning">
  <div class="s-debug-warning-icon" aria-hidden="true">
    <svg
      viewBox="0 0 16 16"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <path d="M8 2L1 14h14L8 2z" />
      <path d="M8 7v2" />
      <circle cx="8" cy="11.5" r="0.5" fill="currentColor" stroke="none" />
    </svg>
  </div>
  <p class="s-debug-warning-text">
    {t(
      'debug.localWarning',
      'Debug traces are stored locally. Provider requests and responses are captured in full, including raw prompt content sent to models. Secret values like API keys and tokens are automatically redacted.',
    )}
  </p>
</div>

<div class="s-footer">
  <Button
    variant="primary"
    class="s-save-button s-save-button--inline"
    onClick={handleManualDebugSettingsSave}
  >
    {saving ? t('common.saving', 'Saving…') : t('debug.save', 'Save')}
  </Button>
</div>
