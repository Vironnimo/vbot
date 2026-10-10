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
  {#if kind === 'plus'}<path d="M12 5v14M5 12h14" />
  {:else if kind === 'close'}<path d="m6 6 12 12M18 6 6 18" />
  {:else if kind === 'refresh'}<path
      d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"
    />
  {:else if kind === 'folder'}<path d="M3 7V5h6l2 2h10v13H3Z" />
  {:else if kind === 'play'}<path d="m8 5 11 7-11 7Z" />
  {:else if kind === 'stop'}<rect x="6" y="6" width="12" height="12" rx="1" />
  {:else if kind === 'edit'}<path
      d="m14 5 5 5M4 20l5-1L20 8a2 2 0 0 0-5-5L4 14Z"
    />
  {:else if kind === 'trash'}<path
      d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7"
    />
  {:else if kind === 'settings'}<path d="M4 7h16M4 17h16" /><circle
      cx="9"
      cy="7"
      r="3"
      fill="var(--bg)"
    /><circle cx="15" cy="17" r="3" fill="var(--bg)" />
  {:else}<path d="M6 3h9l4 4v14H6ZM14 3v5h5M9 12h7M9 16h7" />{/if}
</svg>
