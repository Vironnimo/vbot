<script>
  import { onMount } from 'svelte';
  import { activeLocaleTag, t } from '@vbot/extension-sdk/lib/i18n.js';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import Modal from '@vbot/extension-sdk/ui/Modal.svelte';
  import MarkdownContent from '@vbot/extension-sdk/ui/MarkdownContent.svelte';
  import { failureText, stateLabel, timestamp } from './presentation.js';

  let { model, projectId, factoryId, statusLabel } = $props();
  let rows = $state([]),
    more = $state(''),
    error = $state(''),
    loading = $state(true);
  let reply = $state(null),
    selected = $state(null),
    replyError = $state('');
  let usage = $state(null),
    usageError = $state(''),
    readingUsage = $state(false);
  async function readUsage() {
    if (readingUsage || !factoryId) return;
    readingUsage = true;
    try {
      usage = (await model.call('factory.usage', { id: factoryId })).usage;
      usageError = '';
    } catch (cause) {
      usageError = failureText(cause);
    } finally {
      readingUsage = false;
    }
  }
  let pages = 1,
    request = 0;
  async function refresh() {
    const token = ++request,
      work = [];
    let after = '';
    try {
      for (let page = 0; page < pages; page++) {
        const result = await model.call('work.list', {
          project_id: projectId,
          limit: 20,
          include_replies: true,
          ...(after ? { after } : {}),
        });
        if (request !== token) return;
        work.push(...result.work);
        after = result.next_after;
        if (!after) break;
      }
      rows = work;
      more = after;
      error = '';
    } catch (cause) {
      if (request === token) error = failureText(cause);
    } finally {
      if (request === token) loading = false;
    }
  }
  async function openItem(item) {
    if (item.kind !== 'reply') {
      model.navigate('/' + item.kind + '/' + encodeURIComponent(item.id));
      return;
    }
    selected = item;
    reply = null;
    replyError = '';
    try {
      const value = await model.call('work.reply', {
        project_id: projectId,
        id: item.id,
      });
      if (selected?.id === item.id) reply = value;
    } catch (cause) {
      if (selected?.id === item.id) replyError = failureText(cause);
    }
  }
  onMount(() => {
    void refresh();
    const off = model.client.onInvalidation(({ change } = {}) => {
      if (
        !change ||
        ['tasks', 'cards', 'topics', 'factories'].includes(change.resource)
      )
        void refresh();
    });
    return () => {
      request++;
      off();
    };
  });
</script>

<section class="work">
  <h2>{t('factory.chat.work')}</h2>
  {#if error}<p role="alert">
      {error} <button onclick={refresh}>{t('common.retry')}</button>
    </p>
  {:else if !rows.length}<p>
      {loading ? t('common.loading') : t('factory.chat.empty')}
    </p>{/if}
  <ul>
    {#each rows as item (item.id)}
      <li>
        <button onclick={() => openItem(item)}>
          <span>{item.title}</span>
          <small class:attention={item.attention}
            >{item.attention
              ? t('factory.chat.attention')
              : item.kind === 'task'
                ? statusLabel(item.state)
                : item.kind === 'reply'
                  ? t('factory.work.reply', { name: item.author })
                  : stateLabel(item.state)}</small
          >
        </button>
      </li>
    {/each}
  </ul>
  {#if more}<Button
      onClick={() => {
        pages++;
        void refresh();
      }}>{t('factory.native.more')}</Button
    >{/if}
  {#if factoryId}
    <details
      ontoggle={(event) => {
        if (event.currentTarget.open) void readUsage();
      }}
    >
      <summary>{t('factory.work.usage')}</summary>
      {#if usageError}<p role="alert">{usageError}</p>{:else if usage}
        <p>
          {t('factory.work.usageSummary', {
            runs: usage.runs,
            tools: usage.tool_calls,
            cost:
              usage.cost_usd == null
                ? t('factory.limit.unknown')
                : usage.cost_usd.toLocaleString(activeLocaleTag(), {
                    style: 'currency',
                    currency: 'USD',
                    maximumFractionDigits: 4,
                  }),
          })}
        </p>
        {#if usage.unpriced_calls}<p>
            {t('factory.work.unpriced', { count: usage.unpriced_calls })}
          </p>{/if}
      {:else}<p role="status">{t('common.loading')}</p>{/if}
      <Button variant="tertiary" disabled={readingUsage} onClick={readUsage}
        >{t('factory.work.refreshUsage')}</Button
      >
    </details>
  {/if}
</section>
{#if selected}
  <Modal title={selected.title} size="lg" onClose={() => (selected = null)}>
    {#snippet body()}<div class="modal-body">
        {#if reply}
          <p>{reply.author} · {timestamp(reply.created_at)}</p>
          <MarkdownContent source={reply.text} />
          <Button
            onClick={() => {
              const topic = reply.topic_id;
              selected = null;
              model.navigate('/topic/' + encodeURIComponent(topic));
            }}>{t('factory.work.discussion')}</Button
          >
        {:else if replyError}<p role="alert">{replyError}</p>
        {:else}<p role="status">{t('common.loading')}</p>{/if}
      </div>{/snippet}
  </Modal>
{/if}

<style>
  .work {
    min-width: 0;
    padding: 14px 16px;
    border: 1px solid var(--border);
    border-radius: 12px;
  }
  h2 {
    font-size: 15px;
    margin: 0 0 10px;
  }
  p,
  small {
    color: var(--text-med);
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
    max-height: 20rem;
    overflow: auto;
  }
  li + li {
    border-top: 1px solid var(--border);
  }
  li button {
    width: 100%;
    text-align: left;
    display: flex;
    align-items: baseline;
    gap: 16px;
    justify-content: space-between;
    padding: 10px 2px;
    font: inherit;
    color: var(--text-hi);
    border: 0;
    background: none;
    cursor: pointer;
  }
  li button:hover span {
    text-decoration: underline;
  }
  small {
    white-space: nowrap;
  }
  .attention {
    color: var(--accent);
  }
</style>
