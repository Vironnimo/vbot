// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();
const listCronJobsMock = vi.fn();
const createCronJobMock = vi.fn();
const updateCronJobMock = vi.fn();
const deleteCronJobMock = vi.fn();
const enableCronJobMock = vi.fn();
const disableCronJobMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
  listCronJobs: (...args) => listCronJobsMock(...args),
  createCronJob: (...args) => createCronJobMock(...args),
  updateCronJob: (...args) => updateCronJobMock(...args),
  deleteCronJob: (...args) => deleteCronJobMock(...args),
  enableCronJob: (...args) => enableCronJobMock(...args),
  disableCronJob: (...args) => disableCronJobMock(...args),
}));

const { default: CronView } = await import('../CronView.svelte');

describe('CronView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;

    rpcMock.mockReset();
    listCronJobsMock.mockReset();
    createCronJobMock.mockReset();
    updateCronJobMock.mockReset();
    deleteCronJobMock.mockReset();
    enableCronJobMock.mockReset();
    disableCronJobMock.mockReset();

    rpcMock.mockImplementation(createAgentListRpcMock());
    listCronJobsMock.mockResolvedValue({ jobs: [] });
    createCronJobMock.mockResolvedValue({ id: 'job-created' });
    updateCronJobMock.mockResolvedValue({ ok: true });
    deleteCronJobMock.mockResolvedValue({ ok: true });
    enableCronJobMock.mockResolvedValue({ ok: true });
    disableCronJobMock.mockResolvedValue({ ok: true });
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  it('renders active and paused jobs while filtering completed jobs', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({
          id: 'job-active',
          prompt: 'Nightly summary',
          status: 'active',
        }),
        cronJob({
          id: 'job-paused',
          prompt: 'Pause me',
          status: 'paused',
        }),
        cronJob({
          id: 'job-completed',
          prompt: 'Completed and hidden',
          status: 'completed',
        }),
      ],
    });

    mountedComponent = mount(CronView, { target: document.body });
    flushSync();

    await waitForCondition(() => document.body.textContent.includes('Nightly summary'));

    expect(document.body.textContent).toContain('Nightly summary');
    expect(document.body.textContent).toContain('Pause me');
    expect(document.body.textContent).not.toContain('Completed and hidden');
    expect(document.querySelector('[data-testid="cron-toggle-job-active"]')).toBeTruthy();
    expect(document.querySelector('[data-testid="cron-toggle-job-paused"]')).toBeTruthy();
    expect(
      document.querySelector('[data-testid="cron-toggle-job-completed"]'),
    ).toBeFalsy();
  });

  it('calls disable and enable RPC helpers from row toggles', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({ id: 'job-active', status: 'active' }),
        cronJob({ id: 'job-paused', status: 'paused' }),
      ],
    });

    mountedComponent = mount(CronView, { target: document.body });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-toggle-job-active"]'),
    );

    buttonByTestId('cron-toggle-job-active').click();

    await waitForCondition(() => disableCronJobMock.mock.calls.length === 1);
    expect(disableCronJobMock).toHaveBeenCalledWith('job-active');

    buttonByTestId('cron-toggle-job-paused').click();

    await waitForCondition(() => enableCronJobMock.mock.calls.length === 1);
    expect(enableCronJobMock).toHaveBeenCalledWith('job-paused');
  });

  it('submits create modal form through cron.create helper', async () => {
    listCronJobsMock.mockResolvedValue({ jobs: [] });

    mountedComponent = mount(CronView, { target: document.body });
    flushSync();

    await waitForCondition(() => {
      const button = findButtonByText('New Job');
      return Boolean(button && !button.disabled);
    });

    buttonByText('New Job').click();
    flushSync();

    await waitForCondition(() => document.getElementById('cron-job-prompt'));

    inputById('cron-job-prompt').value = 'Prepare morning digest';
    inputById('cron-job-prompt').dispatchEvent(new Event('input', { bubbles: true }));

    inputById('cron-job-expression').value = '0 6 * * *';
    inputById('cron-job-expression').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();

    buttonByText('Save').click();

    await waitForCondition(() => createCronJobMock.mock.calls.length === 1);
    expect(createCronJobMock).toHaveBeenCalledWith({
      agent_id: 'agent-alpha',
      prompt: 'Prepare morning digest',
      schedule_type: 'cron',
      cron_expression: '0 6 * * *',
    });
  });

  it('keeps once run_at and session_id when saving an unchanged edit', async () => {
    const storedRunAt = '2026-05-14T10:00:00+00:00';

    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({
          id: 'job-once',
          schedule_type: 'once',
          cron_expression: null,
          run_at: storedRunAt,
          timezone: 'UTC',
          session_id: 'session-preserve',
        }),
      ],
    });

    mountedComponent = mount(CronView, { target: document.body });
    flushSync();

    await waitForCondition(() => document.querySelector('[data-testid="cron-edit-job-once"]'));

    buttonByTestId('cron-edit-job-once').click();
    flushSync();

    await waitForCondition(() => document.getElementById('cron-job-run-at'));
    const runAtInput = inputById('cron-job-run-at');
    expect(runAtInput.value.length).toBeGreaterThan(0);

    buttonByText('Save').click();

    await waitForCondition(() => updateCronJobMock.mock.calls.length === 1);
    expect(updateCronJobMock).toHaveBeenCalledWith({
      id: 'job-once',
      agent_id: 'agent-alpha',
      prompt: 'Default cron prompt',
      schedule_type: 'once',
      run_at: storedRunAt,
      timezone: 'UTC',
      session_id: 'session-preserve',
    });
  });

  it('calls cron.delete helper after confirmation', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [cronJob({ id: 'job-delete', status: 'active' })],
    });

    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);

    mountedComponent = mount(CronView, { target: document.body });
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-delete-job-delete"]'),
    );

    buttonByTestId('cron-delete-job-delete').click();

    await waitForCondition(() => deleteCronJobMock.mock.calls.length === 1);
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(deleteCronJobMock).toHaveBeenCalledWith('job-delete');
  });
});

function createAgentListRpcMock(agents = defaultAgents()) {
  return async (method) => {
    if (method === 'agent.list') {
      return { agents };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

function defaultAgents() {
  return [
    {
      id: 'agent-alpha',
      name: 'Agent Alpha',
    },
    {
      id: 'agent-beta',
      name: 'Agent Beta',
    },
  ];
}

function cronJob(overrides = {}) {
  return {
    id: 'job-default',
    agent_id: 'agent-alpha',
    prompt: 'Default cron prompt',
    schedule_type: 'cron',
    cron_expression: '*/30 * * * *',
    run_at: null,
    timezone: 'UTC',
    session_id: null,
    status: 'active',
    last_fired_at: '2026-05-14T10:00:00+00:00',
    next_fire_at: '2026-05-14T10:30:00+00:00',
    created_at: '2026-05-14T09:00:00+00:00',
    ...overrides,
  };
}

function buttonByText(label) {
  const button = findButtonByText(label);
  expect(button).toBeTruthy();
  return button;
}

function findButtonByText(label) {
  return Array.from(document.body.querySelectorAll('button')).find((item) =>
    item.textContent?.includes(label),
  );
}

function buttonByTestId(testId) {
  const button = document.querySelector(`[data-testid="${testId}"]`);
  expect(button).toBeTruthy();
  return button;
}

function inputById(id) {
  const input = document.getElementById(id);
  expect(input).toBeTruthy();
  return input;
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