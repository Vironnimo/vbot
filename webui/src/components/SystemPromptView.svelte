<script>
  import { t } from '$lib/i18n.js';
  import Dropdown from './Dropdown.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import Button from './ui/Button.svelte';
  import TabList from './ui/TabList.svelte';
  import Banner from './ui/Banner.svelte';
  import Badge from './ui/Badge.svelte';
  import Toggle from './ui/Toggle.svelte';
  import TextArea from './ui/TextArea.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import ToolDefinitionsPanel from './ToolDefinitionsPanel.svelte';
  import MarkdownContent from './chat/MarkdownContent.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import { onMount } from 'svelte';
  import { useAutosaveContext } from '$lib/autosave.js';
  import { createPromptScope } from './prompt/scope.svelte.js';
  import { createPromptEditor } from './prompt/editor.svelte.js';
  import './prompt/prompt.css';

  const noop = () => {};

  let {
    onToast = noop,
    // Scope deep-link (from the Agents editor's "Edit this agent's prompt"): a
    // target agent id + a fresh request id per request. When the request id
    // changes, this view selects that agent's scope after scopes have loaded,
    // falling back silently to the default scope when the target scope is absent.
    targetScopeAgentId = '',
    targetScopeRequestId = 0,
  } = $props();

  // The last handled deep-link request id, so a repeated request to the same
  // agent still re-selects (a new request id) but a re-render does not re-fire.
  let handledScopeRequestId = -1;

  let activeTab = $state('prompt');
  let promptFormat = $state('document');

  let tabs = $derived([
    { id: 'prompt', label: t('systemPrompt.tabs.prompt', 'Prompt') },
    { id: 'tools', label: t('systemPrompt.tabs.tools', 'Tools') },
    { id: 'edit', label: t('systemPrompt.tabs.edit', 'Edit blocks') },
  ]);
  let formatTabs = $derived([
    { id: 'document', label: t('systemPrompt.format.document', 'Document') },
    {
      id: 'original',
      label: t('systemPrompt.format.original', 'Original text'),
    },
  ]);

  const autosaveContext = useAutosaveContext();
  const scope = createPromptScope({
    get showToast() {
      return showToast;
    },
    get autosaveContext() {
      return autosaveContext;
    },
    get reorderAnnouncement() {
      return editor.reorderAnnouncement;
    },
    set reorderAnnouncement(value) {
      editor.reorderAnnouncement = value;
    },
    get clearAutoSaveTimers() {
      return editor.clearAutoSaveTimers;
    },
    get blocks() {
      return editor.blocks;
    },
    set blocks(value) {
      editor.blocks = value;
    },
  });
  const editor = createPromptEditor({
    get autosaveContext() {
      return autosaveContext;
    },
    get isAgentScope() {
      return scope.isAgentScope;
    },
    get scopedParams() {
      return scope.scopedParams;
    },
    get schedulePreviewRefresh() {
      return scope.schedulePreviewRefresh;
    },
    get showToast() {
      return showToast;
    },
    get selectedScopeKey() {
      return scope.selectedScopeKey;
    },
    get loadBlocksForScope() {
      return scope.loadBlocksForScope;
    },
  });

  onMount(() => {
    scope.loadData();
    scope.loadProjectTeams();
    return () => {
      scope.destroy();
      editor.destroy();
    };
  });

  // Apply a scope deep-link once (per request id) and only after scopes have
  // loaded, so the target agent scope actually exists in `promptScopes`. An
  // absent target scope falls back silently to the default scope.
  $effect(() => {
    if (targetScopeRequestId === handledScopeRequestId || scope.isLoadingData) {
      return;
    }
    // Wait until the initial scope list is available before consuming the request.
    if (scope.promptScopes.length === 0) {
      return;
    }
    handledScopeRequestId = targetScopeRequestId;
    if (!targetScopeAgentId) {
      return;
    }
    activeTab = 'edit';
    const targetKey = `agent:${targetScopeAgentId}`;
    const nextKey = scope.promptScopes.some((scope) => scope.key === targetKey)
      ? targetKey
      : 'default';
    void scope.selectScope(nextKey);
  });

  // Auto-load the preview whenever the settled preview target changes — the
  // initial data load, an agent pick, or a scope switch — so the user never has
  // to press Refresh to see the current scope's prompt. Gated on `isLoadingData`
  // so it fires once per settled target, not while blocks/scopes are still
  // loading; `refreshPreview` no-ops when there is no valid target.
  $effect(() => {
    if (scope.isLoadingData) {
      return;
    }
    if (!scope.canRefreshPreview()) {
      return;
    }
    void scope.refreshPreview();
  });

  function showToast(message, variant = 'error') {
    onToast?.({ title: message, variant });
  }
