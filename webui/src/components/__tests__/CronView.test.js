// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';

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

vi.mock('$lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    listCronJobs: (...args) => listCronJobsMock(...args),
    createCronJob: (...args) => createCronJobMock(...args),
    updateCronJob: (...args) => updateCronJobMock(...args),
    deleteCronJob: (...args) => deleteCronJobMock(...args),
    enableCronJob: (...args) => enableCronJobMock(...args),
    disableCronJob: (...args) => disableCronJobMock(...args),
  }),
);

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

  it('lists active, paused, failed, completed, and missed job history', async () => {
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
          prompt: 'Completed history',
          status: 'completed',
        }),
        cronJob({
          id: 'job-missed',
          prompt: 'Missed history',
          status: 'missed',
          last_outcome: 'missed',
        }),
      ],
    });

    mountView();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-item-job-active"]'),
    );

    // Terminal jobs remain visible and deletable as execution history.
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
    ).toBeTruthy();
    expect(
      document.querySelector('[data-testid="cron-item-job-missed"]'),
    ).toBeTruthy();
    expect(document.body.textContent).toContain('Failed');
    expect(document.body.textContent).toContain('Missed');
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
      timezone: 'UTC',
    });
  });

  it('can update a newly created job without selecting its list row again', async () => {
    const createdJob = cronJob({
      id: 'job-created',
      prompt: 'Prepare morning digest',
      cron_expression: '0 6 * * *',
    });
    listCronJobsMock
      .mockResolvedValueOnce({ jobs: [] })
      .mockResolvedValueOnce({ jobs: [createdJob] })
      .mockResolvedValue({ jobs: [createdJob] });

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

    await waitForCondition(() => listCronJobsMock.mock.calls.length === 2);
    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-delete-job-created"]'),
    );

    inputById('cron-job-prompt').value = 'Prepare updated digest';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    buttonByText('Save').click();

    await waitForCondition(() => updateCronJobMock.mock.calls.length === 1);
    expect(updateCronJobMock).toHaveBeenCalledWith({
      id: 'job-created',
      agent_id: 'agent-alpha',
      prompt: 'Prepare updated digest',
      schedule_type: 'cron',
      cron_expression: '0 6 * * *',
      timezone: 'UTC',
      session_id: null,
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

  it('keeps once run_at and session_id when saving another field', async () => {
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

    inputById('cron-job-prompt').value = 'Updated once prompt';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    buttonByText('Save').click();

    await waitForCondition(() => updateCronJobMock.mock.calls.length === 1);
    expect(updateCronJobMock).toHaveBeenCalledWith({
      id: 'job-once',
      agent_id: 'agent-alpha',
      prompt: 'Updated once prompt',
      schedule_type: 'once',
      run_at: storedRunAt,
      timezone: 'UTC',
      session_id: 'session-preserve',
    });
  });

  it('does not reset unsaved edits when the selected row is clicked again', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [cronJob({ id: 'job-editing' })],
    });
    mountView();
    await waitForCondition(() => document.getElementById('cron-job-prompt'));

    inputById('cron-job-prompt').value = 'Unsaved draft';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    buttonByTestId('cron-item-job-editing').click();
    flushSync();

    expect(inputById('cron-job-prompt').value).toBe('Unsaved draft');
    expect(document.body.querySelector('.modal-footer')).toBeNull();
  });

  it('asks before switching away from unsaved edits', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [cronJob({ id: 'job-one' }), cronJob({ id: 'job-two' })],
    });
    mountView();
    await waitForCondition(() => document.getElementById('cron-job-prompt'));

    inputById('cron-job-prompt').value = 'Unsaved draft';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    buttonByTestId('cron-item-job-two').click();
    flushSync();

    expect(document.body.textContent).toContain('Discard unsaved changes?');
    confirmDialog('Cancel');
    flushSync();
    expect(inputById('cron-job-prompt').value).toBe('Unsaved draft');
  });

  it('shows a contextual retry state when cron.list fails', async () => {
    listCronJobsMock.mockRejectedValue(new Error('server unavailable'));
    mountView();

    await waitForCondition(() => document.body.textContent.includes('Retry'));

    expect(document.body.textContent).toContain('server unavailable');
    expect(document.body.textContent).not.toContain('No scheduled jobs');
  });

  it('shows execution health and disables toggling terminal history', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({
          id: 'job-completed',
          status: 'completed',
          last_outcome: 'success',
          last_error: 'Outcome recovered after restart',
        }),
      ],
    });
    mountView();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-delete-job-completed"]'),
    );

    expect(document.body.textContent).toContain('Succeeded');
    expect(document.body.textContent).toContain('run-default');
    expect(document.body.textContent).toContain(
      'Outcome recovered after restart',
    );
    expect(
      document.querySelector('[data-testid="cron-toggle-job-completed"]'),
    ).toBeNull();
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
    last_attempt_at: '2026-05-14T10:00:00+00:00',
    last_completed_at: '2026-05-14T10:05:00+00:00',
    last_run_id: 'run-default',
    last_outcome: 'success',
    last_error: null,
    consecutive_failures: 0,
    effective_timezone: 'UTC',
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
