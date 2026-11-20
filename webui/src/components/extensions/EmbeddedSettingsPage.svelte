<script>
  import { onMount } from 'svelte';

  let { client, children } = $props();
  let content;

  onMount(() => {
    if (typeof ResizeObserver === 'undefined') return;
    // Measure the list itself. The frame is capped by the host and may
    // temporarily fill the window while a dialog is open.
    const measure = () => {
      client.setContentHeight(
        Math.ceil(content.getBoundingClientRect().height),
      );
    };
    const sizes = new ResizeObserver(measure);
    sizes.observe(content);
    // Chromium defers resize callbacks for off-screen cross-origin frames.
    // Report async list updates before the user scrolls down to the section.
    const rows = new MutationObserver(measure);
    rows.observe(content, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    measure();
    return () => {
      rows.disconnect();
      sizes.disconnect();
    };
  });
</script>

<main>
  <div bind:this={content}>
    {@render children()}
  </div>
</main>

<style>
  /* A Settings column can be narrower than the standalone app shell. */
  :global(html):has(main),
  :global(body):has(main),
  :global(#app):has(main) {
    min-width: 0;
  }
  main {
    width: 100%;
    min-width: 0;
    overflow: auto;
  }
  div {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 12px;
  }
</style>