</script>

<section class="sp-view view active" aria-labelledby="sp-title">
  <div class="sp-layout">
    <div class="sp-top view-frame">
      <header class="sp-header view-header">
        <div class="view-header__intro">
          <p class="sp-eyebrow view-header__eyebrow">
            {t('systemPrompt.eyebrow', 'Agent context')}
          </p>
          <h2 id="sp-title" class="sp-title view-header__title">
            {t('systemPrompt.title', 'System Prompt')}
          </h2>
          <p class="sp-subtitle view-header__subtitle">
            {t(
              'systemPrompt.subtitle',
              'Read the prompt, inspect available Tools, and adjust instructions.',
            )}
          </p>
        </div>
      </header>

      <div class="sp-context-bar">
        <div class="sp-preview-heading-row">
          <span class="sp-preview-heading">
            {t('systemPrompt.preview.heading', 'Preview for')}
          </span>
          {#if scope.previewAgentOptions.length > 0}
            <span class="sp-agent-label" id="sp-agent-label">
              {t('systemPrompt.preview.agentLabel', 'Agent')}
            </span>
            <Dropdown
              id="sp-agent-select"
              value={scope.selectedAgentId}
              options={scope.previewAgentOptions}
              ariaLabel={t('systemPrompt.preview.agentLabel', 'Agent')}
              triggerClass="sp-agent-dropdown"
              listClass="sp-agent-dropdown-list"
              onValueChange={scope.selectPreviewAgent}
            />
          {/if}
          {#if scope.previewTokens !== null}
            {#if scope.previewToolTokens}
              <span
                class="sp-token-count"
                use:tooltip={t(
                  'systemPrompt.preview.tokenBreakdownHint',
                  'Estimated. Tools = the {count} tool definitions sent to the provider with every request alongside the system prompt.',
                  { count: scope.previewToolCount ?? 0 },
                )}
              >
                {t(
                  'systemPrompt.preview.tokenBreakdown',
                  '~{prompt} prompt + ~{tools} tools = ~{total} tokens',
                  {
                    prompt: scope.previewTokens,
                    tools: scope.previewToolTokens,
                    total: scope.previewTokens + scope.previewToolTokens,
                  },
                )}
              </span>
            {:else}
              <span class="sp-token-count">
                {t('systemPrompt.preview.tokenCount', '~{count} tokens', {
                  count: scope.previewTokens,
                })}
              </span>
            {/if}
          {/if}
        </div>
        <Button
          variant="secondary"
          class="sp-refresh"
          disabled={scope.isRefreshingPreview ||
            scope.isLoadingData ||
            !scope.canRefreshPreview()}
          onClick={scope.refreshPreview}
          >{t('systemPrompt.preview.refresh', 'Refresh')}</Button
        >
      </div>
      <div class="view-toolbar view-toolbar--tabs sp-navigation">
        <TabList
          items={tabs}
          value={activeTab}
          idPrefix="sp-content"
          ariaLabel={t('systemPrompt.tabs.label', 'System Prompt views')}
          onChange={(value) => (activeTab = value)}
        />
      </div>
    </div>
    <div class="sp-scroll view-frame">
      <div
        class="sp-editor"
        hidden={activeTab !== 'edit'}
        role="tabpanel"
        id="sp-content-panel-edit"
        aria-labelledby="sp-content-tab-edit"
        tabindex="0"
      >
        <div class="sp-blocklist-toolbar view-toolbar view-toolbar--split">
          <div class="sp-scope-control">
            <span
              class="sp-scope-label view-toolbar__label"
              id="sp-scope-label"
            >
              {t('systemPrompt.scope.label', 'Prompt scope')}
            </span>
            <Dropdown
              id="sp-scope-select"
              value={scope.selectedScopeKey}
              options={scope.scopeOptions}
              ariaLabel={t('systemPrompt.scope.label', 'Prompt scope')}
              triggerClass="sp-scope-dropdown"
              onValueChange={(value) => scope.selectScope(value)}
            />
          </div>
          {#if !scope.isLoadingData}
            <div class="sp-blocklist-toolbar-actions view-toolbar__actions">
              <Button
                variant="secondary"
                class="sp-btn-sm"
                onClick={editor.createCustomBlock}
              >
                {t('systemPrompt.blockList.newBlock', 'New block')}
              </Button>
              <Button
                variant="secondary"
                class="sp-btn-sm"
                onClick={editor.resetLayout}
              >
                {t(
                  'systemPrompt.blockList.resetLayout',
                  'Reset order & visibility',
                )}
              </Button>
            </div>
          {/if}
        </div>

        {#if scope.isLoadingData}
          <Banner variant="neutral">
            {t('common.loading', 'Loading…')}
          </Banner>
        {:else}
          <details
            class="sp-blocklist-guide"
            aria-labelledby="sp-blocklist-guide-title"
          >
            <summary class="sp-blocklist-guide__intro">
              <span class="sp-blocklist-guide__eyebrow">
                {t('systemPrompt.blockList.guide.label', 'How it works')}
              </span>
              <h3 id="sp-blocklist-guide-title">
                {t(
                  'systemPrompt.blockList.guide.title',
                  'These blocks become the System Prompt.',
                )}
              </h3>
            </summary>
            <div class="sp-blocklist-guide__details">
              <p>
                <strong>
                  {t('systemPrompt.blockList.guide.assemblyLabel', 'Assembly')}
                </strong>
                <span>
                  {t(
                    'systemPrompt.blockList.guide.assembly',
                    'Blocks are read from top to bottom. Drag to reorder them, use the switches to include or exclude them, and edit their content directly.',
                  )}
                </span>
              </p>
              <p>
                <strong>
                  {t('systemPrompt.blockList.guide.scopeLabel', 'Scope')}
                </strong>
                <span>
                  {t(
                    'systemPrompt.blockList.guide.scope',
                    'Default applies to every Agent. Enable “Custom system prompt” in Agents to create an Agent-specific scope here.',
                  )}
                </span>
              </p>
            </div>
          </details>

          <ul class="sp-blocks" role="list">
            {#each editor.blocks as block, index (block.id)}
              <li
                class="sp-block"
                class:sp-block--off={!block.enabled}
                class:sp-block--inherited={scope.isAgentScope &&
                  editor.isInherited(block)}
                ondragover={(event) => editor.handleDragOver(index, event)}
                ondrop={(event) => editor.handleDrop(index, event)}
              >
                <div class="sp-block-row">
                  <button
                    type="button"
                    class="sp-drag-handle"
                    draggable="true"
                    data-block-handle={block.id}
                    aria-label={t(
                      'systemPrompt.blockList.reorderHandle',
                      'Reorder {id} (use arrow keys)',
                      { id: block.id },
                    )}
                    ondragstart={(event) =>
                      editor.handleDragStart(index, event)}
                    ondragend={editor.handleDragEnd}
                    onkeydown={(event) =>
                      editor.handleHandleKeydown(index, event)}
                  >
                    <svg
                      width="12"
                      height="12"
                      viewBox="0 0 12 12"
                      aria-hidden="true"
                      focusable="false"
                    >
                      <circle cx="3.5" cy="2.5" r="1.1" fill="currentColor" />
                      <circle cx="8.5" cy="2.5" r="1.1" fill="currentColor" />
                      <circle cx="3.5" cy="6" r="1.1" fill="currentColor" />
                      <circle cx="8.5" cy="6" r="1.1" fill="currentColor" />
                      <circle cx="3.5" cy="9.5" r="1.1" fill="currentColor" />
                      <circle cx="8.5" cy="9.5" r="1.1" fill="currentColor" />
                    </svg>
                  </button>

                  <div class="sp-block-meta">
                    <strong class="sp-block-title"
                      >{t(
                        `systemPrompt.blockTitle.${block.id}`,
                        block.id,
                      )}</strong
                    >
                    <div class="sp-block-id-row">
                      <span class="sp-block-id">{block.id}</span>
                      {#if !block.enabled}<Badge variant="neutral"
                          >{t('systemPrompt.blockList.off', 'Off')}</Badge
                        >{/if}
                      {#if editor.isCustomBlock(block)}
                        <Badge variant="info">
                          {t('systemPrompt.blockList.customBadge', 'custom')}
                        </Badge>
                      {/if}
                      {#if block.kind === 'data'}
                        <span
                          class="tooltip-anchor"
                          use:tooltip={t(
                            'systemPrompt.blockList.dataHint',
                            'Generated content — rebuilt automatically, not editable.',
                          )}
                        >
                          <Badge variant="neutral">
                            {t('systemPrompt.blockList.dataBadge', 'auto')}
                          </Badge>
                        </span>
                      {/if}
                      {#if scope.isAgentScope && editor.isInherited(block)}
                        <span
                          class="tooltip-anchor"
                          use:tooltip={t(
                            'systemPrompt.blockList.inheritedHint',
                            'Inherited from the Default scope — editing creates an override.',
                          )}
                        >
                          <Badge variant="neutral">
                            {t(
                              'systemPrompt.blockList.inheritedBadge',
                              'inherited',
                            )}
                          </Badge>
                        </span>
                      {:else if block.editable && block.isModified}
                        <span
                          class="tooltip-anchor"
                          use:tooltip={t(
                            'systemPrompt.fragmentEditor.modifiedHint',
                            'Edited — differs from the built-in default.',
                          )}
                        >
                          <Badge variant="info">
                            {t(
                              'systemPrompt.fragmentEditor.modifiedIndicator',
                              'modified',
                            )}
                          </Badge>
                        </span>
                      {/if}
                      {#if block.editable && block.isDirty}
                        <span
                          class="tooltip-anchor"
                          use:tooltip={t(
                            'systemPrompt.fragmentEditor.dirtyIndicator',
                            'Unsaved changes',
                          )}
                        >
                          <Badge variant="warn">
                            {t(
                              'systemPrompt.fragmentEditor.dirtyIndicator',
                              'unsaved',
                            )}
                          </Badge>
                        </span>
                      {/if}
                    </div>
                    <span class="sp-block-owner"
                      >{editor.ownerHint(block.owner)}</span
                    >
                  </div>

                  <div class="sp-block-actions">
                    <Button
                      variant="secondary"
                      aria-expanded={block.editorExpanded}
                      aria-controls={`sp-block-body-${block.id}`}
                      onClick={() =>
                        (block.editorExpanded = !block.editorExpanded)}
                    >
                      {block.editorExpanded
                        ? t('systemPrompt.blockList.close', 'Close')
                        : block.editable
                          ? t('systemPrompt.blockList.edit', 'Edit')
                          : t('systemPrompt.blockList.inspect', 'Inspect')}
                    </Button>
                    {#if block.editable && !(scope.isAgentScope && editor.isInherited(block) && !block.isModified)}
                      <Button
                        variant="secondary"
                        class="sp-btn-sm"
                        disabled={block.isBusy || block.isSaving}
                        onClick={() => editor.resetBlock(block.id)}
                      >
                        {block.isBusy
                          ? t('common.loading', 'Loading…')
                          : t('systemPrompt.fragmentEditor.reset', 'Reset')}
                      </Button>
                    {/if}
                    {#if editor.isCustomBlock(block)}
                      <Button
                        variant="danger"
                        class="sp-btn-sm"
                        onClick={() => editor.removeCustomBlock(block.id)}
                      >
                        {t('common.remove', 'Remove')}
                      </Button>
                    {/if}
                    <Toggle
                      checked={block.enabled}
                      size="sm"
                      ariaLabel={t(
                        'systemPrompt.blockList.toggleAria',
                        'Toggle {id}',
                        { id: block.id },
                      )}
                      onChange={() => editor.toggleBlock(block.id)}
                    />
                  </div>
                </div>

                <div
                  id={`sp-block-body-${block.id}`}
                  hidden={!block.editorExpanded}
                >
                  {#if block.editable}
                    <TextArea
                      ariaLabel={block.id}
                      rows={12}
                      variant="inset"
                      spellcheck="false"
                      value={block.editedContent}
                      onInput={(value) =>
                        editor.handleTextareaInput(block.id, value)}
                    />
                  {:else}
                    <div class="sp-data-block">
                      <div class="sp-data-block-head">
                        <span class="sp-data-block-label"
                          >{editor.dataKindLabel()}</span
                        >
                        {#if block.preview}
                          <button
                            type="button"
                            class="sp-data-toggle"
                            aria-expanded={block.previewExpanded}
                            onclick={() => editor.togglePreview(block.id)}
                          >
                            {block.previewExpanded
                              ? t(
                                  'systemPrompt.blockList.hidePreview',
                                  'Hide preview',
                                )
                              : t(
                                  'systemPrompt.blockList.showPreview',
                                  'Show preview',
                                )}
                          </button>
                        {/if}
                      </div>
                      {#if block.preview && block.previewExpanded}
                        <pre class="sp-data-preview">{block.preview}</pre>
                      {:else if !block.preview}
                        <span class="sp-data-empty">
                          {t(
                            'systemPrompt.blockList.dataEmpty',
                            'No content for the current scope.',
                          )}
                        </span>
                      {/if}
                    </div>
                  {/if}
                </div>
              </li>
            {/each}
          </ul>

          {#if editor.blocks.length === 0}
            <EmptyState
              density="compact"
              description={t(
                'systemPrompt.blockList.empty',
                'No prompt blocks for this scope.',
              )}
            />
          {/if}

          <div class="sp-global-footer">
            <span
              >{t(
                'systemPrompt.editor.autosave',
                'Changes save automatically. The switches control inclusion; opening a block does not change it.',
              )}</span
            >
            <Button
              variant="tertiary"
              class="sp-btn-sm"
              disabled={editor.isBusy}
              onClick={editor.handleManualSaveAll}
            >
              {editor.isBusy
                ? t('common.saving', 'Saving…')
                : t('systemPrompt.fragmentEditor.save', 'Save')}
            </Button>
          </div>
        {/if}
      </div>
      {#if activeTab !== 'edit'}
        <div
          class="sp-reader"
          role="tabpanel"
          id={`sp-content-panel-${activeTab}`}
          aria-labelledby={`sp-content-tab-${activeTab}`}
          tabindex="0"
          aria-busy={scope.isRefreshingPreview}
        >
          <details class="sp-about">
            <summary
              >{t('systemPrompt.preview.about', 'About this preview')}</summary
            >
            <p class="sp-preview-note">
              {t(
                'systemPrompt.preview.baseline',
                'Current Agent configuration. A running Session can also contain pinned context, additional Tools, and conversation results.',
              )}
            </p>
          </details>
          {#if scope.previewError}
            <Banner variant="error"
              >{scope.previewError}
              <Button variant="secondary" onClick={scope.refreshPreview}
                >{t('common.retry', 'Retry')}</Button
              >
            </Banner>
          {:else if scope.isRefreshingPreview || scope.isLoadingData}
            <Banner variant="neutral">{t('common.loading', 'Loading…')}</Banner>
          {:else if !scope.canRefreshPreview()}
            <EmptyState
              description={t(
                'systemPrompt.preview.empty',
                'Select an agent to preview its system prompt.',
              )}
            />
          {:else if activeTab === 'tools'}
            <ToolDefinitionsPanel tools={scope.previewTools} {onToast} />
          {:else}
            <div class="sp-document-toolbar">
              <TabList
                items={formatTabs}
                value={promptFormat}
                appearance="segmented"
                density="compact"
                idPrefix="sp-format"
                ariaLabel={t('systemPrompt.format.label', 'Prompt display')}
                onChange={(value) => (promptFormat = value)}
              />
              <Button
                variant="secondary"
                disabled={!scope.previewText}
                onClick={scope.copyPreview}
                >{t('systemPrompt.preview.copy', 'Copy')}</Button
              >
            </div>
            <div
              class="sp-document"
              role="tabpanel"
              id={`sp-format-panel-${promptFormat}`}
              aria-labelledby={`sp-format-tab-${promptFormat}`}
              tabindex="0"
            >
              {#if !scope.previewText}
                <EmptyState
                  description={t(
                    'systemPrompt.preview.noText',
                    'The current configuration produces an empty System Prompt.',
                  )}
                />
              {:else if promptFormat === 'original'}
                <pre class="sp-preview-pre">{scope.previewText}</pre>
              {:else}
                <MarkdownContent
                  source={scope.previewText}
                  class="sp-document-content"
                />
              {/if}
            </div>
          {/if}
        </div>
      {/if}
    </div>
  </div>

  <div class="sp-sr-only" aria-live="polite" role="status">
    {editor.reorderAnnouncement}
  </div>

  {#if editor.resetConfirmBlockId}
    <ConfirmDialog
      title={t('systemPrompt.fragmentEditor.resetConfirmTitle', 'Reset block')}
      body={editor.resetConfirmBody}
      confirmLabel={t('common.reset', 'Reset')}
      onConfirm={editor.confirmResetBlock}
      onCancel={editor.cancelResetBlock}
    />
  {/if}

  {#if editor.removeConfirmBlockId}
    <ConfirmDialog
      title={t('systemPrompt.blockList.removeConfirmTitle', 'Remove block')}
      body={t(
        'systemPrompt.blockList.removeConfirm',
        'Remove this custom block? This cannot be undone.',
      )}
      confirmLabel={t('common.remove', 'Remove')}
      onConfirm={editor.confirmRemoveCustomBlock}
      onCancel={editor.cancelRemoveCustomBlock}
    />
  {/if}

  {#if editor.resetLayoutConfirmOpen}
    <ConfirmDialog
      title={t(
        'systemPrompt.blockList.resetLayoutConfirmTitle',
        'Reset layout',
      )}
      body={t(
        'systemPrompt.blockList.resetLayoutConfirm',
        'Reset block order and visibility to the default? This cannot be undone.',
      )}
      confirmLabel={t('common.reset', 'Reset')}
      onConfirm={editor.confirmResetLayout}
      onCancel={editor.cancelResetLayout}
    />
  {/if}
</section>
