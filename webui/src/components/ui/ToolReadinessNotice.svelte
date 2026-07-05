<script>
  // Shared "currently unavailable" notice for a not-ready tool row, reused by the
  // Agent editor's Allowed-tools list and the Projects Tool-Whitelist editor so
  // both render the exact same badge, server hint, and Extensions jump. It renders
  // ONLY the not-ready extras — each list keeps its own row markup (name, optional
  // description, toggle) and greys the row itself. Nothing renders when `ready` is
  // not explicitly false, so a ready tool costs nothing.
  //
  // `readinessHint` is server-delivered English content (like a tool description),
  // so it is shown verbatim and NOT run through i18n. The Extensions link appears
  // only when `extension` names an owning extension and a navigation callback is
  // wired.

  import Button from './Button.svelte';

  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    ready = true,
    readinessHint = null,
    extension = null,
    onOpenExtensions = noop,
  } = $props();

  let isNotReady = $derived(ready === false);
  let hintText = $derived(
    typeof readinessHint === 'string' && readinessHint.trim().length > 0
      ? readinessHint
      : '',
  );
  let extensionName = $derived(
    typeof extension === 'string' && extension.length > 0 ? extension : '',
  );
</script>

{#if isNotReady}
  <div class="tool-readiness">
    <span class="tool-readiness__badge">
      {t('agents.tools.notReadyBadge', 'Currently unavailable')}
    </span>
    {#if hintText}
      <span class="tool-readiness__hint">{hintText}</span>
    {/if}
    {#if extensionName}
      <Button
        variant="tertiary"
        class="tool-readiness__link"
        onClick={() => onOpenExtensions(extensionName)}
      >
        {t('agents.tools.openExtensions', 'Open Extensions')}
      </Button>
    {/if}
  </div>
{/if}

<style>
  .tool-readiness {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-top: 4px;
  }

  .tool-readiness__badge {
    display: inline-block;
    padding: 1px 7px;
    border: 1px solid rgba(245, 158, 11, 0.22);
    border-radius: 3px;
    color: var(--amber, #f59e0b);
    background: rgba(245, 158, 11, 0.12);
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.04em;
    line-height: 1.6;
  }

  .tool-readiness__hint {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 1.4;
  }
</style>
