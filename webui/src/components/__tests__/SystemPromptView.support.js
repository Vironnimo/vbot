const { mount, flushSync, unmount } = await import('svelte');
// @vitest-environment jsdom
import { afterEach, beforeEach, expect, vi } from 'vitest';

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join, dirname } from 'node:path';
import { init } from '../../lib/i18n.js';

import { rpcBackedApiMock } from './apiMock.js';

const rpcMock = vi.fn();

const listProjectsMock = vi.fn();

const showProjectMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    listProjects: (...args) => listProjectsMock(...args),
    showProject: (...args) => showProjectMock(...args),
  }),
);

const { default: SystemPromptView } =
  await import('../SystemPromptView.svelte');

const COMPONENT_SOURCE_PATH = join(
  dirname(fileURLToPath(import.meta.url)),
  '../SystemPromptView.svelte',
);

function componentSource() {
  return [
    COMPONENT_SOURCE_PATH,
    join(dirname(COMPONENT_SOURCE_PATH), 'prompt/scope.svelte.js'),
    join(dirname(COMPONENT_SOURCE_PATH), 'prompt/editor.svelte.js'),
  ]
    .map((path) => readFileSync(path, 'utf-8'))
    .join('\n');
}

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

// The "inherited" marker is now the shared Badge primitive, identified by its
// translated label rather than a bespoke per-variant class.
function inheritedBadges() {
  return Array.from(document.body.querySelectorAll('.badge')).filter(
    (element) => element.textContent.trim() === 'inherited',
  );
}

function blockIds() {
  return blockElements().map(
    (element) =>
      element.querySelector('.sp-block-id')?.textContent.trim() ?? '',
  );
}

function blockElement(blockId) {
  const element = blockElements().find(
    (item) =>
      item.querySelector('.sp-block-id')?.textContent.trim() === blockId,
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

function buttonByText(label) {
  return (
    Array.from(document.body.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === label,
    ) ?? null
  );
}

// Clicks the confirm button in the open ConfirmDialog, identified by its label.
// The dialog renders inside a `.modal-footer` (Modal shell) with a cancel and a
// confirm Button; the confirm button carries the action verb.
function confirmDialog(label) {
  const footer = document.body.querySelector('.modal-footer');
  expect(footer, 'confirm dialog not open').toBeTruthy();
  const button = Array.from(footer.querySelectorAll('button')).find(
    (item) => item.textContent.trim() === label,
  );
  expect(button, `confirm button not found: ${label}`).toBeTruthy();
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

function isLoading() {
  return document.body.querySelector('.sp-blocklist-guide') === null;
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

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function clickTab(name) {
  const tab = [...document.querySelectorAll('[role="tab"]')].find(
    (item) => item.textContent.trim() === name,
  );
  expect(tab, `Tab ${name}`).toBeTruthy();
  tab.click();
  flushSync();
}

function setupSystemPromptViewSuite() {
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
  return {
    get mountedComponent() {
      return mountedComponent;
    },
    set mountedComponent(value) {
      mountedComponent = value;
    },
  };
}

export {
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
  blockHandle,
  clickToolbarButton,
  buttonByText,
  confirmDialog,
  lastCall,
  pressKey,
  createDataTransfer,
  dragEvent,
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
};

export { flushSync, mount };
