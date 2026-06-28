// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join, dirname } from 'node:path';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();
const listProjectsMock = vi.fn();
const showProjectMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
  listProjects: (...args) => listProjectsMock(...args),
  showProject: (...args) => showProjectMock(...args),
}));

const { default: SystemPromptView } = await import('../SystemPromptView.svelte');

const COMPONENT_SOURCE_PATH = join(
  dirname(fileURLToPath(import.meta.url)),
  '../SystemPromptView.svelte',
);

function componentSource() {
  return readFileSync(COMPONENT_SOURCE_PATH, 'utf-8');
}

describe('SystemPromptView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    listProjectsMock.mockReset();
    showProjectMock.mockReset();
    listProjectsMock.mockResolvedValue({ projects: [] });
    showProjectMock.mockResolvedValue({ project: {}, scan: { team: [] } });
    mountedComponent = null;
    window.confirm = vi.fn(() => true);
    window.prompt = vi.fn(() => null);
  });

  afterEach(async () => {
    vi.useRealTimers();

    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('renders blocks in layout order with their owner labels', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    expect(blockIds()).toEqual([
      'core:intro',
      'memory:guidance',
      'tool:bash',
      'data:soul',
    ]);

    // Owner labels read "appears when: <owner>".
    expect(document.body.textContent).toContain('appears when: always');
    expect(document.body.textContent).toContain('appears when: memory');
    expect(document.body.textContent).toContain('appears when: tool: bash');
  });

  it('renders an editable textarea for text blocks but not for data blocks', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    // Three editable text blocks → three textareas; the data block has none.
    const textareas = document.body.querySelectorAll('textarea.sp-textarea');
    expect(textareas).toHaveLength(3);
    expect(textareas[0].value).toBe('# Intro');

    // The data block renders the read-only data presentation instead.
    const dataBlock = blockElement('data:soul');
    expect(dataBlock.querySelector('textarea')).toBeNull();
    expect(dataBlock.querySelector('.sp-data-block')).toBeTruthy();
    expect(dataBlock.textContent).toContain('Generated content (read-only)');
  });

  it('reveals the collapsed data block preview on demand', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    const dataBlock = blockElement('data:soul');
    expect(dataBlock.querySelector('.sp-data-preview')).toBeNull();

    dataBlock.querySelector('.sp-data-toggle').click();
    flushSync();

    expect(dataBlock.querySelector('.sp-data-preview').textContent).toContain(
      '<file>SOUL</file>',
    );
  });

  it('toggling a block persists immediately via prompt.set_layout', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    const toggle = blockElement('tool:bash').querySelector('button[role="switch"]');
    expect(toggle.getAttribute('aria-checked')).toBe('true');

    toggle.click();
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.set_layout'),
      100,
    );

    const layoutCall = lastCall('prompt.set_layout');
    const bashEntry = layoutCall[1].layout.find(
      (entry) => entry.id === 'tool:bash',
    );
    expect(bashEntry.enabled).toBe(false);
    // The full ordered layout is sent, not just the toggled block.
    expect(layoutCall[1].layout.map((entry) => entry.id)).toEqual(blockIds());
  });

  it('editing an editable block autosaves via prompt.update after the debounce', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    vi.useFakeTimers();

    const textarea = blockElement('core:intro').querySelector('textarea');
    textarea.value = 'updated intro';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    expect(document.body.textContent).toContain('unsaved');
    expect(rpcMock.mock.calls.some((call) => call[0] === 'prompt.update')).toBe(
      false,
    );

    await vi.advanceTimersByTimeAsync(800);
    await Promise.resolve();
    await Promise.resolve();
    flushSync();

    const updateCall = lastCall('prompt.update');
    expect(updateCall[1]).toMatchObject({
      id: 'core:intro',
      content: 'updated intro',
    });
  });

  it('autosave keys by block id, not array index, after a reorder', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    // Move the first block (core:intro) down via the keyboard, then edit it: the
    // autosave must target core:intro by id even though its index changed.
    const handle = blockHandle('core:intro');
    handle.focus();
    pressKey(handle, 'ArrowDown');
    flushSync();
    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.set_layout'),
      100,
    );

    vi.useFakeTimers();
    const textarea = blockElement('core:intro').querySelector('textarea');
    textarea.value = 'edited after move';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    await vi.advanceTimersByTimeAsync(800);
    await Promise.resolve();
    await Promise.resolve();
    flushSync();

    const updateCall = lastCall('prompt.update');
    expect(updateCall[1]).toMatchObject({
      id: 'core:intro',
      content: 'edited after move',
    });
  });

  it('per-block reset calls prompt.reset after confirm', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        promptReset: {
          id: 'core:intro',
          text: '# Bundled intro',
          is_modified: false,
        },
      }),
    );

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    const resetButton = Array.from(
      blockElement('core:intro').querySelectorAll('button.btn-secondary'),
    ).find((button) => button.textContent.trim() === 'Reset');
    resetButton.click();
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.reset'),
      100,
    );

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(lastCall('prompt.reset')[1]).toMatchObject({ id: 'core:intro' });

    await waitForCondition(
      () =>
        blockElement('core:intro').querySelector('textarea').value ===
        '# Bundled intro',
      50,
    );
  });

  it('reorders via native drag-and-drop and persists the new order', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    const dataTransfer = createDataTransfer();
    // Drag the first handle (core:intro) onto the third row (tool:bash).
    blockHandle('core:intro').dispatchEvent(
      dragEvent('dragstart', dataTransfer),
    );
    flushSync();
    blockElement('tool:bash').dispatchEvent(dragEvent('dragover', dataTransfer));
    flushSync();
    blockElement('tool:bash').dispatchEvent(dragEvent('drop', dataTransfer));
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.set_layout'),
      100,
    );

    // core:intro moved from index 0 to index 2 (where tool:bash was).
    expect(blockIds()).toEqual([
      'memory:guidance',
      'tool:bash',
      'core:intro',
      'data:soul',
    ]);
    expect(lastCall('prompt.set_layout')[1].layout.map((e) => e.id)).toEqual([
      'memory:guidance',
      'tool:bash',
      'core:intro',
      'data:soul',
    ]);
  });

  it('reorders via the keyboard, persists, and announces the new position', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    const handle = blockHandle('memory:guidance');
    handle.focus();
    pressKey(handle, 'ArrowUp');
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.set_layout'),
      100,
    );

    // memory:guidance moved up above core:intro.
    expect(blockIds()).toEqual([
      'memory:guidance',
      'core:intro',
      'tool:bash',
      'data:soul',
    ]);
    expect(lastCall('prompt.set_layout')[1].layout[0].id).toBe(
      'memory:guidance',
    );

    // The aria-live region announces the new position.
    const live = document.body.querySelector('[aria-live="polite"]');
    expect(live.textContent).toContain('position 1');
  });

  it('creates a custom block through prompt.create_block', async () => {
    window.prompt = vi.fn(() => 'my-note');
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    clickToolbarButton('New block');
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.create_block'),
      100,
    );

    expect(lastCall('prompt.create_block')[1]).toMatchObject({ slug: 'my-note' });
  });

  it('surfaces a bad slug as a toast without calling the backend', async () => {
    const toastMock = vi.fn();
    window.prompt = vi.fn(() => '1 bad slug!');
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    clickToolbarButton('New block');
    flushSync();

    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'error' }),
    );
    expect(
      rpcMock.mock.calls.some((call) => call[0] === 'prompt.create_block'),
    ).toBe(false);
  });

  it('surfaces a backend bad-slug rejection as a toast', async () => {
    const toastMock = vi.fn();
    window.prompt = vi.fn(() => 'taken');
    rpcMock.mockImplementation(
      createRpcMock({
        createBlockError: new Error('invalid_request'),
      }),
    );

    mountedComponent = mount(SystemPromptView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    clickToolbarButton('New block');
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.create_block'),
      100,
    );

    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'error' }),
    );
  });

  it('removes a custom block through prompt.remove_block after confirm', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        blocks: [
          ...baseBlocks(),
          {
            id: 'user:my-note',
            owner: 'always',
            kind: 'text',
            source: 'user',
            editable: true,
            enabled: true,
            text: 'custom text',
            is_modified: true,
          },
        ],
      }),
    );

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().includes('user:my-note'), 100);

    const removeButton = Array.from(
      blockElement('user:my-note').querySelectorAll('button.btn-danger'),
    ).find((button) => button.textContent.trim() === 'Remove');
    expect(removeButton).toBeTruthy();

    removeButton.click();
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.remove_block'),
      100,
    );

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(lastCall('prompt.remove_block')[1]).toMatchObject({
      id: 'user:my-note',
    });
  });

  it('resets the layout through prompt.reset_layout', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(() => blockIds().length === baseBlocks().length, 100);

    clickToolbarButton('Reset order & visibility');
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.reset_layout'),
      100,
    );

    expect(window.confirm).toHaveBeenCalledOnce();
  });

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

    mountedComponent = mount(SystemPromptView, { target: document.body });
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

    await waitForCondition(
      () => document.body.querySelector('.sp-badge--inherited'),
      100,
    );
    expect(document.body.querySelectorAll('.sp-badge--inherited').length).toBe(
      3,
    );

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

  it('renders only default and enabled agent prompt scopes', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => scopeTrigger()?.textContent.includes('Default'),
      100,
    );

    expect(scopeOptionLabels()).toEqual(['Default', 'Alpha']);
  });

  it('refresh calls prompt.preview and renders the token count', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        promptPreview: {
          text: 'You are an agent named Alpha...',
          tokens: 1234,
          estimated: true,
        },
      }),
    );

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Preview for'),
      100,
    );

    const refreshButton = Array.from(
      document.body.querySelectorAll('button'),
    ).find((button) => button.textContent.trim() === 'Refresh');
    refreshButton.click();
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.preview'),
      100,
    );

    expect(lastCall('prompt.preview')[1]).toMatchObject({ agent_id: 'agent-1' });
    await waitForCondition(
      () => document.body.textContent.includes('1234'),
      50,
    );
    expect(document.body.textContent).toContain('You are an agent named Alpha');
  });

  it('previews an agent prompt scope without the default agent picker', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        promptPreview: { text: 'Agent scoped preview', tokens: 77 },
      }),
    );

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => scopeTrigger()?.textContent.includes('Default'),
      100,
    );

    selectPromptScope('Alpha');
    await waitForCondition(
      () => document.body.querySelector('.sp-scope-chip')?.textContent.includes('Alpha'),
      100,
    );

    expect(document.body.querySelector('#sp-agent-select')).toBeNull();

    const refreshButton = Array.from(
      document.body.querySelectorAll('button'),
    ).find((button) => button.textContent.trim() === 'Refresh');
    refreshButton.click();
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.preview'),
      100,
    );

    expect(lastCall('prompt.preview')[1]).toEqual({
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

    mountedComponent = mount(SystemPromptView, { target: document.body });
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

    const refreshButton = Array.from(
      document.body.querySelectorAll('button'),
    ).find((button) => button.textContent.trim() === 'Refresh');
    refreshButton.click();
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.preview'),
      100,
    );

    expect(lastCall('prompt.preview')[1]).toEqual({ agent_id: 'builder@vbot' });
    expect(document.body.textContent).toContain('Project agent preview');
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
      'systemPrompt.fragmentEditor.resetConfirm',
      'systemPrompt.fragmentEditor.resetAgentConfirm',
      'systemPrompt.blockList.intro',
      'systemPrompt.blockList.newBlock',
      'systemPrompt.blockList.newBlockPrompt',
      'systemPrompt.blockList.invalidSlug',
      'systemPrompt.blockList.createFailed',
      'systemPrompt.blockList.removeConfirm',
      'systemPrompt.blockList.removeFailed',
      'systemPrompt.blockList.resetLayout',
      'systemPrompt.blockList.resetLayoutConfirm',
      'systemPrompt.blockList.customBadge',
      'systemPrompt.blockList.dataBadge',
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
      'systemPrompt.blockList.appearsWhen',
      'systemPrompt.blockList.owner.always',
      'systemPrompt.blockList.owner.memory',
      'systemPrompt.blockList.owner.channel',
      'systemPrompt.blockList.owner.tool',
      'systemPrompt.blockList.owner.extension',
      'systemPrompt.preview.heading',
      'systemPrompt.preview.refresh',
      'systemPrompt.preview.copy',
      'systemPrompt.preview.tokenCount',
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

function baseBlocks() {
  return [
    {
      id: 'core:intro',
      owner: 'always',
      kind: 'text',
      source: 'core',
      editable: true,
      enabled: true,
      text: '# Intro',
      is_modified: false,
    },
    {
      id: 'memory:guidance',
      owner: 'memory',
      kind: 'text',
      source: 'memory',
      editable: true,
      enabled: true,
      text: '# Memory guidance',
      is_modified: false,
    },
    {
      id: 'tool:bash',
      owner: 'tool:bash',
      kind: 'text',
      source: 'tool',
      editable: true,
      enabled: true,
      text: '# Bash tool',
      is_modified: false,
    },
    {
      id: 'data:soul',
      owner: 'always',
      kind: 'data',
      source: 'core',
      editable: false,
      enabled: true,
      text: '<file>SOUL</file>',
    },
  ];
}

function baseAgents() {
  return [
    { id: 'agent-1', name: 'Alpha', custom_system_prompt_enabled: true },
    { id: 'agent-2', name: 'Beta', custom_system_prompt_enabled: false },
  ];
}

function createRpcMock(options = {}) {
  const blocks = options.blocks ?? baseBlocks();
  const agentBlocks = options.agentBlocks ?? blocks;
  const agents = options.agents ?? baseAgents();
  const scopes = options.scopes ?? [
    { type: 'default', label: 'Default' },
    { type: 'agent', agent_id: 'agent-1', label: 'Alpha' },
  ];
  const promptReset = options.promptReset ?? null;
  const promptPreview = options.promptPreview ?? null;
  const createBlockError = options.createBlockError ?? null;

  return async (method, params) => {
    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'prompt.list') {
      return {
        blocks: params?.scope?.type === 'agent' ? agentBlocks : blocks,
        scopes,
      };
    }

    if (method === 'prompt.update') {
      return {
        id: params.id,
        text: params.content,
        is_modified: true,
        ...(params?.scope?.type === 'agent'
          ? { inheritance: 'agent_override' }
          : {}),
      };
    }

    if (method === 'prompt.reset') {
      if (promptReset) {
        return promptReset;
      }
      return { id: params.id, text: '', is_modified: false };
    }

    if (method === 'prompt.set_layout') {
      return { layout: params.layout };
    }

    if (method === 'prompt.create_block') {
      if (createBlockError) {
        throw createBlockError;
      }
      return {
        id: `user:${params.slug}`,
        owner: 'always',
        kind: 'text',
        source: 'user',
        editable: true,
        enabled: true,
        rank: 0,
      };
    }

    if (method === 'prompt.remove_block') {
      return { layout: [] };
    }

    if (method === 'prompt.reset_layout') {
      return { layout: [] };
    }

    if (method === 'prompt.preview') {
      if (promptPreview) {
        return promptPreview;
      }
      return { text: 'Preview text', tokens: 500, estimated: true };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

// -- DOM helpers ------------------------------------------------------------

function blockElements() {
  return Array.from(document.body.querySelectorAll('li.sp-block'));
}

function blockIds() {
  return blockElements().map(
    (element) => element.querySelector('.sp-block-id')?.textContent.trim() ?? '',
  );
}

function blockElement(blockId) {
  const element = blockElements().find(
    (item) => item.querySelector('.sp-block-id')?.textContent.trim() === blockId,
  );
  expect(element, `block not found: ${blockId}`).toBeTruthy();
  return element;
}

function blockHandle(blockId) {
  return blockElement(blockId).querySelector('[data-block-handle]');
}

function clickToolbarButton(label) {
  const button = Array.from(
    document.body.querySelectorAll('.sp-blocklist-toolbar-actions button'),
  ).find((item) => item.textContent.trim() === label);
  expect(button, `toolbar button not found: ${label}`).toBeTruthy();
  button.click();
}

function lastCall(method) {
  const calls = rpcMock.mock.calls.filter((call) => call[0] === method);
  return calls[calls.length - 1];
}

function pressKey(element, key) {
  const event = new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
  });
  element.dispatchEvent(event);
  return event;
}

// jsdom has no real DataTransfer; a minimal stub backs the drag payload.
function createDataTransfer() {
  const store = new Map();
  return {
    effectAllowed: 'none',
    dropEffect: 'none',
    setData(type, value) {
      store.set(type, String(value));
    },
    getData(type) {
      return store.get(type) ?? '';
    },
  };
}

function dragEvent(type, dataTransfer) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', {
    configurable: true,
    value: dataTransfer,
  });
  return event;
}

