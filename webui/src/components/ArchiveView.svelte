<script>
  // Configure -> Archive: the deleted Agents, Projects and Sessions (and
  // older archived files) that vBot keeps until they are deleted permanently.
  // The list pages `archive.list` newest first under a kind and an
  // Agent/Project filter; an entry opens in place of the list. Restore,
  // "Restore as" (also opened by a restore that meets a taken id) and
  // permanent deletion of one entry, a selection or every matching entry run
  // from here, each deletion after a confirmation.
  import { onDestroy, onMount, tick, untrack } from 'svelte';
  import { SvelteSet } from 'svelte/reactivity';

  import ArchiveEntryDetail from './archive/ArchiveEntryDetail.svelte';
  import ArchiveEntryList from './archive/ArchiveEntryList.svelte';
  import ArchiveRestoreDialog from './archive/ArchiveRestoreDialog.svelte';
  import Dropdown from './Dropdown.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import Checkbox from './ui/Checkbox.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import {
    listArchiveEntries,
    purgeArchiveEntries,
    restoreArchiveEntry,
    showArchiveEntry,
  } from '$lib/api.js';
  import {
    applyArchiveRetention,
    archiveRetention,
  } from '$lib/archiveRetention.svelte.js';
  import {
    ARCHIVE_PAGE_SIZE,
    ARCHIVE_PURGE_BATCH,
    archiveErrorText,
    archiveFilters,
    archiveKindOptions,
    archiveNames,
    archiveScopeOptions,
    canRestoreAs,
    hasArchiveFilters,
    isKeptFromRetention,
    isPurgeable,
    purgeConfirmText,
    purgeResultToast,
    restoreConflictText,
    restoreWarningsText,
    retentionNotice,
  } from '$lib/archiveView.js';
  import { t } from '$lib/i18n.js';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';

  const noop = () => {};
  // `archive.list` returns at most this many entries per call.
  const LIST_LIMIT_MAX = 200;

  let {
    // The place is the shown entry; an empty place shows the list. Filters
    // are not places.
    navigation = createStandaloneNavigation(),
    agents = [],
    projects = [],
    archiveRefreshToken = 0,
    agentsRefreshToken = 0,
    projectsRefreshToken = 0,
    sessionsRefreshToken = 0,
    onToast = noop,
    onOpenRetentionSettings = noop,
  } = $props();

  let filters = $state({ kind: '', scope: '' });
  let entries = $state([]);
  let nextCursor = $state(null);
  let listLoading = $state(true);
  let listLoaded = $state(false);
  let loadingMore = $state(false);
  let listError = $state('');
  const selectedIds = new SvelteSet();

  let detail = $state(null);
  let detailEntryId = $state('');
  let detailLoading = $state(false);
  let detailError = $state('');
  let detailNotFound = $state(false);
  let detailView = $state();

  // A restore or a deletion is running.
  let busy = $state(false);
  let countingAll = $state(false);
  // The deletion awaiting confirmation: { mode: 'one' | 'selected' | 'all',
  // entryIds, filters, count, name, holdsOwnFolders }.
  let purgeConfirm = $state(null);
  // The open "Restore as" dialog: { entry, conflictText, error }.
  let restoreAs = $state(null);

  let listRequest = 0;
  let detailRequest = 0;
  let disposed = false;
  let lastArchiveToken = null;
  let lastScopeTokens = null;

  let shownEntryId = $derived(navigation.place[0] ?? '');
  let retentionDays = $derived(archiveRetention.days);
  let names = $derived(archiveNames({ agents, projects, entries }));
  let kindOptions = $derived(archiveKindOptions());
  let scopeOptions = $derived(
    archiveScopeOptions({ ...names, entries, scope: filters.scope }),
  );
  let filtered = $derived(hasArchiveFilters(filters));
  let purgeableIds = $derived(
    entries.filter(isPurgeable).map((entry) => entry.entry_id),
  );
  let allSelected = $derived(
    purgeableIds.length > 0 && purgeableIds.every((id) => selectedIds.has(id)),
  );
  let countText = $derived.by(() => {
    if (selectedIds.size > 0) {
      return t('archive.selection.count', { count: selectedIds.size });
    }
    if (nextCursor) return t('archive.count.shown', { count: entries.length });
    return entries.length === 1
      ? t('archive.count.one')
      : t('archive.count.many', { count: entries.length });
  });

  onMount(() => {
    void loadList();
  });

  onDestroy(() => {
    disposed = true;
    listRequest += 1;
    detailRequest += 1;
  });

  // Any archive change reloads the list and the shown entry.
  $effect(() => {
    const token = archiveRefreshToken;
    untrack(() => {
      if (lastArchiveToken === null || token === lastArchiveToken) {
        lastArchiveToken = token;
        return;
      }
      lastArchiveToken = token;
      void loadList({ keepCount: true });
      if (detailEntryId) void loadDetail(detailEntryId, { quiet: true });
    });
  });

  // Live Agents, Projects and Sessions decide whether the shown entry can be
  // restored under its own id.
  $effect(() => {
    const tokens = `${agentsRefreshToken}:${projectsRefreshToken}:${sessionsRefreshToken}`;
    untrack(() => {
      if (lastScopeTokens === null || tokens === lastScopeTokens) {
        lastScopeTokens = tokens;
        return;
      }
      lastScopeTokens = tokens;
      if (detailEntryId) void loadDetail(detailEntryId, { quiet: true });
    });
  });

  // Place -> shown entry.
  $effect(() => {
    const entryId = shownEntryId;
    untrack(() => {
      if (entryId === detailEntryId) return;
      if (entryId) {
        void loadDetail(entryId);
        void tick().then(() => detailView?.focus());
      } else {
        void closeDetail(detailEntryId);
      }
    });
  });

  function errorText(error) {
    return typeof error?.message === 'string' ? error.message.trim() : '';
  }

  function adoptRetention(days) {
    applyArchiveRetention(days);
  }

  // Whether a listed entry may hold folders of the user's own: older files
  // always may; another entry shows it only while automatic deletion is on
  // (it is then the one kept from it).
  function mayHoldOwnFolders(entry) {
    return entry?.kind === 'files' || isKeptFromRetention(entry, retentionDays);
  }

  async function loadList({ keepCount = false } = {}) {
    const request = ++listRequest;
    const limit = keepCount
      ? Math.min(LIST_LIMIT_MAX, Math.max(ARCHIVE_PAGE_SIZE, entries.length))
      : ARCHIVE_PAGE_SIZE;
    if (!keepCount) listLoading = true;
    try {
      const result = await listArchiveEntries({
        ...archiveFilters(filters),
        limit,
      });
      if (disposed || request !== listRequest) return;
      entries = Array.isArray(result?.entries) ? result.entries : [];
      nextCursor = result?.next_cursor ?? null;
      adoptRetention(result?.retention_days);
      listError = '';
      listLoaded = true;
      const listed = new Set(
        entries.filter(isPurgeable).map((entry) => entry.entry_id),
      );
      for (const id of [...selectedIds]) {
        if (!listed.has(id)) selectedIds.delete(id);
      }
    } catch (error) {
      if (disposed || request !== listRequest) return;
      listError = [t('archive.loadError'), errorText(error)]
        .filter(Boolean)
        .join(' ');
    } finally {
      if (request === listRequest) listLoading = false;
    }
  }

  async function loadMore() {
    if (!nextCursor || loadingMore) return;
    const request = listRequest;
    loadingMore = true;
    try {
      const result = await listArchiveEntries({
        ...archiveFilters(filters),
        cursor: nextCursor,
        limit: ARCHIVE_PAGE_SIZE,
      });
      if (disposed || request !== listRequest) return;
      const known = new Set(entries.map((entry) => entry.entry_id));
      entries = [
        ...entries,
        ...(result?.entries ?? []).filter(
          (entry) => !known.has(entry.entry_id),
        ),
      ];
      nextCursor = result?.next_cursor ?? null;
      adoptRetention(result?.retention_days);
    } catch (error) {
      if (disposed || request !== listRequest) return;
      listError = [t('archive.loadError'), errorText(error)]
        .filter(Boolean)
        .join(' ');
    } finally {
      loadingMore = false;
    }
  }

  function setFilter(field, value) {
    if (filters[field] === value) return;
    filters = { ...filters, [field]: value };
    selectedIds.clear();
    entries = [];
    nextCursor = null;
    void loadList();
  }

  async function loadDetail(entryId, { quiet = false } = {}) {
    const request = ++detailRequest;
    detailEntryId = entryId;
    if (!quiet) {
      detail = null;
      detailNotFound = false;
    }
    detailLoading = true;
    detailError = '';
    try {
      const result = await showArchiveEntry(entryId);
      if (disposed || request !== detailRequest) return;
      detail = result;
      detailNotFound = false;
    } catch (error) {
      if (disposed || request !== detailRequest) return;
      if (error?.code === 'archive_entry_not_found') {
        detail = null;
        detailNotFound = true;
      } else {
        detailError = [t('archive.detail.loadError'), errorText(error)]
          .filter(Boolean)
          .join(' ');
      }
    } finally {
      if (request === detailRequest) detailLoading = false;
    }
  }

  async function closeDetail(previousId) {
    detailRequest += 1;
    detailEntryId = '';
    detail = null;
    detailLoading = false;
    detailError = '';
    detailNotFound = false;
    restoreAs = null;
    await tick();
    // Focus left with the replaced entry page; return it to the entry's row.
    if (document.activeElement && document.activeElement !== document.body) {
      return;
    }
    const row = [...document.querySelectorAll('.archive-row')].find(
      (item) => item.dataset.entryId === previousId,
    );
    row?.querySelector('.archive-row__main')?.focus();
  }

  function openEntry(entryId) {
    navigation.navigate([entryId]);
  }

  function toggleSelected(entryId, selected) {
    if (selected) selectedIds.add(entryId);
    else selectedIds.delete(entryId);
  }

  function toggleAll(selected) {
    if (!selected) {
      selectedIds.clear();
      return;
    }
    for (const id of purgeableIds) selectedIds.add(id);
  }

  async function restoreShown() {
    const entry = detail?.entry;
    if (!entry || busy) return;
    busy = true;
    try {
      const result = await restoreArchiveEntry(entry.entry_id);
      await finishRestore(entry, result);
    } catch (error) {
      if (
        error?.code === 'archive_restore_conflict' &&
        canRestoreAs(entry, null)
      ) {
        restoreAs = {
          entry,
          conflictText: restoreConflictText(error),
          error: '',
        };
      } else {
        onToast({
          title: t('archive.restore.error'),
          message: archiveErrorText(error),
          variant: 'error',
        });
      }
      void loadDetail(entry.entry_id, { quiet: true });
    } finally {
      busy = false;
    }
  }

  function openRestoreAs() {
    const entry = detail?.entry;
    if (!entry || busy) return;
    restoreAs = { entry, conflictText: '', error: '' };
  }

  async function submitRestoreAs(targetId) {
    const entry = restoreAs?.entry;
    if (!entry || busy) return;
    busy = true;
    restoreAs = { ...restoreAs, error: '' };
    try {
      const result = await restoreArchiveEntry(entry.entry_id, { targetId });
      await finishRestore(entry, result);
    } catch (error) {
      if (!restoreAs) return;
      restoreAs = {
        ...restoreAs,
        conflictText: '',
        error:
          error?.code === 'archive_restore_conflict'
            ? restoreConflictText(error)
            : archiveErrorText(error),
      };
    } finally {
      busy = false;
    }
  }

  async function finishRestore(entry, result) {
    const warnings = restoreWarningsText(result);
    onToast({
      title: t('archive.restore.success', {
        name: entry.label || entry.subject_id,
      }),
      message: warnings,
      variant: warnings ? 'warn' : 'success',
    });
    restoreAs = null;
    selectedIds.delete(entry.entry_id);
    void loadList({ keepCount: true });
    // Going up is a Back step when the list is the previous entry, and Back
    // closes an open dialog instead of navigating: let "Restore as" close
    // first.
    await tick();
    if (shownEntryId === entry.entry_id) navigation.up([]);
  }

  function requestPurgeShown() {
    const entry = detail?.entry;
    if (!entry || busy || !isPurgeable(entry)) return;
    const trees = detail?.files?.trees ?? [];
    purgeConfirm = {
      mode: 'one',
      entryIds: [entry.entry_id],
      name: entry.label || entry.subject_id,
      count: 1,
      holdsOwnFolders:
        entry.kind === 'files' || trees.some((tree) => tree.user_folder),
    };
  }

  function requestPurgeSelected() {
    if (busy || selectedIds.size === 0) return;
    const selected = entries.filter((entry) => selectedIds.has(entry.entry_id));
    purgeConfirm = {
      mode: 'selected',
      entryIds: selected.map((entry) => entry.entry_id),
      count: selected.length,
      holdsOwnFolders: selected.some(mayHoldOwnFolders),
    };
  }

  // Every matching entry that can be deleted now, counted across all pages
  // so the confirmation names the real number.
  async function requestPurgeAll() {
    if (busy || countingAll) return;
    const filterSnapshot = { ...filters };
    countingAll = true;
    try {
      let matching = entries;
      if (nextCursor) {
        matching = [];
        let cursor = null;
        do {
          const page = await listArchiveEntries({
            ...archiveFilters(filterSnapshot),
            cursor,
            limit: LIST_LIMIT_MAX,
          });
          matching.push(...(page?.entries ?? []));
          cursor = page?.next_cursor ?? null;
        } while (cursor);
      }
      if (disposed || filters.kind !== filterSnapshot.kind) return;
      if (filters.scope !== filterSnapshot.scope) return;
      const purgeable = matching.filter(isPurgeable);
      if (purgeable.length === 0) {
        onToast({ title: t('archive.purgeAll.nothing'), variant: 'info' });
        return;
      }
      purgeConfirm = {
        mode: 'all',
        filters: filterSnapshot,
        count: purgeable.length,
        holdsOwnFolders: purgeable.some(mayHoldOwnFolders),
      };
    } catch (error) {
      onToast({
        title: t('archive.loadError'),
        message: errorText(error),
        variant: 'error',
      });
    } finally {
      countingAll = false;
    }
  }

  async function purgeInBatches(entryIds) {
    const result = { purged: [], pending: [] };
    for (let start = 0; start < entryIds.length; start += ARCHIVE_PURGE_BATCH) {
      const batch = await purgeArchiveEntries({
        entryIds: entryIds.slice(start, start + ARCHIVE_PURGE_BATCH),
      });
      result.purged.push(...(batch?.purged ?? []));
      result.pending.push(...(batch?.pending ?? []));
    }
    return result;
  }

  async function confirmPurge() {
    const confirm = purgeConfirm;
    purgeConfirm = null;
    if (!confirm || busy) return;
    busy = true;
    try {
      const result =
        confirm.mode === 'all'
          ? await purgeArchiveEntries({
              all: true,
              ...archiveFilters(confirm.filters),
            })
          : await purgeInBatches(confirm.entryIds);
      onToast(
        purgeResultToast(result, {
          name: confirm.mode === 'one' ? confirm.name : '',
        }),
      );
      if (confirm.mode === 'all') selectedIds.clear();
      else for (const id of confirm.entryIds) selectedIds.delete(id);
      if (confirm.mode === 'one' && shownEntryId === confirm.entryIds[0]) {
        navigation.up([]);
      }
    } catch (error) {
      onToast({
        title: t('archive.purge.error'),
        message: archiveErrorText(error),
        variant: 'error',
      });
    } finally {
      busy = false;
      void loadList({ keepCount: true });
    }
  }
