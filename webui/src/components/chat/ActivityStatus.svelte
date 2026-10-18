<script>
  let { dotStatus } = $props();
</script>

<span
  class="chat-activity__status"
  class:chat-activity__status--running={dotStatus === 'running'}
  class:chat-activity__status--success={dotStatus === 'success'}
  class:chat-activity__status--failed={dotStatus === 'failed'}
  class:chat-activity__status--cancelled={dotStatus === 'cancelled'}
  data-status={dotStatus}
  aria-hidden="true"
>
  {#if dotStatus === 'running'}
    <span class="chat-activity__working-dot"></span>
  {:else if dotStatus === 'success'}
    <svg viewBox="0 0 16 16" width="11" height="11">
      <path d="m3.5 8.2 2.8 2.8 6.2-6.2" />
    </svg>
  {:else if dotStatus === 'cancelled'}
    <svg viewBox="0 0 16 16" width="11" height="11">
      <path d="M4 8h8" />
    </svg>
  {:else if dotStatus === 'failed'}
    <svg viewBox="0 0 16 16" width="11" height="11">
      <path d="M8 3.2 13 12H3L8 3.2Z" />
      <path d="M8 6.3v2.8m0 1.6v.1" />
    </svg>
  {:else}
    <svg viewBox="0 0 16 16" width="11" height="11">
      <circle cx="8" cy="8" r="2" />
    </svg>
  {/if}
</span>
