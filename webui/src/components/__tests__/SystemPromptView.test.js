// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join, dirname } from 'node:path';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
}));

const { default: SystemPromptView } =
  await import('../SystemPromptView.svelte');

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
    mountedComponent = null;
    window.confirm = vi.fn(() => true);
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('renders fragment editors on mount with content from prompt.list', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('system.md'),
      100,
    );

    const textareas = document.body.querySelectorAll('textarea.sp-textarea');
    expect(textareas).toHaveLength(4);

    expect(textareas[0].value).toBe('# System prompt content');
    expect(textareas[1].value).toBe('# Runtime content');
    expect(textareas[2].value).toBe('# Tools content');
    expect(textareas[3].value).toBe('# Skills content');

    expect(document.body.textContent).toContain('system.md');
    expect(document.body.textContent).toContain('runtime.md');
    expect(document.body.textContent).toContain('tools.md');
    expect(document.body.textContent).toContain('skills.md');
  });

  it('renders variable references with tooltip titles', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.querySelectorAll('.sp-variable').length > 0,
      100,
    );

    const variableSpans = document.body.querySelectorAll('.sp-variable');
    const appVersionSpan = Array.from(variableSpans).find(
      (span) => span.textContent === '{app_version}',
    );
    expect(appVersionSpan).toBeTruthy();
    expect(appVersionSpan.getAttribute('title')).toBe('Application version');
  });

  it('marks fragment as dirty when textarea content changes from saved content', async () => {
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.querySelectorAll('textarea.sp-textarea').length === 4,
      100,
    );

    const textarea = document.body.querySelectorAll('textarea.sp-textarea')[0];
    textarea.value = 'changed content';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    expect(document.body.textContent).toContain('unsaved');
  });

  it('save calls prompt.update and clears dirty state', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        promptUpdate: {
          name: 'system.md',
          content: 'updated content',
          is_modified: true,
        },
      }),
    );

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.querySelectorAll('textarea.sp-textarea').length === 4,
      100,
    );

    const textarea = document.body.querySelectorAll('textarea.sp-textarea')[0];
    textarea.value = 'updated content';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    expect(document.body.textContent).toContain('unsaved');

    const saveButtons = Array.from(
      document.body.querySelectorAll('button.btn-primary.sp-btn-sm'),
    ).filter((btn) => btn.textContent.trim() === 'Save');
    expect(saveButtons.length).toBeGreaterThan(0);

    saveButtons[0].click();
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.update'),
      100,
    );

    const updateCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'prompt.update',
    );
    expect(updateCall[1]).toMatchObject({
      name: 'system.md',
      content: 'updated content',
    });

    await waitForCondition(
      () => !document.body.textContent.includes('unsaved'),
      50,
    );
  });

  it('reset calls prompt.reset after confirm and updates content', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        promptReset: {
          name: 'system.md',
          content: '# Bundled default content',
        },
      }),
    );

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.querySelectorAll('textarea.sp-textarea').length === 4,
      100,
    );

    const textarea = document.body.querySelectorAll('textarea.sp-textarea')[0];
    textarea.value = 'dirty content';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    const resetButtons = Array.from(
      document.body.querySelectorAll('button.btn-outline.sp-btn-sm'),
    ).filter((btn) => btn.textContent.trim() === 'Reset');
    expect(resetButtons.length).toBeGreaterThan(0);

    resetButtons[0].click();
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.reset'),
      100,
    );

    expect(window.confirm).toHaveBeenCalledOnce();

    const resetCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'prompt.reset',
    );
    expect(resetCall[1]).toMatchObject({ name: 'system.md' });

    await waitForCondition(() => {
      const areas = document.body.querySelectorAll('textarea.sp-textarea');
      return areas[0]?.value === '# Bundled default content';
    }, 50);
  });

  it('does not call prompt.reset when confirm is cancelled', async () => {
    window.confirm = vi.fn(() => false);
    rpcMock.mockImplementation(createRpcMock());

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () =>
        document.body.querySelectorAll('button.btn-outline.sp-btn-sm').length >
        0,
      100,
    );

    const resetButtons = Array.from(
      document.body.querySelectorAll('button.btn-outline.sp-btn-sm'),
    ).filter((btn) => btn.textContent.trim() === 'Reset');
    resetButtons[0].click();
    flushSync();

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(rpcMock.mock.calls.every((call) => call[0] !== 'prompt.reset')).toBe(
      true,
    );
  });

  it('refresh calls prompt.preview and renders token count', async () => {
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
    ).find((btn) => btn.textContent.trim() === 'Refresh');
    expect(refreshButton).toBeTruthy();

    refreshButton.click();
    flushSync();

    await waitForCondition(
      () => rpcMock.mock.calls.some((call) => call[0] === 'prompt.preview'),
      100,
    );

    const previewCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'prompt.preview',
    );
    expect(previewCall[1]).toMatchObject({ agent_id: 'agent-1' });

    await waitForCondition(
      () => document.body.textContent.includes('1234'),
      50,
    );

    expect(document.body.textContent).toContain('1234');
    expect(document.body.textContent).toContain('You are an agent named Alpha');
  });

  it('shows modified badge when fragment is_modified is true', async () => {
    rpcMock.mockImplementation(
      createRpcMock({
        fragments: baseFragments().map((f, index) =>
          index === 0 ? { ...f, is_modified: true } : f,
        ),
      }),
    );

    mountedComponent = mount(SystemPromptView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.querySelectorAll('.sp-badge--modified').length > 0,
      100,
    );

    expect(document.body.querySelectorAll('.sp-badge--modified')).toHaveLength(
      1,
    );
  });

  it('all new i18n keys have t() calls in the component source', () => {
    const source = componentSource();

    const requiredKeys = [
      'systemPrompt.title',
      'systemPrompt.fragmentEditor.save',
      'systemPrompt.fragmentEditor.reset',
      'systemPrompt.fragmentEditor.dirtyIndicator',
      'systemPrompt.fragmentEditor.modifiedIndicator',
      'systemPrompt.fragmentEditor.resetConfirm',
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
    ];

    for (const key of requiredKeys) {
      expect(source, `Missing i18n key: ${key}`).toContain(`'${key}'`);
    }
  });
});

