<script>
  let { icon, size = 16 } = $props();
  const named = $derived(
    ['network', 'panel', 'grid', 'sparkles', 'factory'].includes(icon),
  );
  const paths = $derived(
    icon && typeof icon === 'object' && Array.isArray(icon.paths)
      ? icon.paths
          .filter(
            (path) =>
              typeof path === 'string' &&
              path.length <= 8192 &&
              /^[MmZzLlHhVvCcSsQqTtAaEe0-9+.,\s-]+$/.test(path),
          )
          .slice(0, 32)
      : [],
  );
  const viewBox = $derived(
    typeof icon?.view_box === 'string' && /^[-+\d.eE\s]+$/.test(icon.view_box)
      ? icon.view_box
      : '0 0 24 24',
  );
</script>

{#if paths.length || named}
  <svg
    viewBox={named ? '0 0 16 16' : viewBox}
    width={size}
    height={size}
    fill="none"
    stroke="currentColor"
    stroke-width="1.5"
    aria-hidden="true"
  >
    {#if icon === 'factory'}
      <path d="M1.5 14.5V8l4-2.5V8l4-2.5V8h3V2h2v12.5z" /><path
        d="M4 11.5h1.5m2.5 0h1.5"
      />
    {:else if icon === 'grid'}
      <rect x="2" y="2" width="5" height="5" rx="1" /><rect
        x="9"
        y="2"
        width="5"
        height="5"
        rx="1"
      /><rect x="2" y="9" width="5" height="5" rx="1" /><rect
        x="9"
        y="9"
        width="5"
        height="5"
        rx="1"
      />
    {:else if icon === 'panel'}
      <rect x="1.5" y="2.5" width="13" height="11" rx="1.5" /><path
        d="M6 2.5v11"
      />
    {:else if icon === 'sparkles'}
      <path
        d="M7 2.5 8.2 6 11.5 7.2 8.2 8.4 7 12 5.8 8.4 2.5 7.2 5.8 6z"
      /><path d="M12.5 10.5v3m-1.5-1.5h3" />
    {:else if icon === 'network'}
      <path d="m7 5-3 5m5-5 3 5M5 12h6" /><circle cx="8" cy="3" r="2" /><circle
        cx="3"
        cy="12"
        r="2"
      /><circle cx="13" cy="12" r="2" />
    {:else}
      {#each paths as path, index (index)}<path d={path} />{/each}
    {/if}
  </svg>
{/if}
