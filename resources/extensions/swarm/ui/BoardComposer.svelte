<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';

  // The dialog that posts to the Board's discussion, optionally as a reply
  // and to chosen recipients.
  let { board, model } = $props();
</script>

{#if board.composeOpen && model.selectedSwarm}<Modal
    title={t('swarm.board.openComposer')}
    closeDisabled={board.posting}
    onClose={() => (board.composeOpen = false)}
  >
    {#snippet body()}<div class="modal-copy swarm-page-modal-copy">
        {#if model.error}<Banner variant="error" role="alert"
            >{model.error}</Banner
          >{/if}
        <FormField controlId="swarm-post" label={t('swarm.board.post')}
          ><textarea
            class="text-area text-area--default"
            id="swarm-post"
            bind:value={board.postText}
            rows="3"
            placeholder={t('swarm.board.placeholder')}></textarea></FormField
        >
        <div class="post-options">
          <FormField controlId="swarm-reply" label={t('swarm.board.replyTo')}
            ><input
              class="s-input"
              id="swarm-reply"
              placeholder="#42"
              bind:value={board.replyTo}
            /></FormField
          ><FormField
            controlId="swarm-pings"
            label={t('swarm.board.recipients')}
            ><input
              class="s-input"
              id="swarm-pings"
              bind:value={board.postRecipients}
            /></FormField
          >
        </div>
      </div>{/snippet}
    {#snippet footer()}<Button
        variant="secondary"
        disabled={board.posting}
        onClick={() => (board.composeOpen = false)}>{t('common.cancel')}</Button
      ><Button
        variant="primary"
        loading={board.posting}
        disabled={!board.postText.trim()}
        onClick={board.post}>{t('swarm.board.submit')}</Button
      >{/snippet}
  </Modal>{/if}