function baseFragments() {
  return [
    {
      name: 'system.md',
      content: '# System prompt content',
      is_modified: false,
      variables: [
        { placeholder: '{app_version}', description: 'Application version' },
        { placeholder: '{runtime}', description: 'Runtime info' },
      ],
    },
    {
      name: 'runtime.md',
      content: '# Runtime content',
      is_modified: false,
      variables: [{ placeholder: '{host}', description: 'Server host' }],
    },
    {
      name: 'tools.md',
      content: '# Tools content',
      is_modified: false,
      variables: [
        { placeholder: '{tool_list}', description: 'Available tools' },
      ],
    },
    {
      name: 'skills.md',
      content: '# Skills content',
      is_modified: false,
      variables: [
        { placeholder: '{skill_list}', description: 'Available skills' },
      ],
    },
  ];
}

function baseAgents() {
  return [
    { id: 'agent-1', name: 'Alpha' },
    { id: 'agent-2', name: 'Beta' },
  ];
}

function createRpcMock(options = {}) {
  const fragments = options.fragments ?? baseFragments();
  const agents = options.agents ?? baseAgents();
  const promptUpdate = options.promptUpdate ?? null;
  const promptReset = options.promptReset ?? null;
  const promptPreview = options.promptPreview ?? null;

  return async (method, params) => {
    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'prompt.list') {
      return { fragments };
    }

    if (method === 'prompt.update') {
      if (promptUpdate) {
        return promptUpdate;
      }

      return {
        name: params.name,
        content: params.content,
        is_modified: true,
      };
    }

    if (method === 'prompt.reset') {
      if (promptReset) {
        return promptReset;
      }

      const fragment = fragments.find((f) => f.name === params.name);
      return { name: params.name, content: fragment?.content ?? '' };
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
