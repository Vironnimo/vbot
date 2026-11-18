<script>
  import { onDestroy, setContext } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { listFiles } from '$lib/api.js';
  import { qualifyAgentAddress } from '$lib/agentAddress.js';
  import { sessionKey } from '$lib/chatState/sessionState.js';
  import Banner from '../ui/Banner.svelte';
  import ChatComposer from '../ChatComposer.svelte';
  import SessionView from '../SessionView.svelte';
  import SecretGiveForm from './SecretGiveForm.svelte';
  import SessionPopupWindow from './SessionPopupWindow.svelte';

  let { popup, active, onClose, onOpenFull } = $props();
  setContext('chatPopupOrigin', () => popup);
  let error = $state('');
  let composer = $state(null);
  let closed = false;
  let sending = false;
  let secretForm = $state(false);
  const address = $derived(popup.address ?? popup.launch?.admitted);
  const title = $derived(popup.title || t('navigation.chat'));
  const draftKey = $derived(popup.launch && `~popup:${popup.launch.id}`);
  const listDraftFiles = $derived.by(() => {
    const source = popup.launch?.source;
    if (!source) return null;
    const agent = qualifyAgentAddress(source.agent_id, source.project_id);
    return (request = {}) =>
      listFiles(agent, { ...request, sessionId: source.session_id });
  });

  function sendSecret(command) {
    return sendFirst(command, {
      uiContext: $state.snapshot(popup.getUiContext?.()),
    });
  }

  function saveRemainingDraft(input) {
    const target = popup.launch?.address;
    // A side fork can already exist when its first send fails. The next
    // /btw then opens its canonical address. Keyed specialist launches keep
    // their own draft until their first input is acknowledged.
    if (!target || !popup.launch.source) return;
    const agent = qualifyAgentAddress(target.agent_id, target.project_id);
    input?.moveDraft(draftKey, sessionKey(agent, target.session_id));
  }

  async function sendFirst(content, options) {
    error = '';
    popup.launch.error = '';
    sending = true;
    const input = composer;
    try {
      const result = await popup.sendFirst(content, options);
      return {
        draftKey: result.draftKey,
        historyKey: qualifyAgentAddress(
          result.address.agent_id,
          result.address.project_id,
        ),
      };
    } catch (cause) {
      popup.launch.error = cause.message || t('chat.sendError');
      if (closed) saveRemainingDraft(input);
      return false;
    } finally {
      sending = false;
    }
  }

  onDestroy(() => {
    closed = true;
    if (!sending && !address) saveRemainingDraft(composer);
  });
</script>

<SessionPopupWindow
  {title}
  {active}
  opener={popup.opener}
  {onClose}
  onOpenFull={address ? () => onOpenFull(address) : null}
>
  {#snippet children(visible)}
    {#if address}
      <div class="session-popup-body">
        <SessionView
          active={visible}
          {address}
          hideOpenFull
          readOnly={popup.readOnly ?? false}
          includeInherited={popup.includeInherited ?? true}
          getUIContext={popup.getUiContext}
          presentation={popup.presentation ?? 'conversation'}
        />
      </div>
    {:else}
      <div class="session-popup-draft">
        {#if error || popup.launch.error}<Banner variant="error" role="alert"
            >{error || popup.launch.error}</Banner
          >{/if}
        <ChatComposer
          bind:this={composer}
          {draftKey}
          getUIContext={popup.getUiContext}
          onListFiles={listDraftFiles}
          onListFilesForSubmit={listDraftFiles ?? popup.listFilesOnSend}
          onOpenSecretForm={() => (secretForm = true)}
          onSendMessage={sendFirst}
          onSendError={(message) => (error = message)}
          onTranscriptionError={(message) => (error = message)}
        />
        {#if secretForm}
          <SecretGiveForm
            onSendCommand={sendSecret}
            onClose={() => (secretForm = false)}
          />
        {/if}
      </div>
    {/if}
  {/snippet}
</SessionPopupWindow>

<style>
  .session-popup-draft {
    margin-top: auto;
    padding: var(--space-sm);
  }
  .session-popup-body {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-height: 0;
    min-width: 0;
  }
</style>
