// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { t } from '../../lib/i18n.js';
import {
  flushSync,
  mount,
  rpcMock,
  listProjectsMock,
  showProjectMock,
  SystemPromptView,
  baseBlocks,
  createRpcMock,
  inheritedBadges,
  blockIds,
  blockElement,
  buttonByText,
  lastCall,
  scopeTrigger,
  agentTrigger,
  dropdownOptionButtons,
  openDropdown,
  scopeOptionLabels,
  agentOptionLabels,
  selectPromptScope,
  isLoading,
  waitForCondition,
  deferred,
  clickTab,
  setupSystemPromptViewSuite,
} from './SystemPromptView.support.js';

import { createStandaloneNavigation } from '../../lib/navigation.svelte.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const DEFAULT_SCOPE = () => t('systemPrompt.scope.default');
const AGENT_SCOPE = { type: 'agent', agent_id: 'agent-1' };

function hasCall(method, predicate = () => true) {
  return rpcMock.mock.calls.some(
    ([called, params]) => called === method && predicate(params ?? {}),
  );
}

function selectPreviewAgent(label) {
  openDropdown(agentTrigger());
  const option = dropdownOptionButtons().find((button) =>
    button.textContent.includes(label),
  );
  expect(option, `preview agent not found: ${label}`).toBeTruthy();
  option.click();
  flushSync();
}

function documentText() {
  return document.querySelector('.sp-document')?.textContent ?? '';
}

function blockText(blockId) {
  return blockElement(blockId).querySelector('textarea').value;
}

function blockIsDirty(blockId) {
  return blockElement(blockId).textContent.includes(
    t('systemPrompt.fragmentEditor.dirtyIndicator'),
  );
}

