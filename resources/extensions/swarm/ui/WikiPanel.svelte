<script>
  import { onMount, onDestroy, untrack } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import { isImeComposing } from '../../../../webui/src/lib/keyboard.js';
  import { createDebouncedAutosave } from '../../../../webui/src/lib/autosave.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import MarkdownContent from '../../../../webui/src/components/chat/MarkdownContent.svelte';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';
  import { requestId } from './pagePresentation.js';
  import { createPageRefresh } from './pageRefresh.js';

  let {
    swarmId,
    client,
    contentLinks,
    initialPageId = null,
    active = true,
  } = $props();
  let loaded = $state(false);
  let activated = false;
  let entries = $state([]),
    next = $state(null),
    query = $state(''),
    includeDeleted = $state(false);
  let selected = $state(null),
    title = $state(''),
    content = $state('');
  let editing = $state(false),
    error = $state(''),
    saving = $state(false);
  let history = $state([]),
    historyNext = $state(null),
    historyOpen = $state(false);
  let pendingTransition = $state.raw(null);
  let baseline = $state('');
  let readGeneration = 0,
    listGeneration = 0,
    historyGeneration = 0,
    disposed = false;
  let retryMutation = null;
  const snapshot = () =>
    JSON.stringify({ page: selected?.page_id, title, content });
  const hasChanges = () => editing && snapshot() !== baseline;
  // Participants refer to a page by its number, as in "w3" or a "#wiki/w3" link.
  const pageRef = (page) => (page?.number != null ? `w${page.number}` : null);
  // The list shows number and title; the tooltip adds the latest revision.
  const pageTooltip = (page) => ({
    title: page.title,
    rows: [
      {
        label: t('swarm.wiki.revisionLabel'),
        value: page.revision,
      },
      { label: t('swarm.wiki.author'), value: page.author?.name },
    ],
    placement: 'right',
  });
  const call = (args) =>
    client.operation('wiki', { swarm_id: swarmId, ...args });
  const autosave = createDebouncedAutosave({
    getSnapshot: snapshot,
    hasChanges,
    save: persist,
  });
  const backgroundRefresh = createPageRefresh(async () => {
    if (!active) return;
    const selection = readGeneration;
    await refresh();
    if (
      !active ||
      selection !== readGeneration ||
      !selected ||
      editing ||
      historyOpen
    )
      return;
    const current = entries.find((entry) => entry.page_id === selected.page_id);
    if (!current || current.revision !== selected.current_revision)
      await readPage(selected.page_id, undefined, { background: true });
  });
  $effect(() => {
    if (active)
      untrack(() => {
        void backgroundRefresh.run();
        if (!activated && initialPageId) void readPage(initialPageId);
        activated = true;
      });
    else
      untrack(() => {
        if (!hasChanges()) editing = false;
      });
  });
  $effect(() => {
    if (active && editing)
      return untrack(() => client.registerAutosave(autosave.participant));
  });
  $effect(() => {
    const dirty = hasChanges();
    if (selected && dirty) autosave.scheduleRun();
    else autosave.cancelPendingTimer();
    client.notifyAutosave();
    return autosave.cancelPendingTimer;
  });
  onMount(() =>
    client.onInvalidation(() => {
      if (active) backgroundRefresh.schedule();
    }),
  );
  onDestroy(() => {
    disposed = true;
    backgroundRefresh.destroy();
    readGeneration += 1;
    listGeneration += 1;
    historyGeneration += 1;
    autosave.cancelPendingTimer();
  });

  async function refresh(continuation = null) {
    const generation = ++listGeneration;
    try {
      const result = await call(
        continuation ?? {
          action: 'list',
          ...(query.trim() ? { query: query.trim() } : {}),
          include_deleted: includeDeleted,
        },
      );
      if (disposed || generation !== listGeneration) return;
      loaded = true;
      entries = continuation ? [...entries, ...result.entries] : result.entries;
      next = result.next_call?.arguments ?? null;
    } catch (cause) {
      if (!disposed && generation === listGeneration) error = cause.message;
    }
  }
  async function readPage(id, revision, { background = false } = {}) {
    const generation = ++readGeneration;
    error = '';
    try {
      let result = await call({
        action: 'read',
        page_id: id,
        ...(revision ? { revision } : {}),
      });
      const page = result;
      let body = result.content;
      while (result.next_call) {
        result = await call(result.next_call.arguments);
        if (disposed || generation !== readGeneration) return;
        body += result.content;
      }
      if (disposed || generation !== readGeneration) return;
      if (
        background &&
        (!active || editing || historyOpen || selected?.page_id !== id)
      )
        return;
      selected = page;
      title = page.title;
      content = body;
      editing = false;
      baseline = snapshot();
      retryMutation = null;
    } catch (cause) {
      if (!disposed && generation === readGeneration) error = cause.message;
    }
  }
  export async function requestTransition(action) {
    if (hasChanges() || saving) {
      const saved = await autosave.participant.flush();
      if (!saved) {
        pendingTransition = action;
        return;
      }
    }
    pendingTransition = null;
    return action();
  }
  export function openPage(id) {
    return requestTransition(() => {
      if (
        (selected?.page_id === id || pageRef(selected) === id) &&
        !historyOpen
      ) {
        editing = false;
        return;
      }
      historyOpen = false;
      history = [];
      return readPage(id);
    });
  }
  function createPage() {
    return requestTransition(() => {
      readGeneration += 1;
      selected = null;
      title = '';
      content = '';
      baseline = snapshot();
      editing = true;
      error = '';
      historyOpen = false;
      retryMutation = null;
    });
  }
  async function persist(reason) {
    if (!hasChanges() && selected) return true;
    if (!selected && reason !== 'manual') return false;
    if (!title.trim()) {
      error = t('swarm.wiki.titleRequired');
      return false;
    }
    const payload = selected
      ? {
          action: 'update',
          page_id: selected.page_id,
          expected_revision: selected.current_revision,
          title,
          content,
        }
      : { action: 'create', title, content };
    const fingerprint = JSON.stringify(payload);
    if (retryMutation?.fingerprint !== fingerprint)
      retryMutation = { fingerprint, id: requestId() };
    saving = true;
    error = '';
    try {
      const result = await call({ ...payload, request_id: retryMutation.id });
      if (disposed) return false;
      selected = { ...result, current_revision: result.revision };
      baseline = JSON.stringify({
        page: result.page_id,
        title: payload.title,
        content: payload.content,
      });
      retryMutation = null;
      void refresh();
      return true;
    } catch (cause) {
      if (!disposed) error = cause.message;
      return false;
    } finally {
      saving = false;
    }
  }
  async function loadHistory(continuation = null) {
    if (!selected) return;
    const id = selected.page_id;
    const generation = ++historyGeneration;
    try {
      const result = await call(
        continuation ?? { action: 'history', page_id: id },
      );
      if (
        disposed ||
        selected?.page_id !== id ||
        generation !== historyGeneration
      )
        return;
      history = continuation ? [...history, ...result.entries] : result.entries;
      historyNext = result.next_call?.arguments ?? null;
      historyOpen = true;
    } catch (cause) {
      if (!disposed) error = cause.message;
    }
  }
  async function changePage(action) {
    if (!selected) return;
    const page = selected;
    saving = true;
    error = '';
    try {
      await call({
        action,
        page_id: page.page_id,
        expected_revision: page.current_revision,
        ...(action === 'restore' ? { revision: page.revision } : {}),
        request_id: requestId(),
      });
      await readPage(page.page_id);
      historyOpen = false;
      await refresh();
    } catch (cause) {
      if (!disposed) error = cause.message;
    } finally {
      saving = false;
    }
  }
  function discardAndContinue() {
    autosave.cancelPendingTimer();
    editing = false;
    error = '';
    const action = pendingTransition;
    pendingTransition = null;
    action?.();
  }
