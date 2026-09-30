<script>
  // One notice block of expanded Tool details: a note, warning or error
  // the Tool reports to the user, with what it is about.
  import { t } from '$lib/i18n.js';

  let { notice } = $props();

  const LEVEL_LABELS = {
    info: () => t('chat.toolNotice.info'),
    warning: () => t('chat.toolNotice.warning'),
    error: () => t('chat.toolNotice.error'),
  };
</script>

<p class={`teb-row tool-notice tool-notice--${notice.level}`}>
  <span class="tool-notice__level">{LEVEL_LABELS[notice.level]()}</span>
  <span class="tool-notice__text">
    {#if notice.subject}
      <span class="tool-notice__subject">{notice.subject}</span>
    {/if}
    {notice.text}
  </span>
</p>
