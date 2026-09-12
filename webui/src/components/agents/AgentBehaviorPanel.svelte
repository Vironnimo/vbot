<script>
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import CompactionPolicyEditor from '../compaction/CompactionPolicyEditor.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import {
    AGENT_FORM_MODE_EDIT,
    AGENT_FORM_MODE_CREATE,
    AGENT_MEMORY_PROMPT_MODES,
  } from '$lib/agentForm.js';
  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import { onDestroy } from 'svelte';
  import {
    listAgentMemories,
    replaceAgentMemory,
    addAgentMemory,
    removeAgentMemory,
  } from '$lib/api.js';
  let {
    agent,
    onNavigateToSettingsPanel,
    memoriesRefreshToken,
    formValues = $bindable(),
    formMode,
    activeDetail,
    handleCustomPromptToggle,
    navigateToAgentPrompt,
    showAgentToast,
    viewErrorMessage,
  } = $props();

  const MEMORY_SCOPES = ['agent', 'user'];

  let memoryPanelOpen = $state(false);

  let memoriesLoaded = $state(false);

  let memoriesLoading = $state(false);

  let memoryError = $state('');

  let memoriesByScope = $state({ agent: [], user: [] });

  let memoryDrafts = $state({ agent: '', user: '' });

  let editingMemory = $state(null);

  let deletingMemory = $state(null);

  let memoryMutation = $state('');

  let pendingMemoryResponse = $state(null);

  let memoryRequestId = 0;

  let lastMemoriesRefreshToken = null;

  let memoryPromptOptions = $derived(
    AGENT_MEMORY_PROMPT_MODES.map((option) => ({
      value: option,
      label: memoryPromptLabel(option),
    })),
  );

  function setOwnCompactionPolicy(enabled) {
    formValues.compaction_policy = enabled
      ? structuredClone(agent?.effective_compaction_policy ?? {})
      : null;
  }

  function memoryPromptLabel(option) {
    return t(`agents.form.memoryPromptModeOption.${option}`, option);
  }

  function toggleMemoryPanel() {
    memoryPanelOpen = !memoryPanelOpen;
    if (
      memoryPanelOpen &&
      !memoriesLoading &&
      formMode === AGENT_FORM_MODE_EDIT
    ) {
      void loadAgentMemoryEntries({
        showLoading: !memoriesLoaded,
        deferWhileBusy: true,
      });
    }
  }

  async function loadAgentMemoryEntries(options = {}) {
    const agentId = agent?.id;
    if (!agentId) {
      return;
    }
    const requestId = ++memoryRequestId;
    if (options.showLoading !== false) {
      memoriesLoading = true;
    }
    memoryError = '';
    try {
      const result = await listAgentMemories(agentId);
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
      memoryError = viewErrorMessage(
        error,
        t('agents.memory.loadError', 'Memory entries could not be loaded.'),
      );
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
      memoryDrafts.agent.trim() ||
      memoryDrafts.user.trim(),
    );
  }

  function memoryScopeActive(scope) {
    if (scope === 'agent') {
      return formValues.memory_prompt_mode !== 'off';
    }
    return formValues.memory_prompt_mode === 'agent_user';
  }

  function memoryScopeLabel(scope) {
    return scope === 'agent'
      ? t('agents.memory.scope.agent', 'Agent Memory')
      : t('agents.memory.scope.user', 'User profile');
  }

  function memoryScopeDescription(scope) {
    return scope === 'agent'
      ? t(
          'agents.memory.scope.agentDescription',
          'Durable notes about this Agent’s work and behavior.',
        )
      : t(
          'agents.memory.scope.userDescription',
          'Durable facts and preferences about the user.',
        );
  }

  function memoryScopeState(scope) {
    return memoryScopeActive(scope)
      ? t('agents.memory.active', 'Active')
      : t('agents.memory.inactive', 'Not active');
  }

  function memoryCountLabel(count) {
    return count === 1
      ? t('agents.memory.countOne', '1 entry')
      : t('agents.memory.countMany', '{count} entries', { count });
  }

  function totalMemoryCount() {
    return MEMORY_SCOPES.reduce(
      (total, scope) => total + memoryEntries(scope).length,
      0,
    );
  }

  async function addMemoryEntry(scope) {
    const agentId = agent?.id;
    const content = memoryDrafts[scope]?.trim();
    if (!agentId || !content || memoryMutation) {
      return;
    }
    memoryRequestId += 1;
    memoriesLoading = false;
    memoryMutation = `add:${scope}`;
    memoryError = '';
    try {
      const result = await addAgentMemory(agentId, scope, content);
      if (destroyed) {
        return;
      }
      applyMemoryResponse(result);
      pendingMemoryResponse = null;
      memoriesLoaded = true;
      memoryDrafts[scope] = '';
      showAgentToast(t('agents.memory.saved', 'Memory entry saved.'));
    } catch (error) {
      if (!destroyed) {
        memoryError = viewErrorMessage(
          error,
          t('agents.memory.saveError', 'Memory entry could not be saved.'),
        );
      }
    } finally {
      if (!destroyed) {
        memoryMutation = '';
      }
    }
  }

  function startEditingMemory(scope, entry) {
    if (memoryMutation) {
      return;
    }
    editingMemory = { scope, id: entry.id, content: entry.content };
    memoryError = '';
  }

  function cancelEditingMemory() {
    editingMemory = null;
  }

  async function saveEditedMemory() {
    const agentId = agent?.id;
    const edit = editingMemory;
    const content = edit?.content?.trim();
    if (!agentId || !edit || !content || memoryMutation) {
      return;
    }
    memoryRequestId += 1;
    memoriesLoading = false;
    memoryMutation = `replace:${edit.scope}:${edit.id}`;
    memoryError = '';
    try {
      const result = await replaceAgentMemory(
        agentId,
        edit.scope,
        edit.id,
        content,
      );
      if (destroyed) {
        return;
      }
      applyMemoryResponse(result);
      pendingMemoryResponse = null;
      memoriesLoaded = true;
      editingMemory = null;
      showAgentToast(t('agents.memory.updated', 'Memory entry updated.'));
    } catch (error) {
      if (!destroyed) {
        memoryError = viewErrorMessage(
          error,
          t('agents.memory.saveError', 'Memory entry could not be saved.'),
        );
      }
    } finally {
      if (!destroyed) {
        memoryMutation = '';
      }
    }
  }

  function requestDeleteMemory(scope, entry) {
    if (memoryMutation) {
      return;
    }
    deletingMemory = { scope, id: entry.id };
  }

  function cancelDeleteMemory() {
    deletingMemory = null;
  }

  async function deleteMemoryEntry() {
    const agentId = agent?.id;
    const target = deletingMemory;
    if (!agentId || !target || memoryMutation) {
      return;
    }
    memoryRequestId += 1;
    memoriesLoading = false;
    deletingMemory = null;
    memoryMutation = `remove:${target.scope}:${target.id}`;
    memoryError = '';
    try {
      const result = await removeAgentMemory(agentId, target.scope, target.id);
      if (destroyed) {
        return;
      }
      applyMemoryResponse(result);
      pendingMemoryResponse = null;
      memoriesLoaded = true;
      if (
        editingMemory?.scope === target.scope &&
        editingMemory?.id === target.id
      ) {
        editingMemory = null;
      }
      showAgentToast(t('agents.memory.deleted', 'Memory entry deleted.'));
    } catch (error) {
      if (!destroyed) {
        memoryError = viewErrorMessage(
          error,
          t('agents.memory.deleteError', 'Memory entry could not be deleted.'),
        );
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
      lastMemoriesRefreshToken = memoriesRefreshToken;
      return;
    }
    if (memoriesRefreshToken === lastMemoriesRefreshToken) {
      return;
    }
    lastMemoriesRefreshToken = memoriesRefreshToken;
    if (memoryPanelOpen && formMode === AGENT_FORM_MODE_EDIT) {
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

<div
  class="management-topic"
  role="tabpanel"
  id="agent-detail-panel-behavior"
  aria-labelledby="agent-detail-tab-behavior"
  hidden={activeDetail !== 'behavior'}
  tabindex="0"
>
  <div class="detail-group agents-view__compaction-group">
    <div class="detail-group-title">
      {t('agents.detail.compaction', 'Compaction Policy')}

      <Button
        variant="tertiary"
        onClick={() => onNavigateToSettingsPanel('compaction')}
        >{t('agents.shared.title', 'Shared defaults')}</Button
      >
    </div>
    <div class="agents-view__prompt-toggle-row">
      <div>
        <div class="agents-view__prompt-toggle-label">
          {formValues.compaction_policy
            ? t('compaction.scope.agentOwn', 'Use an Agent Policy')
            : t(
                'compaction.scope.inheritGlobal',
                'Inherit the global Policy live',
              )}
        </div>
        <div class="agents-view__prompt-toggle-desc">
          {t(
            'compaction.scope.agentDescription',
            'Inherited changes apply to this Agent’s existing Sessions unless a Session has its own override.',
          )}
        </div>
      </div>
      <Toggle
        checked={formValues.compaction_policy !== null}
        ariaLabel={t('compaction.scope.agentOwn', 'Use an Agent Policy')}
        onChange={setOwnCompactionPolicy}
      />
    </div>
    {#if formValues.compaction_policy}
      <CompactionPolicyEditor
        value={formValues.compaction_policy}
        onChange={(next) => (formValues.compaction_policy = next)}
        idPrefix="agent-compaction"
      />
    {/if}
  </div>

  <div class="detail-group agents-view__prompt-group">
    <div class="detail-group-title">
      {t('agents.detail.systemPrompt', 'System Prompt')}
    </div>
    <div class="agents-view__prompt-toggle-row">
      <span class="agents-view__prompt-toggle-label">
        {t('agents.form.customSystemPrompt', 'Custom system prompt')}
        <InfoHint
          text={t(
            'agents.form.customPromptHelp',
            'Gives this agent its own editable copy of the system prompt. Edit it in the System Prompt tab by selecting this agent as the scope. Turning this off keeps the customized blocks but stops using them.',
          )}
        />
      </span>
      <div class="agents-view__prompt-toggle-controls">
        {#if formMode === AGENT_FORM_MODE_EDIT && formValues.custom_system_prompt_enabled}
          <Button
            variant="tertiary"
            class="agents-view__inherit-link"
            onClick={navigateToAgentPrompt}
          >
            {t('agents.form.editAgentPrompt', "Edit this agent's prompt")}
          </Button>
        {/if}
        <Toggle
          size="sm"
          class="agents-view__prompt-toggle"
          checked={formValues.custom_system_prompt_enabled}
          ariaLabel={t(
            'agents.form.customSystemPrompt',
            'Custom system prompt',
          )}
          disabled={formMode === AGENT_FORM_MODE_CREATE}
          onChange={(next) => {
            void handleCustomPromptToggle(next);
          }}
        />
      </div>
    </div>
  </div>

  <div class="detail-group agents-view__memory-group">
    <div class="detail-group-title">
      {t('agents.detail.memory', 'Memory')}
    </div>
    <div class="agents-view__prompt-memory-row">
      <span class="agents-view__prompt-toggle-label">
        {t('agents.form.memoryPromptMode', 'Memory')}
        <InfoHint
          text={t(
            'agents.form.memoryModeHelp',
            'Which memory files are pinned into the System Prompt. The memory tool follows this setting — it is available to the agent unless this is off.',
          )}
        />
      </span>
      <Dropdown
        id="agent-memory-prompt-mode"
        value={formValues.memory_prompt_mode}
        options={memoryPromptOptions}
        ariaLabel={t('agents.form.memoryPromptMode', 'Memory')}
        triggerClass="agents-view__memory-dropdown"
        listClass="agents-view__memory-list"
        onValueChange={(selectedValue) => {
          formValues.memory_prompt_mode = selectedValue;
        }}
      />
    </div>

    {#if formMode === AGENT_FORM_MODE_EDIT}
      <div class="agents-view__memory-disclosure">
        <Button
          variant="tertiary"
          class="agents-view__memory-disclosure-toggle"
          aria-expanded={memoryPanelOpen}
          aria-controls="agent-memory-manager"
          ariaLabel={memoryPanelOpen
            ? t('agents.memory.hide', 'Hide Memory entries')
            : t('agents.memory.manage', 'Manage Memory entries')}
          onClick={toggleMemoryPanel}
        >
          <span
            class:agents-view__memory-chevron--open={memoryPanelOpen}
            class="agents-view__memory-chevron"
            aria-hidden="true">▸</span
          >
          <span>
            {memoryPanelOpen
              ? t('agents.memory.hide', 'Hide Memory entries')
              : t('agents.memory.manage', 'Manage Memory entries')}
          </span>
          {#if memoriesLoaded}
            <span class="agents-view__memory-total">
              {memoryCountLabel(totalMemoryCount())}
            </span>
          {/if}
        </Button>

        {#if memoryPanelOpen}
          <div id="agent-memory-manager" class="agents-view__memory-manager">
            {#if memoriesLoading && !memoriesLoaded}
              <Banner variant="neutral" aria-live="polite">
                {t('agents.memory.loading', 'Loading Memory entries…')}
              </Banner>
            {/if}

            {#if memoryError}
              <Banner variant="error" role="alert">
                <span>{memoryError}</span>
                <Button
                  variant="secondary"
                  onClick={() => loadAgentMemoryEntries()}
                >
                  {t('common.retry', 'Retry')}
                </Button>
              </Banner>
            {/if}

            {#if memoriesLoaded}
              <div class="agents-view__memory-scopes">
                {#each MEMORY_SCOPES as scope (scope)}
                  {@const entries = memoryEntries(scope)}
                  <section
                    class="agents-view__memory-scope"
                    class:agents-view__memory-scope--inactive={!memoryScopeActive(
                      scope,
                    )}
                    aria-labelledby={`agent-memory-${scope}-title`}
                  >
                    <div class="agents-view__memory-scope-header">
                      <div class="agents-view__memory-scope-copy">
                        <div class="agents-view__memory-scope-title-row">
                          <h3 id={`agent-memory-${scope}-title`}>
                            {memoryScopeLabel(scope)}
                          </h3>
                          <StatusChip
                            variant={memoryScopeActive(scope)
                              ? 'success'
                              : 'neutral'}
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
                      <EmptyState
                        density="compact"
                        class="agents-view__memory-empty"
                        title={t('agents.memory.emptyTitle', 'No memories')}
                        description={t(
                          'agents.memory.emptyDescription',
                          'This category has no saved Memory entries.',
                        )}
                      />
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
                                  ariaLabel={t(
                                    'agents.memory.editLabel',
                                    'Edit Memory entry',
                                  )}
                                  onInput={(next) => {
                                    editingMemory.content = next;
                                  }}
                                  disabled={Boolean(memoryMutation)}
                                />
                                <div class="agents-view__memory-entry-actions">
                                  <Button
                                    variant="secondary"
                                    onClick={cancelEditingMemory}
                                    disabled={Boolean(memoryMutation)}
                                  >
                                    {t('common.cancel', 'Cancel')}
                                  </Button>
                                  <Button
                                    variant="primary"
                                    onClick={saveEditedMemory}
                                    loading={memoryMutation.startsWith(
                                      'replace:',
                                    )}
                                    disabled={!editingMemory.content.trim()}
                                  >
                                    {t('common.save', 'Save')}
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
                                  onClick={() =>
                                    startEditingMemory(scope, entry)}
                                  disabled={Boolean(memoryMutation)}
                                >
                                  {t('common.edit', 'Edit')}
                                </Button>
                                <Button
                                  variant="danger"
                                  onClick={() =>
                                    requestDeleteMemory(scope, entry)}
                                  disabled={Boolean(memoryMutation)}
                                >
                                  {t('common.delete', 'Delete')}
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
                        placeholder={t(
                          'agents.memory.addPlaceholder',
                          'Add a durable fact…',
                        )}
                        ariaLabel={t(
                          'agents.memory.addLabel',
                          'New {category} entry',
                          { category: memoryScopeLabel(scope) },
                        )}
                        onInput={(next) => {
                          memoryDrafts[scope] = next;
                        }}
                        disabled={Boolean(memoryMutation)}
                      />
                      <Button
                        variant="secondary"
                        onClick={() => addMemoryEntry(scope)}
                        loading={memoryMutation === `add:${scope}`}
                        disabled={!memoryDrafts[scope].trim()}
                      >
                        {t('agents.memory.add', 'Add Memory')}
                      </Button>
                    </div>
                  </section>
                {/each}
              </div>
            {/if}
          </div>
        {/if}
      </div>
    {/if}
  </div>
</div>

{#if deletingMemory}
  <ConfirmDialog
    title={t('agents.memory.deleteConfirmTitle', 'Delete Memory entry?')}
    body={t(
      'agents.memory.deleteConfirmBody',
      'This permanently removes the selected Memory entry from the Agent’s Workspace.',
    )}
    confirmLabel={t('agents.memory.deleteConfirmAction', 'Delete Memory')}
    onConfirm={deleteMemoryEntry}
    onCancel={cancelDeleteMemory}
  />
{/if}
