<script>
  import Banner from './ui/Banner.svelte';
  import { t } from '../lib/i18n.js';

  let {
    client,
    groupId,
    participantId,
    presentation = 'conversation',
  } = $props();
  let element = $state.raw(null);
  let error = $state('');
  let attempt = $state(0);
  $effect(() => {
    void attempt;
    if (!element || !groupId || !participantId) return;
    error = '';
    return client.mountSession(
      element,
      groupId,
      participantId,
      (cause) => {
        error = cause.message;
      },
      presentation,
    );
  });
</script>

<div class="session-slot" bind:this={element}>
  {#if error}
    <Banner variant="error" role="alert">
      {error}
      <button type="button" onclick={() => attempt++}
        >{t('common.retry')}</button
      >
    </Banner>
  {/if}
</div>

<style>
  .session-slot {
    width: 100%;
    height: 100%;
    min-height: 320px;
  }
</style>
