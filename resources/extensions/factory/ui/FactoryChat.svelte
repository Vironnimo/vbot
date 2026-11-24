<script>
  import { onMount, tick } from 'svelte';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import MarkdownContent from '@vbot/extension-sdk/ui/MarkdownContent.svelte';
  import SessionEmbed from '@vbot/extension-sdk/ui/SessionEmbed.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import { failureText } from './presentation.js';

  let {
    model,
    projectId,
    memberId = '',
    memberName = '',
    inactive = false,
    onSent,
  } = $props();

  let chat = $state(null);
  let draft = $state('');
  let error = $state('');
  let sending = $state(false);
  let loading = $state(true);
  let loaded = $state(false);
  let inspectedBinding = $state(null);
  let fresh = $state(false);
  let log = $state(null);
  const inputId = $props.id();
  let request = 0;
  let disposed = false;
  let attempt = null;
  let historyPages = 1;

  const name = $derived(
    memberName || chat?.member_name || t('factory.chat.leader'),
  );
  const scope = () => ({
    project_id: projectId,
    ...(memberId ? { member_id: memberId } : {}),
    ...(chat?.topic?.id ? { topic_id: chat.topic.id } : {}),
  });

  async function show(value) {
    const follow =
      !chat?.posts?.length ||
      !log ||
      log.scrollHeight - log.clientHeight - log.scrollTop < 48;
    chat = value;
    await tick();
    if (follow && log) log.scrollTop = log.scrollHeight;
  }

  async function refresh() {
    if (sending) return;
    const version = ++request;
    try {
      const args = fresh ? { ...scope(), new_topic: true } : scope();
      const value = await model.call('conversation.get', args);
      for (let page = 1; page < historyPages && value?.before; page++) {
        const older = await model.call('conversation.get', {
          ...args,
          topic_id: value.topic.id,
          before: value.before,
        });
        value.posts = [...older.posts, ...value.posts];
        value.before = older.before;
      }
      if (!disposed && version === request) {
        error = '';
        loaded = true;
        await show(value);
      }
    } catch (cause) {
      if (!disposed && version === request) error = failureText(cause);
    } finally {
      if (!disposed && version === request) loading = false;
    }
  }

  onMount(() => {
    void refresh();
    const off = model.client.onInvalidation(({ change } = {}) => {
      if (
        !change ||
        ['topics', 'factories', 'members', 'agents'].includes(change.resource)
      ) {
        void refresh();
      }
    });
    return () => {
      disposed = true;
      off();
    };
  });

  async function send() {
    const body = draft.trim();
    if (
      !body ||
      sending ||
      !loaded ||
      loading ||
      inactive ||
      chat?.profile_available === false
    )
      return;
    const args = {
      ...scope(),
      text: body,
      ...(fresh || !chat?.topic ? { new_topic: true } : {}),
    };
    const signature = JSON.stringify(args);
    if (attempt?.signature !== signature)
      attempt = { signature, id: crypto.randomUUID() };
    sending = true;
    request++;
    error = '';
    try {
      const value = await model.call('conversation.send', {
        ...args,
        request_id: attempt.id,
      });
      if (disposed) return;
      fresh = false;
      draft = '';
      attempt = null;
      await show(value);
      await onSent?.();
    } catch (cause) {
      if (!disposed) error = failureText(cause);
    } finally {
      sending = false;
    }
    if (!disposed && !error) {
      await refresh();
      document.getElementById(inputId)?.focus();
    }
  }

  async function stopDiscussion() {
    if (!chat?.topic || sending) return;
    try {
      await model.call('conversation.stop', {
        project_id: projectId,
        topic_id: chat.topic.id,
      });
      await refresh();
    } catch (cause) {
      error = failureText(cause);
    }
  }

  function newTopic() {
    request++;
    chat = null;
    inspectedBinding = null;
    fresh = true;
    loaded = false;
    loading = true;
    historyPages = 1;
    attempt = null;
    error = '';
    void refresh();
    document.getElementById(inputId)?.focus();
  }

  function keydown(event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void send();
    }
  }
</script>

