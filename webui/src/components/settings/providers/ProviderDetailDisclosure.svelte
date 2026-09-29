<script>
  // A rarely changed block inside an expanded Provider row (OpenRouter
  // routing, local context windows): one quiet line with a chevron toggle, an
  // optional "?" and a short summary of the current state. The body stays in
  // the DOM while closed so drafts, autosave and settings search survive.
  // `open` has no fallback so a parent can bind it to a per-Provider entry
  // that does not exist yet; undefined reads as closed.
  import InfoHint from '../../ui/InfoHint.svelte';

  let {
    id,
    label,
    help = '',
    helpLabel = '',
    summary = '',
    open = $bindable(),
    children,
  } = $props();

  let bodyId = $derived(`${id}-body`);
</script>

<div class="s-provider-detail">
  <div class="s-provider-detail__head">
    <button
      type="button"
      class="s-provider-detail__toggle"
      aria-expanded={Boolean(open)}
      aria-controls={bodyId}
      onclick={() => (open = !open)}
    >
      <span
        class="disclosure-chevron"
        class:disclosure-chevron--open={Boolean(open)}
        aria-hidden="true"
      ></span>
      <span class="s-provider-connection-label">{label}</span>
    </button>
    {#if help}
      <InfoHint text={help} ariaLabel={helpLabel} />
    {/if}
    {#if summary}
      <span class="s-provider-detail__summary">{summary}</span>
    {/if}
  </div>
  <div class="s-provider-detail__body" id={bodyId} hidden={!open}>
    {@render children?.()}
  </div>
</div>
