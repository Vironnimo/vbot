import { expect, vi } from 'vitest';
import { flushSync } from 'svelte';

export const rpcMock = vi.fn();

export function modelListCallCount() {
  return rpcMock.mock.calls.filter((call) => call[0] === 'model.list').length;
}

export function connectionListCallCount() {
  return rpcMock.mock.calls.filter((call) => call[0] === 'connection.list')
    .length;
}

export function modelTriggerLabel() {
  return triggerTextContent(getSearchableTrigger('agent-model'));
}

export function fallbackTriggerLabel() {
  return triggerTextContent(getSearchableTrigger('agent-fallback-model'));
}

export function thinkingTriggerLabel() {
  return triggerTextContent(getSimpleTrigger('agent-thinking-effort'));
}

export function triggerTextContent(trigger) {
  return (
    trigger
      ?.querySelector(
        '.searchable-dropdown__trigger-label, .dropdown-primitive__trigger-label',
      )
      ?.textContent?.trim() ?? ''
  );
}

export async function openSearchableDropdown(id, rect = defaultTriggerRect()) {
  const trigger = getSearchableTrigger(id);
  stubTriggerRect(trigger, rect);
  trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();

  await waitForCondition(
    () => getSearchableRoot(id).dataset.state === 'open',
    100,
  );
  return getSearchableRoot(id);
}

export function openSearchableDropdownSync(id, rect = defaultTriggerRect()) {
  const trigger = getSearchableTrigger(id);
  stubTriggerRect(trigger, rect);
  trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();

  expect(getSearchableRoot(id).dataset.state).toBe('open');
  return getSearchableRoot(id);
}