<section class="chat" aria-label={t('factory.chat.title', { name })}>
  <header>
    <div>
      <h2>{t('factory.chat.title', { name })}</h2>
      {#if chat?.topic}<span>{chat.topic.title}</span>{/if}
    </div>
    <div class="actions">
      {#if chat?.profile_available && chat.agent_proof}
        <Button
          variant="tertiary"
          onClick={() =>
            model.openAgentProfile(
              chat.agent_id,
              chat.agent_proof,
              chat.factory_id,
            )}>{t('factory.chat.editAgent')}</Button
        >
      {/if}
      {#if chat?.topic?.state === 'open' && !inactive && (chat?.replying || chat.posts?.at(-1)?.author === 'user')}
        <Button variant="danger" onClick={stopDiscussion}
          >{t('factory.chat.stop')}</Button
        >
      {/if}
      {#if chat?.session_binding}
        <Button
          variant="tertiary"
          onClick={() =>
            (inspectedBinding = inspectedBinding ? null : chat.session_binding)}
          >{t('factory.chat.openSession')}</Button
        >
      {/if}
      {#if chat?.topic}
        <Button
          variant="tertiary"
          disabled={sending || Boolean(draft.trim())}
          onClick={newTopic}>{t('factory.chat.new')}</Button
        >
      {/if}
    </div>
  </header>

  {#if inspectedBinding}
    <div class="chat-session-panel">
      <div class="chat-session-header">
        <Button variant="tertiary" onClick={() => (inspectedBinding = null)}
          >{t('common.close')}</Button
        >
      </div>
      <div class="chat-session-slot">
        <SessionEmbed
          client={model.client}
          groupId={inspectedBinding.group_id}
          participantId={inspectedBinding.participant_id ?? inspectedBinding.id}
          presentation="conversation"
        />
      </div>
    </div>
  {/if}

  <div class="messages" role="log" bind:this={log}>
    {#if chat?.before}
      <Button
        variant="tertiary"
        onClick={() => {
          historyPages++;
          void refresh();
        }}>{t('factory.chat.earlier')}</Button
      >
    {/if}
    {#each chat?.posts || [] as post (post.id)}
      <div class:user={post.author === 'user'} class="bubble">
        {#if post.author_name && post.author !== 'member:' + chat.topic?.direct_member_id}
          <small>{post.author_name}</small>
        {/if}
        {#if post.evidence}<strong>{t('factory.chat.outcome')}</strong>{/if}
        {#if post.author === 'user'}
          <p>{post.text}</p>
        {:else}
          <MarkdownContent source={post.text} />
        {/if}
        {#if post.evidence}
          <MarkdownContent source={String(post.evidence)} />
        {/if}
      </div>
    {/each}
  </div>

  {#if chat?.topic?.state === 'closed'}
    <p role="status">{t('factory.chat.stopped')}</p>
  {:else if chat?.replying}
    <p role="status">{t('factory.chat.replying', { name })}</p>
  {/if}
  {#if chat?.profile_available === false}
    <p role="status">{t('factory.chat.profileUnavailable')}</p>
  {/if}
  {#if error}
    <p role="alert">
      {error}
      <Button variant="tertiary" onClick={refresh}>{t('common.retry')}</Button>
    </p>
  {/if}

  <form
    onsubmit={(event) => {
      event.preventDefault();
      void send();
    }}
  >
    <TextArea
      id={inputId}
      class="factory-chat-input"
      value={draft}
      onInput={(value) => (draft = value)}
      rows="2"
      maxlength="20000"
      onkeydown={keydown}
      ariaLabel={t('factory.chat.message', { name })}
      placeholder={t('factory.chat.message', { name })}
      disabled={sending ||
        inactive ||
        chat?.profile_available === false ||
        chat?.topic?.state === 'closed'}
    />
    <Button
      variant="primary"
      onClick={send}
      disabled={!loaded ||
        loading ||
        sending ||
        inactive ||
        chat?.profile_available === false ||
        !draft.trim() ||
        chat?.topic?.state === 'closed'}>{t('factory.chat.send')}</Button
    >
  </form>
</section>

<style>
  .chat {
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  header,
  form {
    display: flex;
    align-items: center;
    gap: 12px;
    justify-content: space-between;
    padding: 12px 16px;
  }
  h2 {
    font-size: 15px;
    margin: 0;
  }
  header,
  .actions {
    flex-wrap: wrap;
  }
  .actions {
    display: flex;
    gap: 4px;
  }
  header span {
    color: var(--text-med);
    font-size: 12px;
    overflow-wrap: anywhere;
  }
  .chat-session-panel {
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
    padding: 10px 16px;
  }
  .chat-session-header {
    display: flex;
    justify-content: flex-end;
    margin-bottom: 8px;
  }
  .chat-session-slot {
    height: clamp(260px, 45dvh, 520px);
  }
  .messages {
    max-height: clamp(14rem, 42vh, 32rem);
    min-height: 6rem;
    overflow: auto;
    padding: 4px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .bubble {
    align-self: flex-start;
    min-width: 0;
    max-width: 94%;
    overflow-wrap: anywhere;
  }
  .user {
    align-self: flex-end;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 6px 12px;
  }
  .bubble p {
    white-space: pre-wrap;
    margin: 0;
  }
  form {
    align-items: flex-end;
    border-top: 1px solid var(--border);
    margin-top: 8px;
  }
  form :global(.factory-chat-input) {
    flex: 1;
    min-width: 0;
    max-height: 10rem;
  }
  p[role] {
    margin: 6px 16px;
    font-size: 13px;
  }
</style>