// The scope and preview-agent pickers are the shared Dropdown primitive: a
// <button> trigger plus a portaled list of <button role="option"> rows that
// only exist in the DOM while the dropdown is open.
function scopeTrigger() {
  return document.body.querySelector('#sp-scope-select');
}

function agentTrigger() {
  return document.body.querySelector('#sp-agent-select');
}

function dropdownOptionButtons() {
  return Array.from(document.body.querySelectorAll('.dropdown-option'));
}

function openDropdown(trigger) {
  expect(trigger).toBeTruthy();
  trigger.click();
  flushSync();
}

function readOpenOptionLabels(trigger) {
  openDropdown(trigger);
  const labels = dropdownOptionButtons().map((button) =>
    button.textContent.trim(),
  );
  trigger.click();
  flushSync();
  return labels;
}

function scopeOptionLabels() {
  return readOpenOptionLabels(scopeTrigger());
}

function agentOptionLabels() {
  return readOpenOptionLabels(agentTrigger());
}

function selectPromptScope(label) {
  openDropdown(scopeTrigger());
  const option = dropdownOptionButtons().find(
    (button) => button.textContent.trim() === label,
  );
  expect(option, `scope option not found: ${label}`).toBeTruthy();
  option.click();
  flushSync();
}

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    if (check()) {
      return;
    }
  }

  throw new Error('Timed out waiting for condition.');
}
