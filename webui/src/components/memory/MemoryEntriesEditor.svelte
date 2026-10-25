<script>
  import { t } from '$lib/i18n.js';
  import { onDestroy } from 'svelte';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import ProjectMemoryHistory from './ProjectMemoryHistory.svelte';
  import '../../styles/memory.css';
  import {
    listAgentMemories,
    addAgentMemory,
    replaceAgentMemory,
    removeAgentMemory,
    listProjectMemories,
    addProjectMemory,
    replaceProjectMemory,
    removeProjectMemory,
  } from '$lib/api.js';
  let {
    agentId = '',
    projectId = '',
    scopes = ['agent', 'user'],
    activeScopes = [],
    refreshToken = 0,
    onToast = () => {},
    viewErrorMessage = (error, fallback) => error?.message || fallback,
  } = $props();
  const editorId = $props.id();

  let memoryPanelOpen = $state(false);

  let memoriesLoaded = $state(false);

  let memoriesLoading = $state(false);

  let memoryError = $state('');

  let memoriesByScope = $state({ agent: [], user: [], project: [] });

  let memoryDrafts = $state({ agent: '', user: '', project: '' });

  let editingMemory = $state(null);

  let deletingMemory = $state(null);

  let memoryMutation = $state('');

  let pendingMemoryResponse = $state(null);

  let historyRefresh = $state(0);
  let historyBusy = $state(false);

  let memoryRequestId = 0;

  let lastMemoriesRefreshToken = null;

  function toggleMemoryPanel() {
    memoryPanelOpen = !memoryPanelOpen;
    if (memoryPanelOpen && !memoriesLoading && Boolean(agentId || projectId)) {
      void loadAgentMemoryEntries({
        showLoading: !memoriesLoaded,
        deferWhileBusy: true,
      });
    }
  }

  async function loadAgentMemoryEntries(options = {}) {
    const ownerId = projectId || agentId;
    if (!ownerId) {
      return;
    }
    const requestId = ++memoryRequestId;
    if (options.showLoading !== false) {
      memoriesLoading = true;
    }
    memoryError = '';
    try {
      const result = await (projectId
        ? listProjectMemories(ownerId)
        : listAgentMemories(ownerId));
      if (destroyed || requestId !== memoryRequestId) {
        return;
      }
      if (options.deferWhileBusy && memoryEditorBusy()) {
        pendingMemoryResponse = result;
      } else {
        applyMemoryResponse(result);
        memoriesLoaded = true;
        editingMemory = null;
      }
    } catch (error) {
      if (destroyed || requestId !== memoryRequestId) {
        return;
      }
      memoryError = viewErrorMessage(error, t('memory.loadError'));
    } finally {
      if (!destroyed && requestId === memoryRequestId) {
        memoriesLoading = false;
      }
    }
  }

  function applyMemoryResponse(result) {
    const scopes = result?.scopes;
    memoriesByScope = {
      agent: normalizeMemoryEntries(scopes?.agent, 'agent'),
      user: normalizeMemoryEntries(scopes?.user, 'user'),
      project: normalizeMemoryEntries(scopes?.project, 'project'),
    };
  }

  function normalizeMemoryEntries(entries, scope) {
    if (!Array.isArray(entries)) {
      return [];
    }
    return entries
      .filter(
        (entry) =>
          Number.isInteger(entry?.id) &&
          entry.id > 0 &&
          typeof entry?.content === 'string',
      )
      .map((entry) => ({ id: entry.id, scope, content: entry.content }));
  }

  function memoryEntries(scope) {
    return Array.isArray(memoriesByScope[scope]) ? memoriesByScope[scope] : [];
  }

  function memoryEditorBusy() {
    return Boolean(
      editingMemory ||
      deletingMemory ||
      memoryMutation ||
      historyBusy ||
      memoryDrafts.agent.trim() ||
      memoryDrafts.user.trim() ||
      memoryDrafts.project.trim(),
    );
  }

  function memoryScopeActive(scope) {
    return activeScopes.includes(scope);
  }

  function memoryScopeLabel(scope) {
    if (scope === 'project') return t('memory.scope.project');
    return scope === 'agent' ? t('memory.scope.agent') : t('memory.scope.user');
  }

  function memoryScopeDescription(scope) {
    if (scope === 'project') return t('memory.scope.projectDescription');
    return scope === 'agent'
      ? t('memory.scope.agentDescription')
      : t('memory.scope.userDescription');
  }

  function memoryScopeState(scope) {
    return memoryScopeActive(scope) ? t('memory.active') : t('memory.inactive');
  }

  function memoryCountLabel(count) {
    return count === 1
      ? t('memory.countOne')
      : t('memory.countMany', { count });
  }

  function totalMemoryCount() {
    return scopes.reduce(
      (total, scope) => total + memoryEntries(scope).length,
      0,
    );
  }

  async function addMemoryEntry(scope) {
    const ownerId = projectId || agentId;
    const content = memoryDrafts[scope]?.trim();
    if (!ownerId || !content || memoryMutation || historyBusy) {
      return;
    }
    memoryRequestId += 1;
    memoriesLoading = false;
    memoryMutation = `add:${scope}`;
    memoryError = '';
    try {
      const result = await (projectId
        ? addProjectMemory(ownerId, content)
        : addAgentMemory(ownerId, scope, content));
      if (destroyed) {
        return;
      }
      applyMemoryResponse(result);
      historyRefresh += 1;
      pendingMemoryResponse = null;
      memoriesLoaded = true;
      memoryDrafts[scope] = '';
      onToast(t('memory.saved'));
    } catch (error) {
      if (!destroyed) {
        memoryError = viewErrorMessage(error, t('memory.saveError'));
      }
    } finally {
      if (!destroyed) {
        memoryMutation = '';
      }
    }
  }

  function startEditingMemory(scope, entry) {
    if (memoryMutation || historyBusy) {
      return;
    }
    editingMemory = { scope, id: entry.id, content: entry.content };
    memoryError = '';
  }

  function cancelEditingMemory() {
    editingMemory = null;
  }

  async function saveEditedMemory() {
    const ownerId = projectId || agentId;
    const edit = editingMemory;
    const content = edit?.content?.trim();
    if (!ownerId || !edit || !content || memoryMutation || historyBusy) {
      return;
    }
    memoryRequestId += 1;
    memoriesLoading = false;
    memoryMutation = `replace:${edit.scope}:${edit.id}`;
    memoryError = '';
    try {
      const result = await (projectId
        ? replaceProjectMemory(ownerId, edit.id, content)
        : replaceAgentMemory(ownerId, edit.scope, edit.id, content));
      if (destroyed) {
        return;
      }
      applyMemoryResponse(result);
      historyRefresh += 1;
      pendingMemoryResponse = null;
      memoriesLoaded = true;
      editingMemory = null;
      onToast(t('memory.updated'));
    } catch (error) {
      if (!destroyed) {
        memoryError = viewErrorMessage(error, t('memory.saveError'));
      }
    } finally {
      if (!destroyed) {
        memoryMutation = '';
      }
    }
  }

  function requestDeleteMemory(scope, entry) {
    if (memoryMutation || historyBusy) {
      return;
    }
    deletingMemory = { scope, id: entry.id };
  }

  function cancelDeleteMemory() {
    deletingMemory = null;
  }

  async function deleteMemoryEntry() {
    const ownerId = projectId || agentId;
    const target = deletingMemory;
    if (!ownerId || !target || memoryMutation || historyBusy) {
      return;
    }
    memoryRequestId += 1;
    memoriesLoading = false;
    deletingMemory = null;
    memoryMutation = `remove:${target.scope}:${target.id}`;
    memoryError = '';
    try {
      const result = await (projectId
        ? removeProjectMemory(ownerId, target.id)
        : removeAgentMemory(ownerId, target.scope, target.id));
      if (destroyed) {
        return;
      }
      applyMemoryResponse(result);
      historyRefresh += 1;
      pendingMemoryResponse = null;
      memoriesLoaded = true;
      if (
        editingMemory?.scope === target.scope &&
        editingMemory?.id === target.id
      ) {
        editingMemory = null;
      }
      onToast(t('memory.deleted'));
    } catch (error) {
      if (!destroyed) {
        memoryError = viewErrorMessage(error, t('memory.deleteError'));
      }
    } finally {
      if (!destroyed) {
        memoryMutation = '';
      }
    }
  }
  let destroyed = false;
  $effect(() => {
    if (lastMemoriesRefreshToken === null) {
      lastMemoriesRefreshToken = refreshToken;
      return;
    }
    if (refreshToken === lastMemoriesRefreshToken) {
      return;
    }
    lastMemoriesRefreshToken = refreshToken;
    if (memoryPanelOpen && Boolean(agentId || projectId)) {
      void loadAgentMemoryEntries({ showLoading: false, deferWhileBusy: true });
    }
  });
  $effect(() => {
    if (!pendingMemoryResponse || memoryEditorBusy()) {
      return;
    }
    const response = pendingMemoryResponse;
    pendingMemoryResponse = null;
    applyMemoryResponse(response);
    memoriesLoaded = true;
    editingMemory = null;
  });
  onDestroy(() => {
    destroyed = true;
    memoryRequestId += 1;
  });
