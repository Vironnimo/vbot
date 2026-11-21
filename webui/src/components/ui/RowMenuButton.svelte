<script>
  import Button from './Button.svelte';

  let { label, expanded, onClick, class: className = '' } = $props();
</script>

<Button
  variant="tertiary"
  icon
  class={['row-menu-button', className].filter(Boolean).join(' ')}
  ariaLabel={label}
  aria-haspopup="menu"
  aria-expanded={expanded}
  data-sortable-ignore
  {onClick}
>
  <svg
    viewBox="0 0 16 16"
    width="16"
    height="16"
    fill="currentColor"
    aria-hidden="true"
  >
    <circle cx="3.5" cy="8" r="1.25" />
    <circle cx="8" cy="8" r="1.25" />
    <circle cx="12.5" cy="8" r="1.25" />
  </svg>
</Button>
