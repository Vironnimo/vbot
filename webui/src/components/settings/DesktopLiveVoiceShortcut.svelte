<script>
  // The Desktop's global Live voice shortcut. Each change is applied at once:
  // the Desktop saves it, registers the key combination with Windows, and
  // answers with the resulting state, which stays authoritative here.
  import { onMount } from 'svelte';

  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import {
    getDesktopLiveHotkey,
    setDesktopLiveHotkey,
  } from '$lib/desktopBridge.js';
  import {
    formatLiveShortcut,
    isModifierKeyCode,
    liveShortcutFromKeyboardEvent,
    loadKeyboardLayoutMap,
  } from '$lib/liveShortcut.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let { onToast = noop } = $props();

  let shortcut = $state(null);
  let loadError = $state(false);
  let busy = $state(false);
  let capturing = $state(false);
  let layoutMap = $state(null);
  let destroyed = false;

  let combinationLabel = $derived(
    formatLiveShortcut(shortcut?.hotkey, layoutMap),
  );
  let controlsDisabled = $derived(
    !shortcut || busy || shortcut.supported === false,
  );
  let errorText = $derived(errorMessage(shortcut?.errorCode));

  function errorMessage(code) {
    if (code === 'hotkey_in_use') {
      return t(
        'settings.liveShortcut.error.inUse',
        'Another app already uses this key combination. Choose a different one.',
      );
    }
    if (code === 'hotkey_invalid') {
      return t(
        'settings.liveShortcut.error.invalid',
        'This key combination cannot be used. Combine a letter, digit, function key, or Space with Ctrl, Alt, Shift, or Win.',
      );
    }
    if (code) {
      return t(
        'settings.liveShortcut.error.failed',
        'Windows could not register the shortcut. Choose another key combination or restart the Desktop app.',
      );
    }
    return '';
  }

  function applyStatus(status) {
    shortcut = {
      supported: status?.supported !== false,
      enabled: status?.enabled === true,
      hotkey:
        status?.hotkey && typeof status.hotkey === 'object'
          ? { ...status.hotkey }
          : null,
      errorCode:
        typeof status?.error_code === 'string' ? status.error_code : null,
    };
  }

  async function load() {
    loadError = false;
    try {
      const status = await getDesktopLiveHotkey();
      if (!destroyed) applyStatus(status);
    } catch {
      if (!destroyed) loadError = true;
    }
  }

  async function update(changes) {
    busy = true;
    try {
      const status = await setDesktopLiveHotkey(changes);
      if (!destroyed) applyStatus(status);
    } catch (error) {
      onToast({
        title: t('errors.generic', 'Something went wrong. Try again.'),
        message: error?.message || '',
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  function toggleCapture() {
    capturing = !capturing;
  }

  function handleCaptureKeydown(event) {
    if (!capturing) return;
    // Tab still moves focus, which ends the capture.
    if (event.code === 'Tab') return;
    event.preventDefault();
    event.stopPropagation();
    if (isModifierKeyCode(event.code)) return;
    capturing = false;
    if (event.code === 'Escape') return;
    void update(liveShortcutFromKeyboardEvent(event));
  }

  onMount(() => {
    void load();
    void loadKeyboardLayoutMap().then((map) => {
      if (!destroyed) layoutMap = map;
    });
    return () => {
      destroyed = true;
    };
  });
</script>

<div class="s-group live-shortcut">
  {#if loadError}
    <div class="s-group__block">
      <Banner variant="error" role="alert">
        <span>
          {t(
            'settings.liveShortcut.loadError',
            'The Desktop app did not return the shortcut settings.',
          )}
        </span>
        <Button variant="secondary" onClick={load}>
          {t('common.retry', 'Retry')}
        </Button>
      </Banner>
    </div>
  {:else}
    <div class="s-row s-row--compact">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.liveShortcut.enabled', 'Global shortcut')}
        </div>
        <div class="s-row-desc">
          {t(
            'settings.liveShortcut.description',
            'Start or stop Live voice with a key combination, even while another app is in front.',
          )}
        </div>
      </div>
      <div class="s-row-control">
        <Toggle
          checked={shortcut?.enabled === true}
          onChange={(enabled) => update({ enabled })}
          disabled={controlsDisabled}
          ariaLabel={t(
            'settings.liveShortcut.enabledAria',
            'Enable the Live voice shortcut',
          )}
        />
      </div>
    </div>

    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.liveShortcut.combination', 'Key combination')}
        </div>
        <div class="s-row-desc" aria-live="polite">
          {capturing
            ? t(
                'settings.liveShortcut.captureHint',
                'Press the new key combination. Escape cancels.',
              )
            : t(
                'settings.liveShortcut.combinationDescription',
                'Combine a letter, digit, function key, or Space with Ctrl, Alt, Shift, or Win. F13 to F24 also work alone.',
              )}
        </div>
      </div>
      <div class="s-row-control">
        <Button
          variant="secondary"
          class="live-shortcut__capture"
          disabled={controlsDisabled}
          ariaLabel={capturing
            ? t(
                'settings.liveShortcut.capturingAria',
                'Recording a new key combination',
              )
            : t(
                'settings.liveShortcut.changeAria',
                'Change the key combination, currently {combination}',
                { combination: combinationLabel },
              )}
          aria-pressed={capturing}
          onkeydown={handleCaptureKeydown}
          onblur={() => (capturing = false)}
          onClick={toggleCapture}
        >
          {capturing
            ? t('settings.liveShortcut.capturing', 'Press keys…')
            : combinationLabel}
        </Button>
      </div>
    </div>

    {#if shortcut?.supported === false}
      <div class="s-group__block s-group__block--attached">
        <Banner variant="neutral" role="status">
          {t(
            'settings.liveShortcut.unsupported',
            'Global shortcuts are available in the vBot Desktop app on Windows.',
          )}
        </Banner>
      </div>
    {:else if errorText}
      <div class="s-group__block s-group__block--attached">
        <Banner variant="warn" role="status">{errorText}</Banner>
      </div>
    {/if}
  {/if}
</div>

<style>
  .live-shortcut :global(.live-shortcut__capture) {
    min-width: 12em;
    font-variant-numeric: tabular-nums;
  }
</style>