</script>

<div class="memory-editor">
  <button
    type="button"
    class="s-row s-row--compact s-disclosure-row agents-view__memory-disclosure-toggle"
    aria-expanded={memoryPanelOpen}
    aria-controls={`memory-manager-${editorId}`}
    aria-label={memoryPanelOpen ? t('memory.hide') : t('memory.manage')}
    onclick={toggleMemoryPanel}
  >
    <span class="s-row-label">
      <span
        class="disclosure-chevron"
        class:disclosure-chevron--open={memoryPanelOpen}
        aria-hidden="true"
      ></span>
      {memoryPanelOpen ? t('memory.hide') : t('memory.manage')}
    </span>
    {#if memoriesLoaded}
      <span class="s-disclosure__meta agents-view__memory-total">
        {memoryCountLabel(totalMemoryCount())}
      </span>
    {/if}
  </button>

  {#if memoryPanelOpen}
    <div
      id={`memory-manager-${editorId}`}
      class="s-group__block agents-view__memory-manager"
    >
      {#if memoriesLoading && !memoriesLoaded}
        <Banner variant="neutral" aria-live="polite">
          {t('memory.loading')}
        </Banner>
      {/if}

      {#if memoryError}
        <Banner variant="error" role="alert">
          <span>{memoryError}</span>
          <Button variant="secondary" onClick={() => loadAgentMemoryEntries()}>
            {t('common.retry')}
          </Button>
        </Banner>
      {/if}

      {#if memoriesLoaded}
        {#each scopes as scope (scope)}
          {@const entries = memoryEntries(scope)}
          <section
            class="agents-view__memory-scope"
            class:agents-view__memory-scope--inactive={!memoryScopeActive(
              scope,
            )}
            aria-labelledby={`memory-${editorId}-${scope}-title`}
          >
            <div class="agents-view__memory-scope-header">
              <div class="agents-view__memory-scope-copy">
                <div class="agents-view__memory-scope-title-row">
                  <h4 id={`memory-${editorId}-${scope}-title`}>
                    {memoryScopeLabel(scope)}
                  </h4>
                  <StatusChip
                    variant={memoryScopeActive(scope) ? 'success' : 'neutral'}
                  >
                    {memoryScopeState(scope)}
                  </StatusChip>
                </div>
                <p>{memoryScopeDescription(scope)}</p>
              </div>
              <span class="agents-view__memory-count">
                {memoryCountLabel(entries.length)}
              </span>
            </div>

            {#if entries.length === 0}
              <p class="agents-view__memory-empty">
                {t('memory.emptyTitle')}
              </p>
            {:else}
              <ol class="agents-view__memory-list">
                {#each entries as entry (entry.id)}
                  <li class="agents-view__memory-entry">
                    <span class="agents-view__memory-entry-id">
                      {entry.id}
                    </span>
                    {#if editingMemory?.scope === scope && editingMemory?.id === entry.id}
                      <div class="agents-view__memory-entry-editor">
                        <TextArea
                          rows={3}
                          value={editingMemory.content}
                          ariaLabel={t('memory.editLabel')}
                          onInput={(next) => {
                            editingMemory.content = next;
                          }}
                          disabled={Boolean(memoryMutation) || historyBusy}
                        />
                        <div class="agents-view__memory-entry-actions">
                          <Button
                            variant="secondary"
                            onClick={cancelEditingMemory}
                            disabled={Boolean(memoryMutation) || historyBusy}
                          >
                            {t('common.cancel')}
                          </Button>
                          <Button
                            variant="primary"
                            onClick={saveEditedMemory}
                            loading={memoryMutation.startsWith('replace:')}
                            disabled={!editingMemory.content.trim() ||
                              historyBusy}
                          >
                            {t('common.save')}
                          </Button>
                        </div>
                      </div>
                    {:else}
                      <p class="agents-view__memory-entry-content">
                        {entry.content}
                      </p>
                      <div class="agents-view__memory-entry-actions">
                        <Button
                          variant="tertiary"
                          onClick={() => startEditingMemory(scope, entry)}
                          disabled={Boolean(memoryMutation) || historyBusy}
                        >
                          {t('common.edit')}
                        </Button>
                        <Button
                          variant="tertiary"
                          class="agents-view__memory-delete"
                          onClick={() => requestDeleteMemory(scope, entry)}
                          disabled={Boolean(memoryMutation) || historyBusy}
                        >
                          {t('common.delete')}
                        </Button>
                      </div>
                    {/if}
                  </li>
                {/each}
              </ol>
            {/if}

            <div class="agents-view__memory-add">
              <TextArea
                rows={2}
                value={memoryDrafts[scope]}
                placeholder={t('memory.addPlaceholder')}
                ariaLabel={t('memory.addLabel', {
                  category: memoryScopeLabel(scope),
                })}
                onInput={(next) => {
                  memoryDrafts[scope] = next;
                }}
                disabled={Boolean(memoryMutation) || historyBusy}
              />
              <Button
                variant="secondary"
                onClick={() => addMemoryEntry(scope)}
                loading={memoryMutation === `add:${scope}`}
                disabled={!memoryDrafts[scope].trim() || historyBusy}
              >
                {t('memory.add')}
              </Button>
            </div>
          </section>
        {/each}
      {/if}
      {#if projectId && memoriesLoaded}
        <ProjectMemoryHistory
          {projectId}
          refreshToken={`${refreshToken}:${historyRefresh}`}
          disabled={memoryEditorBusy()}
          {onToast}
          onBusyChange={(busy) => {
            historyBusy = busy;
            if (busy) {
              memoryRequestId += 1;
              memoriesLoading = false;
              pendingMemoryResponse = null;
            }
          }}
          onReverted={(result) => {
            applyMemoryResponse(result);
            pendingMemoryResponse = null;
          }}
        />
      {/if}
    </div>
  {/if}
  {#if deletingMemory}
    <ConfirmDialog
      title={t('memory.deleteConfirmTitle')}
      body={t('memory.deleteConfirmBody')}
      confirmLabel={t('memory.deleteConfirmAction')}
      onConfirm={deleteMemoryEntry}
      onCancel={cancelDeleteMemory}
    />
  {/if}
</div>
