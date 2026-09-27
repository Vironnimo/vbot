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

function cleanScan(overrides = {}) {
  return { team: [], report: { clean: true, findings: [] }, ...overrides };
}

// Serve one listed Project `demo` and its scan; returns the stored record.
function serveProject(fields = {}, scan = {}) {
  const record = project({
    project_id: 'demo',
    display_name: 'Demo',
    ...fields,
  });
  listProjectsMock.mockResolvedValue({ projects: [record] });
  showProjectMock.mockResolvedValue({ project: record, scan: cleanScan(scan) });
  return record;
}

// Answer the catalog RPCs the view loads on mount. `tools` entries may be bare
// names or partial tool objects.
function mockCatalogs({
  models = [],
  connections = [],
  settings = { defaults: { agent: {} } },
  tools = [],
  defaultProjectTools = [],
} = {}) {
  rpcMock.mockImplementation((method) => {
    if (method === 'model.list') {
      return Promise.resolve({ models });
    }
    if (method === 'connection.list') {
      return Promise.resolve({ connections });
    }
    if (method === 'settings.get') {
      return Promise.resolve(settings);
    }
    if (method === 'tool.list') {
      return Promise.resolve({
        tools: tools.map((tool) =>
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

function buttonByTestId(testId) {
  const button = document.querySelector(`[data-testid="${testId}"]`);
  expect(button, testId).toBeTruthy();
  return button;
}

function buttonWithTextContent(label, root = document) {
  const button = Array.from(root.querySelectorAll('button')).find(
    (item) => item.textContent.trim() === label,
  );
  expect(button, `button not found: ${label}`).toBeTruthy();
  return button;
}

function confirmDialog(label) {
  const footer = document.querySelector('.modal-footer');
  expect(footer, 'confirm dialog not open').toBeTruthy();
  buttonWithTextContent(label, footer).click();
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

function optionLabels() {
  return Array.from(document.querySelectorAll('[role="option"]')).map((item) =>
    item.textContent.trim(),
  );
}

// The ordered section titles must appear in the given sequence. The
// InfoHint "?" dot inside a title is presentation, not part of the title text.
function expectSectionOrder(titles) {
  const rendered = Array.from(
    document.querySelectorAll('.s-section__title'),
  ).map((node) => {
    const clone = node.cloneNode(true);
    clone.querySelectorAll('.info-hint').forEach((dot) => dot.remove());
    clone.querySelectorAll('button').forEach((button) => button.remove());
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

// Select the `demo` project in the list pane, opening its detail pane.
async function selectDemo() {
  await waitForCondition(() =>
    document.querySelector('[data-testid="project-toggle-demo"]'),
  );
  buttonByTestId('project-toggle-demo').click();
  flushSync();
}

// Expand a Team member row and wait for its detail.
async function expandMember(agentId) {
  await waitForCondition(() =>
    document.querySelector(`[data-testid="project-team-toggle-${agentId}"]`),
  );
  buttonByTestId(`project-team-toggle-${agentId}`).click();
  flushSync();
  await waitForCondition(() =>
    document.querySelector(
      `[data-testid="project-team-member-${agentId}"] .projects-team-detail`,
    ),
  );
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
    mockCatalogs();

    const saved = {
      project: project({ project_id: 'demo' }),
      scan: cleanScan(),
    };
    listProjectsMock.mockResolvedValue({ projects: [] });
    addProjectMock.mockResolvedValue(saved);
    showProjectMock.mockResolvedValue(saved);
    setProjectMock.mockResolvedValue(saved);
    removeProjectMock.mockResolvedValue({ project_id: 'demo', archived: true });
    setOverrideMock.mockResolvedValue(saved);
    clearOverrideMock.mockResolvedValue(saved);
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
    // Mount the view into the document; the suite unmounts it after the test.
    mount(props = {}) {
      mountedComponent = mount(ProjectsView, { target: document.body, props });
      flushSync();
      return mountedComponent;
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
  AUTO_SAVE_WAIT_MS,
  project,
  member,
  cleanScan,
  serveProject,
  mockCatalogs,
  buttonByTestId,
  buttonWithTextContent,
  confirmDialog,
  submitButtonInDialog,
  inputById,
  optionByText,
  optionLabels,
  expectSectionOrder,
  setInputValue,
  wait,
  waitForCondition,
  selectDemo,
  expandMember,
  toggleByAriaLabel,
  setupProjectsViewSuite,
};

export { flushSync };
