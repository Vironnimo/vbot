<script>
  import { onMount, tick } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';

  import Dropdown from './Dropdown.svelte';
  import MarkdownContent from './chat/MarkdownContent.svelte';
  import ToolDefinitionsPanel from './ToolDefinitionsPanel.svelte';
  import TabList from './ui/TabList.svelte';
  import Badge from './ui/Badge.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import TextArea from './ui/TextArea.svelte';
  import Toggle from './ui/Toggle.svelte';
  import {
    buildAgentTargetDropdownOptions,
    projectIdsFromList,
    projectTeamEntry,
  } from '$lib/agentTargetOptions.js';
  import {
    createPromptBlock,
    listAgents,
    listProjects,
    listPrompts,
    previewPrompt,
    removePromptBlock,
    resetPromptBlock,
    resetPromptLayout,
    setPromptLayout,
    showProject,
    updatePromptBlock,
  } from '$lib/api.js';
  import { useAutosaveContext } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  const AUTO_SAVE_DEBOUNCE_MS = 800;
  const PREVIEW_REFRESH_DEBOUNCE_MS = 100;
  const MAX_PROMPT_FLUSH_PASSES = 10;
  // The custom-block slug rule mirrors the backend agent-id rule (validated again
  // at the RPC edge and the store): letters/digits plus `-`/`_`, alphanumeric
  // start, bounded length. This is a UX pre-check; the server stays authoritative.
  const SLUG_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*$/u;
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

  // Blocks come from `prompt.list` in layout order. Each block is keyed by its
  // stable `id` (never an array index), so autosave timers and DnD identity
  // survive a reorder. Editable text blocks carry `editedContent`/`isDirty`
  // live-edit state; data blocks (`kind === 'data'`) have none.
  let blocks = $state([]);
  let agents = $state([]);
  let promptScopes = $state([]);
  let selectedScopeKey = $state('default');
  let selectedAgentId = $state('');
  let activeTab = $state('prompt');
  let promptFormat = $state('document');
  let previewTools = $state([]);
  let previewError = $state('');
  let previewText = $state('');
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
  let previewTokens = $state(null);
  let previewToolTokens = $state(null);
  let previewToolCount = $state(null);
  let isLoadingData = $state(true);
  let isRefreshingPreview = $state(false);
  let scopeLoadRequestId = 0;
  let previewRequestId = 0;
  let previewRefreshTimer = null;
  let reorderAnnouncement = $state('');

  // Autosave timers keyed by block id (a reorder must not reassign a timer to a
  // different block, which an index key would do). A plain null-proto object,
  // not reactive state — it only holds setTimeout handles.
  const autoSaveTimers = Object.create(null);
  const blockSavePromises = new SvelteMap();
  const autosaveContext = useAutosaveContext();
  const promptAutosaveParticipant = {
    flush: flushPendingPromptAutosaves,
    hasPending: () =>
      blockSavePromises.size > 0 ||
      Object.keys(autoSaveTimers).length > 0 ||
      blocks.some((block) => block.editable && block.isDirty),
  };
  const unregisterPromptAutosave = autosaveContext.register(
    promptAutosaveParticipant,
  );
  // The block id whose reorder handle should regain focus after a keyboard move,
  // so the focus follows the moving row across the DOM re-render.
  let pendingFocusBlockId = null;
  // The drag source index for a native HTML5 drag (mirrored from dataTransfer so
  // a same-document drop can reorder without parsing the payload defensively).
  let dragSourceIndex = null;

  // Pending confirmations (null = the dialog is closed). Each destructive action
  // opens its own dialog and runs only once the user confirms. `resetBlock` and
  // `removeCustomBlock` remember the target block id; `resetLayout` takes none.
  let resetConfirmBlockId = $state(null);
  let removeConfirmBlockId = $state(null);
  let resetLayoutConfirmOpen = $state(false);

  // The reset-block confirm body speaks of the Default scope's built-in default
  // or an Agent scope's inherited Default content, matching the scope in effect.
  let resetConfirmBody = $derived(
    isAgentScope
      ? t(
          'systemPrompt.fragmentEditor.resetAgentConfirm',
          'Reset this Agent block to the current Default content? This cannot be undone.',
        )
      : t(
          'systemPrompt.fragmentEditor.resetConfirm',
          'Reset this block to its default? This cannot be undone.',
        ),
  );

  // Project teams power the project-agent options in the preview agent picker.
  // Identity agents come from `agent.list`; project agents are scanned lazily
  // (one `project.show` per project) and cached, so the N+1 scan never runs on
  // every render. A scan failure is non-fatal — identity agents still preview.
  let projectTeams = $state([]);
  let projectTeamsLoaded = false;
  let projectTeamsRequestId = 0;

  let isBusy = $derived(blocks.some((block) => block.isSaving || block.isBusy));
  let selectedScope = $derived(
    promptScopes.find((scope) => scope.key === selectedScopeKey) ??
      defaultPromptScope(),
  );
  let isAgentScope = $derived(selectedScope.type === 'agent');
  let scopeOptions = $derived(
    promptScopes.map((scope) => ({ value: scope.key, label: scope.label })),
  );
  // Identity agents (bare-id values, unchanged) plus project agents addressed as
  // `agent@projekt`. A project option's value IS the address, which the backend
  // `prompt.preview` accepts directly as its `agent_id`. Group headers appear
  // only when project agents exist, so an identity-only install is unchanged.
  let previewAgentOptions = $derived(
    buildAgentTargetDropdownOptions(agents, projectTeams, {
      identityGroupLabel: t(
        'systemPrompt.preview.agentGroup.identity',
        'Identity agents',
      ),
      projectGroupLabel: t(
        'systemPrompt.preview.agentGroup.project',
        'Project agents',
      ),
    }),
  );

  onMount(() => {
    loadData();
    loadProjectTeams();
    return () => {
      scopeLoadRequestId += 1;
      previewRequestId += 1;
      unregisterPromptAutosave();
      clearAutoSaveTimers();
      clearPreviewRefreshTimer();
    };
  });

  // Apply a scope deep-link once (per request id) and only after scopes have
  // loaded, so the target agent scope actually exists in `promptScopes`. An
  // absent target scope falls back silently to the default scope.
  $effect(() => {
    if (targetScopeRequestId === handledScopeRequestId || isLoadingData) {
      return;
    }
    // Wait until the initial scope list is available before consuming the request.
    if (promptScopes.length === 0) {
      return;
    }
    handledScopeRequestId = targetScopeRequestId;
    if (!targetScopeAgentId) {
      return;
    }
    activeTab = 'edit';
    const targetKey = `agent:${targetScopeAgentId}`;
    const nextKey = promptScopes.some((scope) => scope.key === targetKey)
      ? targetKey
      : 'default';
    void selectScope(nextKey);
  });

  // Auto-load the preview whenever the settled preview target changes — the
  // initial data load, an agent pick, or a scope switch — so the user never has
  // to press Refresh to see the current scope's prompt. Gated on `isLoadingData`
  // so it fires once per settled target, not while blocks/scopes are still
  // loading; `refreshPreview` no-ops when there is no valid target.
  $effect(() => {
    if (isLoadingData) {
      return;
    }
    if (!canRefreshPreview()) {
      return;
    }
    void refreshPreview();
  });

  async function loadData() {
    isLoadingData = true;

    try {
      const [agentsResult, promptsResult] = await Promise.all([
        listAgents(),
        listPrompts(),
      ]);

      agents = Array.isArray(agentsResult?.agents) ? agentsResult.agents : [];
      selectedAgentId = resolvePreviewAgentId(selectedAgentId);
      promptScopes = normalizePromptScopes(promptsResult?.scopes, agents);
      selectedScopeKey = resolveScopeKey(selectedScopeKey);
      applyBlocks(promptsResult?.blocks);
    } catch {
      showToast(
        t('systemPrompt.error.loadFailed', 'Failed to load prompt data'),
        'error',
      );
    } finally {
      isLoadingData = false;
    }
  }

  // Lazily scan project teams so the preview picker can offer project agents as
  // `agent@projekt`. Kicked off on mount; a failure is non-fatal (identity
  // agents still preview) and leaves the cache unset so a reload can retry.
  async function loadProjectTeams() {
    if (projectTeamsLoaded) {
      return;
    }

    const requestId = projectTeamsRequestId + 1;
    projectTeamsRequestId = requestId;

    try {
      const listResult = await listProjects();
      if (requestId !== projectTeamsRequestId) {
        return;
      }

      const projectIds = projectIdsFromList(listResult);
      const showResults = await Promise.all(
        projectIds.map((projectId) =>
          showProject(projectId)
            .then((showResult) => projectTeamEntry(projectId, showResult))
            .catch(() => null),
        ),
      );
      if (requestId !== projectTeamsRequestId) {
        return;
      }

      projectTeams = showResults.filter((entry) => entry !== null);
      projectTeamsLoaded = true;
    } catch {
      // Identity agents remain available; leave projectTeams empty and allow a
      // retry on the next mount (projectTeamsLoaded stays false).
      if (requestId === projectTeamsRequestId) {
        projectTeams = [];
      }
    }
  }

  function selectPreviewAgent(agentId) {
    if (agentId === selectedAgentId) return;
    return autosaveContext.requestTransition(async () => {
      selectedAgentId = agentId;
      const scopeKey = promptScopes.some(
        (scope) => scope.key === `agent:${agentId}`,
      )
        ? `agent:${agentId}`
        : 'default';
      await applyScopeSelection(scopeKey);
    });
  }

  function selectScope(nextScopeKey) {
    if (nextScopeKey === selectedScopeKey) {
      return false;
    }
    return autosaveContext.requestTransition(() =>
      applyScopeSelection(nextScopeKey),
    );
  }

  async function applyScopeSelection(nextScopeKey) {
    selectedScopeKey = nextScopeKey;
    const scope = promptScopes.find((entry) => entry.key === nextScopeKey);
    if (scope?.type === 'agent') selectedAgentId = scope.agent_id;
    previewRequestId += 1;
    previewTools = [];
    previewError = '';
    previewText = '';
    previewTokens = null;
    previewToolTokens = null;
    previewToolCount = null;
    reorderAnnouncement = '';
    clearAutoSaveTimers();
    await loadBlocksForScope(nextScopeKey);
    return true;
  }

  async function loadBlocksForScope(scopeKey) {
    const requestId = scopeLoadRequestId + 1;
    scopeLoadRequestId = requestId;
    isLoadingData = true;

    try {
      const promptsResult = await listPrompts(promptListParams(scopeKey));
      if (requestId !== scopeLoadRequestId) {
        return false;
      }
      promptScopes = normalizePromptScopes(promptsResult?.scopes, agents);
      selectedScopeKey = resolveScopeKey(scopeKey);
      applyBlocks(promptsResult?.blocks);
      return true;
    } catch {
      if (requestId !== scopeLoadRequestId) {
        return false;
      }
      showToast(
        t('systemPrompt.error.loadFailed', 'Failed to load prompt data'),
        'error',
      );
      return false;
    } finally {
      if (requestId === scopeLoadRequestId) {
        isLoadingData = false;
      }
    }
  }

  // Map the server block metadata into the local row model. Editable text blocks
  // get the live-edit fields; non-editable data blocks get a `preview` of their
  // current text. The id is the stable identity used everywhere.
  function applyBlocks(rawBlocks) {
    const source = Array.isArray(rawBlocks) ? rawBlocks : [];
    const previousById = new Map(blocks.map((block) => [block.id, block]));
    clearAutoSaveTimers();

    blocks = source.map((raw) => {
      const editable = raw.editable === true && raw.kind === 'text';
      const content = typeof raw.text === 'string' ? raw.text : '';
      const previous = previousById.get(raw.id);
      // Preserve an in-flight unsaved edit across a re-list (e.g. after a
      // toggle/reorder of another block) so the user's typing is not lost.
      const keepDraft =
        editable && previous?.isDirty && previous.editedContent !== content;
      return {
        id: raw.id,
        owner: typeof raw.owner === 'string' ? raw.owner : 'always',
        kind: raw.kind === 'data' ? 'data' : 'text',
        source: typeof raw.source === 'string' ? raw.source : 'core',
        editable,
        enabled: raw.enabled !== false,
        content,
        editedContent: keepDraft ? previous.editedContent : content,
        isDirty: keepDraft,
        isModified: editable ? raw.is_modified === true : false,
        inheritance:
          typeof raw.inheritance === 'string' ? raw.inheritance : null,
        preview: !editable ? content : '',
        previewExpanded: previous?.previewExpanded ?? false,
        editorExpanded: previous?.editorExpanded ?? false,
        isSaving: false,
        isBusy: false,
      };
    });
  }

  function normalizePromptScopes(rawScopes, currentAgents) {
    const scopes = Array.isArray(rawScopes)
      ? rawScopes.map(normalizePromptScope).filter(Boolean)
      : [];

    const hasDefaultScope = scopes.some((scope) => scope.key === 'default');
    const availableScopes = hasDefaultScope
      ? scopes
      : [defaultPromptScope(), ...scopes];

    if (availableScopes.length > 1 || scopes.length > 0) {
      return availableScopes;
    }

    return [
      defaultPromptScope(),
      ...currentAgents
        .filter((agent) => agent.custom_system_prompt_enabled)
        .map((agent) =>
          normalizePromptScope({
            type: 'agent',
            agent_id: agent.id,
            label: agent.name || agent.id,
          }),
        ),
    ];
  }

  function normalizePromptScope(scope) {
    if (!scope || typeof scope !== 'object') {
      return null;
    }

    if (scope.type === 'agent' && scope.agent_id) {
      return {
        key: `agent:${scope.agent_id}`,
        type: 'agent',
        agent_id: scope.agent_id,
        label: scope.label || scope.agent_id,
      };
    }

    if (!scope.type || scope.type === 'default') {
      return defaultPromptScope();
    }

    return null;
  }

  function defaultPromptScope() {
    return {
      key: 'default',
      type: 'default',
      label: t('systemPrompt.scope.default', 'Default'),
    };
  }

  function resolveScopeKey(scopeKey) {
    if (promptScopes.some((scope) => scope.key === scopeKey)) {
      return scopeKey;
    }

    return 'default';
  }

  function resolvePreviewAgentId(agentId) {
    if (agents.some((agent) => agent.id === agentId)) {
      return agentId;
    }

    return agents[0]?.id ?? '';
  }

  function promptListParams(scopeKey) {
    const scope = scopePayloadForKey(scopeKey);
    return scope ? { scope } : {};
  }

  function scopePayloadForKey(scopeKey) {
    if (!scopeKey || scopeKey === 'default') {
      return null;
    }

    const agentId = scopeKey.replace(/^agent:/u, '');
    return { type: 'agent', agent_id: agentId };
  }

  function selectedScopePayload() {
    return scopePayloadForKey(selectedScopeKey);
  }

  function scopedParams(baseParams = {}) {
    const scope = selectedScopePayload();
    return scope ? { ...baseParams, scope } : baseParams;
  }

  function previewParams() {
    const scope = selectedScopePayload();
    if (scope?.type === 'agent') {
      return { agent_id: scope.agent_id, scope };
    }

    if (!selectedAgentId) {
      return null;
    }

    return { agent_id: selectedAgentId };
  }

  function canRefreshPreview() {
    return Boolean(previewParams());
  }

  // -- Owner / inheritance labels ------------------------------------------

  // The owner is gate 2 of the three-gate prompt filter: a block renders only
  // while its owner condition holds. This turns the internal owner token into a
  // plain sentence explaining that render condition.
  function ownerHint(owner) {
    if (owner.startsWith('tool:')) {
      return t(
        'systemPrompt.blockList.ownerHint.tool',
        'Requires the {name} Tool to be available.',
        { name: owner.slice('tool:'.length) },
      );
    }
    if (owner.startsWith('extension:')) {
      return t(
        'systemPrompt.blockList.ownerHint.extension',
        'Requires the {name} Extension to be active.',
        { name: owner.slice('extension:'.length) },
      );
    }
    if (owner === 'memory') {
      return t(
        'systemPrompt.blockList.ownerHint.memory',
        'Requires Memory in the System Prompt to be enabled.',
      );
    }
    if (owner === 'channel') {
      return t(
        'systemPrompt.blockList.ownerHint.channel',
        'Requires an enabled Channel for this Agent.',
      );
    }
    return t(
      'systemPrompt.blockList.ownerHint.always',
      'Included when enabled and non-empty.',
    );
  }

  function dataKindLabel() {
    return t(
      'systemPrompt.blockList.dataLabel',
      'Generated content (read-only)',
    );
  }

  function isCustomBlock(block) {
    return block.source === 'user';
  }

  // An inherited block shows the greyed default + "inherited" badge in an agent
  // scope (T5). Inheritance is a text-cascade concept, so it applies only to
  // editable blocks — a data block has no override to inherit or create.
  function isInherited(block) {
    return block.editable && block.inheritance === 'owner_default';
  }

  // -- Edit + autosave ------------------------------------------------------

  function blockIndexById(blockId) {
    return blocks.findIndex((block) => block.id === blockId);
  }

  function handleTextareaInput(blockId, nextContent) {
    const index = blockIndexById(blockId);
    if (index === -1) {
      return;
    }
    blocks[index].editedContent = nextContent;
    blocks[index].isDirty = nextContent !== blocks[index].content;

    clearAutoSaveTimer(blockId);
    if (blocks[index].isDirty) {
      scheduleAutoSaveTimer(blockId);
    }
  }

  function scheduleAutoSaveTimer(blockId) {
    if (autoSaveTimers[blockId]) {
      return;
    }
    autoSaveTimers[blockId] = setTimeout(() => {
      delete autoSaveTimers[blockId];
      void saveBlock(blockId);
    }, AUTO_SAVE_DEBOUNCE_MS);
  }

  function clearAutoSaveTimer(blockId) {
    const timer = autoSaveTimers[blockId];
    if (timer) {
      clearTimeout(timer);
      delete autoSaveTimers[blockId];
    }
  }

  function clearAutoSaveTimers() {
    for (const blockId of Object.keys(autoSaveTimers)) {
      clearTimeout(autoSaveTimers[blockId]);
      delete autoSaveTimers[blockId];
    }
  }

  async function flushPendingPromptAutosaves() {
    clearAutoSaveTimers();

    for (let pass = 0; pass < MAX_PROMPT_FLUSH_PASSES; pass += 1) {
      const activeResults = await Promise.all(blockSavePromises.values());
      if (!activeResults.every(Boolean)) {
        return false;
      }

      const dirtyIds = blocks
        .filter((block) => block.editable && block.isDirty)
        .map((block) => block.id);
      if (dirtyIds.length === 0) {
        return true;
      }

      const results = await Promise.all(
        dirtyIds.map((blockId) =>
          saveBlock(blockId, { showSuccessToast: false }),
        ),
      );
      if (!results.every(Boolean)) {
        return false;
      }
    }

    return false;
  }

  function saveBlock(blockId, options = {}) {
    const activeSave = blockSavePromises.get(blockId);
    if (activeSave) {
      return activeSave;
    }

    const operation = persistBlock(blockId, options);
    blockSavePromises.set(blockId, operation);
    void operation.finally(() => {
      if (blockSavePromises.get(blockId) === operation) {
        blockSavePromises.delete(blockId);
      }
    });
    return operation;
  }

  async function persistBlock(blockId, options = {}) {
    const index = blockIndexById(blockId);
    if (index === -1) {
      return false;
    }
    const block = blocks[index];
    const showSuccessToast = options.showSuccessToast ?? true;

    if (!block.editable || !block.isDirty || block.isSaving || block.isBusy) {
      return false;
    }

    const draftContent = block.editedContent;
    blocks[index].isSaving = true;

    try {
      const result = await updatePromptBlock({
        id: block.id,
        content: draftContent,
        ...scopedParams(),
      });

      const liveIndex = blockIndexById(blockId);
      if (liveIndex === -1) {
        return true;
      }
      const nextSaved =
        typeof result.text === 'string' ? result.text : draftContent;
      blocks[liveIndex].content = nextSaved;
      if (blocks[liveIndex].editedContent === draftContent) {
        blocks[liveIndex].editedContent = nextSaved;
        blocks[liveIndex].isDirty = false;
      } else {
        blocks[liveIndex].isDirty =
          blocks[liveIndex].editedContent !== blocks[liveIndex].content;
      }
      blocks[liveIndex].isModified = result.is_modified === true;
      if (typeof result.inheritance === 'string') {
        blocks[liveIndex].inheritance = result.inheritance;
      }
      if (showSuccessToast) {
        showToast(t('common.saved', 'Saved'), 'success');
      }
      schedulePreviewRefresh();
      return true;
    } catch {
      showToast(t('systemPrompt.error.saveFailed', 'Failed to save'), 'error');
      return false;
    } finally {
      const liveIndex = blockIndexById(blockId);
      if (liveIndex !== -1) {
        blocks[liveIndex].isSaving = false;
      }
    }
  }

  async function handleManualSaveAll() {
    if (isBusy) {
      return;
    }

    const dirtyIds = blocks
      .filter((block) => block.editable && block.isDirty)
      .map((block) => block.id);

    if (dirtyIds.length === 0) {
      showToast(t('common.alreadySaved', 'Already saved'), 'success');
      return;
    }

    for (const blockId of dirtyIds) {
      clearAutoSaveTimer(blockId);
    }

    const results = await Promise.all(
      dirtyIds.map((blockId) =>
        saveBlock(blockId, { showSuccessToast: false }),
      ),
    );

    if (results.every(Boolean)) {
      showToast(t('common.saved', 'Saved'), 'success');
    }
  }

  function resetBlock(blockId) {
    if (blockIndexById(blockId) === -1) {
      return;
    }
    resetConfirmBlockId = blockId;
  }

  function cancelResetBlock() {
    resetConfirmBlockId = null;
  }

  async function confirmResetBlock() {
    const blockId = resetConfirmBlockId;
    resetConfirmBlockId = null;
    const index = blockIndexById(blockId);
    if (index === -1) {
      return;
    }
    const block = blocks[index];

    clearAutoSaveTimer(blockId);
    blocks[index].isBusy = true;

    try {
      const result = await resetPromptBlock(scopedParams({ id: block.id }));
      const liveIndex = blockIndexById(blockId);
      if (liveIndex === -1) {
        return;
      }
      const restored = typeof result.text === 'string' ? result.text : '';
      blocks[liveIndex].content = restored;
      blocks[liveIndex].editedContent = restored;
      blocks[liveIndex].isDirty = false;
      blocks[liveIndex].isModified = result.is_modified === true;
      if (typeof result.inheritance === 'string') {
        blocks[liveIndex].inheritance = result.inheritance;
      }
      schedulePreviewRefresh();
    } catch {
      showToast(
        t('systemPrompt.error.resetFailed', 'Failed to reset'),
        'error',
      );
    } finally {
      const liveIndex = blockIndexById(blockId);
      if (liveIndex !== -1) {
        blocks[liveIndex].isBusy = false;
      }
    }
  }

  // -- Toggle + layout persistence -----------------------------------------

  // Build the `[{id, enabled, source}]` layout payload from the current row order
  // and send it to `prompt.set_layout`, which persists immediately (T6).
  async function persistLayout() {
    try {
      await setPromptLayout(
        scopedParams({
          layout: blocks.map((block) => ({
            id: block.id,
            enabled: block.enabled,
            source: block.source,
          })),
        }),
      );
      schedulePreviewRefresh();
    } catch {
      showToast(
        t('systemPrompt.error.layoutFailed', 'Failed to save layout'),
        'error',
      );
      // Re-sync from the server so the on-screen order/toggle matches what is
      // actually persisted after a failed write.
      await loadBlocksForScope(selectedScopeKey);
    }
  }

  async function toggleBlock(blockId) {
    const index = blockIndexById(blockId);
    if (index === -1) {
      return;
    }
    blocks[index].enabled = !blocks[index].enabled;
    await persistLayout();
  }

  function togglePreview(blockId) {
    const index = blockIndexById(blockId);
    if (index !== -1) {
      blocks[index].previewExpanded = !blocks[index].previewExpanded;
    }
  }

  // -- Drag-and-drop reorder (native HTML5) --------------------------------

  function handleDragStart(index, event) {
    dragSourceIndex = index;
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = 'move';
      // A payload is required for a valid drag in some browsers; the index is
      // also mirrored in `dragSourceIndex` for the same-document drop path.
      event.dataTransfer.setData('text/plain', String(index));
    }
  }

  function handleDragOver(index, event) {
    if (dragSourceIndex === null) {
      return;
    }
    // preventDefault marks this row as a valid drop target.
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'move';
    }
  }

  async function handleDrop(index, event) {
    event.preventDefault();
    const from = dragSourceIndex;
    dragSourceIndex = null;
    if (from === null || from === index) {
      return;
    }
    moveBlock(from, index);
    await persistLayout();
  }

  function handleDragEnd() {
    dragSourceIndex = null;
  }

  // -- Keyboard reorder (accessibility, T2) --------------------------------

  async function handleHandleKeydown(index, event) {
    let target;
    if (event.key === 'ArrowUp') {
      target = index - 1;
    } else if (event.key === 'ArrowDown') {
      target = index + 1;
    } else {
      return;
    }

    event.preventDefault();
    if (target < 0 || target >= blocks.length) {
      return;
    }

    const movedId = blocks[index].id;
    moveBlock(index, target);
    pendingFocusBlockId = movedId;
    announceReorder(target);
    await persistLayout();
    await tick();
    focusPendingHandle();
  }

  function moveBlock(from, to) {
    const next = [...blocks];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    blocks = next;
  }

  function announceReorder(position) {
    reorderAnnouncement = t(
      'systemPrompt.blockList.reorderAnnouncement',
      'Moved to position {position} of {total}',
      { position: position + 1, total: blocks.length },
    );
  }

  function focusPendingHandle() {
    if (!pendingFocusBlockId) {
      return;
    }
    const handle = document.querySelector(
      `[data-block-handle="${cssEscape(pendingFocusBlockId)}"]`,
    );
    pendingFocusBlockId = null;
    if (handle instanceof HTMLElement) {
      handle.focus();
    }
  }

  function cssEscape(value) {
    if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
      return CSS.escape(value);
    }
    return value.replace(/["\\]/gu, '\\$&');
  }

  // -- Custom block create / remove (T1) -----------------------------------

  async function createCustomBlock() {
    const slug = window.prompt(
      t(
        'systemPrompt.blockList.newBlockPrompt',
        'Name for the new block (letters, digits, “-” or “_”):',
      ),
    );
    if (slug === null) {
      return;
    }
    const trimmed = slug.trim();
    if (!trimmed) {
      return;
    }
    if (!SLUG_PATTERN.test(trimmed)) {
      showToast(
        t(
          'systemPrompt.blockList.invalidSlug',
          'Invalid name — use letters, digits, “-” or “_”, starting with a letter or digit.',
        ),
        'error',
      );
      return;
    }

    try {
      await createPromptBlock(scopedParams({ slug: trimmed }));
      await loadBlocksForScope(selectedScopeKey);
      const created = blocks.find((block) => block.id === `user:${trimmed}`);
      if (created) {
        created.editorExpanded = true;
        await tick();
        document
          .getElementById(`sp-block-body-${created.id}`)
          ?.querySelector('textarea')
          ?.focus();
      }
      schedulePreviewRefresh();
    } catch {
      showToast(
        t(
          'systemPrompt.blockList.createFailed',
          'Failed to create block. The slug may be invalid or already used.',
        ),
        'error',
      );
    }
  }

  function removeCustomBlock(blockId) {
    removeConfirmBlockId = blockId;
  }

  function cancelRemoveCustomBlock() {
    removeConfirmBlockId = null;
  }

  async function confirmRemoveCustomBlock() {
    const blockId = removeConfirmBlockId;
    removeConfirmBlockId = null;
    if (!blockId) {
      return;
    }

    clearAutoSaveTimer(blockId);
    try {
      await removePromptBlock(scopedParams({ id: blockId }));
      await loadBlocksForScope(selectedScopeKey);
      schedulePreviewRefresh();
    } catch {
      showToast(
        t('systemPrompt.blockList.removeFailed', 'Failed to remove block'),
        'error',
      );
    }
  }

  function resetLayout() {
    resetLayoutConfirmOpen = true;
  }

  function cancelResetLayout() {
    resetLayoutConfirmOpen = false;
  }

  async function confirmResetLayout() {
    resetLayoutConfirmOpen = false;

    try {
      await resetPromptLayout(scopedParams());
      await loadBlocksForScope(selectedScopeKey);
      schedulePreviewRefresh();
    } catch {
      showToast(
        t('systemPrompt.error.layoutFailed', 'Failed to save layout'),
        'error',
      );
    }
  }

  // -- Preview --------------------------------------------------------------

  function schedulePreviewRefresh() {
    clearPreviewRefreshTimer();
    previewRefreshTimer = setTimeout(() => {
      previewRefreshTimer = null;
      void refreshPreview();
    }, PREVIEW_REFRESH_DEBOUNCE_MS);
  }

  function clearPreviewRefreshTimer() {
    if (previewRefreshTimer !== null) {
      clearTimeout(previewRefreshTimer);
      previewRefreshTimer = null;
    }
  }

  async function refreshPreview() {
    const params = previewParams();
    if (!params) {
      return;
    }

    const requestId = previewRequestId + 1;
    previewRequestId = requestId;
    isRefreshingPreview = true;
    previewError = '';
    previewText = '';
    previewTools = [];
    previewTokens = null;
    previewToolTokens = null;
    previewToolCount = null;

    try {
      const result = await previewPrompt({ ...params, include_tools: true });
      if (requestId !== previewRequestId) {
        return;
      }
      previewTools = result.tools ?? [];
      previewText = result.text ?? '';
      previewTokens = result.tokens ?? null;
      previewToolTokens = result.tool_tokens ?? null;
      previewToolCount = result.tool_count ?? null;
    } catch {
      if (requestId !== previewRequestId) {
        return;
      }
      previewError = t(
        'systemPrompt.error.previewFailed',
        'Failed to load preview',
      );
    } finally {
      if (requestId === previewRequestId) {
        isRefreshingPreview = false;
      }
    }
  }

  async function copyPreview() {
    if (!previewText) {
      return;
    }

    try {
      await navigator.clipboard.writeText(previewText);
      showToast(t('common.copied', 'Copied'), 'success');
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
          {#if previewAgentOptions.length > 0}
            <span class="sp-agent-label" id="sp-agent-label">
              {t('systemPrompt.preview.agentLabel', 'Agent')}
            </span>
            <Dropdown
              id="sp-agent-select"
              value={selectedAgentId}
              options={previewAgentOptions}
              ariaLabel={t('systemPrompt.preview.agentLabel', 'Agent')}
              triggerClass="sp-agent-dropdown"
              listClass="sp-agent-dropdown-list"
              onValueChange={selectPreviewAgent}
            />
          {/if}
          {#if previewTokens !== null}
            {#if previewToolTokens}
              <span
                class="sp-token-count"
                use:tooltip={t(
                  'systemPrompt.preview.tokenBreakdownHint',
                  'Estimated. Tools = the {count} tool definitions sent to the provider with every request alongside the system prompt.',
                  { count: previewToolCount ?? 0 },
                )}
              >
                {t(
                  'systemPrompt.preview.tokenBreakdown',
                  '~{prompt} prompt + ~{tools} tools = ~{total} tokens',
                  {
                    prompt: previewTokens,
                    tools: previewToolTokens,
                    total: previewTokens + previewToolTokens,
                  },
                )}
              </span>
            {:else}
              <span class="sp-token-count">
                {t('systemPrompt.preview.tokenCount', '~{count} tokens', {
                  count: previewTokens,
                })}
              </span>
            {/if}
          {/if}
        </div>
        <Button
          variant="secondary"
          class="sp-refresh"
          disabled={isRefreshingPreview ||
            isLoadingData ||
            !canRefreshPreview()}
          onClick={refreshPreview}
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
              value={selectedScopeKey}
              options={scopeOptions}
              ariaLabel={t('systemPrompt.scope.label', 'Prompt scope')}
              triggerClass="sp-scope-dropdown"
              onValueChange={(value) => selectScope(value)}
            />
          </div>
          {#if !isLoadingData}
            <div class="sp-blocklist-toolbar-actions view-toolbar__actions">
              <Button
                variant="secondary"
                class="sp-btn-sm"
                onClick={createCustomBlock}
              >
                {t('systemPrompt.blockList.newBlock', 'New block')}
              </Button>
              <Button
                variant="secondary"
                class="sp-btn-sm"
                onClick={resetLayout}
              >
                {t(
                  'systemPrompt.blockList.resetLayout',
                  'Reset order & visibility',
                )}
              </Button>
            </div>
          {/if}
        </div>

        {#if isLoadingData}
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
            {#each blocks as block, index (block.id)}
              <li
                class="sp-block"
                class:sp-block--off={!block.enabled}
                class:sp-block--inherited={isAgentScope && isInherited(block)}
                ondragover={(event) => handleDragOver(index, event)}
                ondrop={(event) => handleDrop(index, event)}
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
                    ondragstart={(event) => handleDragStart(index, event)}
                    ondragend={handleDragEnd}
                    onkeydown={(event) => handleHandleKeydown(index, event)}
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
                      {#if isCustomBlock(block)}
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
                      {#if isAgentScope && isInherited(block)}
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
                    <span class="sp-block-owner">{ownerHint(block.owner)}</span>
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
                    {#if block.editable && !(isAgentScope && isInherited(block) && !block.isModified)}
                      <Button
                        variant="secondary"
                        class="sp-btn-sm"
                        disabled={block.isBusy || block.isSaving}
                        onClick={() => resetBlock(block.id)}
                      >
                        {block.isBusy
                          ? t('common.loading', 'Loading…')
                          : t('systemPrompt.fragmentEditor.reset', 'Reset')}
                      </Button>
                    {/if}
                    {#if isCustomBlock(block)}
                      <Button
                        variant="danger"
                        class="sp-btn-sm"
                        onClick={() => removeCustomBlock(block.id)}
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
                      onChange={() => toggleBlock(block.id)}
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
                      onInput={(value) => handleTextareaInput(block.id, value)}
                    />
                  {:else}
                    <div class="sp-data-block">
                      <div class="sp-data-block-head">
                        <span class="sp-data-block-label"
                          >{dataKindLabel()}</span
                        >
                        {#if block.preview}
                          <button
                            type="button"
                            class="sp-data-toggle"
                            aria-expanded={block.previewExpanded}
                            onclick={() => togglePreview(block.id)}
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

          {#if blocks.length === 0}
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
              variant="primary"
              class="sp-btn-sm"
              disabled={isBusy}
              onClick={handleManualSaveAll}
            >
              {isBusy
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
          aria-busy={isRefreshingPreview}
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
          {#if previewError}
            <Banner variant="error"
              >{previewError}
              <Button variant="secondary" onClick={refreshPreview}
                >{t('common.retry', 'Retry')}</Button
              >
            </Banner>
          {:else if isRefreshingPreview || isLoadingData}
            <Banner variant="neutral">{t('common.loading', 'Loading…')}</Banner>
          {:else if !canRefreshPreview()}
            <EmptyState
              description={t(
                'systemPrompt.preview.empty',
                'Select an agent to preview its system prompt.',
              )}
            />
          {:else if activeTab === 'tools'}
            <ToolDefinitionsPanel tools={previewTools} {onToast} />
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
                disabled={!previewText}
                onClick={copyPreview}
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
              {#if !previewText}
                <EmptyState
                  description={t(
                    'systemPrompt.preview.noText',
                    'The current configuration produces an empty System Prompt.',
                  )}
                />
              {:else if promptFormat === 'original'}
                <pre class="sp-preview-pre">{previewText}</pre>
              {:else}
                <MarkdownContent
                  source={previewText}
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
    {reorderAnnouncement}
  </div>

  {#if resetConfirmBlockId}
    <ConfirmDialog
      title={t('systemPrompt.fragmentEditor.resetConfirmTitle', 'Reset block')}
      body={resetConfirmBody}
      confirmLabel={t('common.reset', 'Reset')}
      onConfirm={confirmResetBlock}
      onCancel={cancelResetBlock}
    />
  {/if}

  {#if removeConfirmBlockId}
    <ConfirmDialog
      title={t('systemPrompt.blockList.removeConfirmTitle', 'Remove block')}
      body={t(
        'systemPrompt.blockList.removeConfirm',
        'Remove this custom block? This cannot be undone.',
      )}
      confirmLabel={t('common.remove', 'Remove')}
      onConfirm={confirmRemoveCustomBlock}
      onCancel={cancelRemoveCustomBlock}
    />
  {/if}

  {#if resetLayoutConfirmOpen}
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
      onConfirm={confirmResetLayout}
      onCancel={cancelResetLayout}
    />
  {/if}
</section>

<style>
  .sp-view,
  .sp-layout {
    display: flex;
    min-height: 0;
    min-width: 0;
    flex: 1;
    overflow: hidden;
    background: var(--bg);
  }
  .sp-layout {
    flex-direction: column;
  }
  .sp-top {
    flex-shrink: 0;
    padding-bottom: 0;
  }
  .sp-top > *,
  .sp-scroll > * {
    width: 100%;
    max-width: var(--content-max-wide);
    margin-inline: auto;
  }
  .sp-scroll {
    display: flex;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
  }
  .sp-editor {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .sp-editor[hidden] {
    display: none;
  }
  .sp-context-bar,
  .sp-preview-heading-row,
  .sp-document-toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .sp-context-bar,
  .sp-document-toolbar {
    justify-content: space-between;
  }
  .sp-navigation {
    margin-bottom: 0;
  }
  .sp-preview-heading,
  .sp-agent-label,
  .sp-scope-label {
    font-size: var(--fs-label-md);
    color: var(--text-hi);
  }
  .sp-token-count {
    color: var(--text-med);
    font: var(--fs-mono-body)/1.5 var(--font-mono);
  }
  .sp-scope-control {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }
  :global(.sp-scope-dropdown),
  :global(.sp-agent-dropdown) {
    max-width: min(280px, 65vw);
  }
  :global(.sp-agent-dropdown-list) {
    max-height: 260px;
    overflow-y: auto;
  }
  .sp-about {
    margin-bottom: 16px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .sp-about summary {
    cursor: pointer;
    padding: 4px 0;
  }
  .sp-about summary:focus-visible {
    outline: 2px solid var(--accent);
  }
  .sp-preview-note {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.6;
    margin: 0 0 20px;
  }
  .sp-document {
    margin-top: 16px;
    background: var(--preview-surface);
    border: 1px solid var(--border-2);
    border-radius: var(--r-lg);
    padding: 28px;
    min-width: 0;
  }
  .sp-preview-pre {
    margin: 0;
    color: var(--text-hi);
    font: var(--fs-mono-body)/1.8 var(--font-mono);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .sp-document :global(.sp-document-content) {
    max-width: 85ch;
    margin: auto;
    color: var(--text-hi);
    font: var(--fs-body-lg)/1.8 var(--font-ui);
    overflow-wrap: anywhere;
  }
  .sp-document :global(.sp-document-content h1),
  .sp-document :global(.sp-document-content h2),
  .sp-document :global(.sp-document-content h3) {
    color: var(--text-hi);
    font-weight: 600;
    line-height: 1.4;
    margin-top: 28px;
    margin-bottom: 14px;
  }
  .sp-document :global(.sp-document-content h1) {
    font-size: var(--fs-heading-lg);
  }
  .sp-document :global(.sp-document-content h2) {
    font-size: var(--fs-heading-md);
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }
  .sp-document :global(.sp-document-content h3) {
    font-size: var(--fs-heading-sm);
  }
  .sp-document :global(.sp-document-content > :first-child) {
    margin-top: 0;
  }
  .sp-document :global(.sp-document-content p),
  .sp-document :global(.sp-document-content ul),
  .sp-document :global(.sp-document-content ol) {
    margin-block: 14px;
  }
  .sp-document :global(.sp-document-content li) {
    margin-block: 6px;
  }
  .sp-document :global(.sp-document-content pre) {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--text-hi);
  }
  .sp-document :global(.sp-document-content code) {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-body);
    color: var(--text-hi);
  }
  .sp-document :global(.sp-document-content img) {
    max-width: 100%;
  }
  .sp-document :global(.sp-document-content table) {
    display: block;
    overflow-x: auto;
  }
  .sp-document :global(.sp-document-content th),
  .sp-document :global(.sp-document-content td) {
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
  }
  .sp-document :global(.sp-document-content a) {
    color: var(--accent);
  }
  .sp-blocklist-toolbar-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .sp-blocklist-guide {
    padding: 14px 16px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    color: var(--text-med);
  }
  .sp-blocklist-guide__intro {
    cursor: pointer;
    color: var(--text-hi);
  }
  .sp-blocklist-guide__eyebrow {
    display: none;
  }
  .sp-blocklist-guide h3 {
    display: inline;
    font-size: var(--fs-body-md);
    font-weight: 500;
  }
  .sp-blocklist-guide__details {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-top: 12px;
  }
  .sp-blocklist-guide__details p {
    margin: 0;
    font-size: var(--fs-body-sm);
    line-height: 1.6;
  }
  .sp-blocklist-guide__details strong {
    display: block;
    color: var(--text-hi);
  }
  .sp-blocks {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .sp-block {
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    overflow: hidden;
    background: var(--prompt-content-surface);
  }
  .sp-block--off {
    border-style: dashed;
  }
  .sp-block-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px;
    background: var(--prompt-header-surface);
  }
  .sp-block-meta {
    min-width: 0;
    flex: 1;
  }
  .sp-block-title {
    display: block;
    margin-bottom: 6px;
    color: var(--text-hi);
    font-size: var(--fs-body-lg);
    font-weight: 500;
  }
  .sp-block-id-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .sp-block-id {
    color: var(--text-hi);
    font: 500 var(--fs-mono-body)/1.5 var(--font-mono);
    overflow-wrap: anywhere;
  }
  .sp-block-owner {
    display: block;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    margin-top: 6px;
  }
  .sp-block-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .sp-drag-handle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    border: 1px solid var(--border-2);
    border-radius: var(--r-sm);
    color: var(--text-med);
    background: transparent;
    cursor: grab;
    flex-shrink: 0;
  }
  .sp-drag-handle:focus-visible,
  .sp-blocklist-guide__intro:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .sp-block :global(.text-area) {
    color: var(--text-hi);
    font: var(--fs-body-lg)/1.7 var(--font-ui);
    padding: 20px;
    min-height: 240px;
  }
  .sp-data-block {
    padding: 20px;
  }
  .sp-data-block-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .sp-data-block-label,
  .sp-data-empty {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .sp-data-toggle {
    padding: 8px 12px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    color: var(--text-hi);
    background: transparent;
    cursor: pointer;
  }
  .sp-data-preview {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--text-hi);
    font: var(--fs-mono-body)/1.7 var(--font-mono);
  }
  .sp-global-footer {
    position: sticky;
    bottom: -20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    background: var(--bg);
    border-top: 1px solid var(--border);
    padding: 16px 0;
  }
  .sp-global-footer span {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
  }
  .sp-sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    border: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }
  @media (max-width: 640px) {
    .sp-eyebrow,
    .sp-subtitle {
      display: none;
    }
    .sp-context-bar {
      position: relative;
    }
    .sp-preview-heading-row {
      width: 100%;
    }
    .sp-token-count {
      flex-basis: 100%;
      font-size: var(--fs-mono-sm);
    }
    :global(.sp-refresh) {
      position: absolute;
      right: 0;
      top: 0;
    }
    .sp-document {
      padding: 16px;
    }
    .sp-block-row {
      flex-wrap: wrap;
    }
    .sp-block-actions {
      width: 100%;
      justify-content: flex-end;
    }
    .sp-scope-control {
      align-items: flex-start;
      flex-direction: column;
    }
    .sp-blocklist-guide__details {
      grid-template-columns: 1fr;
    }
    .sp-drag-handle {
      width: 40px;
      height: 40px;
    }
    .sp-global-footer {
      bottom: -16px;
    }
    .sp-preview-heading {
      display: none;
    }
  }
</style>
