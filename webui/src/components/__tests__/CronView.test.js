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
  let toastMock;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    toastMock = vi.fn();

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

  function mountView() {
    mountedComponent = mount(CronView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();
    return mountedComponent;
  }

  it('lists active, paused, and failed jobs while filtering completed jobs', async () => {
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
          id: 'job-failed',
          prompt: 'Never ran',
          status: 'failed',
        }),
        cronJob({
          id: 'job-completed',
          prompt: 'Completed and hidden',
          status: 'completed',
        }),
      ],
    });

    mountView();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-item-job-active"]'),
    );

    // A failed once job stays visible (labelled Failed) so the user sees it
    // never ran; a successful completed fire is hidden from the list.
    expect(
      document.querySelector('[data-testid="cron-item-job-active"]'),
    ).toBeTruthy();
    expect(
      document.querySelector('[data-testid="cron-item-job-paused"]'),
    ).toBeTruthy();
    expect(
      document.querySelector('[data-testid="cron-item-job-failed"]'),
    ).toBeTruthy();
    expect(
      document.querySelector('[data-testid="cron-item-job-completed"]'),
    ).toBeFalsy();
    expect(document.body.textContent).toContain('Failed');
  });

  it('auto-selects the first job so its detail form renders on load', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [cronJob({ id: 'job-first', status: 'active' })],
    });

    mountView();

    await waitForCondition(() => document.getElementById('cron-job-prompt'));

    // The first job is selected: its toggle/delete controls live in the detail
    // header, and the prompt field is pre-filled from that job.
    expect(
      document.querySelector('[data-testid="cron-toggle-job-first"]'),
    ).toBeTruthy();
    expect(
      document.querySelector('[data-testid="cron-delete-job-first"]'),
    ).toBeTruthy();
    expect(document.getElementById('cron-job-prompt').value).toBe(
      'Default cron prompt',
    );
  });

  it('disables the selected job via the detail toggle', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [cronJob({ id: 'job-active', status: 'active' })],
    });

    mountView();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-toggle-job-active"]'),
    );

    buttonByTestId('cron-toggle-job-active').click();

    await waitForCondition(() => disableCronJobMock.mock.calls.length === 1);
    expect(disableCronJobMock).toHaveBeenCalledWith('job-active');
  });

  it('enables a paused job after selecting it', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({ id: 'job-active', status: 'active' }),
        cronJob({ id: 'job-paused', status: 'paused' }),
      ],
    });

    mountView();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-item-job-paused"]'),
    );

    // The first (active) job is auto-selected; select the paused one to bring
    // its enable toggle into the detail header.
    buttonByTestId('cron-item-job-paused').click();
    flushSync();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-toggle-job-paused"]'),
    );
    buttonByTestId('cron-toggle-job-paused').click();

    await waitForCondition(() => enableCronJobMock.mock.calls.length === 1);
    expect(enableCronJobMock).toHaveBeenCalledWith('job-paused');
  });

  it('creates a job from the blank create form through cron.create helper', async () => {
    listCronJobsMock.mockResolvedValue({ jobs: [] });

    mountView();

    await waitForCondition(() => {
      const button = findButtonByText('Add');
      return Boolean(button && !button.disabled);
    });

    buttonByText('Add').click();
    flushSync();

    await waitForCondition(() => document.getElementById('cron-job-prompt'));

    inputById('cron-job-prompt').value = 'Prepare morning digest';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );

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

  it('fills the cron expression from a schedule preset selection', async () => {
    listCronJobsMock.mockResolvedValue({ jobs: [] });

    mountView();

    await waitForCondition(() => {
      const button = findButtonByText('Add');
      return Boolean(button && !button.disabled);
    });
    buttonByText('Add').click();
    flushSync();

    await waitForCondition(() => document.getElementById('cron-job-preset'));

    // Open the preset dropdown and pick "Every hour"; its expression fills the
    // still-editable cron field.
    document.getElementById('cron-job-preset').click();
    flushSync();
    const hourlyOption = Array.from(
      document.querySelectorAll('.dropdown-option'),
    ).find((option) => option.textContent.trim() === 'Every hour');
    expect(hourlyOption, 'preset option not found').toBeTruthy();
    hourlyOption.click();
    flushSync();

    expect(inputById('cron-job-expression').value).toBe('0 * * * *');
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

    mountView();

    // The single job auto-selects, so its edit form is already rendered.
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

  it('calls cron.delete helper after confirming the dialog', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [cronJob({ id: 'job-delete', status: 'active' })],
    });

    mountView();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-delete-job-delete"]'),
    );

    buttonByTestId('cron-delete-job-delete').click();
    flushSync();

    // The detail action opens the shared ConfirmDialog; nothing is deleted until
    // the user confirms.
    expect(deleteCronJobMock).not.toHaveBeenCalled();
    confirmDialog('Delete');

    await waitForCondition(() => deleteCronJobMock.mock.calls.length === 1);
    expect(deleteCronJobMock).toHaveBeenCalledWith('job-delete');
  });

  it('does not delete a cron job when the dialog is cancelled', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [cronJob({ id: 'job-delete', status: 'active' })],
    });

    mountView();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-delete-job-delete"]'),
    );

    buttonByTestId('cron-delete-job-delete').click();
    flushSync();

    confirmDialog('Cancel');
    flushSync();

    expect(deleteCronJobMock).not.toHaveBeenCalled();
    expect(document.body.querySelector('.modal-footer')).toBeNull();
  });
});

// Clicks a button in the open ConfirmDialog by its label (Delete / Cancel).
function confirmDialog(label) {
  const footer = document.body.querySelector('.modal-footer');
  expect(footer, 'confirm dialog not open').toBeTruthy();
  const button = Array.from(footer.querySelectorAll('button')).find(
    (item) => item.textContent.trim() === label,
  );
  expect(button, `confirm button not found: ${label}`).toBeTruthy();
  button.click();
}

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
