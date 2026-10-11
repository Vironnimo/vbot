<script>
  // The page's action icons, drawn in the current text color.
  let { kind } = $props();
</script>

<svg
  width="16"
  height="16"
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.7"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
>
  {#if kind === 'close'}<path d="m6 6 12 12M18 6 6 18" />
  {:else if kind === 'folder'}<path d="M3 7V5h6l2 2h10v13H3Z" />
  {:else if kind === 'play'}<path d="m8 5 11 7-11 7Z" />
  {:else if kind === 'stop'}<rect x="6" y="6" width="12" height="12" rx="1" />
  {:else if kind === 'edit'}<path
      d="m14 5 5 5M4 20l5-1L20 8a2 2 0 0 0-5-5L4 14Z"
    />
  {:else if kind === 'more'}<g fill="currentColor" stroke="none"
      ><circle cx="5.25" cy="12" r="1.9" /><circle
        cx="12"
        cy="12"
        r="1.9"
      /><circle cx="18.75" cy="12" r="1.9" /></g
    >{/if}
</svg>