</script>

{#if active}
  <div
    class="wiki-panel"
    role="tabpanel"
    tabindex="0"
    aria-label={t('swarm.wiki.title')}
  >
    <div class="wiki-toolbar">
      <h3>{t('swarm.wiki.title')}</h3>
      <Button variant="secondary" onClick={createPage}
        >{t('swarm.wiki.new')}</Button
      >
    </div>
    {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
    {#if pendingTransition}<Banner variant="warn">
        {t('swarm.wiki.unsaved')}
        <Button
          variant="tertiary"
          onClick={async () => {
            if (await autosave.participant.runSave('manual', { force: true })) {
              const action = pendingTransition;
              pendingTransition = null;
              await requestTransition(action);
            }
          }}>{t('common.save')}</Button
        >
        <Button variant="tertiary" onClick={discardAndContinue}
          >{t('swarm.wiki.discard')}</Button
        >
      </Banner>{/if}
    <div class="wiki-columns">
      <aside class="wiki-index">
        <form
          onsubmit={(event) => {
            event.preventDefault();
            void refresh();
          }}
        >
          <FormField controlId="wiki-search" label={t('swarm.wiki.search')}>
            <TextField
              id="wiki-search"
              onkeydown={(event) => {
                if (event.key === 'Enter' && !isImeComposing(event)) {
                  event.preventDefault();
                  void refresh();
                }
              }}
              value={query}
              onInput={(value) => (query = value)}
            />
          </FormField>
          <Button onClick={() => refresh()} variant="tertiary"
            >{t('common.search')}</Button
          >
        </form>
        <label
          ><input
            type="checkbox"
            bind:checked={includeDeleted}
            onchange={() => void refresh()}
          />
          {t('swarm.wiki.showDeleted')}</label
        >
        <nav class="wiki-pages" aria-label={t('swarm.wiki.pages')}>
          {#each entries as page (page.page_id)}
            <button
              class="secondary-list__item wiki-page-link"
              class:active={selected?.page_id === page.page_id}
              aria-current={selected?.page_id === page.page_id
                ? 'page'
                : undefined}
              use:tooltip={pageTooltip(page)}
              onclick={() => openPage(page.page_id)}
            >
              {#if page.number != null}<span class="wiki-number"
                  >{pageRef(page)}</span
                >{/if}<span class="wiki-page-title">{page.title}</span>
              {#if page.deleted}<small>{t('swarm.wiki.deleted')}</small>{/if}
            </button>
          {/each}
        </nav>
        {#if !loaded && !error}<p role="status">
            {t('common.loading')}
          </p>
        {:else if loaded && !entries.length}<p>
            {t('swarm.wiki.empty')}
          </p>{/if}
        {#if next}<Button variant="tertiary" onClick={() => refresh(next)}
            >{t('swarm.wiki.loadMore')}</Button
          >{/if}
      </aside>
      <article class="wiki-content">
        {#if editing}
          <FormField controlId="wiki-title" label={t('swarm.wiki.pageTitle')}
            ><TextField
              id="wiki-title"
              value={title}
              onInput={(value) => (title = value)}
            /></FormField
          >
          <FormField controlId="wiki-content" label={t('swarm.wiki.content')}
            ><TextArea
              id="wiki-content"
              value={content}
              onInput={(value) => (content = value)}
              rows={20}
            /></FormField
          >
          <div class="wiki-toolbar">
            <Button
              variant="tertiary"
              onClick={() =>
                requestTransition(() => {
                  editing = false;
                })}>{t('swarm.wiki.preview')}</Button
            >
            <Button
              variant="tertiary"
              loading={saving}
              onClick={() =>
                autosave.participant.runSave('manual', { force: true })}
              >{t('common.save')}</Button
            >
          </div>
        {:else if selected}
          <h3>
            {#if selected.number != null}<span class="wiki-number"
                >{pageRef(selected)}</span
              >{/if}{selected.title}
          </h3>
          <p class="wiki-meta">
            {t('swarm.wiki.revision', {
              revision: selected.revision,
              author: selected.author?.name,
            })}
          </p>
          <TextField
            ariaLabel={t('swarm.wiki.reference')}
            value={selected.link}
            readonly
          />
          <div class="wiki-toolbar">
            {#if !selected.deleted && selected.revision === selected.current_revision}<Button
                variant="secondary"
                onClick={() => {
                  editing = true;
                  baseline = snapshot();
                }}>{t('common.edit')}</Button
              >{/if}
            <Button variant="tertiary" onClick={() => loadHistory()}
              >{t('swarm.wiki.history')}</Button
            >
            {#if selected.deleted || selected.revision !== selected.current_revision}<Button
                variant="secondary"
                disabled={saving}
                onClick={() => changePage('restore')}
                >{t('swarm.wiki.restore')}</Button
              >
            {:else}<Button
                variant="tertiary"
                disabled={saving}
                onClick={() => changePage('delete')}
                >{t('swarm.wiki.delete')}</Button
              >{/if}
          </div>
          {#if historyOpen}<div class="wiki-history">
              {#each history as version (version.revision)}<Button
                  variant="tertiary"
                  onClick={() => readPage(selected.page_id, version.revision)}
                  >{t('swarm.wiki.revision', {
                    revision: version.revision,
                    author: version.author.name,
                  })}{version.deleted
                    ? ` (${t('swarm.wiki.deleted')})`
                    : ''}</Button
                >{/each}
              {#if historyNext}<Button
                  variant="tertiary"
                  onClick={() => loadHistory(historyNext)}
                  >{t('swarm.wiki.loadMore')}</Button
                >{/if}
            </div>{/if}
          {#if selected.deleted}<Banner>{t('swarm.wiki.deletedHelp')}</Banner
            >{/if}
          <div use:contentLinks>
            <MarkdownContent source={content} class="msg-markdown" />
          </div>
        {:else}<p>
            {t('swarm.wiki.choose')}
          </p>{/if}
      </article>
    </div>
  </div>
{/if}

<style>
  .wiki-panel {
    padding: 1rem;
    min-width: 0;
  }
  .wiki-toolbar {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    flex-wrap: wrap;
    margin-bottom: 1rem;
  }
  .wiki-toolbar h3 {
    margin: 0;
    margin-right: auto;
  }
  .wiki-columns {
    display: grid;
    grid-template-columns: minmax(12rem, 18rem) minmax(0, 1fr);
    gap: 1.5rem;
  }
  .wiki-index {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }
  /* Rows follow the shared secondary-list geometry: number column, one-line
     title; revision and author live in the row tooltip. */
  .wiki-pages {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .wiki-page-link {
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    padding: 8px 10px;
    text-align: left;
    font: inherit;
    cursor: pointer;
  }
  .wiki-number {
    color: var(--text-med);
    font: 600 var(--fs-label-sm) var(--font-mono);
    font-variant-numeric: tabular-nums;
    margin-right: 0.5rem;
  }
  .wiki-page-link .wiki-number {
    flex-shrink: 0;
    min-width: 3.5ch;
    margin-right: 0;
  }
  .wiki-page-title {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }
  .wiki-page-link small {
    flex-shrink: 0;
    font-size: var(--fs-label-sm);
  }
  .wiki-page-link small,
  .wiki-meta {
    color: var(--text-med);
  }
  .wiki-content {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .wiki-history {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    margin: 1rem 0;
  }
  @media (max-width: 700px) {
    .wiki-columns {
      grid-template-columns: 1fr;
    }
  }
</style>