</script>

<section
  class="archive-view view-frame"
  class:archive-view--entry={Boolean(shownEntryId)}
  aria-labelledby={shownEntryId ? undefined : 'archive-title'}
  aria-label={shownEntryId ? t('archive.title') : undefined}
>
  {#if shownEntryId}
    <ArchiveEntryDetail
      bind:this={detailView}
      entryId={shownEntryId}
      {detail}
      loading={detailLoading}
      error={detailError}
      notFound={detailNotFound}
      {busy}
      {retentionDays}
      agentNames={names.agentNames}
      projectNames={names.projectNames}
      onBack={() => navigation.up([])}
      onRetry={() => loadDetail(shownEntryId)}
      onRestore={restoreShown}
      onRestoreAs={openRestoreAs}
      onPurge={requestPurgeShown}
      onOpenEntry={openEntry}
    />
  {:else}
    <header class="view-header">
      <div class="view-header__intro">
        <h2 id="archive-title" class="view-header__title">
          {t('archive.title')}
        </h2>
        <p class="view-header__subtitle">{t('archive.description')}</p>
      </div>
    </header>

    <div class="archive-toolbar view-toolbar view-toolbar--split">
      <div class="archive-filters">
        <Dropdown
          value={filters.kind}
          options={kindOptions}
          ariaLabel={t('archive.filter.kind')}
          triggerClass="archive-filter"
          onValueChange={(value) => setFilter('kind', value)}
        />
        <Dropdown
          value={filters.scope}
          options={scopeOptions}
          ariaLabel={t('archive.filter.scope')}
          triggerClass="archive-filter"
          panelMinWidth={240}
          onValueChange={(value) => setFilter('scope', value)}
        />
      </div>
      {#if retentionNotice(retentionDays)}
        <p class="archive-retention">
          <span>{retentionNotice(retentionDays)}</span>
          <button
            type="button"
            class="archive-retention__link"
            onclick={onOpenRetentionSettings}
            >{t('archive.retention.change')}</button
          >
        </p>
      {/if}
    </div>

    {#if listError}
      <Banner variant="error" aria-live="polite"
        ><span>{listError}</span><Button
          variant="secondary"
          onClick={() => loadList({ keepCount: listLoaded })}
          >{t('common.retry')}</Button
        ></Banner
      >
    {/if}

    {#if listLoading && entries.length === 0}
      <Banner variant="neutral">{t('common.loading')}</Banner>
    {:else if listLoaded && entries.length === 0}
      <EmptyState
        fill
        title={filtered ? t('archive.emptyFiltered') : t('archive.empty')}
        description={filtered ? '' : t('archive.emptyDescription')}
      />
    {:else if entries.length > 0}
      <div class="archive-list-head">
        <Checkbox
          checked={allSelected}
          indeterminate={selectedIds.size > 0 && !allSelected}
          disabled={purgeableIds.length === 0 || busy}
          onChange={toggleAll}>{countText}</Checkbox
        >
        <div class="archive-list-head__actions">
          {#if selectedIds.size > 0}
            <Button
              variant="tertiary"
              disabled={busy}
              onClick={() => selectedIds.clear()}
              >{t('archive.selection.clear')}</Button
            >
            <Button
              variant="danger"
              disabled={busy}
              onClick={requestPurgeSelected}
              >{t('archive.deletePermanently')}</Button
            >
          {:else}
            <Button
              variant="danger"
              disabled={busy || purgeableIds.length === 0}
              loading={countingAll}
              onClick={requestPurgeAll}
              >{filtered
                ? t('archive.purgeAll.matching')
                : t('archive.purgeAll.everything')}</Button
            >
          {/if}
        </div>
      </div>
      <div class="archive-list-scroll">
        <ArchiveEntryList
          {entries}
          {retentionDays}
          agentNames={names.agentNames}
          projectNames={names.projectNames}
          {selectedIds}
          onToggle={toggleSelected}
          onOpen={openEntry}
        />
        {#if nextCursor}
          <div class="archive-more">
            <Button
              variant="secondary"
              loading={loadingMore}
              disabled={loadingMore}
              onClick={loadMore}>{t('archive.loadMore')}</Button
            >
          </div>
        {/if}
      </div>
    {/if}
  {/if}
</section>

{#if purgeConfirm}
  <ConfirmDialog
    title={purgeConfirm.mode === 'all' &&
    !hasArchiveFilters(purgeConfirm.filters)
      ? t('archive.purgeAll.everythingTitle')
      : t('archive.purge.confirmTitle')}
    body={purgeConfirmText(purgeConfirm)}
    confirmLabel={t('archive.deletePermanently')}
    onConfirm={confirmPurge}
    onCancel={() => (purgeConfirm = null)}
  />
{/if}

{#if restoreAs}
  <ArchiveRestoreDialog
    entry={restoreAs.entry}
    conflictText={restoreAs.conflictText}
    error={restoreAs.error}
    {busy}
    onSubmit={submitRestoreAs}
    onClose={() => {
      if (!busy) restoreAs = null;
    }}
  />
{/if}

<style>
  .archive-view {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }

  .archive-toolbar {
    max-width: 1100px;
  }

  .archive-filters {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .archive-filters :global(.archive-filter) {
    min-width: 180px;
  }

  .archive-retention {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 2px 8px;
    margin: 0;
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }

  .archive-retention__link {
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--accent);
    font: inherit;
    cursor: pointer;
  }

  .archive-retention__link:hover {
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .archive-retention__link:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  .archive-list-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px 12px;
    max-width: 1100px;
    min-height: 36px;
    flex-shrink: 0;
    padding: 0 18px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }

  .archive-list-head__actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .archive-list-scroll {
    min-height: 0;
    max-width: 1100px;
    flex: 1;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding-bottom: 16px;
  }

  .archive-more {
    display: flex;
    justify-content: center;
    padding-top: 14px;
  }

  @media (max-width: 640px) {
    .archive-filters,
    .archive-filters :global(.dropdown) {
      width: 100%;
    }

    .archive-filters :global(.archive-filter) {
      min-width: 0;
      width: 100%;
    }

    .archive-list-head {
      padding: 0 12px;
    }
  }
</style>
