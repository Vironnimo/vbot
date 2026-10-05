<script>
  // The "Key combination" row of a Desktop global shortcut: shows the saved
  // combination as the keyboard layout prints it and records a new one. A
  // click starts recording; the next key with its modifiers goes to
  // `onChange` in the Desktop's `{ctrl, alt, shift, win, key}` shape, Escape
  // cancels, and Tab or leaving the button ends the recording.
  import { onMount } from 'svelte';

  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import {
    formatShortcut,
    isModifierKeyCode,
    loadKeyboardLayoutMap,
    shortcutFromKeyboardEvent,
  } from '$lib/globalShortcut.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let { hotkey = null, disabled = false, onChange = noop } = $props();

  let capturing = $state(false);
  let layoutMap = $state(null);

  let combinationLabel = $derived(formatShortcut(hotkey, layoutMap));

  $effect(() => {
    if (disabled) capturing = false;
  });

  function handleCaptureKeydown(event) {
    if (!capturing) return;
    // Tab still moves focus, which ends the capture.
    if (event.code === 'Tab') return;
    event.preventDefault();
    event.stopPropagation();
    if (isModifierKeyCode(event.code)) return;
    capturing = false;
    if (event.code === 'Escape') return;
    onChange(shortcutFromKeyboardEvent(event));
  }

  onMount(() => {
    let destroyed = false;
    void loadKeyboardLayoutMap().then((map) => {
      if (!destroyed) layoutMap = map;
    });
    return () => {
      destroyed = true;
    };
  });
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.shortcut.combination')}
      <InfoHint
        text={t('settings.shortcut.combinationHelp')}
        ariaLabel={t('settings.shortcut.combinationHelpAria')}
      />
    </div>
    <!-- Speaks up only while a new combination is being recorded. -->
    <div class="s-row-desc shortcut-combination__hint" aria-live="polite">
      {#if capturing}{t('settings.shortcut.captureHint')}{/if}
    </div>
  </div>
  <div class="s-row-control">
    <Button
      variant="secondary"
      class="shortcut-combination__capture"
      {disabled}
      ariaLabel={capturing
        ? t('settings.shortcut.capturingAria')
        : t('settings.shortcut.changeAria', {
            combination: combinationLabel,
          })}
      aria-pressed={capturing}
      onkeydown={handleCaptureKeydown}
      onblur={() => (capturing = false)}
      onClick={() => (capturing = !capturing)}
    >
      {capturing ? t('settings.shortcut.capturing') : combinationLabel}
    </Button>
  </div>
</div>

<style>
  .s-row-control :global(.shortcut-combination__capture) {
    min-width: 12em;
    font-variant-numeric: tabular-nums;
  }

  /* The empty live region takes no room until it speaks. */
  .shortcut-combination__hint:empty {
    margin: 0;
  }
</style>
