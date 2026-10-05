<script>
  // Desktop dictation: a global shortcut records from the shared microphone
  // and types the transcription into the app in front. Each change applies at
  // once: the Desktop saves it, registers the key combination with Windows,
  // and answers with the resulting state, which stays authoritative here.
  import { onMount } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { bridgeErrorMessage } from '../voice/voiceLabels.js';
  import ShortcutCombination from './ShortcutCombination.svelte';
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import {
    getDesktopDictation,
    onDesktopDictationRecording,
    setDesktopDictation,
  } from '$lib/desktopBridge.js';
  import { shortcutErrorMessage } from '$lib/globalShortcut.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';

  // While a dictation runs, its state is read again this often.
  const ACTIVE_REFRESH_MS = 1000;
  const MODE_OPTIONS = Object.freeze([
    { value: 'toggle', label: t('settings.dictation.modeToggle') },
    { value: 'hold', label: t('settings.dictation.modeHold') },
  ]);
  const noop = () => {};

  let { onToast = noop } = $props();

  let dictation = $state(null);
  let loadError = $state(false);
  let busy = $state(false);
  let destroyed = false;
  // A read applies only while it is the newest one and no change started
  // after it: the change's own answer is newer.
  let reads = 0;
  let changes = 0;
  let refreshTimer = null;

  let controlsDisabled = $derived(
    !dictation || busy || dictation.supported === false,
  );
  let errorText = $derived(dictationErrorMessage(dictation?.error_code));
  let modeDescription = $derived(
    dictation?.mode === 'hold'
      ? t('settings.dictation.modeHoldDescription')
      : t('settings.dictation.modeToggleDescription'),
  );
  let lastResult = $derived(lastResultText(dictation?.last_failure));

  function dictationErrorMessage(code) {
    if (code === 'dictation_config_invalid')
      return t('settings.dictation.error.configInvalid');
    return shortcutErrorMessage(code);
  }

  function failureMessage(code) {
    switch (code) {
      case 'inserted_to_clipboard':
        return t('settings.dictation.result.clipboard');
      case 'nothing_heard':
        return t('settings.dictation.result.nothingHeard');
      case 'microphone_unavailable':
        return t('settings.dictation.result.microphoneUnavailable');
      case 'server_unreachable':
        return t('settings.dictation.result.serverUnreachable');
      case 'speech_to_text_unconfigured':
        return t('settings.dictation.result.speechToTextUnconfigured');
      case 'speech_to_text_unavailable':
        return t('settings.dictation.result.speechToTextUnavailable');
      case 'transcription_failed':
        return t('settings.dictation.result.transcriptionFailed');
      case 'insert_failed':
        return t('settings.dictation.result.insertFailed');
      case 'dictation_failed':
        return t('settings.dictation.result.dictationFailed');
      default:
        return t('settings.dictation.result.failed');
    }
  }

  function lastResultText(failure) {
    if (!failure) return '';
    const message = failureMessage(failure.code);
    const time = failure.at
      ? formatDateTimeInApplicationZone(failure.at, activeLocaleTag(), {
          dateStyle: 'medium',
          timeStyle: 'short',
        })
      : '';
    return time
      ? t('settings.dictation.lastResult', { time, message })
      : message;
  }

  function apply(status) {
    dictation = status;
    loadError = false;
    scheduleRefresh();
  }

  // The Desktop pushes only whether it records, so how a dictation ended is
  // read again until it is idle; a push that raced its state change cannot
  // leave a stale state behind.
  function scheduleRefresh() {
    if (destroyed || refreshTimer !== null) return;
    if (!dictation || dictation.state === 'idle') return;
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      void refresh();
    }, ACTIVE_REFRESH_MS);
  }

  async function refresh() {
    const read = ++reads;
    const changesAtStart = changes;
    try {
      const status = await getDesktopDictation();
      if (destroyed || read !== reads || changes !== changesAtStart) return;
      apply(status);
    } catch {
      if (!destroyed && read === reads && !dictation) loadError = true;
    }
  }

  async function update(change) {
    changes += 1;
    busy = true;
    try {
      const status = await setDesktopDictation(change);
      if (!destroyed) apply(status);
    } catch (error) {
      if (!destroyed)
        onToast({
          title: t('errors.generic'),
          message: bridgeErrorMessage(error),
          variant: 'error',
        });
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    void refresh();
    const stopPushes = onDesktopDictationRecording(() => void refresh());
    return () => {
      destroyed = true;
      stopPushes();
      if (refreshTimer !== null) clearTimeout(refreshTimer);
      refreshTimer = null;
    };
  });
</script>

<div class="s-group">
  {#if loadError}
    <div class="s-group__block">
      <Banner variant="error" role="alert">
        <span>{t('settings.dictation.loadError')}</span>
        <Button variant="secondary" onClick={refresh}>
          {t('common.retry')}
        </Button>
      </Banner>
    </div>
  {:else}
    <div class="s-row s-row--compact">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.dictation.enabled')}
          <InfoHint
            text={t('settings.dictation.help')}
            ariaLabel={t('settings.dictation.helpAria')}
          />
        </div>
        <div class="s-row-desc">
          {t('settings.dictation.description')}
        </div>
      </div>
      <div class="s-row-control">
        <Toggle
          checked={dictation?.enabled === true}
          onChange={(enabled) => update({ enabled })}
          disabled={controlsDisabled}
          ariaLabel={t('settings.dictation.enabledAria')}
        />
      </div>
    </div>

    <ShortcutCombination
      hotkey={dictation?.hotkey ?? null}
      disabled={controlsDisabled}
      onChange={update}
    />

    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">{t('settings.dictation.mode')}</div>
        <div class="s-row-desc">{modeDescription}</div>
      </div>
      <div class="s-row-control">
        <Dropdown
          value={dictation?.mode ?? 'toggle'}
          options={MODE_OPTIONS}
          ariaLabel={t('settings.dictation.mode')}
          disabled={controlsDisabled}
          onValueChange={(mode) => update({ mode })}
        />
      </div>
    </div>

    {#if dictation?.supported === false}
      <div class="s-group__block s-group__block--attached">
        <Banner variant="neutral" role="status">
          {t('settings.dictation.unsupported')}
        </Banner>
      </div>
    {:else if errorText}
      <div class="s-group__block s-group__block--attached">
        <Banner variant="warn" role="status">{errorText}</Banner>
      </div>
    {/if}

    {#if lastResult}
      <div
        class="s-group__block s-group__block--attached s-group__note dictation-last-result"
        role="status"
      >
        {lastResult}
      </div>
    {/if}
  {/if}
</div>
