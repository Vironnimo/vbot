<script>
  // Reusable copy-to-clipboard control. Wraps the shared Button primitive and
  // owns the clipboard write plus the transient "copied" confirmation, so any
  // view can drop in `<CopyButton text={…} />` without re-implementing the timer
  // or the icon swap. Placement and reveal-on-hover are intentionally left to the
  // caller's container, so the same control fits a dense log row or a chat
  // message; the caller styles/positions it through the passed-in `class`.
  import { onDestroy } from 'svelte';

  import { t } from '$lib/i18n.js';

  import Button from './Button.svelte';

  const COPIED_FEEDBACK_MS = 1500;

  let {
    text = '',
    label = '',
    copiedLabel = '',
    variant = 'tertiary',
    disabled = false,
    class: className = '',
    onCopied = undefined,
  } = $props();

  let copied = $state(false);
  let resetTimer = null;

  let copyText = $derived(typeof text === 'string' ? text : '');
  let restingLabel = $derived(label || t('common.copy', 'Copy'));
  let doneLabel = $derived(copiedLabel || t('common.copied', 'Copied'));
  let currentLabel = $derived(copied ? doneLabel : restingLabel);

  async function handleCopy() {
    if (!copyText) {
      return;
    }

    try {
      await navigator.clipboard.writeText(copyText);
      copied = true;
      clearResetTimer();
      resetTimer = setTimeout(() => {
        copied = false;
        resetTimer = null;
      }, COPIED_FEEDBACK_MS);
      onCopied?.();
    } catch {
      // Clipboard access can be blocked (no permission, insecure context); the
      // copy is best-effort and must not disrupt the surrounding view.
    }
  }

  function clearResetTimer() {
    if (resetTimer) {
      clearTimeout(resetTimer);
      resetTimer = null;
    }
  }

  onDestroy(clearResetTimer);
</script>

<Button
  {variant}
  icon
  class={`copy-button ${className}`.trim()}
  ariaLabel={currentLabel}
  title={currentLabel}
  disabled={disabled || !copyText}
  onClick={handleCopy}
>
  {#if copied}
    <svg
      class="copy-button__icon"
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <path d="M20 6 9 17l-5-5" />
    </svg>
  {:else}
    <svg
      class="copy-button__icon"
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" />
    </svg>
  {/if}
</Button>

<style>
  .copy-button__icon {
    display: block;
  }
</style>