function typeBlock(blockId, value) {
  const textarea = blockElement(blockId).querySelector('textarea');
  textarea.value = value;
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

describe('SystemPromptView scope and preview', () => {
  const suite = setupSystemPromptViewSuite();

  function mountView(props) {
    suite.mountedComponent = mount(SystemPromptView, {
      target: document.body,
      props,
    });
    flushSync();
  }

  async function waitForDefaultScope() {
    await waitForCondition(
      () => scopeTrigger()?.textContent.includes(DEFAULT_SCOPE()),
      100,
    );
  }

  it('edits an Agent scope with inherited badges and previews that scope', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        agentBlocks: baseBlocks().map((block) => ({
          ...block,
          is_modified: false,
          inheritance: 'owner_default',
        })),
        promptPreview: { text: 'Agent scoped preview', tokens: 77 },
      }),
    );
    const navigation = createStandaloneNavigation();
    const navigate = vi.spyOn(navigation, 'navigate');
    mountView({ navigation });
    await waitForDefaultScope();
    // The empty place shows the Prompt tab and the default scope previewed
    // with the first Agent, and records that place.
    expect(navigation.place).toEqual(['prompt', 'default', 'agent:agent-1']);

    // Only the scopes the server offers (default and enabled Agents) appear.
    expect(scopeOptionLabels()).toEqual([DEFAULT_SCOPE(), 'Alpha']);

    // A draft the switch leaves unsaved (the user discarded it) stays with
    // the default scope, also where the Agent scope has a block of that id.
    typeBlock('core:intro', 'default draft');

    // Choosing a scope is a step to its place.
    selectPromptScope('Alpha');
    expect(navigate).toHaveBeenCalledWith(['prompt', 'agent:agent-1']);
    await waitForCondition(
      () => hasCall('prompt.list', (params) => params.scope),
      100,
    );
    expect(
      rpcMock.mock.calls.find(
        ([method, params]) => method === 'prompt.list' && params?.scope,
      )[1],
    ).toEqual({ scope: AGENT_SCOPE });

    await waitForCondition(() => inheritedBadges().length > 0, 100);
    expect(inheritedBadges()).toHaveLength(3);
    expect(inheritedBadges()[0].textContent.trim()).toBe(
      t('systemPrompt.blockList.inheritedBadge'),
    );
    expect(blockText('core:intro')).toBe('# Intro');
    expect(blockIsDirty('core:intro')).toBe(false);

    // The Agent picker stays available and the preview carries the scope.
    await waitForCondition(
      () =>
        agentTrigger()?.textContent.includes('Alpha') &&
        hasCall('prompt.preview', (params) => params.scope),
      100,
    );
    expect(lastCall('prompt.preview')[1]).toMatchObject({
      agent_id: 'agent-1',
      scope: AGENT_SCOPE,
    });
    await waitForCondition(
      () => documentText().includes('Agent scoped preview'),
      100,
    );

    // Editing an inherited block autosaves the override with the Agent scope.
    vi.useFakeTimers();
    typeBlock('core:intro', 'agent override');
    await vi.advanceTimersByTimeAsync(800);
    await Promise.resolve();
    await Promise.resolve();
    flushSync();

    const updates = rpcMock.mock.calls.filter(
      ([method]) => method === 'prompt.update',
    );
    expect(updates).toHaveLength(1);
    expect(updates[0][1]).toMatchObject({
      id: 'core:intro',
      content: 'agent override',
      scope: AGENT_SCOPE,
    });
  });

  // Leaving while a save runs lets it finish in the background.
  it('keeps a block save that settles after a scope switch in its own scope', async () => {
    const defaultSave = deferred();
    const baseRpc = createRpcMock();
    rpcMock.mockImplementation((method, params) =>
      method === 'prompt.update' && !params?.scope
        ? defaultSave.promise
        : baseRpc(method, params),
    );
    mountView();
    await waitForDefaultScope();

    vi.useFakeTimers();
    typeBlock('core:intro', 'default draft');
    await vi.advanceTimersByTimeAsync(800);
    vi.useRealTimers();
    expect(lastCall('prompt.update')[1]).toEqual({
      id: 'core:intro',
      content: 'default draft',
    });

    selectPromptScope('Alpha');
    await waitForCondition(
      () => hasCall('prompt.list', (params) => params.scope) && !isLoading(),
      100,
    );
    expect(blockText('core:intro')).toBe('# Intro');
    expect(blockIsDirty('core:intro')).toBe(false);

    defaultSave.resolve({
      id: 'core:intro',
      text: 'default draft',
      is_modified: true,
    });
    await waitForCondition(() => true);
    expect(blockText('core:intro')).toBe('# Intro');
    expect(blockIsDirty('core:intro')).toBe(false);
  });

  it('keeps the newest scope when an older prompt list settles late', async () => {
    const staleAgentResponse = deferred();
    const baseRpc = createRpcMock();
    rpcMock.mockImplementation((method, params) => {
      if (method === 'prompt.list' && params?.scope?.agent_id === 'agent-1') {
        return staleAgentResponse.promise;
      }
      return baseRpc(method, params);
    });
    mountView();
    await waitForDefaultScope();

    selectPromptScope('Alpha');
    await waitForCondition(
      () =>
        hasCall(
          'prompt.list',
          (params) => params.scope?.agent_id === 'agent-1',
        ),
      100,
    );
    selectPromptScope(DEFAULT_SCOPE());
    await waitForCondition(
      () =>
        scopeTrigger()?.textContent.includes(DEFAULT_SCOPE()) && !isLoading(),
      100,
    );

    staleAgentResponse.resolve({
      blocks: [
        {
          id: 'user:stale',
          owner: 'always',
          kind: 'text',
          source: 'user',
          editable: true,
          enabled: true,
          text: 'stale',
        },
      ],
      scopes: [
        { type: 'default', label: 'Default' },
        { type: 'agent', agent_id: 'agent-1', label: 'Alpha' },
      ],
    });
    await Promise.resolve();
    await Promise.resolve();
    flushSync();

    expect(scopeTrigger().textContent).toContain(DEFAULT_SCOPE());
    expect(blockIds()).toEqual(baseBlocks().map((block) => block.id));
  });

  it.each([
    ['agent-1', 'Alpha', 'Alpha', ['edit', 'agent:agent-1']],
    // An Agent without a prompt scope opens the default scope previewed with
    // that Agent; an unknown Agent previews the first Agent. Neither requests
    // a scoped prompt list, and the place records what is shown.
    ['agent-2', null, 'Beta', ['edit', 'default', 'agent:agent-2']],
    ['ghost', null, 'Alpha', ['edit', 'default', 'agent:agent-1']],
  ])(
    'opens the editor place of the Agent scope %s',
    async (agentId, scopeLabel, previewLabel, shownPlace) => {
      rpcMock.mockImplementation(createRpcMock());
      const navigation = createStandaloneNavigation([
        'edit',
        `agent:${agentId}`,
      ]);
      const navigate = vi.spyOn(navigation, 'navigate');
      mountView({ navigation });

      const scopedList = () =>
        hasCall('prompt.list', (params) => params.scope?.agent_id === agentId);
      await waitForCondition(
        () =>
          scopeTrigger()?.textContent.includes(scopeLabel ?? DEFAULT_SCOPE()) &&
          agentTrigger()?.textContent.includes(previewLabel) &&
          !isLoading(),
        100,
      );
      expect(scopedList()).toBe(Boolean(scopeLabel));
      expect(document.querySelector('.sp-editor').hidden).toBe(false);
      expect(navigation.place).toEqual(shownPlace);
      expect(navigate).not.toHaveBeenCalled();
    },
  );

  it.each([
    [
      'with Tools',
      { tokens: 1234, tool_tokens: 456, tool_count: 12 },
      () =>
        t('systemPrompt.preview.tokenBreakdown', {
          prompt: 1234,
          tools: 456,
          total: 1690,
        }),
    ],
    [
      'without Tools',
      { tokens: 200, tool_tokens: 0, tool_count: 0 },
      () => t('systemPrompt.preview.tokenCount', { count: 200 }),
    ],
  ])(
    'loads the first Agent preview on mount and shows its token count %s',
    async (_case, tokens, expectedCount) => {
      rpcMock.mockImplementation(
        createRpcMock({
          promptPreview: {
            text: 'You are an agent named Alpha...',
            estimated: true,
            ...tokens,
          },
        }),
      );
      mountView();

      // No Refresh click: the preview loads for the first selected Agent.
      await waitForCondition(
        () => document.body.textContent.includes(expectedCount()),
        100,
      );
      expect(lastCall('prompt.preview')[1]).toMatchObject({
        agent_id: 'agent-1',
      });
      expect(document.body.textContent).toContain(
        t('systemPrompt.preview.heading'),
      );
      expect(documentText()).toContain('You are an agent named Alpha');
      expect(buttonByText(t('systemPrompt.preview.refresh'))).toBeTruthy();
      if (!tokens.tool_count) {
        expect(document.body.textContent).not.toContain('= ~');
      }
    },
  );

  it('offers Project Agents in the preview picker and previews by address', async () => {
    listProjectsMock.mockResolvedValue({ projects: [{ project_id: 'vbot' }] });
    showProjectMock.mockResolvedValue({
      project: { display_name: 'vBot' },
      scan: { team: [{ agent_id: 'builder', display_name: 'Builder' }] },
    });
    rpcMock.mockImplementation(
      createRpcMock({
        promptPreview: { text: 'Project agent preview', tokens: 88 },
      }),
    );
    mountView();

    await waitForCondition(() => agentTrigger(), 100);
    await waitForCondition(
      () => agentOptionLabels().some((label) => label.includes('builder@vbot')),
      100,
    );

    openDropdown(agentTrigger());
    expect(document.body.textContent).toContain(
      t('systemPrompt.preview.agentGroup.project'),
    );
    agentTrigger().click();
    flushSync();
    selectPreviewAgent('builder@vbot');

    // Selecting the Project Agent loads its preview without a Refresh click.
    await waitForCondition(
      () =>
        hasCall(
          'prompt.preview',
          (params) => params.agent_id === 'builder@vbot',
        ),
      100,
    );
    await waitForCondition(
      () => documentText().includes('Project agent preview'),
      100,
    );
  });

  it('names the Agents anew after an Agent change and keeps the shown scope and blocks', async () => {
    const agents = [
      { id: 'agent-1', name: 'Alpha', custom_system_prompt_enabled: true },
      { id: 'agent-2', name: 'Beta', custom_system_prompt_enabled: false },
    ];
    rpcMock.mockImplementation(createRpcMock({ agents }));
    const props = reactiveProps({ agentsRefreshToken: 0 });
    mountView(props);
    await waitForDefaultScope();
    await waitForCondition(
      () => agentTrigger()?.textContent.includes('Alpha'),
      100,
    );
    const promptLists = () =>
      rpcMock.mock.calls.filter(([method]) => method === 'prompt.list').length;
    const listedBefore = promptLists();

    agents[0] = { ...agents[0], name: 'Alpha Prime' };
    props.agentsRefreshToken += 1;
    flushSync();
    await waitForCondition(
      () => agentTrigger().textContent.includes('Alpha Prime'),
      100,
    );

    expect(scopeOptionLabels()).toEqual([DEFAULT_SCOPE(), 'Alpha Prime']);
    expect(scopeTrigger().textContent).toContain(DEFAULT_SCOPE());
    expect(promptLists()).toBe(listedBefore);
  });

  it('reloads the preview for a newly selected Agent and ignores the older one that finishes late', async () => {
    const old = deferred();
    const base = createRpcMock();
    rpcMock.mockImplementation((method, params) => {
      if (method !== 'prompt.preview') return base(method, params);
      return params.agent_id === 'agent-1'
        ? old.promise
        : Promise.resolve({ text: 'NEW-AGENT', tools: [], tokens: 5 });
    });
    const navigation = createStandaloneNavigation();
    const navigate = vi.spyOn(navigation, 'navigate');
    mountView({ navigation });
    await waitForCondition(
      () =>
        hasCall('prompt.preview', (params) => params.agent_id === 'agent-1'),
      100,
    );

    // Choosing a preview Agent is a step; Beta has no scope of its own.
    selectPreviewAgent('Beta');
    expect(navigate).toHaveBeenCalledWith([
      'prompt',
      'default',
      'agent:agent-2',
    ]);
    await waitForCondition(() => documentText().includes('NEW-AGENT'), 100);
    expect(lastCall('prompt.preview')[1]).toMatchObject({
      agent_id: 'agent-2',
    });

    old.resolve({
      text: 'STALE-AGENT',
      tools: [{ definition: { name: 'stale', parameters: {} }, tokens: 5 }],
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    expect(documentText()).toContain('NEW-AGENT');
    clickTab(t('systemPrompt.tabs.tools'));
    expect(navigate).toHaveBeenLastCalledWith([
      'tools',
      'default',
      'agent:agent-2',
    ]);
    expect(document.querySelector('.tool-detail')).toBeNull();
  });

  it('opens in document view and copies the exact original prompt', async () => {
    const text =
      '# Inspection fixture\n\nKeep **all** words.\n<external>literal</external>';
    const writeText = vi.fn().mockResolvedValue();
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    rpcMock.mockImplementation(
      createRpcMock({ promptPreview: { text, tokens: 25, tools: [] } }),
    );
    mountView();
    await waitForCondition(
      () => document.querySelector('.sp-document h1'),
      100,
    );
    expect(document.querySelector('.sp-editor').hidden).toBe(true);
    expect(document.querySelector('external')).toBeNull();
    clickTab(t('systemPrompt.format.original'));
    expect(document.querySelector('.sp-preview-pre').textContent).toBe(text);
    document.querySelector('.sp-document-toolbar button.btn-secondary').click();
    await waitForCondition(() => writeText.mock.calls.length > 0, 100);
    expect(writeText).toHaveBeenCalledWith(text);
  });

  it('shows searchable complete Tool definitions separately from the prompt', async () => {
    const definition = {
      name: 'mcp_fixture',
      description: 'TEST-MCP-DESCRIPTION <script>inert</script>',
      parameters: {
        type: 'object',
        properties: { action: { enum: ['search', 'describe'] } },
      },
    };
    rpcMock.mockImplementation(
      createRpcMock({
        promptPreview: {
          text: 'PROMPT-ONLY',
          tokens: 10,
          tools: [
            { definition, tokens: 350 },
            {
              definition: {
                name: 'read',
                description: 'READ-FIXTURE',
                parameters: {},
              },
              tokens: 100,
              on_demand: true,
            },
          ],
        },
      }),
    );
    mountView();
    await waitForCondition(() => document.querySelector('.sp-document'), 100);
    expect(documentText()).not.toContain('TEST-MCP-DESCRIPTION');
    expect(lastCall('prompt.preview')[1].include_tools).toBe(true);
    clickTab(t('systemPrompt.tabs.tools'));
    expect(document.querySelector('.tool-description').textContent).toBe(
      definition.description,
    );
    expect(
      JSON.parse(document.querySelector('.tool-schema').textContent),
    ).toEqual(definition.parameters);
    expect(document.querySelector('.tool-detail script')).toBeNull();
    expect(document.querySelector('.tool-on-demand-note')).toBeNull();
    const search = document.querySelector('input[type="search"]');
    search.value = 'READ-FIXTURE';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(document.querySelector('.tool-detail h3').textContent).toBe('read');
    expect(document.querySelectorAll('.tool-index-item')).toHaveLength(1);
    expect(document.querySelector('.tool-index-item').textContent).toContain(
      t('systemPrompt.tools.onDemand'),
    );
    expect(document.querySelector('.tool-on-demand-note')).not.toBeNull();
    search.value = 'not-found';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(document.querySelector('.tool-detail')).toBeNull();
    expect(document.body.textContent).toContain(
      t('systemPrompt.tools.noMatches'),
    );
  });

  it('shows a failed preview and retries without retaining old content', async () => {
    const base = createRpcMock();
    let failing = true;
    rpcMock.mockImplementation((method, params) =>
      method === 'prompt.preview' && failing
        ? Promise.reject(new Error('test outage'))
        : base(method, params),
    );
    const retryButton = () => buttonByText(t('common.retry'));
    mountView();
    await waitForCondition(retryButton, 100);
    expect(document.querySelector('.sp-document')).toBeNull();
    failing = false;
    retryButton().click();
    await waitForCondition(() => document.querySelector('.sp-document'), 100);
    expect(retryButton()).toBeNull();
  });
});
