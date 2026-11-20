<script>
  // Monochrome category glyph in the fixed left column of Tool rows. It never
  // carries status; ToolRowStatus owns that at the row end.
  let { category = 'other' } = $props();
</script>

<svg
  class="te-icon"
  data-category={category}
  viewBox="0 0 16 16"
  width="13"
  height="13"
  aria-hidden="true"
>
  {#if category === 'read'}
    <path d="M4 1.75h5.5L12.5 4.75v9.5h-8.5z" />
    <path d="M9.5 1.75v3h3M6.5 8h4M6.5 10.75h4" />
  {:else if category === 'change'}
    <path d="M10.5 2.5l3 3-8 8H2.5v-3z" />
    <path d="M8.75 4.25l3 3" />
  {:else if category === 'run'}
    <path d="M2.5 4l4 4-4 4" />
    <path d="M8.5 12.5h5" />
  {:else if category === 'search'}
    <circle cx="7" cy="7" r="4.25" />
    <path d="M10.25 10.25l3.5 3.5" />
  {:else if category === 'web'}
    <circle cx="8" cy="8" r="6" />
    <path d="M2 8h12M8 2c-2 2.2-2 9.8 0 12M8 2c2 2.2 2 9.8 0 12" />
  {:else if category === 'agent'}
    <circle cx="4" cy="4" r="1.75" />
    <circle cx="12" cy="12" r="1.75" />
    <path d="M4 5.75v1.5a3 3 0 0 0 3 3h3.25" />
  {:else}
    <path
      d="M10.2 2.1a3.4 3.4 0 0 0-3.9 4.5L2.4 10.5a1.4 1.4 0 0 0 2 2l3.9-3.9a3.4 3.4 0 0 0 4.5-3.9l-2 2-1.9-.6-.6-1.9z"
    />
  {/if}
</svg>
