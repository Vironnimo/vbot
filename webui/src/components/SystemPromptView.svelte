<script>
  import { t, tOr } from '$lib/i18n.js';
  import Dropdown from './Dropdown.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import Button from './ui/Button.svelte';
  import SaveStatus from './ui/SaveStatus.svelte';
  import TabList from './ui/TabList.svelte';
  import Banner from './ui/Banner.svelte';
  import Badge from './ui/Badge.svelte';
  import Toggle from './ui/Toggle.svelte';
  import TextArea from './ui/TextArea.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import SortableList from './ui/SortableList.svelte';
  import ToolDefinitionsPanel from './ToolDefinitionsPanel.svelte';
  import MarkdownContent from './chat/MarkdownContent.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import { onMount, untrack } from 'svelte';
  import { useAutosaveContext } from '$lib/autosave.js';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';
  import { createPromptScope } from './prompt/scope.svelte.js';
  import { createPromptEditor } from './prompt/editor.svelte.js';
  import './prompt/prompt.css';

  const noop = () => {};
  const TAB_IDS = ['prompt', 'tools', 'edit'];
  const AGENT_PREFIX = 'agent:';

  let {
    // The place is `[tab, 'agent:<id>']` for an Agent scope and
    // `[tab, 'default', 'agent:<id>']` for the default scope previewed with
    // that Agent. The Agents editor links to `['edit', 'agent:<id>']`, which
    // shows the default scope when that Agent has no scope of its own. An
    // empty place shows the Prompt tab with the default scope.
    navigation = createStandaloneNavigation(),
    onToast = noop,
    // Bumped when the Agents change, such as a new name.
    agentsRefreshToken = 0,
  } = $props();

  let activeTab = $derived(
    TAB_IDS.includes(navigation.place[0]) ? navigation.place[0] : TAB_IDS[0],
  );
  let promptFormat = $state('document');

  let tabs = $derived([
    { id: 'prompt', label: t('systemPrompt.tabs.prompt') },
    { id: 'tools', label: t('systemPrompt.tabs.tools') },
    { id: 'edit', label: t('systemPrompt.tabs.edit') },
  ]);
  let formatTabs = $derived([
    { id: 'document', label: t('systemPrompt.format.document') },
    {
      id: 'original',
      label: t('systemPrompt.format.original'),
    },
  ]);

  const autosaveContext = useAutosaveContext();
  const scope = createPromptScope({
    get showToast() {
      return showToast;
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
    get blocksScopeKey() {
      return scope.blocksScopeKey;
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

  let lastAgentsRefreshToken = null;
  $effect(() => {
    const token = agentsRefreshToken;
    if (lastAgentsRefreshToken === null) {
      lastAgentsRefreshToken = token;
      return;
    }
    if (token === lastAgentsRefreshToken) return;
    lastAgentsRefreshToken = token;
    void scope.reloadAgents();
  });

  let scopesLoaded = $derived(scope.promptScopes.length > 0);

  // Place -> shown scope and preview Agent, once the scope list has loaded;
  // the tab follows the place directly. What cannot be shown (a scope that
  // no longer exists, an unknown Agent, an empty place) falls back to the
  // default scope and the first Agent, and the place is corrected without a
  // step.
  $effect(() => {
    const place = navigation.place;
    if (!scopesLoaded) return;
    untrack(() => showInPlace(scope.showSelection(selectionFromPlace(place))));
  });

  // Shown scope -> place: a reload that no longer lists the shown scope
  // falls back to the default scope and corrects the entry.
  $effect(() => {
    const shown = {
      scopeKey: scope.selectedScopeKey,
      agentId: scope.selectedAgentId,
    };
    if (!scopesLoaded) return;
    untrack(() => showInPlace(shown));
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

  function selectionFromPlace(place) {
    const [, scopeKey = '', agentSegment = ''] = place;
    if (scopeKey.startsWith(AGENT_PREFIX)) {
      return { scopeKey, agentId: scopeKey.slice(AGENT_PREFIX.length) };
    }
    return {
      scopeKey: 'default',
      agentId: agentSegment.startsWith(AGENT_PREFIX)
        ? agentSegment.slice(AGENT_PREFIX.length)
        : '',
    };
  }

  function placeFor(tab, { scopeKey, agentId }) {
    if (scopeKey !== 'default') return [tab, scopeKey];
    return agentId
      ? [tab, 'default', AGENT_PREFIX + agentId]
      : [tab, 'default'];
  }

  function showInPlace(selection) {
    const place = placeFor(activeTab, selection);
    const current = navigation.place;
    if (
      place.length !== current.length ||
      place.some((segment, index) => segment !== current[index])
    ) {
      navigation.replace(place);
    }
  }

  // Tabs, scopes and preview Agents are steps; the navigator saves pending
  // block edits before any of them is shown.
  function selectTab(tab) {
    return navigation.navigate([tab, ...navigation.place.slice(1)]);
  }

  function selectPreviewAgent(agentId) {
    if (agentId === scope.selectedAgentId) return false;
    return openSelection({ scopeKey: AGENT_PREFIX + agentId, agentId });
  }

  function selectScope(scopeKey) {
    if (scopeKey === scope.selectedScopeKey) return false;
    return openSelection({ scopeKey, agentId: scope.selectedAgentId });
  }

  function openSelection(selection) {
    return navigation.navigate(
      placeFor(activeTab, scope.resolveSelection(selection)),
    );
  }
</script>

<section class="sp-view view active" aria-labelledby="sp-title">
  <div class="sp-layout">
    <div class="sp-top view-frame">
      <header class="sp-header view-header">
        <div class="view-header__intro">
          <h2 id="sp-title" class="sp-title view-header__title">
            {t('systemPrompt.title')}
          </h2>
          <p class="sp-subtitle view-header__subtitle">
            {t('systemPrompt.subtitle')}
          </p>
        </div>
      </header>

      <div class="sp-context-bar">
        <div class="sp-preview-heading-row">
          <span class="sp-preview-heading">
            {t('systemPrompt.preview.heading')}
          </span>
          {#if scope.previewAgentOptions.length > 0}
            <span class="sp-agent-label" id="sp-agent-label">
              {t('systemPrompt.preview.agentLabel')}
            </span>
            <Dropdown
              id="sp-agent-select"
              value={scope.selectedAgentId}
              options={scope.previewAgentOptions}
              ariaLabel={t('systemPrompt.preview.agentLabel')}
              triggerClass="sp-agent-dropdown"
              listClass="sp-agent-dropdown-list"
              onValueChange={selectPreviewAgent}
            />
          {/if}
          {#if scope.previewTokens !== null}
            {#if scope.previewToolTokens}
              <span
                class="sp-token-count"
                use:tooltip={t('systemPrompt.preview.tokenBreakdownHint', {
                  count: scope.previewToolCount ?? 0,
                })}
              >
                {t('systemPrompt.preview.tokenBreakdown', {
                  prompt: scope.previewTokens,
                  tools: scope.previewToolTokens,
                  total: scope.previewTokens + scope.previewToolTokens,
                })}
              </span>
            {:else}
              <span class="sp-token-count">
                {t('systemPrompt.preview.tokenCount', {
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
          >{t('systemPrompt.preview.refresh')}</Button
        >
      </div>
      <div class="view-toolbar view-toolbar--tabs sp-navigation">
        <TabList
          items={tabs}
          value={activeTab}
          idPrefix="sp-content"
          ariaLabel={t('systemPrompt.tabs.label')}
          onChange={selectTab}
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
              {t('systemPrompt.scope.label')}
            </span>
            <Dropdown
              id="sp-scope-select"
              value={scope.selectedScopeKey}
              options={scope.scopeOptions}
              ariaLabel={t('systemPrompt.scope.label')}
              triggerClass="sp-scope-dropdown"
              onValueChange={selectScope}
            />
          </div>
          {#if !scope.isLoadingData}
            <div class="sp-blocklist-toolbar-actions view-toolbar__actions">
              <Button
                variant="secondary"
                class="sp-btn-sm"
                onClick={editor.createCustomBlock}
              >
                {t('systemPrompt.blockList.newBlock')}
              </Button>
              <Button
                variant="secondary"
                class="sp-btn-sm"
                onClick={editor.resetLayout}
              >
                {t('systemPrompt.blockList.resetLayout')}
              </Button>
            </div>
          {/if}
        </div>

        {#if scope.isLoadingData}
          <Banner variant="neutral">
            {t('common.loading')}
          </Banner>
        {:else}
          <details
            class="sp-blocklist-guide"
            aria-labelledby="sp-blocklist-guide-title"
          >
            <summary class="sp-blocklist-guide__intro">
              <span class="sp-blocklist-guide__eyebrow">
                {t('systemPrompt.blockList.guide.label')}
              </span>
              <h3 id="sp-blocklist-guide-title">
                {t('systemPrompt.blockList.guide.title')}
              </h3>
            </summary>
            <div class="sp-blocklist-guide__details">
              <p>
                <strong>
                  {t('systemPrompt.blockList.guide.assemblyLabel')}
                </strong>
                <span>
                  {t('systemPrompt.blockList.guide.assembly')}
                </span>
              </p>
              <p>
                <strong>
                  {t('systemPrompt.blockList.guide.scopeLabel')}
                </strong>
                <span>
                  {t('systemPrompt.blockList.guide.scope')}
                </span>
              </p>
            </div>
          </details>

          <SortableList
            class="sp-blocks"
            items={editor.blocks}
            itemClass={(block) =>
              [
                'sp-block',
                !block.enabled && 'sp-block--off',
                scope.isAgentScope &&
                  editor.isInherited(block) &&
                  'sp-block--inherited',
              ]
                .filter(Boolean)
                .join(' ')}
            getLabel={(block) =>
              tOr(`systemPrompt.blockTitle.${block.id}`, block.id)}
            disabled={editor.isBusy}
            aria-label={t('systemPrompt.tabs.edit')}
            onReorder={editor.reorderBlocks}
          >
            {#snippet item(block)}
              <div class="sp-block-row">
                <div class="sp-block-meta">
                  <strong class="sp-block-title"
                    >{tOr(
                      `systemPrompt.blockTitle.${block.id}`,
                      block.id,
                    )}</strong
                  >
                  <div class="sp-block-id-row">
                    <span class="sp-block-id">{block.id}</span>
                    {#if !block.enabled}<Badge variant="neutral"
                        >{t('systemPrompt.blockList.off')}</Badge
                      >{/if}
                    {#if editor.isCustomBlock(block)}
                      <span
                        class="tooltip-anchor"
                        use:tooltip={t('systemPrompt.blockList.customHint')}
                      >
                        <Badge variant="info">
                          {t('systemPrompt.blockList.customBadge')}
                        </Badge>
                      </span>
                    {/if}
                    {#if block.kind === 'data'}
                      <span
                        class="tooltip-anchor"
                        use:tooltip={t('systemPrompt.blockList.dataHint')}
                      >
                        <Badge variant="neutral">
                          {t('systemPrompt.blockList.dataBadge')}
                        </Badge>
                      </span>
                    {/if}
                    {#if scope.isAgentScope && editor.isInherited(block)}
                      <span
                        class="tooltip-anchor"
                        use:tooltip={t('systemPrompt.blockList.inheritedHint')}
                      >
                        <Badge variant="neutral">
                          {t('systemPrompt.blockList.inheritedBadge')}
                        </Badge>
                      </span>
                    {:else if block.editable && block.isModified}
                      <span
                        class="tooltip-anchor"
                        use:tooltip={t(
                          'systemPrompt.fragmentEditor.modifiedHint',
                        )}
                      >
                        <Badge variant="info">
                          {t('systemPrompt.fragmentEditor.modifiedIndicator')}
                        </Badge>
                      </span>
                    {/if}
                    {#if block.editable && block.isDirty}
                      <span
                        class="tooltip-anchor"
                        use:tooltip={t('systemPrompt.fragmentEditor.dirtyHint')}
                      >
                        <Badge variant="warn">
                          {t('systemPrompt.fragmentEditor.dirtyIndicator')}
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
                      ? t('systemPrompt.blockList.close')
                      : block.editable
                        ? t('systemPrompt.blockList.edit')
                        : t('systemPrompt.blockList.inspect')}
                  </Button>
                  {#if block.editable && !(scope.isAgentScope && editor.isInherited(block) && !block.isModified)}
                    <Button
                      variant="secondary"
                      class="sp-btn-sm"
                      disabled={block.isBusy || block.isSaving}
                      onClick={() => editor.resetBlock(block.id)}
                    >
                      {block.isBusy
                        ? t('common.loading')
                        : t('systemPrompt.fragmentEditor.reset')}
                    </Button>
                  {/if}
                  {#if editor.isCustomBlock(block)}
                    <Button
                      variant="danger"
                      class="sp-btn-sm"
                      onClick={() => editor.removeCustomBlock(block.id)}
                    >
                      {t('common.remove')}
                    </Button>
                  {/if}
                  <Toggle
                    checked={block.enabled}
                    size="sm"
                    ariaLabel={t('systemPrompt.blockList.toggleAria', {
                      id: block.id,
                    })}
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
                            ? t('systemPrompt.blockList.hidePreview')
                            : t('systemPrompt.blockList.showPreview')}
                        </button>
                      {/if}
                    </div>
                    {#if block.preview && block.previewExpanded}
                      <pre class="sp-data-preview">{block.preview}</pre>
                    {:else if !block.preview}
                      <span class="sp-data-empty">
                        {t('systemPrompt.blockList.dataEmpty')}
                      </span>
                    {/if}
                  </div>
                {/if}
              </div>
            {/snippet}
          </SortableList>

          {#if editor.blocks.length === 0}
            <EmptyState
              density="compact"
              description={t('systemPrompt.blockList.empty')}
            />
          {/if}

          <div class="sp-global-footer">
            <span>{t('systemPrompt.editor.autosave')}</span>
            <SaveStatus
              saving={editor.isBusy}
              pending={editor.hasUnsavedEdits}
              onClick={editor.handleManualSaveAll}
            />
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
            <summary>{t('systemPrompt.preview.about')}</summary>
            <p class="sp-preview-note">
              {t('systemPrompt.preview.baseline')}
            </p>
          </details>
          {#if scope.previewError}
            <Banner variant="error"
              >{scope.previewError}
              <Button variant="secondary" onClick={scope.refreshPreview}
                >{t('common.retry')}</Button
              >
            </Banner>
          {:else if scope.isRefreshingPreview || scope.isLoadingData}
            <Banner variant="neutral">{t('common.loading')}</Banner>
          {:else if !scope.canRefreshPreview()}
            <EmptyState description={t('systemPrompt.preview.empty')} />
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
                ariaLabel={t('systemPrompt.format.label')}
                onChange={(value) => (promptFormat = value)}
              />
              <Button
                variant="secondary"
                disabled={!scope.previewText}
                onClick={scope.copyPreview}
                >{t('systemPrompt.preview.copy')}</Button
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
                <EmptyState description={t('systemPrompt.preview.noText')} />
              {:else if promptFormat === 'original'}
                <pre class="sp-preview-pre">{scope.previewText}</pre>
              {:else}
                <MarkdownContent
                  source={scope.previewText}
                  class="msg-markdown md-document sp-document-content"
                />
              {/if}
            </div>
          {/if}
        </div>
      {/if}
    </div>
  </div>

  {#if editor.resetConfirmBlockId}
    <ConfirmDialog
      title={t('systemPrompt.fragmentEditor.resetConfirmTitle')}
      body={editor.resetConfirmBody}
      confirmLabel={t('common.reset')}
      onConfirm={editor.confirmResetBlock}
      onCancel={editor.cancelResetBlock}
    />
  {/if}

  {#if editor.removeConfirmBlockId}
    <ConfirmDialog
      title={t('systemPrompt.blockList.removeConfirmTitle')}
      body={t('systemPrompt.blockList.removeConfirm')}
      confirmLabel={t('common.remove')}
      onConfirm={editor.confirmRemoveCustomBlock}
      onCancel={editor.cancelRemoveCustomBlock}
    />
  {/if}

  {#if editor.resetLayoutConfirmOpen}
    <ConfirmDialog
      title={t('systemPrompt.blockList.resetLayoutConfirmTitle')}
      body={t('systemPrompt.blockList.resetLayoutConfirm')}
      confirmLabel={t('common.reset')}
      onConfirm={editor.confirmResetLayout}
      onCancel={editor.cancelResetLayout}
    />
  {/if}
</section>
