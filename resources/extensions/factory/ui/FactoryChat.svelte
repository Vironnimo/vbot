<script>
  import { onMount, tick } from 'svelte';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import MarkdownContent from '@vbot/extension-sdk/ui/MarkdownContent.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import CardSession from './CardSession.svelte';
  import { failureText, waitingLabel } from './presentation.js';

  let {
    model,
    projectId,
    memberId = '',
    memberName = '',
    taskId = '',
    cardId = '',
    topicId = '',
    inactive = false,
    onSent,
  } = $props();
  let chat = $state(null),
    draft = $state(''),
    error = $state(''),
    sending = $state(false),
    loading = $state(true),
    loaded = $state(false);
  let inspectedBinding = $state(null);
  let fresh = $state(false),
    log = $state(null);
  const inputId = $props.id();
  let request = 0,
    disposed = false,
    attempt = null;
  let historyPages = 1;
  const name = $derived(
    memberName || chat?.member_name || t('factory.native.leader'),
  );
  const scope = () => ({
    project_id: projectId,
    ...(memberId ? { member_id: memberId } : {}),
    ...(taskId ? { task_id: taskId } : {}),
    ...(cardId ? { card_id: cardId } : {}),
    ...(topicId || chat?.topic?.id
      ? { topic_id: topicId || chat.topic.id }
      : {}),
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
        ['topics', 'tasks', 'cards', 'factories', 'members', 'agents'].includes(
          change.resource,
        )
      )
        void refresh();
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
      ...((fresh || !chat?.topic) && !taskId && !cardId && !topicId
        ? { new_topic: true }
        : {}),
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
            )}>{t('factory.native.editAgent')}</Button
        >
      {/if}
      {#if chat?.topic?.state === 'open' && !inactive && !chat?.topic.task_id && (chat?.replying || chat.posts?.at(-1)?.author === 'user')}
        <Button variant="danger" onClick={stopDiscussion}
          >{t('factory.chat.stop')}</Button
        >
      {/if}
      {#if chat?.session_binding}
        <Button
          variant="tertiary"
          onClick={() => (inspectedBinding = chat.session_binding)}
          >{t('factory.native.openSession')}</Button
        >
      {/if}
      {#if chat?.task_session_binding}
        <Button
          variant="tertiary"
          onClick={() => (inspectedBinding = chat.task_session_binding)}
          >{t('factory.chat.openTaskSession')}</Button
        >
      {/if}
      {#if !taskId && !cardId && !topicId && chat?.topic}
        <Button
          variant="tertiary"
          disabled={sending || Boolean(draft.trim())}
          onClick={newTopic}>{t('factory.chat.new')}</Button
        >
      {/if}
    </div>
  </header>
  {#if inspectedBinding}
    <CardSession
      client={model.client}
      groupId={inspectedBinding.group_id}
      participant={{ ...inspectedBinding, display_name: name }}
      onClose={() => (inspectedBinding = null)}
    />
  {/if}
  <div class="messages" role="log" bind:this={log}>
    {#if chat?.before}<Button
        variant="tertiary"
        onClick={() => {
          historyPages++;
          void refresh();
        }}>{t('factory.chat.earlier')}</Button
      >{/if}
    {#each chat?.posts || [] as post (post.id)}
      <div class:user={post.author === 'user'} class="bubble">
        {#if post.author_name && post.author !== 'member:' + chat.topic.direct_member_id}<small
            >{post.author_name}</small
          >{/if}
        {#if post.evidence}<strong>{t('factory.native.outcome')}</strong>{/if}
        {#if post.author === 'user'}<p>{post.text}</p>{:else}<MarkdownContent
            source={post.text}
          />{/if}
        {#if post.evidence}<MarkdownContent
            source={String(post.evidence)}
          />{/if}
      </div>
    {/each}
  </div>
  {#if chat?.topic?.task_id && !taskId}
    <button
      class="task-link"
      onclick={() =>
        model.navigate('/task/' + encodeURIComponent(chat.topic.task_id))}
      >{chat.task?.title || t('factory.native.task')} ↗</button
    >
  {/if}
  {#if chat?.topic?.state === 'closed'}<p role="status">
      {t('factory.chat.stopped')}
    </p>{:else if chat?.replying}<p role="status">
      {t('factory.chat.replying', { name })}
    </p>{/if}
  {#if chat?.failure}<p role="status">{waitingLabel(chat.failure)}</p>{/if}
  {#if error}<p role="alert">
      {error} <button onclick={refresh}>{t('common.retry')}</button>
    </p>{/if}
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
  .messages {
    max-height: clamp(12rem, 32vh, 23rem);
    min-height: 3rem;
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
  .task-link {
    align-self: flex-start;
    margin: 6px 16px;
    color: var(--accent);
    background: none;
    border: 0;
    cursor: pointer;
  }
  p[role] {
    margin: 6px 16px;
    font-size: 13px;
  }
</style>
