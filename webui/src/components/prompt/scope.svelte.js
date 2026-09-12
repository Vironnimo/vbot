import { SvelteMap } from 'svelte/reactivity';
import {
  createAgentTargetCatalogLoader,
  buildAgentTargetDropdownOptions,
} from '$lib/agentTargetOptions.js';
import {
  listProjects,
  showProject,
  listAgents,
  listPrompts,
  previewPrompt,
} from '$lib/api.js';
import { t } from '$lib/i18n.js';

export function createPromptScope(context) {
  const PREVIEW_REFRESH_DEBOUNCE_MS = 100;

  let agents = $state([]);

  let promptScopes = $state([]);

  let selectedScopeKey = $state('default');

  let selectedAgentId = $state('');

  let previewTools = $state([]);

  let previewError = $state('');

  let previewText = $state('');

  let previewTokens = $state(null);

  let previewToolTokens = $state(null);

  let previewToolCount = $state(null);

  let isLoadingData = $state(true);

  let isRefreshingPreview = $state(false);

  let scopeLoadRequestId = 0;

  let previewRequestId = 0;

  let previewRefreshTimer = null;

  // Project teams power the project-agent options in the preview agent picker.
  // Identity agents come from `agent.list`; project agents are scanned lazily
  // (one `project.show` per project) and cached, so the N+1 scan never runs on
  // every render. A scan failure is non-fatal — identity agents still preview.
  let projectTeams = $state([]);

  let projectTeamsLoaded = false;

  const targetCatalog = createAgentTargetCatalogLoader({
    listProjects,
    showProject,
  });

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
      context.showToast(
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
    if (projectTeamsLoaded) return;
    const catalog = await targetCatalog.load();
    if (!catalog) return;
    projectTeams = catalog.projectTeams;
    projectTeamsLoaded = !catalog.projectError;
  }

  function selectPreviewAgent(agentId) {
    if (agentId === selectedAgentId) return;
    return context.autosaveContext.requestTransition(async () => {
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
    return context.autosaveContext.requestTransition(() =>
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
    context.reorderAnnouncement = '';
    context.clearAutoSaveTimers();
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
      context.showToast(
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
    const previousById = new SvelteMap(
      context.blocks.map((block) => [block.id, block]),
    );
    context.clearAutoSaveTimers();

    context.blocks = source.map((raw) => {
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
      context.showToast(t('common.copied', 'Copied'), 'success');
    } catch {
      context.showToast(
        t('systemPrompt.error.copyFailed', 'Failed to copy'),
        'error',
      );
    }
  }
  function destroy() {
    targetCatalog.dispose();
    scopeLoadRequestId += 1;
    previewRequestId += 1;
    clearPreviewRefreshTimer();
  }

  return {
    destroy,
    get promptScopes() {
      return promptScopes;
    },
    set promptScopes(value) {
      promptScopes = value;
    },
    get selectedScopeKey() {
      return selectedScopeKey;
    },
    set selectedScopeKey(value) {
      selectedScopeKey = value;
    },
    get selectedAgentId() {
      return selectedAgentId;
    },
    set selectedAgentId(value) {
      selectedAgentId = value;
    },
    get previewTools() {
      return previewTools;
    },
    set previewTools(value) {
      previewTools = value;
    },
    get previewError() {
      return previewError;
    },
    set previewError(value) {
      previewError = value;
    },
    get previewText() {
      return previewText;
    },
    set previewText(value) {
      previewText = value;
    },
    get previewTokens() {
      return previewTokens;
    },
    set previewTokens(value) {
      previewTokens = value;
    },
    get previewToolTokens() {
      return previewToolTokens;
    },
    set previewToolTokens(value) {
      previewToolTokens = value;
    },
    get previewToolCount() {
      return previewToolCount;
    },
    set previewToolCount(value) {
      previewToolCount = value;
    },
    get isLoadingData() {
      return isLoadingData;
    },
    set isLoadingData(value) {
      isLoadingData = value;
    },
    get isRefreshingPreview() {
      return isRefreshingPreview;
    },
    set isRefreshingPreview(value) {
      isRefreshingPreview = value;
    },
    get isAgentScope() {
      return isAgentScope;
    },
    set isAgentScope(value) {
      isAgentScope = value;
    },
    get scopeOptions() {
      return scopeOptions;
    },
    set scopeOptions(value) {
      scopeOptions = value;
    },
    get previewAgentOptions() {
      return previewAgentOptions;
    },
    set previewAgentOptions(value) {
      previewAgentOptions = value;
    },
    get loadData() {
      return loadData;
    },
    get loadProjectTeams() {
      return loadProjectTeams;
    },
    get selectPreviewAgent() {
      return selectPreviewAgent;
    },
    get selectScope() {
      return selectScope;
    },
    get loadBlocksForScope() {
      return loadBlocksForScope;
    },
    get scopedParams() {
      return scopedParams;
    },
    get canRefreshPreview() {
      return canRefreshPreview;
    },
    get schedulePreviewRefresh() {
      return schedulePreviewRefresh;
    },
    get refreshPreview() {
      return refreshPreview;
    },
    get copyPreview() {
      return copyPreview;
    },
  };
}
