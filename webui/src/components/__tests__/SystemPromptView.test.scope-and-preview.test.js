// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  rpcMock,
  listProjectsMock,
  showProjectMock,
  SystemPromptView,
  componentSource,
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

import { reactiveProps } from './_reactiveProps.svelte.js';

describe('SystemPromptView', () => {
  const suite = setupSystemPromptViewSuite();

  it('shows the inherited badge in an agent scope and edits with the scope', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        agentBlocks: baseBlocks().map((block) =>
          block.id === 'core:intro'
            ? {
                ...block,
                is_modified: false,
                inheritance: 'owner_default',
              }
            : { ...block, inheritance: 'owner_default' },
        ),
      }),
    );

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => scopeTrigger()?.textContent.includes('Default'),
      100,
    );

    selectPromptScope('Alpha');
    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          (call) => call[0] === 'prompt.list' && call[1]?.scope,
        ),
      100,
    );

    const scopedListCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'prompt.list' && call[1]?.scope,
    );
    expect(scopedListCall[1]).toEqual({
      scope: { type: 'agent', agent_id: 'agent-1' },
    });

    await waitForCondition(() => inheritedBadges().length > 0, 100);
    expect(inheritedBadges().length).toBe(3);

    // Editing an inherited block autosaves the override with the agent scope.
    vi.useFakeTimers();
    const textarea = blockElement('core:intro').querySelector('textarea');
    textarea.value = 'agent override';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    await vi.advanceTimersByTimeAsync(800);
    await Promise.resolve();
    await Promise.resolve();
    flushSync();

    expect(lastCall('prompt.update')[1]).toMatchObject({
      id: 'core:intro',
      content: 'agent override',
      scope: { type: 'agent', agent_id: 'agent-1' },
    });
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
    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();
    await waitForCondition(
      () => scopeTrigger()?.textContent.includes('Default'),
      100,
    );

    selectPromptScope('Alpha');
    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'prompt.list' && params?.scope?.agent_id === 'agent-1',
        ),
      100,
    );
    selectPromptScope('Default');
    await waitForCondition(
      () => scopeTrigger()?.textContent.includes('Default') && !isLoading(),
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

    expect(scopeTrigger().textContent).toContain('Default');
    expect(blockIds()).toEqual(baseBlocks().map((block) => block.id));
  });

  it('renders only default and enabled agent prompt scopes', async () => {
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => scopeTrigger()?.textContent.includes('Default'),
      100,
    );

    expect(scopeOptionLabels()).toEqual(['Default', 'Alpha']);
  });

  it('selects the target agent scope when a scope deep-link request arrives', async () => {
    rpcMock.mockImplementation(createRpcMock());

    const props = reactiveProps({
      targetScopeAgentId: '',
      targetScopeRequestId: 0,
    });
    suite.mountedComponent = mount(SystemPromptView, {
      target: document.body,
      props,
    });
    flushSync();

    await waitForCondition(
      () => scopeTrigger()?.textContent.includes('Default'),
      100,
    );

    // A deep-link request bumps the request id with a valid agent scope target.
    props.targetScopeAgentId = 'agent-1';
    props.targetScopeRequestId = 1;
    flushSync();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          (call) =>
            call[0] === 'prompt.list' && call[1]?.scope?.agent_id === 'agent-1',
        ),
      100,
    );

    await waitForCondition(
      () => scopeTrigger()?.textContent.includes('Alpha'),
      100,
    );
    expect(scopeTrigger().textContent).toContain('Alpha');
  });

  it('falls back to the default scope when the deep-link target scope is absent', async () => {
    rpcMock.mockImplementation(createRpcMock());

    const props = reactiveProps({
      targetScopeAgentId: '',
      targetScopeRequestId: 0,
    });
    suite.mountedComponent = mount(SystemPromptView, {
      target: document.body,
      props,
    });
    flushSync();

    await waitForCondition(
      () => scopeTrigger()?.textContent.includes('Default'),
      100,
    );

    // A request for an agent with no prompt scope leaves the default selected
    // and never issues a scoped prompt.list for the missing agent.
    props.targetScopeAgentId = 'ghost';
    props.targetScopeRequestId = 1;
    flushSync();

    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    expect(scopeTrigger().textContent).toContain('Default');
    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'prompt.list' && call[1]?.scope?.agent_id === 'ghost',
      ),
    ).toBe(false);
  });

  it('loads prompt.preview automatically and renders the token breakdown', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        promptPreview: {
          text: 'You are an agent named Alpha...',
          tokens: 1234,
          tool_tokens: 456,
          tool_count: 12,
          estimated: true,
        },
      }),
    );

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Preview for'),
      100,
    );

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.preview'),
      100,
    );

    expect(lastCall('prompt.preview')[1]).toMatchObject({
      agent_id: 'agent-1',
    });
    await waitForCondition(
      () => document.body.textContent.includes('1234'),
      50,
    );
    expect(document.body.textContent).toContain(
      '~1234 prompt + ~456 tools = ~1690 tokens',
    );
    expect(document.body.textContent).toContain('You are an agent named Alpha');
    expect(buttonByText('Refresh')).toBeTruthy();
  });

  it('falls back to the plain token count when the agent has no tools', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        promptPreview: {
          text: 'Toolless preview',
          tokens: 200,
          tool_tokens: 0,
          tool_count: 0,
          estimated: true,
        },
      }),
    );

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Preview for'),
      100,
    );

    await waitForCondition(
      () => document.body.textContent.includes('~200 tokens'),
      100,
    );
    expect(document.body.textContent).not.toContain('= ~');
  });

  it('keeps the Agent picker available when previewing a custom scope', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        promptPreview: { text: 'Agent scoped preview', tokens: 77 },
      }),
    );

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => scopeTrigger()?.textContent.includes('Default'),
      100,
    );

    selectPromptScope('Alpha');
    await waitForCondition(
      () => agentTrigger()?.textContent.includes('Alpha'),
      100,
    );

    expect(document.body.querySelector('#sp-agent-select')).toBeTruthy();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.preview'),
      100,
    );

    expect(lastCall('prompt.preview')[1]).toMatchObject({
      agent_id: 'agent-1',
      scope: { type: 'agent', agent_id: 'agent-1' },
    });
    expect(document.body.textContent).toContain('Agent scoped preview');
  });

  it('offers project agents in the preview picker and previews by address', async () => {
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

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => agentTrigger(), 100);
    await waitForCondition(
      () => agentOptionLabels().some((label) => label.includes('builder@vbot')),
      100,
    );

    openDropdown(agentTrigger());
    expect(document.body.textContent).toContain('Project agents');
    const projectOption = dropdownOptionButtons().find((button) =>
      button.textContent.includes('builder@vbot'),
    );
    projectOption.click();
    flushSync();

    // Selecting the project agent auto-loads its preview — no Refresh click.
    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          (call) =>
            call[0] === 'prompt.preview' &&
            call[1]?.agent_id === 'builder@vbot',
        ),
      100,
    );

    expect(lastCall('prompt.preview')[1]).toMatchObject({
      agent_id: 'builder@vbot',
    });
    expect(document.body.textContent).toContain('Project agent preview');
  });

  it('loads the preview automatically on mount without pressing Refresh', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        promptPreview: { text: 'Auto-loaded preview', tokens: 321 },
      }),
    );

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    // No Refresh click — the preview fetches for the first selected agent.
    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.preview'),
      100,
    );

    expect(lastCall('prompt.preview')[1]).toMatchObject({
      agent_id: 'agent-1',
    });
    await waitForCondition(
      () => document.body.textContent.includes('Auto-loaded preview'),
      50,
    );
  });

  it('reloads the preview when the preview agent changes', async () => {
    rpcMock.mockImplementation(createRpcMock());

    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    // The initial auto-load previews the first agent (agent-1).
    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          (call) =>
            call[0] === 'prompt.preview' && call[1]?.agent_id === 'agent-1',
        ),
      100,
    );

    // Switching the preview agent re-fetches for the newly selected agent with
    // no Refresh click.
    openDropdown(agentTrigger());
    const betaOption = dropdownOptionButtons().find((button) =>
      button.textContent.includes('Beta'),
    );
    expect(betaOption, 'Beta option not found').toBeTruthy();
    betaOption.click();
    flushSync();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          (call) =>
            call[0] === 'prompt.preview' && call[1]?.agent_id === 'agent-2',
        ),
      100,
    );
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
    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    await waitForCondition(
      () => document.querySelector('.sp-document h1'),
      100,
    );
    expect(document.querySelector('.sp-editor').hidden).toBe(true);
    expect(document.querySelector('external')).toBeNull();
    clickTab('Original text');
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
            },
          ],
        },
      }),
    );
    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    await waitForCondition(() => document.querySelector('.sp-document'), 100);
    expect(document.querySelector('.sp-document').textContent).not.toContain(
      'TEST-MCP-DESCRIPTION',
    );
    expect(lastCall('prompt.preview')[1].include_tools).toBe(true);
    clickTab('Tools');
    expect(document.querySelector('.tool-description').textContent).toBe(
      definition.description,
    );
    expect(
      JSON.parse(document.querySelector('.tool-schema').textContent),
    ).toEqual(definition.parameters);
    expect(document.querySelector('.tool-detail script')).toBeNull();
    const search = document.querySelector('input[type="search"]');
    search.value = 'READ-FIXTURE';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(document.querySelector('.tool-detail h3').textContent).toBe('read');
    expect(document.querySelectorAll('.tool-index-item')).toHaveLength(1);
    search.value = 'not-found';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(document.querySelector('.tool-detail')).toBeNull();
    expect(document.body.textContent).toContain('No matching Tools');
  });

  it('opens a block without changing inclusion or losing edits across tabs', async () => {
    rpcMock.mockImplementation(createRpcMock());
    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    await waitForCondition(
      () => blockIds().length === baseBlocks().length,
      100,
    );
    clickTab('Edit blocks');
    const block = blockElement('core:intro');
    const disclosure = block.querySelector('button[aria-expanded]');
    expect(disclosure.getAttribute('aria-expanded')).toBe('false');
    disclosure.click();
    flushSync();
    expect(disclosure.getAttribute('aria-expanded')).toBe('true');
    expect(lastCall('prompt.set_layout')).toBeUndefined();
    const textarea = block.querySelector('textarea');
    textarea.value = 'PRESERVED-DRAFT';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    clickTab('Tools');
    clickTab('Edit blocks');
    expect(block.querySelector('textarea').value).toBe('PRESERVED-DRAFT');
    expect(disclosure.getAttribute('aria-expanded')).toBe('true');
  });

  it('shows a failed preview and retries without retaining old content', async () => {
    const base = createRpcMock();
    let failing = true;
    rpcMock.mockImplementation((method, params) =>
      method === 'prompt.preview' && failing
        ? Promise.reject(new Error('test outage'))
        : base(method, params),
    );
    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    await waitForCondition(() => buttonByText('Retry'), 100);
    expect(document.querySelector('.sp-document')).toBeNull();
    failing = false;
    buttonByText('Retry').click();
    await waitForCondition(() => document.querySelector('.sp-document'), 100);
    expect(buttonByText('Retry')).toBeNull();
  });

  it('ignores an old Agent preview that finishes after a new selection', async () => {
    const old = deferred();
    const base = createRpcMock();
    rpcMock.mockImplementation((method, params) => {
      if (method !== 'prompt.preview') return base(method, params);
      return params.agent_id === 'agent-1'
        ? old.promise
        : Promise.resolve({ text: 'NEW-AGENT', tools: [], tokens: 5 });
    });
    suite.mountedComponent = mount(SystemPromptView, { target: document.body });
    await waitForCondition(() => lastCall('prompt.preview'), 100);
    openDropdown(agentTrigger());
    dropdownOptionButtons()
      .find((button) => button.textContent.includes('Beta'))
      .click();
    flushSync();
    await waitForCondition(
      () =>
        document
          .querySelector('.sp-document')
          ?.textContent.includes('NEW-AGENT'),
      100,
    );
    old.resolve({
      text: 'STALE-AGENT',
      tools: [{ definition: { name: 'stale', parameters: {} }, tokens: 5 }],
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
    expect(document.querySelector('.sp-document').textContent).toContain(
      'NEW-AGENT',
    );
    clickTab('Tools');
    expect(document.querySelector('.tool-detail')).toBeNull();
  });

  it('all new i18n keys have t() calls in the component source', () => {
    const source = componentSource();

    const requiredKeys = [
      'common.saved',
      'common.alreadySaved',
      'common.remove',
      'systemPrompt.title',
      'systemPrompt.scope.label',
      'systemPrompt.scope.default',
      'systemPrompt.fragmentEditor.save',
      'systemPrompt.fragmentEditor.reset',
      'systemPrompt.fragmentEditor.dirtyIndicator',
      'systemPrompt.fragmentEditor.modifiedIndicator',
      'systemPrompt.fragmentEditor.modifiedHint',
      'systemPrompt.fragmentEditor.resetConfirm',
      'systemPrompt.fragmentEditor.resetAgentConfirm',
      'systemPrompt.fragmentEditor.resetConfirmTitle',
      'systemPrompt.blockList.guide.label',
      'systemPrompt.blockList.guide.title',
      'systemPrompt.blockList.guide.assemblyLabel',
      'systemPrompt.blockList.guide.assembly',
      'systemPrompt.blockList.guide.scopeLabel',
      'systemPrompt.blockList.guide.scope',
      'systemPrompt.blockList.newBlock',
      'systemPrompt.blockList.newBlockPrompt',
      'systemPrompt.blockList.invalidSlug',
      'systemPrompt.blockList.createFailed',
      'systemPrompt.blockList.removeConfirm',
      'systemPrompt.blockList.removeConfirmTitle',
      'systemPrompt.blockList.removeFailed',
      'systemPrompt.blockList.resetLayout',
      'systemPrompt.blockList.resetLayoutConfirm',
      'systemPrompt.blockList.resetLayoutConfirmTitle',
      'systemPrompt.blockList.customBadge',
      'systemPrompt.blockList.dataBadge',
      'systemPrompt.blockList.dataHint',
      'systemPrompt.blockList.inheritedBadge',
      'systemPrompt.blockList.inheritedHint',
      'systemPrompt.blockList.dataLabel',
      'systemPrompt.blockList.dataEmpty',
      'systemPrompt.blockList.showPreview',
      'systemPrompt.blockList.hidePreview',
      'systemPrompt.blockList.empty',
      'systemPrompt.blockList.toggleAria',
      'systemPrompt.blockList.reorderHandle',
      'systemPrompt.blockList.reorderAnnouncement',
      'systemPrompt.blockList.ownerHint.always',
      'systemPrompt.blockList.ownerHint.memory',
      'systemPrompt.blockList.ownerHint.channel',
      'systemPrompt.blockList.ownerHint.tool',
      'systemPrompt.blockList.ownerHint.extension',
      'systemPrompt.preview.heading',
      'systemPrompt.preview.copy',
      'systemPrompt.preview.tokenCount',
      'systemPrompt.preview.tokenBreakdown',
      'systemPrompt.preview.tokenBreakdownHint',
      'systemPrompt.preview.agentLabel',
      'systemPrompt.preview.empty',
      'systemPrompt.error.loadFailed',
      'systemPrompt.error.saveFailed',
      'systemPrompt.error.resetFailed',
      'systemPrompt.error.previewFailed',
      'systemPrompt.error.copyFailed',
      'systemPrompt.error.layoutFailed',
    ];

    for (const key of requiredKeys) {
      expect(source, `Missing i18n key: ${key}`).toContain(`'${key}'`);
    }
  });
});
