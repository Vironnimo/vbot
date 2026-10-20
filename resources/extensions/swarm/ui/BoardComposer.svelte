<script>
  import { tick } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Checkbox from '../../../../webui/src/components/ui/Checkbox.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import ParticipantAvatar from './ParticipantAvatar.svelte';
  import './boardComposer.css';

  // The dialog that posts to the Board's discussion: the text, the post it
  // replies to (set by a post's Reply action, removable) and the participants
  // it addresses.
  let { board, model } = $props();

  let participants = $derived(model.selectedSwarm?.participants ?? []);
  let reply = $derived(
    board.replyTarget
      ? t('swarm.board.inReplyTo', {
          reference: `#${board.replyTarget.sequence}`,
        })
      : '',
  );

  // The text field takes focus once the dialog has focused itself.
  function focusText(node) {
    void tick().then(() => node.querySelector('textarea')?.focus());
  }
</script>

{#if board.composeOpen && model.selectedSwarm}<Modal
    title={t('swarm.board.openComposer')}
    size="md"
    closeDisabled={board.posting}
    onClose={() => (board.composeOpen = false)}
  >
    {#snippet body()}<div class="modal-body board-composer" use:focusText>
        {#if model.error}<Banner variant="error" role="alert"
            >{model.error}</Banner
          >{/if}
        {#if board.replyTarget}<div class="board-composer__reply">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
              ><path d="m9 17-5-5 5-5" /><path
                d="M20 18v-2a4 4 0 0 0-4-4H4"
              /></svg
            ><span
              class="board-composer__reply-label"
              use:tooltip={board.replyTarget.preview}>{reply}</span
            ><Button
              variant="tertiary"
              icon
              class="board-composer__reply-remove"
              ariaLabel={t('swarm.compose.removeReply', {
                reference: `#${board.replyTarget.sequence}`,
              })}
              tooltip={t('swarm.compose.removeReply', {
                reference: `#${board.replyTarget.sequence}`,
              })}
              disabled={board.posting}
              onClick={board.clearReply}
              ><svg viewBox="0 0 24 24" aria-hidden="true"
                ><path d="m6 6 12 12M18 6 6 18" /></svg
              ></Button
            >
          </div>{/if}
        <TextArea
          id="swarm-post"
          autoGrow
          rows={4}
          value={board.postText}
          disabled={board.posting}
          ariaLabel={t('swarm.compose.text')}
          placeholder={t('swarm.compose.placeholder')}
          onInput={(value) => (board.postText = value)}
        />
        {#if participants.length}<FormField
            controlId="swarm-recipients"
            label={t('swarm.compose.recipients')}
            help={t('swarm.compose.recipientsHelp')}
          >
            {#snippet children({ labelId, describedBy })}<div
                class="board-composer__recipients"
                id="swarm-recipients"
                role="group"
                aria-labelledby={labelId}
                aria-describedby={describedBy}
              >
                {#each participants as participant (participant.id)}<Checkbox
                    checked={board.addresses(participant.id)}
                    disabled={board.posting}
                    onChange={(chosen) => board.address(participant.id, chosen)}
                    ><ParticipantAvatar
                      id={participant.id}
                      name={participant.display_name}
                    /><span class="board-composer__name"
                      >{participant.display_name}</span
                    ></Checkbox
                  >{/each}
              </div>{/snippet}
          </FormField>{/if}
      </div>{/snippet}
    {#snippet footer()}<Button
        variant="secondary"
        disabled={board.posting}
        onClick={() => (board.composeOpen = false)}>{t('common.cancel')}</Button
      ><Button
        variant="primary"
        loading={board.posting}
        disabled={!board.postText.trim()}
        onClick={board.post}>{t('swarm.compose.submit')}</Button
      >{/snippet}
  </Modal>{/if}
