<script>
  // The Desktop's global Live voice shortcut. Each change is applied at once:
  // the Desktop saves it, registers the key combination with Windows, and
  // answers with the resulting state, which stays authoritative here.
  import { onMount } from 'svelte';

  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import ShortcutCombination from './ShortcutCombination.svelte';
  import {
    getDesktopLiveHotkey,
    setDesktopLiveHotkey,
  } from '$lib/desktopBridge.js';
  import { shortcutErrorMessage } from '$lib/globalShortcut.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let { onToast = noop } = $props();

  let shortcut = $state(null);
  let loadError = $state(false);
  let busy = $state(false);
  let destroyed = false;

  let controlsDisabled = $derived(
    !shortcut || busy || shortcut.supported === false,
  );
  let errorText = $derived(shortcutErrorMessage(shortcut?.errorCode));

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
        title: t('errors.generic'),
        message: error?.message || '',
        variant: 'error',
      });
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    void load();
    return () => {
      destroyed = true;
    };
  });
</script>

<div class="s-group">
  {#if loadError}
    <div class="s-group__block">
      <Banner variant="error" role="alert">
        <span>
          {t('settings.liveShortcut.loadError')}
        </span>
        <Button variant="secondary" onClick={load}>
          {t('common.retry')}
        </Button>
      </Banner>
    </div>
  {:else}
    <div class="s-row s-row--compact">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.liveShortcut.enabled')}
        </div>
        <div class="s-row-desc">
          {t('settings.liveShortcut.description')}
        </div>
      </div>
      <div class="s-row-control">
        <Toggle
          checked={shortcut?.enabled === true}
          onChange={(enabled) => update({ enabled })}
          disabled={controlsDisabled}
          ariaLabel={t('settings.liveShortcut.enabledAria')}
        />
      </div>
    </div>

    <ShortcutCombination
      hotkey={shortcut?.hotkey ?? null}
      disabled={controlsDisabled}
      onChange={update}
    />

    {#if shortcut?.supported === false}
      <div class="s-group__block s-group__block--attached">
        <Banner variant="neutral" role="status">
          {t('settings.liveShortcut.unsupported')}
        </Banner>
      </div>
    {:else if errorText}
      <div class="s-group__block s-group__block--attached">
        <Banner variant="warn" role="status">{errorText}</Banner>
      </div>
    {/if}
  {/if}
</div>
