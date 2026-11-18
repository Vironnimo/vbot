<script>
  import { onMount } from 'svelte';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import { failureText, stateLabel } from './presentation.js';

  let { model, projectId, statusLabel } = $props();
  let rows = $state([]),
    more = $state(''),
    error = $state(''),
    loading = $state(true);
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
  onMount(() => {
    void refresh();
    const off = model.client.onInvalidation(({ change } = {}) => {
      if (!change || ['tasks', 'cards', 'factories'].includes(change.resource))
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
        <button
          onclick={() =>
            model.navigate('/' + item.kind + '/' + encodeURIComponent(item.id))}
        >
          <span>{item.title}</span>
          <small class:attention={item.attention}
            >{item.attention
              ? t('factory.chat.attention')
              : item.kind === 'task'
                ? statusLabel(item.state)
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
</section>

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
