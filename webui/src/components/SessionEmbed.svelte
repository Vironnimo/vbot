<script>
  import Banner from './ui/Banner.svelte';
  import { t } from '../lib/i18n.js';

  let {
    client,
    groupId,
    participantId,
    presentation = 'conversation',
    clipBelow = undefined,
    references = [],
    onReference = null,
  } = $props();
  let element = $state.raw(null);
  let error = $state('');
  let attempt = $state(0);
  let binding = $state.raw(null);
  // Projected group/participant records can refresh without changing the
  // Session. Only the slot identity and presentation drive the mount.
  const group = $derived(groupId);
  const participant = $derived(participantId);
  const openReference = $derived(onReference);
  $effect(() => {
    void attempt;
    if (!element || !group || !participant) return;
    error = '';
    const release = client.mountSession(
      element,
      group,
      participant,
      (cause) => {
        error = cause.message;
      },
      presentation,
      ...(openReference ? [openReference] : []),
    );
    binding = release;
    return () => {
      binding = null;
      release();
    };
  });
  $effect(() => {
    binding?.setReferences?.(references);
  });
</script>

<div
  class="session-slot"
  data-session-clip-below={clipBelow}
  bind:this={element}
>
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
