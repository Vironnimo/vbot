<script>
  import { onMount, tick } from 'svelte';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import ContextUsageRing from '@vbot/extension-sdk/ui/ContextUsageRing.svelte';
  import MarkdownContent from '@vbot/extension-sdk/ui/MarkdownContent.svelte';
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
  let usageSnapshot = $state(null);
  let draft = $state('');
  let error = $state('');
  let sending = $state(false);
  let loading = $state(true);
  let loaded = $state(false);
  let fresh = $state(false);
  let log = $state(null);
  const inputId = $props.id();
  let request = 0;
  let usageRequest = 0;
  let disposed = false;
  let attempt = null;
  let historyPages = 1;

  const name = $derived(
    memberName || chat?.member_name || t('factory.chat.leader'),
  );
  const catalogContextWindow = $derived.by(() => {
    const modelId = chat?.agent_model;
    if (!modelId) return null;
    const bare = modelId.includes(':')
      ? modelId.replace(/^([^/:]+):[^/]+\//, '$1/')
      : modelId;
    const found = (model.catalog?.models ?? []).find(
      (item) => item.id === modelId || item.id === bare,
    );
    return Number.isFinite(found?.context_window) ? found.context_window : null;
  });
  const contextUsage = $derived(usageSnapshot?.context_usage ?? null);
  const contextWindow = $derived(
    contextUsage?.context_window ?? catalogContextWindow,
  );
  const scope = () => ({
    project_id: projectId,
    ...(memberId ? { member_id: memberId } : {}),
    ...(chat?.topic?.id ? { topic_id: chat.topic.id } : {}),
  });

  async function refreshUsage(binding) {
    if (
      !binding?.group_id ||
      !binding?.participant_id ||
      !model.client.readHistory
    ) {
      usageSnapshot = null;
      return;
    }
    const version = ++usageRequest;
    try {
      const history = await model.client.readHistory(
        binding.group_id,
        binding.participant_id,
        { limit: 1 },
      );
      if (!disposed && version === usageRequest) {
        usageSnapshot = history ?? null;
      }
    } catch {
      if (!disposed && version === usageRequest) {
        usageSnapshot = null;
      }
    }
  }

  async function show(value) {
    const follow =
      !chat?.posts?.length ||
      !log ||
      log.scrollHeight - log.clientHeight - log.scrollTop < 48;
    chat = value;
    void refreshUsage(value?.session_binding);
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

  function newSession() {
    request++;
    usageRequest++;
    chat = null;
    usageSnapshot = null;
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
    if (
      event.key === 'Enter' &&
      !event.shiftKey &&
      !event.isComposing &&
      event.keyCode !== 229
    ) {
      event.preventDefault();
      void send();
    }
  }
</script>

<section class="chat" aria-label={t('factory.chat.title', { name })}>
  <header>
    <div class="chat-heading">
      <h2>{t('factory.chat.title', { name })}</h2>
      {#if chat?.topic?.title}<span>{chat.topic.title}</span>{/if}
    </div>
    <div class="actions">
      <ContextUsageRing
        {contextUsage}
        {contextWindow}
        sessionUsage={usageSnapshot?.session_usage ?? null}
        runUsage={usageSnapshot?.run_usage ?? null}
        showFigures
      />
      {#if chat?.topic?.state === 'open' && !inactive && (chat?.replying || chat.posts?.at(-1)?.author === 'user')}
        <Button variant="danger" onClick={stopDiscussion}
          >{t('factory.chat.stop')}</Button
        >
      {/if}
      {#if chat?.session}
        <Button
          variant="tertiary"
          onClick={() => model.client.openSession(chat.session)}
          >{t('factory.chat.openSession')}</Button
        >
      {/if}
      {#if chat?.topic}
        <Button
          variant="tertiary"
          disabled={sending || Boolean(draft.trim())}
          onClick={newSession}>{t('factory.chat.new')}</Button
        >
      {/if}
    </div>
  </header>

  <div class="messages md-chat" role="log" bind:this={log}>
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
          <small class="bubble-author">{post.author_name}</small>
        {/if}
        {#if post.evidence}<strong class="bubble-outcome"
            >{t('factory.chat.outcome')}</strong
          >{/if}
        {#if post.author === 'user'}
          <p>{post.text}</p>
        {:else}
          <MarkdownContent class="msg-markdown" source={post.text} />
        {/if}
        {#if post.evidence}
          <MarkdownContent
            class="msg-markdown"
            source={String(post.evidence)}
          />
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
    flex: 1;
    min-height: 0;
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
    flex-shrink: 0;
  }
  header {
    border-bottom: 1px solid var(--border);
  }
  .chat-heading {
    min-width: 0;
    display: grid;
    gap: 2px;
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
    align-items: center;
    gap: 6px;
  }
  header span {
    color: var(--text-med);
    font-size: 12px;
    overflow-wrap: anywhere;
  }
  .messages {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 16px 20px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .bubble {
    align-self: flex-start;
    width: 100%;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .bubble-author {
    display: block;
    margin-bottom: 4px;
    color: var(--text-med);
  }
  .bubble-outcome {
    display: block;
    margin-bottom: 4px;
  }
  .user {
    align-self: flex-end;
    width: auto;
    max-width: 85%;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 8px 14px;
    font-size: var(--fs-chat-prose, 14.5px);
    line-height: 1.5;
  }
  .user p {
    white-space: pre-wrap;
    margin: 0;
  }
  form {
    align-items: flex-end;
    border-top: 1px solid var(--border);
  }
  form :global(.factory-chat-input) {
    flex: 1;
    min-width: 0;
    max-height: 10rem;
  }
  p[role] {
    margin: 8px 16px;
    font-size: 13px;
    flex-shrink: 0;
  }
</style>
