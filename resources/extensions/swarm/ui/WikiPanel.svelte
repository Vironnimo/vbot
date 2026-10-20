<script>
  // The Swarm's Wiki tab: the page list beside the open page, or one of them
  // at a time when the panel is narrow. It owns the list, the open page, its
  // history, the edit draft and every Wiki request; WikiIndex, WikiPage and
  // WikiEditor show them.
  import { onMount, onDestroy, tick, untrack } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import { createDebouncedAutosave } from '../../../../webui/src/lib/autosave.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import { failureText } from './failureText.js';
  import { requestId } from './pagePresentation.js';
  import { createPageRefresh, swarmChanged } from './pageRefresh.js';
  import WikiEditor from './WikiEditor.svelte';
  import WikiIndex from './WikiIndex.svelte';
  import WikiPage from './WikiPage.svelte';

  let { swarmId, client, contentLinks, active = true } = $props();

  // Typing searches once the input rests this long; Enter searches at once.
  const SEARCH_DELAY_MS = 300;
  // Relative times ("5 minutes ago") follow the clock at this pace.
  const CLOCK_MS = 60_000;

  let loaded = $state(false);
  let entries = $state.raw([]);
  let next = $state.raw(null);
  let query = $state('');
  let shownQuery = $state('');
  // The search no page held as written, whose words the listed pages hold.
  let wordsMatch = $state('');
  let includeDeleted = $state(false);
  let listError = $state('');
  let selected = $state.raw(null);
  let title = $state('');
  let content = $state('');
  let editing = $state(false);
  let error = $state('');
  let titleError = $state('');
  let saving = $state(false);
  let changingPage = $state(false);
  let history = $state.raw([]);
  let historyNext = $state.raw(null);
  let historyOpen = $state(false);
  let pendingTransition = $state.raw(null);
  let baseline = $state('');
  // Which pane a narrow panel shows: the list, or the open page or editor.
  let view = $state('list');
  let now = $state(Date.now());
  let indexPane = $state(null);
  let index = $state(null);
  let pageView = $state(null);
  let editor = $state(null);
  let readGeneration = 0,
    listGeneration = 0,
    historyGeneration = 0,
    disposed = false;
  let retryMutation = null;
  let searchTimer = null;
  // The page shown before New page, which Cancel returns to.
  let beforeNewPage = null;

  const snapshot = () =>
    JSON.stringify({ page: selected?.page_id, title, content });
  const hasChanges = () => editing && snapshot() !== baseline;
  // Participants refer to a page by its number, as in "w3" or a "#wiki/w3" link.
  const pageRef = (page) => (page?.number != null ? `w${page.number}` : null);
  const showsOlderRevision = () =>
    selected != null && selected.revision !== selected.current_revision;
  // The list is hidden while a narrow panel shows a page.
  const indexVisible = () => (indexPane?.getClientRects().length ?? 0) > 0;
  const wikiEmpty = $derived(
    loaded && !entries.length && !shownQuery && !listError,
  );
  const call = (args) =>
    client.operation('wiki', { swarm_id: swarmId, ...args });
  const autosave = createDebouncedAutosave({
    getSnapshot: snapshot,
    hasChanges,
    save: persist,
  });
  // Only changes of this Swarm's Wiki reload it.
  const backgroundRefresh = createPageRefresh(async (changes) => {
    if (!active || !swarmChanged(changes, swarmId, 'wiki')) return;
    const selection = readGeneration;
    await refresh({ keep: true });
    if (!active || selection !== readGeneration || !selected || editing) return;
    const current = entries.find((entry) => entry.page_id === selected.page_id);
    if (current && current.revision === selected.current_revision) return;
    const read = await readPage(
      selected.page_id,
      showsOlderRevision() ? selected.revision : undefined,
      { background: true },
    );
    if (read && historyOpen) await loadHistory({ keep: true });
  });

  $effect(() => {
    if (active)
      untrack(() => {
        now = Date.now();
        void backgroundRefresh.run();
      });
    else
      untrack(() => {
        cancelSearch();
        if (!hasChanges() && editing) closeEditor();
      });
  });
  $effect(() => {
    if (!active) return;
    const clock = setInterval(() => (now = Date.now()), CLOCK_MS);
    return () => clearInterval(clock);
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
    client.onInvalidation((invalidation) => {
      if (active) backgroundRefresh.schedule(invalidation?.change ?? null);
    }),
  );
  onDestroy(() => {
    disposed = true;
    backgroundRefresh.destroy();
    readGeneration += 1;
    listGeneration += 1;
    historyGeneration += 1;
    autosave.cancelPendingTimer();
    cancelSearch();
  });

  // Loads the list for the shown search. `keep` reloads at least as many
  // pages as are loaded now, so a refresh never takes back "Load more".
  async function refresh({ keep = false } = {}) {
    const generation = ++listGeneration;
    const wanted = keep ? entries.length : 0;
    const searched = shownQuery;
    try {
      let result = await call({
        action: 'list',
        ...(shownQuery ? { query: shownQuery } : {}),
        include_deleted: includeDeleted,
      });
      let list = result.entries;
      while (result.next_call && list.length < wanted) {
        if (disposed || generation !== listGeneration) return;
        result = await call(result.next_call.arguments);
        list = [...list, ...result.entries];
      }
      if (disposed || generation !== listGeneration) return;
      loaded = true;
      listError = '';
      entries = list;
      wordsMatch = result.matched === 'words' ? searched : '';
      next = result.next_call?.arguments ?? null;
      now = Date.now();
    } catch (cause) {
      if (!disposed && generation === listGeneration)
        listError = failureText(cause);
    }
  }
  async function loadMore() {
    if (!next) return;
    const generation = ++listGeneration;
    try {
      const result = await call(next);
      if (disposed || generation !== listGeneration) return;
      listError = '';
      entries = [...entries, ...result.entries];
      next = result.next_call?.arguments ?? null;
      // The button may be gone; continue with the first page it loaded.
      const first = result.entries[0]?.page_id;
      await tick();
      if (first) index?.focusPage(first);
    } catch (cause) {
      if (!disposed && generation === listGeneration)
        listError = failureText(cause);
    }
  }
  function cancelSearch() {
    clearTimeout(searchTimer);
    searchTimer = null;
  }
  function search() {
    cancelSearch();
    shownQuery = query.trim();
    return refresh();
  }
  function changeQuery(value) {
    query = value;
    cancelSearch();
    if (value.trim() === shownQuery) return;
    // Clearing the field shows all pages again at once.
    if (!value.trim()) void search();
    else searchTimer = setTimeout(search, SEARCH_DELAY_MS);
  }
  function changeIncludeDeleted(value) {
    includeDeleted = value;
    void refresh();
  }

  // Reads page `id` (a page id or "w3") at `revision`, or its current
  // revision, following continuations until the whole content arrived. A
  // background read gives way to a newer read and to an edit draft.
  async function readPage(id, revision, { background = false } = {}) {
    const generation = ++readGeneration;
    if (!background) error = '';
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
        if (disposed || generation !== readGeneration) return false;
        body += result.content;
      }
      if (disposed || generation !== readGeneration) return false;
      if (
        background &&
        (!active || editing || selected?.page_id !== page.page_id)
      )
        return false;
      selected = page;
      title = page.title;
      content = body;
      editing = false;
      titleError = '';
      baseline = snapshot();
      retryMutation = null;
      now = Date.now();
      return true;
    } catch (cause) {
      if (!disposed && generation === readGeneration)
        error = failureText(cause);
      return false;
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
  // Shows page `id`. A page opened from the list keeps focus on its row
  // while the list stays in view; otherwise focus moves to the page title.
  async function showPage(id, { fromList = false } = {}) {
    const same =
      selected && (selected.page_id === id || pageRef(selected) === id);
    if (same && !showsOlderRevision()) {
      if (editing) closeEditor();
    } else {
      if (!same) {
        historyGeneration += 1;
        historyOpen = false;
        history = [];
        historyNext = null;
      }
      if (!(await readPage(id)) && error === '') return;
    }
    view = 'page';
    await tick();
    if (!fromList || !indexVisible()) pageView?.focusTitle();
  }
  /** Opens page `id` (a page id or "w3"), as a `#wiki/w3` link does. */
  export function openPage(id) {
    return requestTransition(() => showPage(id));
  }
  function openFromList(id) {
    return requestTransition(() => showPage(id, { fromList: true }));
  }
  function backToList() {
    return requestTransition(async () => {
      if (editing) closeEditor();
      view = 'list';
      await tick();
      index?.focusPage(selected?.page_id);
    });
  }
  function createPage() {
    return requestTransition(async () => {
      readGeneration += 1;
      historyGeneration += 1;
      beforeNewPage = selected
        ? { page: selected, title, content, history, historyNext, historyOpen }
        : null;
      selected = null;
      title = '';
      content = '';
      history = [];
      historyNext = null;
      historyOpen = false;
      baseline = snapshot();
      editing = true;
      error = '';
      titleError = '';
      retryMutation = null;
      view = 'page';
      await tick();
      editor?.focusTitle();
    });
  }
  // Leaves the editor without saving. A new page returns to the page shown
  // before it, or to the list.
  function closeEditor() {
    editing = false;
    titleError = '';
    if (selected) return;
    const previous = beforeNewPage;
    beforeNewPage = null;
    if (!previous) {
      title = '';
      content = '';
      view = 'list';
      return;
    }
    selected = previous.page;
    title = previous.title;
    content = previous.content;
    history = previous.history;
    historyNext = previous.historyNext;
    historyOpen = previous.historyOpen;
    baseline = snapshot();
  }
  function cancelNewPage() {
    return requestTransition(async () => {
      closeEditor();
      await tick();
      if (view === 'list') index?.focusPage(selected?.page_id);
      else pageView?.focusTitle();
    });
  }
  async function startEdit() {
    if (!selected || selected.deleted || showsOlderRevision()) return;
    historyGeneration += 1;
    historyOpen = false;
    editing = true;
    titleError = '';
    baseline = snapshot();
    await tick();
    editor?.focusContent();
  }
  function finishEditing() {
    return requestTransition(async () => {
      editing = false;
      await tick();
      pageView?.focusEdit();
    });
  }
  async function persist(reason) {
    if (!hasChanges() && selected) return true;
    if (!selected && reason !== 'manual') return false;
    if (!title.trim()) {
      titleError = t('swarm.wiki.titleRequired');
      if (reason === 'manual') {
        await tick();
        editor?.focusTitle();
      }
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
      beforeNewPage = null;
      void refresh({ keep: true });
      return true;
    } catch (cause) {
      if (!disposed) error = failureText(cause);
      return false;
    } finally {
      saving = false;
    }
  }
  // Loads the open page's versions, newest first: the first page of them,
  // the next one (`more`), or as many as are loaded now (`keep`).
  async function loadHistory({ more = false, keep = false } = {}) {
    if (!selected || (more && !historyNext)) return false;
    const id = selected.page_id;
    const generation = ++historyGeneration;
    const wanted = keep ? history.length : 0;
    try {
      let result = await call(
        more ? historyNext : { action: 'history', page_id: id },
      );
      let list = more ? [...history, ...result.entries] : result.entries;
      while (!more && result.next_call && list.length < wanted) {
        if (disposed || generation !== historyGeneration) return false;
        result = await call(result.next_call.arguments);
        list = [...list, ...result.entries];
      }
      if (
        disposed ||
        selected?.page_id !== id ||
        generation !== historyGeneration ||
        (keep && !historyOpen)
      )
        return false;
      history = list;
      historyNext = result.next_call?.arguments ?? null;
      historyOpen = true;
      now = Date.now();
      if (more && result.entries[0]) {
        await tick();
        pageView?.focusVersion(result.entries[0].revision);
      }
      return true;
    } catch (cause) {
      if (!disposed && generation === historyGeneration)
        error = failureText(cause);
      return false;
    }
  }
  function toggleHistory() {
    if (!historyOpen) return loadHistory();
    historyGeneration += 1;
    historyOpen = false;
  }
  function showRevision(revision) {
    if (!selected || revision === selected.revision) return;
    return readPage(
      selected.page_id,
      revision === selected.current_revision ? undefined : revision,
    );
  }
  async function backToCurrent() {
    if (selected && (await readPage(selected.page_id))) {
      await tick();
      pageView?.focusTitle();
    }
  }
  // Deletes the open page, or restores the shown revision as the newest.
  async function changePage(action) {
    if (!selected || changingPage) return;
    const page = selected;
    const generation = readGeneration;
    const stillReading = () =>
      !disposed &&
      active &&
      !editing &&
      selected?.page_id === page.page_id &&
      generation === readGeneration;
    changingPage = true;
    error = '';
    try {
      await call({
        action,
        page_id: page.page_id,
        expected_revision: page.current_revision,
        ...(action === 'restore' ? { revision: page.revision } : {}),
        request_id: requestId(),
      });
      if (stillReading()) {
        const read = await readPage(page.page_id, undefined, {
          background: true,
        });
        if (read && historyOpen) await loadHistory();
        await tick();
        if (read && active && !editing && selected?.page_id === page.page_id)
          pageView?.focusTitle();
      }
      await refresh({ keep: true });
    } catch (cause) {
      if (stillReading()) error = failureText(cause);
    } finally {
      changingPage = false;
    }
  }
  async function saveAndContinue() {
    if (await autosave.participant.runSave('manual', { force: true })) {
      const action = pendingTransition;
      pendingTransition = null;
      await requestTransition(action);
    }
  }
  function discardAndContinue() {
    autosave.cancelPendingTimer();
    const saved = JSON.parse(baseline);
    title = saved.title;
    content = saved.content;
    error = '';
    closeEditor();
    // The draft may have met a newer revision; show the current one.
    if (selected)
      void readPage(selected.page_id, undefined, { background: true });
    const action = pendingTransition;
    pendingTransition = null;
    action?.();
  }
</script>

{#snippet newPageAction()}
  <Button variant="secondary" onClick={createPage}>{t('swarm.wiki.new')}</Button
  >
{/snippet}

{#if active}
  <div
    class="panel wiki-panel"
    data-view={view}
    role="tabpanel"
    id="swarm-run-panel-wiki"
    aria-labelledby="swarm-run-tab-wiki"
  >
    <div class="wiki-layout">
      <div class="wiki-index" bind:this={indexPane}>
        <WikiIndex
          bind:this={index}
          {entries}
          {loaded}
          more={Boolean(next)}
          {query}
          {shownQuery}
          {wordsMatch}
          {includeDeleted}
          error={listError}
          selectedId={selected?.page_id ?? null}
          {now}
          onQuery={changeQuery}
          onSearch={search}
          onClearSearch={() => {
            query = '';
            void search();
          }}
          onIncludeDeleted={changeIncludeDeleted}
          onOpen={openFromList}
          onLoadMore={loadMore}
          onCreate={createPage}
          onRetry={() => refresh({ keep: true })}
        />
      </div>
      <div class="wiki-content">
        <Button variant="tertiary" class="wiki-back" onClick={backToList}
          ><svg
            viewBox="0 0 24 24"
            width="16"
            height="16"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"><path d="m15 6-6 6 6 6" /></svg
          >{t('swarm.wiki.allPages')}</Button
        >
        {#if pendingTransition}<Banner
            variant="warn"
            role="alert"
            class="wiki-notice"
            ><p>{t('swarm.wiki.unsaved')}</p>
            <div class="wiki-notice__actions">
              <Button variant="secondary" onClick={saveAndContinue}
                >{t('common.save')}</Button
              ><Button variant="tertiary" onClick={discardAndContinue}
                >{t('swarm.wiki.discard')}</Button
              >
            </div></Banner
          >{/if}
        {#if error}<Banner variant="error" role="alert"><p>{error}</p></Banner
          >{/if}
        {#if editing}
          <WikiEditor
            bind:this={editor}
            page={selected}
            {title}
            {content}
            {titleError}
            {saving}
            pending={hasChanges()}
            onTitle={(value) => {
              title = value;
              titleError = '';
            }}
            onContent={(value) => (content = value)}
            onDone={finishEditing}
            onCancel={cancelNewPage}
            onSave={() => autosave.participant.runSave('manual')}
          />
        {:else if selected}
          <WikiPage
            bind:this={pageView}
            page={selected}
            {content}
            {history}
            historyMore={Boolean(historyNext)}
            {historyOpen}
            busy={saving || changingPage}
            {now}
            {contentLinks}
            onEdit={startEdit}
            onToggleHistory={toggleHistory}
            onShowRevision={showRevision}
            onLoadMoreHistory={() => loadHistory({ more: true })}
            onBackToCurrent={backToCurrent}
            onRestore={() => changePage('restore')}
            onDelete={() => changePage('delete')}
          />
        {:else}
          <EmptyState
            class="wiki-content__empty"
            title={wikiEmpty ? t('swarm.wiki.empty') : t('swarm.wiki.choose')}
            description={t('swarm.wiki.about')}
            actions={wikiEmpty ? newPageAction : undefined}
          >
            {#snippet icon()}<svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="1.2"
                stroke-linecap="round"
                stroke-linejoin="round"
                ><path d="M6 3h9l4 4v14H6ZM14 3v5h5M9 12h7M9 16h7" /></svg
              >{/snippet}
          </EmptyState>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  /* The panel is the query container: it shows the list beside the page
     when it has room for both, and one of them at a time otherwise. */
  .wiki-panel {
    container: wiki / inline-size;
    min-width: 0;
  }
  .wiki-layout {
    display: grid;
    min-width: 0;
  }
  .wiki-index,
  .wiki-content {
    min-width: 0;
  }
  .wiki-content {
    display: grid;
    align-content: start;
    gap: var(--space-md);
  }
  .wiki-panel[data-view='page'] .wiki-index,
  .wiki-panel[data-view='list'] .wiki-content {
    display: none;
  }
  .wiki-content :global(.wiki-back) {
    justify-self: start;
    margin-inline-start: calc(-1 * var(--space-sm));
    padding-inline: var(--space-sm) var(--space-md);
    color: var(--text-med);
  }
  .wiki-content :global(.wiki-notice) {
    flex-wrap: wrap;
  }
  .wiki-notice__actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-xs);
  }
  .wiki-content :global(.wiki-content__empty) {
    min-height: 240px;
  }
  @container wiki (min-width: 680px) {
    .wiki-layout {
      grid-template-columns: 232px minmax(0, 1fr);
    }
    .wiki-panel[data-view] .wiki-index {
      display: block;
      padding-inline-end: var(--space-lg);
      border-inline-end: 1px solid var(--border);
    }
    .wiki-panel[data-view] .wiki-content {
      display: grid;
      padding-inline-start: var(--space-xl);
    }
    .wiki-content :global(.wiki-back) {
      display: none;
    }
    /* The reading pane already says the Wiki is empty. */
    .wiki-index :global(.wiki-index__empty--wiki) {
      display: none;
    }
  }
</style>
