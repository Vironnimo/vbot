const { mount, flushSync, unmount } = await import('svelte');
// @vitest-environment jsdom
import { afterEach, beforeEach, expect, vi } from 'vitest';

import { init } from '../../lib/i18n.js';

import { rpcBackedApiMock } from './apiMock.js';

const addProjectMock = vi.fn();

const listProjectsMock = vi.fn();

const showProjectMock = vi.fn();

const setProjectMock = vi.fn();

const removeProjectMock = vi.fn();

const setOverrideMock = vi.fn();

const clearOverrideMock = vi.fn();

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    addProject: (...args) => addProjectMock(...args),
    listProjects: (...args) => listProjectsMock(...args),
    showProject: (...args) => showProjectMock(...args),
    setProject: (...args) => setProjectMock(...args),
    removeProject: (...args) => removeProjectMock(...args),
    setOverride: (...args) => setOverrideMock(...args),
    clearOverride: (...args) => clearOverrideMock(...args),
  }),
);

const { default: ProjectsView } = await import('../ProjectsView.svelte');

// Just above the component's 800ms auto-save debounce, so the timer has fired
// by the time the test inspects the mock.
const AUTO_SAVE_WAIT_MS = 900;

function project(overrides = {}) {
  return {
    project_id: 'project-default',
    display_name: 'Default Project',
    cwd: 'C:/repos/default',
    cwd_exists: true,
    default_agent: '',
    default_model: '',
    auto_load: [],
    created_at: '2026-06-18T00:00:00Z',
    updated_at: '2026-06-18T00:00:00Z',
    ...overrides,
  };
}

// A scan team member with the step-1/2 payload shape (effective + overrides).
function member(overrides = {}) {
  return {
    agent_id: 'agent',
    display_name: 'Agent',
    description: '',
    model: '',
    temperature: null,
    thinking_effort: null,
    source_format: 'opencode',
    source_path: '.opencode/agents/agent.md',
    denied_tools: [],
    tools: {},
    overrides: null,
    effective: {
      model: { value: null, source: null },
      temperature: { value: null, source: null },
      thinking_effort: { value: null, source: null },
      tool_access: { value: { mode: 'all' }, source: 'agent' },
    },
    ...overrides,
  };
}

function buttonByTestId(testId) {
  const button = document.querySelector(`[data-testid="${testId}"]`);
  expect(button, testId).toBeTruthy();
  return button;
}

function buttonWithTextContent(label) {
  const button = Array.from(document.querySelectorAll('button')).find(
    (item) => item.textContent.trim() === label,
  );
  expect(button, `button not found: ${label}`).toBeTruthy();
  return button;
}

function confirmDialog(label) {
  const footer = document.querySelector('.modal-footer');
  expect(footer, 'confirm dialog not open').toBeTruthy();
  const button = Array.from(footer.querySelectorAll('button')).find(
    (item) => item.textContent.trim() === label,
  );
  expect(button, `confirm button not found: ${label}`).toBeTruthy();
  button.click();
}

function submitButtonInDialog(label) {
  const dialog = document.querySelector('[role="dialog"]');
  expect(dialog, 'open dialog').toBeTruthy();
  const button = Array.from(dialog.querySelectorAll('button')).find(
    (item) =>
      item.getAttribute('type') === 'submit' &&
      item.textContent?.includes(label) &&
      !item.disabled,
  );
  expect(button, `submit button "${label}" in dialog`).toBeTruthy();
  return button;
}

function inputById(id) {
  return document.getElementById(id);
}

function optionByText(text) {
  return Array.from(document.querySelectorAll('[role="option"]')).find(
    (item) => item.textContent?.trim() === text,
  );
}

// The ordered detail-section titles must appear in the given sequence. The
// InfoHint "?" dot inside a title is presentation, not part of the title text.
function expectSectionOrder(titles) {
  const rendered = Array.from(
    document.querySelectorAll('.detail-section-title'),
  ).map((node) => {
    const clone = node.cloneNode(true);
    clone.querySelectorAll('.info-hint').forEach((dot) => dot.remove());
    clone
      .querySelectorAll('.projects-section-refresh')
      .forEach((button) => button.remove());
    return clone.textContent.trim();
  });
  expect(rendered).toEqual(titles);
}

function setInputValue(id, value) {
  const input = document.getElementById(id);
  expect(input, `input #${id}`).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForCondition(condition, maxAttempts = 20) {
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (condition()) {
      return;
    }
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
  throw new Error('Timed out waiting for condition');
}

// Stub the tool-catalog RPC for the whitelist editor while keeping the model/
// connection/defaults catalogs the settings form needs.
function mockToolCatalog(toolNames, defaultProjectTools) {
  rpcMock.mockImplementation((method) => {
    if (method === 'model.list') {
      return Promise.resolve({ models: [] });
    }
    if (method === 'connection.list') {
      return Promise.resolve({ connections: [] });
    }
    if (method === 'settings.get') {
      return Promise.resolve({ defaults: { agent: {} } });
    }
    if (method === 'tool.list') {
      return Promise.resolve({
        tools: toolNames.map((tool) =>
          typeof tool === 'string'
            ? { name: tool, description: '' }
            : { description: '', ...tool },
        ),
        default_project_tools: defaultProjectTools,
      });
    }
    return Promise.resolve({});
  });
}

// Select the `demo` project in the list pane, opening its detail pane.
async function selectDemo() {
  await waitForCondition(() =>
    document.querySelector('[data-testid="project-toggle-demo"]'),
  );
  buttonByTestId('project-toggle-demo').click();
  flushSync();
}

function toggleByAriaLabel(label) {
  return document.querySelector(`button[aria-label="${label}"]`);
}

function setupProjectsViewSuite() {
  let mountedComponent;
  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;

    addProjectMock.mockReset();
    listProjectsMock.mockReset();
    showProjectMock.mockReset();
    setProjectMock.mockReset();
    removeProjectMock.mockReset();
    setOverrideMock.mockReset();
    clearOverrideMock.mockReset();
    rpcMock.mockReset();

    rpcMock.mockImplementation((method) => {
      if (method === 'model.list') {
        return Promise.resolve({ models: [] });
      }
      if (method === 'connection.list') {
        return Promise.resolve({ connections: [] });
      }
      if (method === 'settings.get') {
        return Promise.resolve({ defaults: { agent: {} } });
      }
      return Promise.resolve({});
    });

    listProjectsMock.mockResolvedValue({ projects: [] });
    addProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });
    showProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });
    setProjectMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });
    removeProjectMock.mockResolvedValue({ project_id: 'demo', archived: true });
    setOverrideMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });
    clearOverrideMock.mockResolvedValue({
      project: project({ project_id: 'demo' }),
      scan: { team: [], report: { clean: true, findings: [] } },
    });
  });
  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.restoreAllMocks();
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
  addProjectMock,
  listProjectsMock,
  showProjectMock,
  setProjectMock,
  removeProjectMock,
  setOverrideMock,
  clearOverrideMock,
  rpcMock,
  ProjectsView,
  AUTO_SAVE_WAIT_MS,
  project,
  member,
  buttonByTestId,
  buttonWithTextContent,
  confirmDialog,
  submitButtonInDialog,
  inputById,
  optionByText,
  expectSectionOrder,
  setInputValue,
  wait,
  waitForCondition,
  mockToolCatalog,
  selectDemo,
  toggleByAriaLabel,
  setupProjectsViewSuite,
};

export { flushSync, mount };
