<script>
  import { onMount, onDestroy, untrack } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import { createDebouncedAutosave } from '../../../../webui/src/lib/autosave.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import MarkdownContent from '../../../../webui/src/components/chat/MarkdownContent.svelte';
  import { requestId } from './pagePresentation.js';

  let { swarmId, client, contentLinks, initialPageId = null } = $props();
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
  const call = (args) =>
    client.operation('wiki', { swarm_id: swarmId, ...args });
  const autosave = createDebouncedAutosave({
    getSnapshot: snapshot,
    hasChanges,
    save: persist,
  });
  $effect(() => {
    if (editing)
      return untrack(() => client.registerAutosave(autosave.participant));
  });
  $effect(() => {
    const dirty = hasChanges();
    if (selected && dirty) autosave.scheduleRun();
    else autosave.cancelPendingTimer();
    client.notifyAutosave();
    return autosave.cancelPendingTimer;
  });
  onMount(() => {
    void refresh();
    if (initialPageId) void readPage(initialPageId);
    return client.onInvalidation(() => {
      void refresh();
      if (selected && !editing && !historyOpen) void readPage(selected.page_id);
    });
  });
  onDestroy(() => {
    disposed = true;
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
      entries = continuation ? [...entries, ...result.entries] : result.entries;
      next = result.next_call?.arguments ?? null;
    } catch (cause) {
      if (!disposed && generation === listGeneration) error = cause.message;
    }
  }
  async function readPage(id, revision) {
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
      error = t('swarm.wiki.titleRequired', 'Enter a page title.');
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

<div
  class="wiki-panel"
  role="tabpanel"
  tabindex="0"
  aria-label={t('swarm.wiki.title', 'Wiki')}
>
  <div class="wiki-toolbar">
    <h3>{t('swarm.wiki.title', 'Wiki')}</h3>
    <Button variant="secondary" onClick={createPage}
      >{t('swarm.wiki.new', 'New page')}</Button
    >
  </div>
  {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
  {#if pendingTransition}<Banner variant="warn">
      {t(
        'swarm.wiki.unsaved',
        'Your changes have not been saved. Save them or discard them before leaving.',
      )}
      <Button
        variant="tertiary"
        onClick={async () => {
          if (await autosave.participant.runSave('manual', { force: true })) {
            const action = pendingTransition;
            pendingTransition = null;
            await requestTransition(action);
          }
        }}>{t('common.save', 'Save')}</Button
      >
      <Button variant="tertiary" onClick={discardAndContinue}
        >{t('swarm.wiki.discard', 'Discard changes and continue')}</Button
      >
    </Banner>{/if}
  <div class="wiki-columns">
    <aside class="wiki-index">
      <form
        onkeydown={(event) => {
          if (event.key === 'Enter' && event.target.tagName === 'INPUT') {
            event.preventDefault();
            void refresh();
          }
        }}
        onsubmit={(event) => {
          event.preventDefault();
          void refresh();
        }}
      >
        <FormField
          controlId="wiki-search"
          label={t('swarm.wiki.search', 'Search Wiki')}
        >
          <TextField
            id="wiki-search"
            value={query}
            onInput={(value) => (query = value)}
          />
        </FormField>
        <Button onClick={() => refresh()} variant="tertiary"
          >{t('common.search', 'Search')}</Button
        >
      </form>
      <label
        ><input
          type="checkbox"
          bind:checked={includeDeleted}
          onchange={() => void refresh()}
        />
        {t('swarm.wiki.showDeleted', 'Include deleted pages')}</label
      >
      {#each entries as page (page.page_id)}
        <button
          class="wiki-page-link"
          class:selected={selected?.page_id === page.page_id}
          onclick={() => openPage(page.page_id)}
        >
          <strong>{page.title}</strong>
          {#if page.deleted}<small>{t('swarm.wiki.deleted', 'Deleted')}</small
            >{/if}
          <small>{page.excerpt}</small>
        </button>
      {:else}<p>{t('swarm.wiki.empty', 'No pages found.')}</p>{/each}
      {#if next}<Button variant="tertiary" onClick={() => refresh(next)}
          >{t('common.loadMore', 'Load more')}</Button
        >{/if}
    </aside>
    <article class="wiki-content">
      {#if editing}
        <FormField
          controlId="wiki-title"
          label={t('swarm.wiki.pageTitle', 'Page title')}
          ><TextField
            id="wiki-title"
            value={title}
            onInput={(value) => (title = value)}
          /></FormField
        >
        <FormField
          controlId="wiki-content"
          label={t('swarm.wiki.content', 'Markdown content')}
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
              })}>{t('swarm.wiki.preview', 'View page')}</Button
          >
          <Button
            variant="tertiary"
            loading={saving}
            onClick={() =>
              autosave.participant.runSave('manual', { force: true })}
            >{t('common.save', 'Save')}</Button
          >
        </div>
      {:else if selected}
        <h3>{selected.title}</h3>
        <p class="wiki-meta">
          {t('swarm.wiki.revision', 'Revision {revision} · {author}', {
            revision: selected.revision,
            author: selected.author?.name,
          })}
        </p>
        <TextField
          ariaLabel={t('swarm.wiki.reference', 'Page reference')}
          value={selected.link}
          readonly
        />
        <div class="wiki-toolbar">
          {#if !selected.deleted && selected.revision === selected.current_revision}<Button
              variant="secondary"
              onClick={() => {
                editing = true;
                baseline = snapshot();
              }}>{t('common.edit', 'Edit')}</Button
            >{/if}
          <Button variant="tertiary" onClick={() => loadHistory()}
            >{t('swarm.wiki.history', 'Version history')}</Button
          >
          {#if selected.deleted || selected.revision !== selected.current_revision}<Button
              variant="secondary"
              disabled={saving}
              onClick={() => changePage('restore')}
              >{t('swarm.wiki.restore', 'Restore this version')}</Button
            >
          {:else}<Button
              variant="tertiary"
              disabled={saving}
              onClick={() => changePage('delete')}
              >{t('swarm.wiki.delete', 'Delete page')}</Button
            >{/if}
        </div>
        {#if historyOpen}<div class="wiki-history">
            {#each history as version (version.revision)}<Button
                variant="tertiary"
                onClick={() => readPage(selected.page_id, version.revision)}
                >{t('swarm.wiki.revision', 'Revision {revision} · {author}', {
                  revision: version.revision,
                  author: version.author.name,
                })}{version.deleted
                  ? ` (${t('swarm.wiki.deleted', 'Deleted')})`
                  : ''}</Button
              >{/each}
            {#if historyNext}<Button
                variant="tertiary"
                onClick={() => loadHistory(historyNext)}
                >{t('common.loadMore', 'Load more')}</Button
              >{/if}
          </div>{/if}
        {#if selected.deleted}<Banner
            >{t(
              'swarm.wiki.deletedHelp',
              'This page is deleted. Its content and earlier versions can still be restored.',
            )}</Banner
          >{/if}
        <div use:contentLinks>
          <MarkdownContent source={content} class="msg-markdown" />
        </div>
      {:else}<p>
          {t(
            'swarm.wiki.choose',
            'Choose a page or create one. Everyone in this Run shares this Wiki.',
          )}
        </p>{/if}
    </article>
  </div>
</div>

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
  .wiki-page-link {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    text-align: left;
    padding: 0.75rem;
    color: var(--text-hi);
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 0.4rem;
    overflow-wrap: anywhere;
    cursor: pointer;
  }
  .wiki-page-link.selected {
    background: var(--surface-2);
    border-color: var(--text-med);
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
