<script>
  import { onMount } from 'svelte';

  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';

  const AUTO_SAVE_DEBOUNCE_MS = 800;
  const noop = () => {};

  let { onToast = noop } = $props();

  let fragments = $state([]);
  let agents = $state([]);
  let selectedAgentId = $state('');
  let previewText = $state('');
  let previewTokens = $state(null);
  let isLoadingData = $state(true);
  let isRefreshingPreview = $state(false);
  let autoSaveTimers = [];

  let isPromptSaveBusy = $derived(
    fragments.some((fragment) => fragment.isSaving || fragment.isResetting),
  );

  onMount(() => {
    loadData();
    return () => {
      clearAutoSaveTimers();
    };
  });

  $effect(() => {
    autoSaveTimers.forEach((timer, index) => {
      if (timer && !fragments[index]) {
        clearAutoSaveTimer(index);
      }
    });

    fragments.forEach((fragment, index) => {
      const shouldAutoSave =
        fragment.isDirty && !fragment.isSaving && !fragment.isResetting;

      if (!shouldAutoSave) {
        clearAutoSaveTimer(index);
        return;
      }

      if (autoSaveTimers[index]) {
        return;
      }

      scheduleAutoSaveTimer(index);
    });
  });

  async function loadData() {
    isLoadingData = true;

    try {
      const [agentsResult, promptsResult] = await Promise.all([
        rpc('agent.list'),
        rpc('prompt.list'),
      ]);

      agents = Array.isArray(agentsResult?.agents) ? agentsResult.agents : [];
      selectedAgentId = agents[0]?.id ?? '';

      const rawFragments = Array.isArray(promptsResult?.fragments)
        ? promptsResult.fragments
        : [];

      fragments = rawFragments.map((fragment) => ({
        name: fragment.name,
        content: fragment.content ?? '',
        editedContent: fragment.content ?? '',
        isDirty: false,
        isModified: fragment.is_modified ?? false,
        variables: Array.isArray(fragment.variables) ? fragment.variables : [],
        isSaving: false,
        isResetting: false,
      }));
    } catch {
      showToast(
        t('systemPrompt.error.loadFailed', 'Failed to load prompt data'),
        'error',
      );
    } finally {
      isLoadingData = false;
    }
  }

  function handleTextareaInput(index, event) {
    const nextContent = event.currentTarget.value;
    fragments[index].editedContent = nextContent;
    fragments[index].isDirty = nextContent !== fragments[index].content;

    if (fragments[index].isDirty) {
      clearAutoSaveTimer(index);
      scheduleAutoSaveTimer(index);
      return;
    }

    clearAutoSaveTimer(index);
  }

  async function saveFragment(index, options = {}) {
    const fragment = fragments[index];
    const showSuccessToast = options.showSuccessToast ?? true;

    if (
      !fragment ||
      !fragment.isDirty ||
      fragment.isSaving ||
      fragment.isResetting
    ) {
      return false;
    }

    const draftContent = fragment.editedContent;

    fragments[index].isSaving = true;

    try {
      const result = await rpc('prompt.update', {
        name: fragment.name,
        content: draftContent,
      });

      const nextSavedContent = result.content ?? draftContent;

      if (!fragments[index]) {
        return;
      }

      fragments[index].content = nextSavedContent;

      if (fragments[index].editedContent === draftContent) {
        fragments[index].editedContent = nextSavedContent;
        fragments[index].isDirty = false;
      } else {
        fragments[index].isDirty =
          fragments[index].editedContent !== fragments[index].content;
      }

      fragments[index].isModified = result.is_modified ?? true;
      if (showSuccessToast) {
        showToast(t('common.saved', 'Saved'), 'success');
      }
      return true;
    } catch {
      showToast(t('systemPrompt.error.saveFailed', 'Failed to save'), 'error');
      return false;
    } finally {
      if (fragments[index]) {
        fragments[index].isSaving = false;
      }
    }
  }

  function scheduleAutoSaveTimer(index) {
    const fragment = fragments[index];
    if (
      !fragment ||
      !fragment.isDirty ||
      fragment.isSaving ||
      fragment.isResetting ||
      autoSaveTimers[index]
    ) {
      return;
    }

    const timer = setTimeout(() => {
      delete autoSaveTimers[index];
      void saveFragment(index);
    }, AUTO_SAVE_DEBOUNCE_MS);

    autoSaveTimers[index] = timer;
  }

  function clearAutoSaveTimer(index) {
    const timer = autoSaveTimers[index];
    if (!timer) {
      return;
    }

    clearTimeout(timer);
    delete autoSaveTimers[index];
  }

  function clearAutoSaveTimers() {
    for (const timer of autoSaveTimers) {
      if (timer) {
        clearTimeout(timer);
      }
    }

    autoSaveTimers = [];
  }

  async function handleManualSaveAll() {
    if (isPromptSaveBusy) {
      return;
    }

    const dirtyIndexes = fragments.reduce((indexes, fragment, index) => {
      if (fragment.isDirty) {
        indexes.push(index);
      }

      return indexes;
    }, []);

    if (dirtyIndexes.length === 0) {
      showToast(t('common.alreadySaved', 'Already saved'), 'success');
      return;
    }

    for (const index of dirtyIndexes) {
      clearAutoSaveTimer(index);
    }

    const results = await Promise.all(
      dirtyIndexes.map((index) =>
        saveFragment(index, { showSuccessToast: false }),
      ),
    );

    if (results.every(Boolean)) {
      showToast(t('common.saved', 'Saved'), 'success');
    }
  }

  async function resetFragment(index) {
    const fragment = fragments[index];
    const confirmed = window.confirm(
      t(
        'systemPrompt.fragmentEditor.resetConfirm',
        'Reset this fragment to its bundled default? This cannot be undone.',
      ),
    );

    if (!confirmed) {
      return;
    }

    fragments[index].isResetting = true;

    try {
      const result = await rpc('prompt.reset', { name: fragment.name });
      const restoredContent = result.content ?? '';
      fragments[index].content = restoredContent;
      fragments[index].editedContent = restoredContent;
      fragments[index].isDirty = false;
      fragments[index].isModified = false;
    } catch {
      showToast(
        t('systemPrompt.error.resetFailed', 'Failed to reset'),
        'error',
      );
    } finally {
      fragments[index].isResetting = false;
    }
  }

  async function refreshPreview() {
    if (!selectedAgentId) {
      return;
    }

    isRefreshingPreview = true;

    try {
      const result = await rpc('prompt.preview', {
        agent_id: selectedAgentId,
      });
      previewText = result.text ?? '';
      previewTokens = result.tokens ?? null;
    } catch {
      showToast(
        t('systemPrompt.error.previewFailed', 'Failed to load preview'),
        'error',
      );
    } finally {
      isRefreshingPreview = false;
    }
  }

  async function copyPreview() {
    if (!previewText) {
      return;
    }

    try {
      await navigator.clipboard.writeText(previewText);
    } catch {
      showToast(t('systemPrompt.error.copyFailed', 'Failed to copy'), 'error');
    }
  }

  function showToast(message, variant = 'error') {
    onToast?.({ title: message, variant });
  }
