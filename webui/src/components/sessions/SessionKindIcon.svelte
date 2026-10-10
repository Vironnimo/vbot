<script>
  // Draws an icon from sessionKindIcon() inline.
  let { icon } = $props();
</script>

<svg viewBox={icon.viewBox} {...icon.attrs} aria-hidden="true">
  {#each icon.shapes as [tag, attrs], index (index)}
    <svelte:element this={tag} {...attrs} />
  {/each}
</svg>