export function setSearchableFilter(id, value) {
  const input = getSearchablePanel(id).querySelector('input');
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

export function selectSearchableOption(id, label) {
  const option = Array.from(
    getSearchablePanel(id).querySelectorAll('.searchable-dropdown__option'),
  ).find((item) => item.textContent.trim() === label);
  expect(option).toBeTruthy();
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

export function searchableOptionLabels(id) {
  return Array.from(
    getSearchablePanel(id)?.querySelectorAll('.searchable-dropdown__option') ??
      [],
  ).map((option) => option.textContent.trim());
}

export function getSearchableRoot(id) {
  return getSearchableTrigger(id)?.closest('.searchable-dropdown');
}

export function getSearchableTrigger(id) {
  const trigger = document.body.querySelector(`button#${id}`);
  expect(trigger).toBeTruthy();
  return trigger;
}

export function getSearchablePanel() {
  // The panel is portaled to <body>; only the open dropdown renders one.
  return document.body.querySelector('.searchable-dropdown__panel');
}

export function getSearchableOptionsContainer() {
  return getSearchablePanel()?.querySelector('.searchable-dropdown__options');
}

export function openSimpleDropdown(id) {
  const trigger = getSimpleTrigger(id);
  trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
  return getSimpleRoot(id);
}

export function selectSimpleOption(id, label) {
  const option = Array.from(
    getSimpleList(id)?.querySelectorAll('.dropdown-option') ?? [],
  ).find((item) => item.textContent.trim() === label);
  expect(option).toBeTruthy();
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

export function simpleOptionLabels(id) {
  return Array.from(
    getSimpleList(id)?.querySelectorAll('.dropdown-option') ?? [],
  ).map((option) => option.textContent.trim());
}

export function getSimpleRoot(id) {
  return getSimpleTrigger(id)?.closest('.dropdown-primitive');
}

export function getSimpleTrigger(id) {
  const trigger = document.body.querySelector(`button#${id}`);
  expect(trigger).toBeTruthy();
  return trigger;
}

export function getSimpleList() {
  // The list is portaled to <body>; only the open dropdown renders one.
  return document.body.querySelector('.dropdown-primitive__list');
}

export function stubTriggerRect(trigger, rect) {
  trigger.getBoundingClientRect = () => ({
    x: rect.left,
    y: rect.top,
    left: rect.left,
    top: rect.top,
    right: rect.right,
    bottom: rect.bottom,
    width: rect.width,
    height: rect.height,
    toJSON: () => rect,
  });
}

export function defaultTriggerRect() {
  return {
    left: 96,
    top: 144,
    right: 416,
    bottom: 176,
    width: 320,
    height: 32,
  };
}

export function setTextInputValue(index, value) {
  const input = Array.from(
    document.body.querySelectorAll('input.s-input[type="text"]'),
  )[index];
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

export function getButton(label) {
  const button = Array.from(document.body.querySelectorAll('button')).find(
    (item) => item.textContent.trim() === label,
  );
  expect(button).toBeTruthy();
  return button;
}

export function getDialog(title) {
  const dialog = Array.from(
    document.body.querySelectorAll('[role="dialog"]'),
  ).find((item) => item.textContent.includes(title));
  expect(dialog).toBeTruthy();
  return dialog;
}

export function setTextInputValueWithin(container, index, value) {
  const input = Array.from(
    container.querySelectorAll('input.s-input[type="text"]'),
  )[index];
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

export function setNumberInputValueWithin(container, index, value) {
  const input = Array.from(
    container.querySelectorAll('input.s-input[inputmode="decimal"]'),
  )[index];
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

export function getButtonByAriaLabel(label) {
  const button = document.body.querySelector(`button[aria-label="${label}"]`);
  expect(button).toBeTruthy();
  return button;
}

export function temperatureInput() {
  return document.body.querySelector('input.s-input[inputmode="decimal"]');
}

export function getAgentButton(label) {
  const button = Array.from(
    document.body.querySelectorAll('button.agent-item'),
  ).find((item) => item.textContent.includes(label));
  expect(button).toBeTruthy();
  return button;
}

export function submitAgentForm() {
  document.body
    .querySelector('form')
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  flushSync();
}

export function findSetToDefaultButton() {
  return Array.from(
    document.body.querySelectorAll('.agent-detail-pane button'),
  ).find((button) => button.textContent.trim() === 'Set to default');
}

export function getAgentUpdateCalls() {
  return rpcMock.mock.calls.filter((call) => call[0] === 'agent.update');
}

export async function flushAsyncUpdates(iterations = 4) {
  for (let index = 0; index < iterations; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}

export function textInputValue(index) {
  const input = Array.from(
    document.body.querySelectorAll('input.s-input[type="text"]'),
  )[index];
  expect(input).toBeTruthy();
  return input.value;
}

export function createAgentsRpcMock(options = {}) {
  const models = options.models ?? [openaiModel(), anthropicModel()];
  const agents = options.agents ?? [baseAgent()];
  const connections = options.connections ?? [
    usableConnection('openai:api-key', 'openai', 'API Key'),
  ];

  return async (method, params) => {
    if (method === 'model.list') {
      return { models };
    }

    if (method === 'connection.list') {
      return { connections };
    }

    if (method === 'tool.list') {
      return { tools: options.tools ?? [] };
    }

    if (method === 'skill.list') {
      return options.skills ?? skillCatalog();
    }

    if (method === 'project.list') {
      return { projects: options.projects ?? [] };
    }

    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'settings.get') {
      // The create modal fetches the global agent defaults for its inherit
      // labels. `settingsDefaults` (or an empty object) stands in for
      // `defaults.agent`.
      return { defaults: { agent: options.settingsDefaults ?? {} } };
    }

    if (method === 'prompt.list') {
      // The disable-custom-prompt confirm fetches scopes to read
      // has_customizations for the current agent.
      return {
        scopes: options.scopes ?? [{ type: 'default', label: 'Default' }],
      };
    }

    if (method === 'agent.create' || method === 'agent.update') {
      if (typeof options.agentUpdate === 'function') {
        return options.agentUpdate(params, method);
      }

      return { ...params, current_session_id: 'session-saved' };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

export function skillCatalog() {
  return {
    skills: [
      {
        name: 'sample-skill',
        description: 'A loadable sample skill.',
        valid: true,
        warnings: [],
      },
      {
        name: 'warning-skill',
        description: 'Loads with a warning.',
        valid: false,
        warnings: ['name differs from folder'],
      },
    ],
    invalid_skills: [
      {
        name: 'broken-skill',
        path: 'C:/skills/broken-skill/SKILL.md',
        valid: false,
        warnings: ['missing description'],
      },
    ],
  };
}

export function usableConnection(id, providerId, label) {
  return {
    id,
    provider_id: providerId,
    type: id.endsWith(':subscription') ? 'oauth' : 'api_key',
    label,
    usable: true,
  };
}

export function openaiModel() {
  return {
    id: 'openai/gpt-5.2',
    provider_id: 'openai',
    model_id: 'gpt-5.2',
    name: 'GPT-5.2',
    capabilities: { tools: true },
    context_window: 256000,
    effective_context_window: 256000,
  };
}

export function anthropicModel() {
  return {
    id: 'anthropic/claude-sonnet-4-20250219',
    provider_id: 'anthropic',
    model_id: 'claude-sonnet-4-20250219',
    name: 'Claude Sonnet 4',
    capabilities: { tools: true },
    context_window: 200000,
    effective_context_window: 200000,
  };
}

export function baseAgent() {
  return {
    id: 'alpha',
    name: 'Alpha',
    model: 'openai/gpt-5.2::api-key',
    fallback_model: '',
    workspace: 'C:/agents/alpha',
    current_session_id: 'session-1',
    temperature: '0.1',
    thinking_effort: '',
    memory_prompt_mode: 'agent_user',
    allowed_tools: ['*'],
    allowed_skills: ['*'],
    custom_system_prompt_enabled: false,
    created_at: '2026-05-08T00:00:00+00:00',
    updated_at: '2026-05-08T00:00:00+00:00',
  };
}

export async function waitForCondition(check, attempts = 20) {
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

export async function waitForText(text, attempts = 20) {
  await waitForCondition(
    () => document.body.textContent?.includes(text),
    attempts,
  );
}
