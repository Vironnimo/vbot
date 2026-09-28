// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

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

  function mountView(props = {}) {
    mountedComponent = mount(CronView, {
      target: document.body,
      props: { onToast: toastMock, ...props },
    });
    flushSync();
    return mountedComponent;
  }

  it('lists active, paused, failed, completed, and missed job history', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({
          id: 'job-active',
          name: 'Nightly summary job',
          prompt: 'Prompt content must stay out',
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
    expect(
      document.querySelector('.cron-list-scroll.secondary-list'),
    ).toBeTruthy();
    expect(
      document
        .querySelector('[data-testid="cron-item-job-active"]')
        .classList.contains('secondary-list__item'),
    ).toBe(true);
    expect(document.querySelector('.cron-bar')).toBeNull();
    expect(document.body.textContent).toContain('Nightly summary job');
    expect(
      document.querySelector(
        '[data-testid="cron-item-job-failed"] .chip.error',
      ),
    ).toBeTruthy();
    expect(
      document.querySelector('[data-testid="cron-item-job-missed"] .chip.warn'),
    ).toBeTruthy();
    const activeRow = document.querySelector(
      '[data-testid="cron-item-job-active"]',
    );
    expect(activeRow.querySelector('.cron-item-next')).toBeTruthy();
    expect(activeRow.textContent).not.toContain('Prompt content must stay out');
    expect(activeRow.textContent).not.toContain('Agent Alpha');
    expect(activeRow.textContent).not.toContain('*/30 * * * *');
    expect(activeRow.querySelector('.cron-item-prompt')).toBeNull();
    expect(activeRow.querySelector('.cron-item-schedule')).toBeNull();
    expect(document.getElementById('cron-job-timezone')).toBeNull();
  });

  it('auto-selects the first job so its detail form renders on load', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [cronJob({ id: 'job-first', status: 'active' })],
    });

    mountView();

    await waitForCondition(() => document.getElementById('cron-job-prompt'));

    // The first job is selected: status and Enabled remain in the header, while
    // deletion is available under Technical details and the editor is grouped.
    expect(
      document.querySelector('[data-testid="cron-toggle-job-first"]'),
    ).toBeTruthy();
    expect(
      document.querySelector('[data-testid="cron-delete-job-first"]'),
    ).toBeTruthy();
    expect(document.getElementById('cron-job-prompt').value).toBe(
      'Default cron prompt',
    );
    expect(document.getElementById('cron-job-name').value).toBe(
      'Default scheduled run',
    );
    expect(document.querySelectorAll('.cron-summary-item')).toHaveLength(4);
    expect(
      document.querySelectorAll('.cron-detail-scroll .s-section'),
    ).toHaveLength(3);
    expect(document.querySelector('.cron-technical-details')).toBeTruthy();
    expect(document.querySelector('.detail-sub').textContent).not.toContain(
      'job-first',
    );
    expect(
      document.querySelectorAll(
        '.cron-detail-scroll .s-group label.s-row-label',
      ).length,
    ).toBe(7);
  });

  it('disables the selected job and enables a paused job after selecting it', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({ id: 'job-active', status: 'active' }),
        cronJob({ id: 'job-paused', status: 'paused' }),
      ],
    });

    mountView();

    // The first (active) job is auto-selected with its toggle in the header.
    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-toggle-job-active"]'),
    );
    buttonByTestId('cron-toggle-job-active').click();
    await waitForCondition(() => disableCronJobMock.mock.calls.length === 1);
    expect(disableCronJobMock).toHaveBeenCalledWith('job-active');

    buttonByTestId('cron-item-job-paused').click();
    flushSync();
    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-toggle-job-paused"]'),
    );
    buttonByTestId('cron-toggle-job-paused').click();
    await waitForCondition(() => enableCronJobMock.mock.calls.length === 1);
    expect(enableCronJobMock).toHaveBeenCalledWith('job-paused');
  });

  it('creates a job from the blank form and updates it without selecting its list row again', async () => {
    const createdJob = cronJob({
      id: 'job-created',
      name: 'Morning digest',
      prompt: 'Prepare morning digest',
      cron_expression: '0 6 * * *',
    });
    listCronJobsMock
      .mockResolvedValueOnce({ jobs: [] })
      .mockResolvedValueOnce({ jobs: [createdJob] })
      .mockResolvedValue({ jobs: [createdJob] });

    mountView();

    await waitForCondition(() => {
      const button = findButtonByAriaLabel('Create schedule');
      return Boolean(button && !button.disabled);
    });
    buttonByAriaLabel('Create schedule').click();
    flushSync();

    await waitForCondition(() => document.getElementById('cron-job-prompt'));
    inputById('cron-job-name').value = 'Morning digest';
    inputById('cron-job-name').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    inputById('cron-job-prompt').value = 'Prepare morning digest';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    inputById('cron-job-expression').value = '0 6 * * *';
    inputById('cron-job-expression').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();

    buttonByText(t('common.save')).click();

    await waitForCondition(() => createCronJobMock.mock.calls.length === 1);
    expect(createCronJobMock).toHaveBeenCalledWith({
      agent_id: 'agent-alpha',
      name: 'Morning digest',
      prompt: 'Prepare morning digest',
      schedule_type: 'cron',
      cron_expression: '0 6 * * *',
    });
    await waitForCondition(() => listCronJobsMock.mock.calls.length === 2);
    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-delete-job-created"]'),
    );

    inputById('cron-job-prompt').value = 'Prepare updated digest';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    buttonByText(t('common.save')).click();

    await waitForCondition(() => updateCronJobMock.mock.calls.length === 1);
    expect(updateCronJobMock).toHaveBeenCalledWith({
      id: 'job-created',
      agent_id: 'agent-alpha',
      name: 'Morning digest',
      prompt: 'Prepare updated digest',
      schedule_type: 'cron',
      cron_expression: '0 6 * * *',
      repeat: null,
      session_id: null,
    });
  });

  it('creates a job from a schedule preset and keeps it saved when a later reload lists it', async () => {
    const createdJob = cronJob({
      id: 'job-created',
      name: '',
      prompt: 'Check the inbox',
      cron_expression: '0 * * * *',
    });
    listCronJobsMock
      .mockResolvedValueOnce({ jobs: [] })
      .mockResolvedValueOnce({ jobs: [] })
      .mockResolvedValue({ jobs: [createdJob] });
    const props = reactiveProps({ onToast: toastMock, cronRefreshToken: 0 });
    mountedComponent = mount(CronView, { target: document.body, props });
    flushSync();

    await waitForCondition(() => {
      const button = findButtonByAriaLabel('Create schedule');
      return Boolean(button && !button.disabled);
    });
    buttonByAriaLabel('Create schedule').click();
    flushSync();

    await waitForCondition(() => document.getElementById('cron-job-preset'));

    // Open the preset dropdown and pick "Every hour"; its expression fills the
    // still-editable cron field.
    document.getElementById('cron-job-preset').click();
    flushSync();
    const hourlyOption = Array.from(
      document.querySelectorAll('.dropdown-option'),
    ).find((option) => option.textContent.trim() === t('cron.presets.hourly'));
    expect(hourlyOption, 'preset option not found').toBeTruthy();
    hourlyOption.click();
    flushSync();

    expect(inputById('cron-job-expression').value).toBe('0 * * * *');

    inputById('cron-job-prompt').value = 'Check the inbox';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    buttonByText(t('common.save')).click();
    await waitForCondition(() => listCronJobsMock.mock.calls.length === 2);
    expect(createCronJobMock).toHaveBeenCalledWith(
      expect.objectContaining({
        prompt: 'Check the inbox',
        cron_expression: '0 * * * *',
      }),
    );

    // The first reload does not list the created job yet. When a later
    // reload does, its submitted values count as saved: no second write.
    props.cronRefreshToken += 1;
    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-item-job-created"]'),
    );
    buttonByText(t('common.save')).click();
    await waitForCondition(() =>
      toastMock.mock.calls.some(
        ([toast]) => toast.title === t('common.alreadySaved'),
      ),
    );
    expect(createCronJobMock).toHaveBeenCalledOnce();
    expect(updateCronJobMock).not.toHaveBeenCalled();
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
          remaining_runs: 1,
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
    buttonByText(t('common.save')).click();

    await waitForCondition(() => updateCronJobMock.mock.calls.length === 1);
    expect(updateCronJobMock).toHaveBeenCalledWith({
      id: 'job-once',
      agent_id: 'agent-alpha',
      name: 'Default scheduled run',
      prompt: 'Updated once prompt',
      schedule_type: 'once',
      run_at: storedRunAt,
      repeat: 1,
      session_id: 'session-preserve',
    });
  });

  it('keeps unsaved edits on reselecting the row and saves them before switching jobs', async () => {
    const jobs = [cronJob({ id: 'job-one' }), cronJob({ id: 'job-two' })];
    listCronJobsMock.mockResolvedValue({
      jobs,
    });
    updateCronJobMock.mockImplementation(async ({ id, prompt }) => {
      jobs.find((job) => job.id === id).prompt = prompt;
      return { ok: true };
    });
    mountView();
    await waitForCondition(() => document.getElementById('cron-job-prompt'));

    inputById('cron-job-prompt').value = 'Unsaved draft';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    buttonByTestId('cron-item-job-one').click();
    flushSync();
    expect(inputById('cron-job-prompt').value).toBe('Unsaved draft');
    expect(document.body.querySelector('.modal-footer')).toBeNull();
    expect(updateCronJobMock).not.toHaveBeenCalled();

    buttonByTestId('cron-item-job-two').click();
    flushSync();

    await waitForCondition(() => updateCronJobMock.mock.calls.length === 1);
    await waitForCondition(
      () => inputById('cron-job-prompt').value !== 'Unsaved draft',
    );
    expect(updateCronJobMock).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'job-one', prompt: 'Unsaved draft' }),
    );
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it('shows a contextual retry state when cron.list fails', async () => {
    listCronJobsMock.mockRejectedValue(new Error('server unavailable'));
    mountView();

    await waitForCondition(() => document.querySelector('.cron-load-error'));

    expect(document.body.textContent).toContain('server unavailable');
    expect(document.querySelector('.cron-list-scroll .empty-state')).toBeNull();
  });

  it('defers transport failures to the global outage notice while offline', async () => {
    rpcMock.mockRejectedValue(new Error('agent transport unavailable'));
    listCronJobsMock.mockRejectedValue(new Error('cron transport unavailable'));

    mountView({ serverUnavailable: true });

    await waitForCondition(() =>
      document.querySelector('.cron-list-scroll .empty-state'),
    );

    expect(
      document.querySelector('.cron-list-scroll .empty-state'),
    ).toBeTruthy();
    expect(document.querySelector('.cron-load-error')).toBeNull();
    expect(document.querySelector('.cron-list-state--warn')).toBeNull();
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

    expect(document.querySelector('.detail-btns .chip.neutral')).toBeTruthy();
    expect(document.body.textContent).toContain('run-default');
    expect(document.body.textContent).toContain(
      'Outcome recovered after restart',
    );
    expect(
      document.querySelector('[data-testid="cron-toggle-job-completed"]'),
    ).toBeNull();
  });

  it('deletes a job only after confirming the dialog', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [cronJob({ id: 'job-delete', status: 'active' })],
    });

    mountView();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-delete-job-delete"]'),
    );

    // The detail action opens the shared ConfirmDialog; nothing is deleted
    // until the user confirms.
    buttonByTestId('cron-delete-job-delete').click();
    flushSync();
    confirmDialog(t('common.cancel'));
    flushSync();
    expect(deleteCronJobMock).not.toHaveBeenCalled();
    expect(document.body.querySelector('.modal-footer')).toBeNull();

    buttonByTestId('cron-delete-job-delete').click();
    flushSync();
    confirmDialog(t('common.delete'));

    await waitForCondition(() => deleteCronJobMock.mock.calls.length === 1);
    expect(deleteCronJobMock).toHaveBeenCalledWith('job-delete');
  });
});

// Clicks a button in the open ConfirmDialog by its label.
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
    name: 'Default scheduled run',
    prompt: 'Default cron prompt',
    schedule_type: 'cron',
    cron_expression: '*/30 * * * *',
    run_at: null,
    remaining_runs: null,
    session_id: null,
    status: 'active',
    last_fired_at: '2026-05-14T10:00:00+00:00',
    last_attempt_at: '2026-05-14T10:00:00+00:00',
    last_completed_at: '2026-05-14T10:05:00+00:00',
    last_run_id: 'run-default',
    last_outcome: 'success',
    last_error: null,
    consecutive_failures: 0,
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

function buttonByAriaLabel(label) {
  const button = findButtonByAriaLabel(label);
  expect(button).toBeTruthy();
  return button;
}

function findButtonByAriaLabel(label) {
  return document.body.querySelector(`button[aria-label="${label}"]`);
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