</script>

<section class="sp-view view active" aria-labelledby="sp-title">
  <div class="sp-layout">
    <div class="sp-scroll">
      <div class="sp-header">
        <h2 id="sp-title" class="sp-title">
          {t('systemPrompt.title', 'System Prompt')}
        </h2>
      </div>

      {#if isLoadingData}
        <div class="sp-feedback sp-feedback--neutral">
          {t('common.loading', 'Loading…')}
        </div>
      {:else}
        <div class="sp-fragments">
          {#each fragments as fragment, index (fragment.name)}
            <div class="sp-fragment">
              <div class="sp-fragment-header">
                <div class="sp-fragment-meta">
                  <span class="sp-fragment-name">{fragment.name}</span>
                  {#if fragment.isDirty}
                    <span
                      class="sp-badge sp-badge--dirty"
                      title={t(
                        'systemPrompt.fragmentEditor.dirtyIndicator',
                        'Unsaved changes',
                      )}
                    >
                      {t(
                        'systemPrompt.fragmentEditor.dirtyIndicator',
                        'unsaved',
                      )}
                    </span>
                  {/if}
                  {#if fragment.isModified}
                    <span
                      class="sp-badge sp-badge--modified"
                      title={t(
                        'systemPrompt.fragmentEditor.modifiedIndicator',
                        'User copy — differs from bundled default',
                      )}
                    >
                      {t(
                        'systemPrompt.fragmentEditor.modifiedIndicator',
                        'modified',
                      )}
                    </span>
                  {/if}
                </div>
                <div class="sp-fragment-actions">
                  <button
                    class="btn-outline sp-btn-sm"
                    type="button"
                    disabled={fragment.isResetting || fragment.isSaving}
                    onclick={() => resetFragment(index)}
                  >
                    {fragment.isResetting
                      ? t('common.loading', 'Loading…')
                      : t('systemPrompt.fragmentEditor.reset', 'Reset')}
                  </button>
                </div>
              </div>

              {#if fragment.variables.length > 0}
                <div class="sp-variables">
                  {#each fragment.variables as variable (variable.placeholder)}
                    <span class="sp-variable" title={variable.description}
                      >{variable.placeholder}</span
                    >
                  {/each}
                </div>
              {/if}

              <textarea
                class="sp-textarea"
                spellcheck="false"
                value={fragment.editedContent}
                oninput={(event) => handleTextareaInput(index, event)}
              ></textarea>
            </div>
          {/each}
        </div>

        <div class="sp-preview-section">
          <div class="sp-preview-header">
            <div class="sp-preview-heading-row">
              <span class="sp-preview-heading">
                {t('systemPrompt.preview.heading', 'Preview for')}
              </span>
              {#if agents.length > 0}
                <label class="sp-agent-label" for="sp-agent-select">
                  {t('systemPrompt.preview.agentLabel', 'Agent')}
                </label>
                <select
                  id="sp-agent-select"
                  class="sp-agent-select"
                  bind:value={selectedAgentId}
                >
                  {#each agents as agent (agent.id)}
                    <option value={agent.id}>{agent.name || agent.id}</option>
                  {/each}
                </select>
              {/if}
              {#if previewTokens !== null}
                <span class="sp-token-count">
                  {t('systemPrompt.preview.tokenCount', '~{count} tokens', {
                    count: previewTokens,
                  })}
                </span>
              {/if}
            </div>
            <div class="sp-preview-controls">
              <button
                class="btn-outline sp-btn-sm"
                type="button"
                disabled={!previewText}
                onclick={copyPreview}
              >
                {t('systemPrompt.preview.copy', 'Copy')}
              </button>
              <button
                class="btn-primary sp-btn-sm"
                type="button"
                disabled={isRefreshingPreview || !selectedAgentId}
                onclick={refreshPreview}
              >
                {isRefreshingPreview
                  ? t('common.loading', 'Loading…')
                  : t('systemPrompt.preview.refresh', 'Refresh')}
              </button>
            </div>
          </div>

          <div class="sp-preview-body">
            {#if previewText}
              <pre class="sp-preview-pre">{previewText}</pre>
            {:else}
              <div class="sp-preview-empty">
                {t(
                  'systemPrompt.preview.empty',
                  'Click Refresh to generate a preview for the selected agent.',
                )}
              </div>
            {/if}
          </div>
        </div>

        <div class="sp-global-footer">
          <button
            class="btn-primary sp-btn-sm"
            type="button"
            disabled={isPromptSaveBusy}
            onclick={handleManualSaveAll}
          >
            {isPromptSaveBusy
              ? t('common.saving', 'Saving…')
              : t('systemPrompt.fragmentEditor.save', 'Save')}
          </button>
        </div>
      {/if}
    </div>
  </div>
</section>

<style>
  .sp-view {
    display: flex;
    min-height: 0;
    flex: 1;
    overflow: hidden;
    background: var(--bg);
    position: relative;
  }

  .sp-layout {
    display: flex;
    min-height: 0;
    min-width: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
  }

  .sp-scroll {
    display: flex;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    gap: 24px;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 26px 30px;
    scrollbar-gutter: stable;
  }

  .sp-header {
    flex-shrink: 0;
  }

  .sp-title {
    color: var(--text-hi);
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.03em;
    line-height: 1.2;
    margin: 0;
  }

  .sp-feedback {
    padding: 12px 14px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .sp-feedback--neutral {
    color: var(--text-med);
    background: rgba(255, 255, 255, 0.02);
  }

  .sp-fragments {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .sp-fragment {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    overflow: hidden;
    position: relative;
    background: var(--bg);
  }

  .sp-fragment-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    border-radius: var(--r-lg) var(--r-lg) 0 0;
    background: var(--surface);
    flex-shrink: 0;
  }

  .sp-fragment-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .sp-fragment-name {
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: 12.5px;
    font-weight: 500;
  }

  .sp-badge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.04em;
    line-height: 1.6;
    text-transform: lowercase;
  }

  .sp-badge--dirty {
    color: var(--amber, #f59e0b);
    background: rgba(245, 158, 11, 0.12);
    border: 1px solid rgba(245, 158, 11, 0.22);
  }

  .sp-badge--modified {
    color: var(--accent);
    background: var(--accent-dim);
    border: 1px solid rgba(232, 135, 10, 0.2);
  }

  .sp-fragment-actions {
    display: flex;
    gap: 6px;
    flex-shrink: 0;
  }

  .sp-btn-sm {
    padding: 4px 10px;
    font-size: 12px;
  }

  .sp-variables {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    padding: 8px 14px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .sp-variable {
    display: inline-block;
    padding: 2px 7px;
    border-radius: 3px;
    border: 1px solid var(--border-2);
    color: var(--text-lo);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 11px;
    cursor: default;
  }

  .sp-variable:hover {
    color: var(--text-med);
    border-color: var(--accent);
  }

  .sp-textarea {
    width: 100%;
    min-height: 180px;
    padding: 12px 14px;
    border: 0;
    color: var(--text-hi);
    background: var(--bg);
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.6;
    resize: vertical;
    box-sizing: border-box;
  }

  .sp-textarea:focus {
    outline: none;
    box-shadow: inset 0 0 0 1px rgba(232, 135, 10, 0.3);
  }

  .sp-global-footer {
    display: flex;
    flex-shrink: 0;
    justify-content: flex-end;
    padding: 0 0 4px;
  }

  .sp-preview-section {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    overflow: hidden;
    flex-shrink: 0;
  }

  .sp-preview-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    flex-shrink: 0;
    flex-wrap: wrap;
  }

  .sp-preview-heading-row {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    flex-wrap: wrap;
  }

  .sp-preview-heading {
    color: var(--text-hi);
    font-size: 12.5px;
    font-weight: 500;
  }

  .sp-agent-label {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    line-height: 1;
  }

  .sp-agent-select {
    padding: 3px 8px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-sm);
    color: var(--text-hi);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 12px;
    appearance: none;
    cursor: pointer;
    max-width: 220px;
  }

  .sp-token-count {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 3px;
    background: var(--surface-2);
  }

  .sp-preview-controls {
    display: flex;
    gap: 6px;
    flex-shrink: 0;
  }

  .sp-preview-body {
    min-height: 120px;
    background: var(--bg);
  }

  .sp-preview-pre {
    margin: 0;
    padding: 14px;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .sp-preview-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 120px;
    padding: 20px;
    color: var(--text-lo);
    font-size: 12.5px;
    text-align: center;
  }

  @media (max-width: 760px) {
    .sp-scroll {
      padding: 18px 16px;
    }

    .sp-fragment-header {
      flex-wrap: wrap;
    }

    .sp-preview-header {
      flex-direction: column;
      align-items: flex-start;
    }
  }
</style>
