// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { createAutosaveCoordinator } from '../../lib/autosave.js';
import { createStandaloneNavigation } from '../../lib/navigation.svelte.js';
import { rpcBackedApiMock } from './apiMock.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const rpcMock = vi.fn();
const listCronJobsMock = vi.fn();
const createCronJobMock = vi.fn();
const updateCronJobMock = vi.fn();
const deleteCronJobMock = vi.fn();
const enableCronJobMock = vi.fn();
const disableCronJobMock = vi.fn();
const retryCronDeliveryMock = vi.fn();
const acknowledgeCronAlertMock = vi.fn();

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
    retryCronDelivery: (...args) => retryCronDeliveryMock(...args),
    acknowledgeCronAlert: (...args) => acknowledgeCronAlertMock(...args),
  }),
);

const { default: CronView } = await import('../CronView.svelte');
const { default: AutosaveContextHost } =
  await import('./AutosaveContextHost.support.svelte');

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
    retryCronDeliveryMock.mockReset();
    acknowledgeCronAlertMock.mockReset();

    rpcMock.mockImplementation(createAgentListRpcMock());
    listCronJobsMock.mockResolvedValue({ jobs: [] });
    createCronJobMock.mockResolvedValue({ id: 'job-created' });
    updateCronJobMock.mockResolvedValue({ ok: true });
    deleteCronJobMock.mockResolvedValue({ ok: true });
    enableCronJobMock.mockResolvedValue({ ok: true });
    disableCronJobMock.mockResolvedValue({ ok: true });
    retryCronDeliveryMock.mockResolvedValue({ ok: true });
    acknowledgeCronAlertMock.mockResolvedValue({ ok: true });
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    vi.useRealTimers();
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

  it('requires an explicit Project for new Sessions and pins a chosen Session with its own Model settings', async () => {
    const pinned = {
      id: 'ses-pinned',
      auto_title: 'Release investigation',
      working_project_id: 'release',
      agent_overrides: { model: 'fake/efficient', thinking_effort: 'low' },
    };
    rpcMock.mockImplementation(async (method) => {
      if (method === 'agent.list')
        return {
          agents: [
            {
              id: 'agent-alpha',
              name: 'Alpha',
              model: 'fake/general',
              root_project_id: 'other',
            },
          ],
        };
      if (method === 'project.list')
        return {
          projects: [
            {
              project_id: 'release',
              display_name: 'Release project',
              scan: { team: [] },
            },
            {
              project_id: 'other',
              display_name: 'Other project',
              scan: { team: [] },
            },
          ],
        };
      if (method === 'session.list')
        return { sessions: [pinned], next_cursor: null };
      if (method === 'session.get') return { session: pinned };
      if (method === 'channel.list') return { channels: [] };
      if (method === 'model.list')
        return {
          models: [
            {
              id: 'fake/efficient',
              provider_id: 'fake',
              effective_context_window: 128000,
              capabilities: {
                tools: true,
                reasoning: {
                  supported: true,
                  mandatory: true,
                  levels: ['low', 'high'],
                },
              },
            },
          ],
        };
      if (method === 'connection.list')
        return {
          connections: [
            { id: 'fake/default', provider_id: 'fake', usable: true },
          ],
        };
      throw new Error(`Unexpected RPC ${method}`);
    });
    mountView({ navigation: createStandaloneNavigation(['webhooks', 'new']) });
    await waitForCondition(() =>
      document.getElementById('cron-working-project'),
    );
    inputById('cron-job-prompt').value = 'Handle this event';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    buttonByText(t('common.save')).click();
    flushSync();
    expect(createCronJobMock).not.toHaveBeenCalled();
    expect(
      document.getElementById('cron-working-project').textContent,
    ).toContain(t('cron.execution.selectProject'));

    chooseOption('cron-working-project', t('cron.execution.workspace'));
    chooseOption('cron-session-mode', t('cron.execution.existingSession'));
    await waitForCondition(() =>
      rpcMock.mock.calls.some(([method]) => method === 'session.list'),
    );
    await waitForCondition(
      () =>
        !document
          .getElementById('cron-job-session')
          .textContent.includes(t('common.loading')),
    );
    chooseOption('cron-job-session', pinned.auto_title);
    await waitForCondition(() =>
      document
        .querySelector('[data-testid="cron-fixed-project"]')
        .textContent.includes('Release project'),
    );
    expect(document.getElementById('cron-working-project')).toBeNull();
    const advanced = document.querySelector('.cron-advanced');
    advanced.open = true;
    advanced.dispatchEvent(new Event('toggle'));
    flushSync();
    await waitForCondition(() =>
      rpcMock.mock.calls.some(([method]) => method === 'model.list'),
    );
    expect(document.getElementById('cron-run-model').textContent).toContain(
      'fake/efficient',
    );
    chooseOption('cron-run-effort', t('agents.form.thinkingEffortOption.high'));
    buttonByText(t('common.save')).click();
    await waitForCondition(() => createCronJobMock.mock.calls.length === 1);
    const payload = createCronJobMock.mock.calls[0][0];
    expect(payload).toMatchObject({
      schedule_type: 'webhook',
      session_id: 'ses-pinned',
      run_overrides: { thinking_effort: 'high' },
    });
    expect(payload).not.toHaveProperty('working_project_id');
    expect(
      rpcMock.mock.calls.some(([method]) =>
        ['session.set_agent_overrides', 'agent.update'].includes(method),
      ),
    ).toBe(false);
  });

  it('uses the Project Agent effective Model to offer only its supported Reasoning levels', async () => {
    const fallback = createAgentListRpcMock();
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'agent.list') return { agents: [] };
      if (method === 'project.list')
        return {
          projects: [
            {
              project_id: 'work',
              display_name: 'Work',
              scan: {
                team: [
                  {
                    agent_id: 'builder',
                    display_name: 'Builder',
                    effective: {
                      model: { value: 'fake/reasoner' },
                      thinking_effort: { value: 'low' },
                    },
                  },
                ],
              },
            },
          ],
        };
      if (method === 'model.list')
        return {
          models: [
            {
              id: 'fake/reasoner',
              provider_id: 'fake',
              capabilities: {
                reasoning: {
                  supported: true,
                  mandatory: true,
                  levels: ['low'],
                },
              },
            },
          ],
        };
      if (method === 'connection.list') return { connections: [] };
      return fallback(method, params);
    });
    mountView({ navigation: createStandaloneNavigation(['schedules', 'new']) });
    await waitForCondition(() => document.getElementById('cron-form-agent'));
    await waitForCondition(() =>
      rpcMock.mock.calls.some(([method]) => method === 'project.list'),
    );
    chooseOption('cron-form-agent', 'builder@work');
    const advanced = document.querySelector('.cron-advanced');
    advanced.open = true;
    advanced.dispatchEvent(new Event('toggle'));
    flushSync();
    await waitForCondition(() =>
      rpcMock.mock.calls.some(([method]) => method === 'model.list'),
    );
    await waitForCondition(() =>
      document
        .getElementById('cron-run-model')
        .textContent.includes('fake/reasoner'),
    );
    document.getElementById('cron-run-effort').click();
    flushSync();
    const choices = [...document.querySelectorAll('[role="option"]')].map(
      (option) => option.textContent.trim(),
    );
    expect(choices).toContain(t('agents.form.thinkingEffortOption.low'));
    expect(choices).not.toContain(t('agents.form.thinkingEffortOption.high'));
    expect(choices).not.toContain(t('agents.form.thinkingEffortOption.none'));
    expect(document.getElementById('cron-working-project')).toBeNull();
  });

  it('lets legacy jobs change notifications but requires a fixed Project before an execution edit', async () => {
    const legacy = cronJob();
    delete legacy.working_project_id;
    updateCronJobMock.mockImplementation(async (changes) => {
      Object.assign(legacy, changes);
      return { ok: true };
    });
    listCronJobsMock.mockResolvedValue({ jobs: [legacy] });
    mountView();
    await waitForCondition(() => document.getElementById('cron-job-prompt'));
    buttonByAriaLabel(t('notifications.label')).click();
    flushSync();
    const muted = [...document.querySelectorAll('[role="option"]')].find(
      (option) => option.textContent.trim() === t('notifications.muted'),
    );
    muted.click();
    flushSync();
    buttonByText(t('common.save')).click();
    await waitForCondition(() => updateCronJobMock.mock.calls.length === 1);
    expect(updateCronJobMock).toHaveBeenLastCalledWith({
      id: legacy.id,
      notification_policy: 'muted',
    });
    inputById('cron-job-prompt').value = 'Changed execution';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    await waitForCondition(() => findButtonByText(t('common.save')));
    buttonByText(t('common.save')).click();
    flushSync();
    expect(updateCronJobMock).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain(
      t('cron.execution.chooseProject'),
    );
  });

  it('opens five recent Runs, pages more, and reads only on an explicit action', async () => {
    const job = cronJob({ id: 'job-history', name: 'Daily review' });
    listCronJobsMock.mockResolvedValue({ jobs: [job] });
    const runs = Array.from({ length: 7 }, (_, i) => ({
      run_id: `run-${7 - i}`,
      session_id: `session-${7 - i}`,
      agent_id: 'coder',
      project_id: 'work',
      title: `Result ${7 - i}`,
      status: 'completed',
      started_at: '2026-10-24T10:00:00Z',
      unread: true,
    }));
    const fallback = createAgentListRpcMock();
    rpcMock.mockImplementation((method, params) => {
      if (method === 'cron.runs')
        return Promise.resolve({
          runs: runs.slice(0, params.limit),
          next_before: params.limit < runs.length ? 3 : null,
        });
      if (method === 'cron.mark_read') {
        runs.forEach((run) => {
          run.unread = false;
        });
        return Promise.resolve({ ok: true });
      }
      return fallback(method, params);
    });
    const onOpenSession = vi.fn();
    const onRefreshActivity = vi.fn();
    mountView({
      onOpenSession,
      onRefreshActivity,
      scheduleActivity: { 'job-history': { unread_count: 7, read_through: 7 } },
    });
    await waitForCondition(() => document.querySelector('.cron-expand'));
    expect(rpcMock.mock.calls.some(([method]) => method === 'cron.runs')).toBe(
      false,
    );
    document.querySelector('.cron-expand').click();
    flushSync();
    await waitForCondition(
      () => document.querySelectorAll('.cron-run').length === 5,
    );
    expect(
      rpcMock.mock.calls.some(([method]) => method === 'cron.mark_read'),
    ).toBe(false);
    const more = [...document.querySelectorAll('.cron-runs button')].find(
      (button) => button.textContent.includes(t('cron.runs.more')),
    );
    more.click();
    await waitForCondition(
      () => document.querySelectorAll('.cron-run').length === 7,
    );
    document.querySelectorAll('.cron-run')[6].click();
    expect(onOpenSession).toHaveBeenCalledWith(
      'coder@work',
      'session-1',
      false,
      'run-1',
    );
    const read = [...document.querySelectorAll('.cron-runs button')].find(
      (button) => button.textContent.includes(t('cron.runs.read')),
    );
    read.click();
    await waitForCondition(() => onRefreshActivity.mock.calls.length === 1);
    expect(rpcMock).toHaveBeenCalledWith('cron.mark_read', {
      id: 'job-history',
      through: 7,
    });
    expect(document.querySelectorAll('.cron-runs .unread-dot')).toHaveLength(0);
  });

  it('lists active, paused, failed, completed, and missed job history', async () => {
    // Row times are relative to the local date in the schedule timezone.
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-05-14T08:00:00Z'));
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
          cron_expression: '0 9 * * 1-5',
        }),
        cronJob({
          id: 'job-failed',
          prompt: 'Never ran',
          status: 'failed',
          last_outcome: 'failed',
          last_error: 'Provider timed out',
          consecutive_failures: 3,
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
    // Status is a labelled dot; the name keeps the full row width.
    const rowDetail = (id) =>
      document
        .querySelector(`[data-testid="cron-item-${id}"] .cron-item-detail`)
        .textContent.trim();
    const rowDot = (id) =>
      document.querySelector(
        `[data-testid="cron-item-${id}"] .cron-status-dot`,
      );
    expect(document.querySelector('.cron-list .chip')).toBeNull();
    expect(rowDot('job-failed').classList).toContain('cron-status-dot--error');
    expect(rowDot('job-failed').getAttribute('aria-label')).toBe('Failed');
    expect(rowDot('job-missed').classList).toContain('cron-status-dot--warn');
    expect(rowDetail('job-active')).toBe('Next Today 10:30');
    expect(rowDetail('job-paused')).toBe('Paused · Weekdays at 09:00');
    expect(rowDetail('job-failed')).toBe('Failed · Every 30 minutes');
    expect(rowDetail('job-completed')).toBe('Completed · Today 10:05');
    expect(rowDetail('job-missed')).toBe('Missed');
    const activeRow = document.querySelector(
      '[data-testid="cron-item-job-active"]',
    );
    expect(activeRow.textContent).not.toContain('Prompt content must stay out');
    expect(activeRow.textContent).not.toContain('Agent Alpha');
    expect(activeRow.textContent).not.toContain('*/30 * * * *');
    expect(activeRow.querySelector('.cron-item-prompt')).toBeNull();
    expect(activeRow.querySelector('.cron-item-schedule')).toBeNull();
    expect(document.getElementById('cron-job-timezone')).toBeNull();

    // The row's details card explains the dot and carries what the row omits.
    document.querySelector('[data-testid="cron-item-job-failed"]').focus();
    flushSync();
    const card = document.getElementById('app-tooltip');
    expect(card.querySelector('.app-tooltip__title').textContent).toBe(
      'Default scheduled run',
    );
    expect(card.querySelector('.app-tooltip__text').textContent).toBe(
      'Provider timed out',
    );
    const cardRows = Object.fromEntries(
      [...card.querySelectorAll('dt')].map((term) => [
        term.textContent,
        term.nextElementSibling,
      ]),
    );
    expect(cardRows[t('cron.card.status')].textContent).toBe(
      t('cron.card.statusFailures', { status: 'Failed', count: 3 }),
    );
    expect(cardRows[t('cron.card.status')].classList).toContain(
      'app-tooltip__value--danger',
    );
    expect(cardRows[t('cron.card.expression')].textContent).toBe(
      '*/30 * * * *',
    );
    expect(cardRows[t('cron.detail.lastResult')].textContent).toContain(
      t('cron.outcome.failed'),
    );
    expect(cardRows[t('cron.detail.target')].textContent).toBe('Agent Alpha');
    expect(cardRows[t('cron.card.session')].textContent).toBe(
      t('cron.detail.newSessionEachRun'),
    );
    expect(cardRows[t('cron.card.delivery')].textContent).toBe(
      t('cron.delivery.none'),
    );
    expect(card.dataset.floatingSide).toBe('right');
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
    expect(document.getElementById('cron-execution-title')).toBeTruthy();
    expect(document.querySelector('.cron-technical-details')).toBeTruthy();
    // The Enabled switch states active or paused; no duplicate chip.
    expect(document.querySelector('.detail-btns .chip')).toBeNull();
    // An expression the planner cannot express stays editable as Custom.
    expect(inputById('cron-job-expression').value).toBe('*/30 * * * *');
  });

  it('shows the job named by the place and records job and new-job steps in it', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({ id: 'job-one' }),
        cronJob({ id: 'job-two', prompt: 'Second prompt' }),
        cronJob({ id: 'job-hook', schedule_type: 'webhook' }),
      ],
    });
    const navigation = createStandaloneNavigation(['job-two']);
    mountView({ navigation });
    await waitForCondition(
      () =>
        document.getElementById('cron-job-prompt')?.value === 'Second prompt',
    );

    buttonByTestId('cron-item-job-one').click();
    flushSync();
    expect(navigation.place).toEqual(['schedules', 'job-one']);
    expect(inputById('cron-job-prompt').value).toBe('Default cron prompt');

    buttonByAriaLabel('Create schedule').click();
    flushSync();
    expect(navigation.place).toEqual(['schedules', 'new']);
    expect(inputById('cron-job-prompt').value).toBe('');

    // Cancel goes up to the job the new job's form was opened from.
    buttonByText(t('common.cancel')).click();
    flushSync();
    expect(navigation.place).toEqual(['schedules', 'job-one']);

    // The empty place (the view's start page) names the first job.
    navigation.navigate([]);
    flushSync();
    expect(navigation.place).toEqual(['schedules', 'job-one']);

    // Categories are page-level destinations, and each Create uses its trigger.
    document.getElementById('automation-categories-tab-webhooks').click();
    flushSync();
    expect(navigation.place).toEqual(['webhooks', 'job-hook']);
    expect(
      document.querySelector('[data-testid="cron-item-job-one"]'),
    ).toBeNull();
    expect(
      document.querySelector('[data-testid="cron-item-job-hook"]'),
    ).toBeTruthy();
    buttonByAriaLabel(t('cron.detail.createWebhookTitle')).click();
    flushSync();
    expect(navigation.place).toEqual(['webhooks', 'new']);
    expect(document.getElementById('cron-job-frequency')).toBeNull();
    expect(document.getElementById('cron-section-webhook')).toBeTruthy();

    navigation.navigate(['schedules', 'job-two']);
    flushSync();
    expect(inputById('cron-job-prompt').value).toBe('Second prompt');
  });

  it('keeps a changed new job when its form is left and discards it only on Cancel', async () => {
    listCronJobsMock.mockResolvedValue({ jobs: [cronJob({ id: 'job-one' })] });
    let keptDraft = null;
    let navigation = createStandaloneNavigation(['new']);
    const props = {
      onToast: toastMock,
      get navigation() {
        return navigation;
      },
      get newJobDraft() {
        return keptDraft;
      },
      set newJobDraft(value) {
        keptDraft = value;
      },
    };
    const prompt = () => document.getElementById('cron-job-prompt');
    // Mounted with the accessors themselves, as `bind:newJobDraft` passes them.
    const mountWithDraft = () => {
      mountedComponent = mount(CronView, { target: document.body, props });
      flushSync();
    };
    mountWithDraft();
    await waitForCondition(() => prompt()?.value === '');
    prompt().value = 'Unfinished prompt';
    prompt().dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    // Choosing a job leaves the form without asking.
    buttonByTestId('cron-item-job-one').click();
    flushSync();
    expect(navigation.place).toEqual(['schedules', 'job-one']);
    expect(document.querySelector('[role="dialog"]')).toBeNull();

    // The view closes (another main view) and reopens on the new job's form.
    await unmount(mountedComponent);
    expect(keptDraft?.prompt).toBe('Unfinished prompt');
    navigation = createStandaloneNavigation(['new']);
    mountWithDraft();
    await waitForCondition(() => prompt()?.value === 'Unfinished prompt');

    buttonByText(t('common.cancel')).click();
    flushSync();
    confirmDialog(t('common.discard'));
    flushSync();
    expect(keptDraft).toBeNull();
    buttonByAriaLabel('Create schedule').click();
    flushSync();
    await waitForCondition(() => prompt()?.value === '');
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

  it('enables and deletes jobs from their row context menu without selecting them', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({ id: 'job-active', status: 'active' }),
        cronJob({ id: 'job-paused', status: 'paused' }),
        cronJob({
          id: 'job-completed',
          name: 'Launch reminder',
          status: 'completed',
        }),
      ],
    });
    mountView();
    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-toggle-job-active"]'),
    );

    const openRowMenu = (jobId) => {
      const event = new MouseEvent('contextmenu', {
        bubbles: true,
        cancelable: true,
        clientX: 40,
        clientY: 80,
      });
      buttonByTestId(`cron-item-${jobId}`).dispatchEvent(event);
      flushSync();
      expect(event.defaultPrevented).toBe(true);
      return [...document.body.querySelectorAll('[role="menuitem"]')];
    };
    const labels = (items) => items.map((item) => item.textContent.trim());

    let items = openRowMenu('job-paused');
    expect(labels(items)).toEqual(['Enable', 'Delete…']);
    items[0].click();
    await waitForCondition(() => enableCronJobMock.mock.calls.length === 1);
    expect(enableCronJobMock).toHaveBeenCalledWith('job-paused');

    // A completed job can no longer be switched on or off.
    items = openRowMenu('job-completed');
    expect(labels(items)).toEqual(['Delete…']);
    items[0].click();
    flushSync();
    expect(
      document.body.querySelector('[role="dialog"]').textContent,
    ).toContain('Launch reminder');
    confirmDialog(t('common.delete'));
    await waitForCondition(() => deleteCronJobMock.mock.calls.length === 1);
    expect(deleteCronJobMock).toHaveBeenCalledWith('job-completed');
    // The shown job stays selected.
    expect(
      document.querySelector('[data-testid="cron-toggle-job-active"]'),
    ).toBeTruthy();
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
    const navigation = createStandaloneNavigation();

    mountView({ navigation });

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
    // A new schedule starts as a daily planner schedule.
    inputById('cron-job-time').value = '06:00';
    inputById('cron-job-time').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();

    chooseOption('cron-working-project', t('cron.execution.workspace'));

    buttonByText(t('common.save')).click();

    await waitForCondition(() => createCronJobMock.mock.calls.length === 1);
    expect(createCronJobMock).toHaveBeenCalledWith({
      agent_id: 'agent-alpha',
      name: 'Morning digest',
      prompt: 'Prepare morning digest',
      schedule_type: 'cron',
      cron_expression: '0 6 * * *',
      working_project_id: null,
      notification_policy: 'all',
      delivery: { kind: 'app' },
    });
    await waitForCondition(() => listCronJobsMock.mock.calls.length === 2);
    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-delete-job-created"]'),
    );
    // The saved job replaces the new job's entry.
    expect(navigation.place).toEqual(['schedules', 'job-created']);

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
      notification_policy: 'all',
      delivery: { kind: 'none' },
      watch: null,
      working_project_id: null,
    });
  });

  it('creates a weekly job from the planner and keeps it saved when a later reload lists it', async () => {
    const createdJob = cronJob({
      id: 'job-created',
      name: '',
      prompt: 'Check the inbox',
      cron_expression: '30 8 * * 1,3',
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

    await waitForCondition(() => document.getElementById('cron-job-frequency'));

    // Pick Weekly, keep Monday and Wednesday of the default weekdays, and set
    // the time; the readable preview shows the generated expression.
    document.getElementById('cron-job-frequency').click();
    flushSync();
    const weeklyOption = Array.from(
      document.querySelectorAll('.dropdown-option'),
    ).find(
      (option) => option.textContent.trim() === t('cron.frequency.weekly'),
    );
    expect(weeklyOption, 'frequency option not found').toBeTruthy();
    weeklyOption.click();
    flushSync();

    for (const day of [2, 4, 5]) {
      buttonByTestId(`cron-weekday-${day}`).click();
    }
    flushSync();
    expect(buttonByTestId('cron-weekday-1').getAttribute('aria-pressed')).toBe(
      'true',
    );
    expect(buttonByTestId('cron-weekday-2').getAttribute('aria-pressed')).toBe(
      'false',
    );
    inputById('cron-job-time').value = '08:30';
    inputById('cron-job-time').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    expect(document.getElementById('cron-job-expression')).toBeNull();
    expect(
      document.querySelector('.cron-schedule-preview').textContent,
    ).toContain('30 8 * * 1,3');

    inputById('cron-job-prompt').value = 'Check the inbox';
    inputById('cron-job-prompt').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    chooseOption('cron-working-project', t('cron.execution.workspace'));
    buttonByText(t('common.save')).click();
    await waitForCondition(() => listCronJobsMock.mock.calls.length === 2);
    expect(createCronJobMock).toHaveBeenCalledWith(
      expect.objectContaining({
        prompt: 'Check the inbox',
        schedule_type: 'cron',
        cron_expression: '30 8 * * 1,3',
      }),
    );

    // The first reload does not list the created job yet. When a later
    // reload does, its submitted values count as saved: nothing is left to
    // save and nothing is written again.
    props.cronRefreshToken += 1;
    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-item-job-created"]'),
    );
    await waitForCondition(() => document.querySelector('.save-status'));
    expect(document.querySelector('.save-status button')).toBeNull();
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
    // Only a repeating schedule can check before each Run.
    expect(document.getElementById('cron-job-watch')).toBeNull();

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
      notification_policy: 'all',
      delivery: { kind: 'none' },
      watch: null,
    });
  });

  it('names the event of an event job and saves only its timing', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({
          id: 'job-event',
          schedule_type: 'event',
          cron_expression: null,
          event_id: 'evt-1',
          event_title: 'Team review',
          event_edge: 'start',
          event_offset_minutes: -30,
        }),
      ],
    });

    mountView();

    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-event-title"]'),
    );
    const row = document.querySelector('[data-testid="cron-item-job-event"]');
    expect(row.textContent).toContain(
      'Event: Team review — 30 minutes before start',
    );
    expect(
      document.querySelector('[data-testid="cron-event-title"]').textContent,
    ).toBe('Team review');
    // The event's occurrences repeat the job: no frequency and no repeat limit.
    expect(document.getElementById('cron-job-frequency')).toBeNull();
    expect(document.getElementById('cron-job-repeat')).toBeNull();

    inputById('cron-job-event-amount').value = '45';
    inputById('cron-job-event-amount').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    buttonByText(t('common.save')).click();

    await waitForCondition(() => updateCronJobMock.mock.calls.length === 1);
    expect(updateCronJobMock).toHaveBeenCalledWith({
      id: 'job-event',
      agent_id: 'agent-alpha',
      name: 'Default scheduled run',
      prompt: 'Default cron prompt',
      schedule_type: 'event',
      event_id: 'evt-1',
      event_time: 'start - 45m',
      working_project_id: null,
      session_id: null,
      notification_policy: 'all',
      delivery: { kind: 'none' },
      watch: null,
    });
  });

  it('keeps uncertain delivery visible on a completed job and retries without rerunning it', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({
          id: 'job-done',
          status: 'completed',
          delivery_progress: {
            waiting: 1,
            uncertain: 1,
            error: 'Unknown delivery',
          },
          unread_alert: {
            message: 'Delivery could not be confirmed.',
            at: '2026-05-14T10:06:00.000000Z',
            kind: 'delivery',
          },
        }),
      ],
    });
    mountView();
    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-delivery-progress"]'),
    );
    // The unread alert shows on the job's row and in its view until marked read.
    expect(
      document
        .querySelector('[data-testid="cron-item-alert-job-done"]')
        .getAttribute('aria-label'),
    ).toBe(t('cron.alert.unread'));
    expect(
      document.querySelector('[data-testid="cron-unread-alert"]').textContent,
    ).toContain('Delivery could not be confirmed.');
    const banner = document.querySelector(
      '[data-testid="cron-delivery-progress"]',
    );
    expect(banner.textContent).toContain(t('cron.delivery.uncertain'));
    banner.querySelector('button').click();
    await waitForCondition(() => retryCronDeliveryMock.mock.calls.length === 1);
    expect(retryCronDeliveryMock).toHaveBeenCalledWith('job-done');
    expect(enableCronJobMock).not.toHaveBeenCalled();
    expect(updateCronJobMock).not.toHaveBeenCalled();

    const alert = () =>
      document.querySelector('[data-testid="cron-unread-alert"] button');
    await waitForCondition(() => !alert().disabled);
    alert().click();
    await waitForCondition(
      () => acknowledgeCronAlertMock.mock.calls.length === 1,
    );
    expect(acknowledgeCronAlertMock).toHaveBeenCalledWith('job-done');
  });

  it('shows a silent Run and the last answer and delivers to a chat of the Agent', async () => {
    rpcMock.mockImplementation(
      createAgentListRpcMock(defaultAgents(), [
        {
          id: 'tg-alpha',
          platform: 'telegram',
          agent_id: 'agent-alpha',
          allowed_chat_ids: ['42'],
        },
        { id: 'tg-beta', platform: 'telegram', agent_id: 'agent-beta' },
      ]),
    );
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({
          id: 'job-app',
          notification_policy: 'all',
          delivery: { kind: 'app' },
          last_run_silent: true,
          last_result: {
            text: 'Two new mails.',
            at: '2026-05-14T09:35:00+00:00',
            run_id: 'run-earlier',
          },
        }),
      ],
    });
    mountView();
    await waitForCondition(() => document.getElementById('cron-job-delivery'));

    // The latest Run had nothing to report; the answer before it stays.
    expect(document.querySelector('.cron-summary').textContent).toContain(
      t('cron.outcome.silent'),
    );
    expect(
      document.querySelector('.cron-last-answer__text').textContent.trim(),
    ).toBe('Two new mails.');
    expect(document.getElementById('cron-job-delivery').textContent).toContain(
      t('cron.deliveryKind.app'),
    );

    // A chat offers only the Agent's own Channel and preselects it.
    chooseOption('cron-job-delivery', t('cron.deliveryKind.chat'));
    document.getElementById('cron-job-delivery-channel').click();
    flushSync();
    const channelOptions = [
      ...document.querySelectorAll('.dropdown-option'),
    ].map((option) => option.textContent.trim());
    expect(channelOptions).toEqual(['tg-alpha · Telegram']);
    document.getElementById('cron-job-delivery-channel').click();
    flushSync();

    buttonByText(t('common.save')).click();
    await waitForCondition(() =>
      document.body.textContent.includes(t('cron.errors.chatDelivery')),
    );
    expect(updateCronJobMock).not.toHaveBeenCalled();

    expect(
      [...document.querySelectorAll('#cron-job-delivery-chats option')].map(
        (option) => option.value,
      ),
    ).toEqual(['42']);
    inputById('cron-job-delivery-chat').value = '42';
    inputById('cron-job-delivery-chat').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    buttonByText(t('common.save')).click();
    await waitForCondition(() => updateCronJobMock.mock.calls.length === 1);
    expect(updateCronJobMock).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'job-app',
        notification_policy: 'all',
        delivery: {
          kind: 'chat',
          channel_id: 'tg-alpha',
          platform_target: '42',
        },
      }),
    );
  });

  it('offers no chat delivery to a job of a Project Agent', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({
          id: 'job-project',
          agent_id: 'builder',
          project_id: 'vbot',
          target: 'builder@vbot',
          notification_policy: 'all',
          delivery: { kind: 'app' },
        }),
      ],
    });
    mountView();
    await waitForCondition(() => document.getElementById('cron-job-delivery'));
    document.getElementById('cron-job-delivery').click();
    flushSync();
    const kinds = [...document.querySelectorAll('.dropdown-option')].map(
      (option) => option.textContent.trim(),
    );
    expect(kinds).toEqual([
      t('cron.deliveryKind.app'),
      t('cron.deliveryKind.none'),
    ]);
  });

  it('checks a web page before each Run and shows its last check and change', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({
          id: 'job-watch',
          last_outcome: null,
          last_completed_at: null,
          last_run_id: null,
          last_checked_at: '2026-05-14T10:00:00+00:00',
          last_changed_at: '2026-05-14T09:00:00+00:00',
          last_check_error: 'The watched URL answered HTTP 503',
        }),
      ],
    });
    mountView();
    await waitForCondition(() => document.getElementById('cron-job-watch'));

    // Without a watch, failed checks say nothing about the job.
    expect(
      document.querySelector('[data-testid="cron-check-error"]'),
    ).toBeNull();
    expect(document.getElementById('cron-job-watch-target')).toBeNull();

    chooseOption('cron-job-watch', t('cron.watchKind.url'));
    inputById('cron-job-watch-target').value = 'example.com/status';
    inputById('cron-job-watch-target').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    buttonByText(t('common.save')).click();
    await waitForCondition(() =>
      document.body.textContent.includes(t('cron.errors.watch')),
    );
    expect(updateCronJobMock).not.toHaveBeenCalled();

    inputById('cron-job-watch-target').value = 'https://example.com/status';
    inputById('cron-job-watch-target').dispatchEvent(
      new Event('input', { bubbles: true }),
    );
    flushSync();
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({
          id: 'job-watch',
          last_outcome: null,
          last_completed_at: null,
          last_run_id: null,
          watch: { url: 'https://example.com/status' },
          last_checked_at: '2026-05-14T10:00:00+00:00',
          last_changed_at: '2026-05-14T09:00:00+00:00',
          last_check_error: 'The watched URL answered HTTP 503',
        }),
      ],
    });
    buttonByText(t('common.save')).click();
    await waitForCondition(() => updateCronJobMock.mock.calls.length === 1);
    expect(updateCronJobMock).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'job-watch',
        watch: { url: 'https://example.com/status' },
      }),
    );

    await waitForCondition(() => listCronJobsMock.mock.calls.length === 2);
    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-check-error"]'),
    );
    expect(
      document.querySelector('[data-testid="cron-check-error"]').textContent,
    ).toContain('The watched URL answered HTTP 503');
    const details = document.querySelector('.cron-execution-grid').textContent;
    expect(details).toContain(t('cron.detail.lastCheck'));
    expect(details).toContain(t('cron.detail.lastChange'));
    expect(document.querySelector('.cron-summary').textContent).toContain(
      t('cron.detail.waitingForChange'),
    );

    // The row's details card names the check and how the last one went.
    document.querySelector('[data-testid="cron-item-job-watch"]').focus();
    flushSync();
    const cardRows = Object.fromEntries(
      [...document.getElementById('app-tooltip').querySelectorAll('dt')].map(
        (term) => [term.textContent, term.nextElementSibling],
      ),
    );
    expect(cardRows[t('cron.card.watch')].textContent).toBe(
      'https://example.com/status',
    );
    expect(cardRows[t('cron.detail.lastCheck')].textContent).toContain(
      t('cron.card.checkFailed'),
    );
    expect(cardRows[t('cron.detail.lastCheck')].classList).toContain(
      'app-tooltip__value--danger',
    );
  });

  it("shows a webhook job's URL and reveals and replaces its secret", async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-05-14T12:00:00Z'));
    const hookJob = cronJob({
      id: 'job-hook',
      name: 'Triage issues',
      schedule_type: 'webhook',
      cron_expression: null,
      next_fire_at: null,
      last_outcome: null,
      last_completed_at: null,
      last_run_id: null,
      webhook: {
        url: 'http://127.0.0.1:8765/api/hooks/job-hook',
        path: '/api/hooks/job-hook',
        reach: 'computer',
        last_received_at: '2026-05-14T10:00:00+00:00',
        waiting: 2,
        dropped: 0,
      },
    });
    listCronJobsMock.mockResolvedValue({
      jobs: [hookJob],
      system_timezone: 'UTC',
    });
    const secrets = ['vbw_first', 'vbw_second'];
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'cron.webhook_secret') {
        expect(params).toEqual({ id: 'job-hook' });
        return { secret: secrets[0], webhook: hookJob.webhook };
      }
      if (method === 'cron.rotate_webhook_secret') {
        secrets.shift();
        return { secret: secrets[0], webhook: hookJob.webhook };
      }
      return createAgentListRpcMock()(method);
    });
    const navigation = createStandaloneNavigation(['job-hook']);
    mountView({ navigation });
    await waitForCondition(() =>
      document.querySelector('[data-testid="cron-webhook-url"]'),
    );

    // A webhook job keeps no time and checks nothing; its request starts it.
    expect(navigation.place).toEqual(['webhooks', 'job-hook']);
    expect(document.getElementById('cron-job-frequency')).toBeNull();
    expect(document.getElementById('cron-job-watch')).toBeNull();
    expect(document.querySelector('.cron-summary').textContent).toContain(
      t('cron.detail.nextRequest'),
    );
    expect(
      document.querySelector('[data-testid="cron-webhook-url"]').textContent,
    ).toBe('http://127.0.0.1:8765/api/hooks/job-hook');
    expect(
      document.querySelector('[data-testid="cron-webhook-reach"]').textContent,
    ).toContain(t('cron.webhook.reach.computer'));
    expect(
      document.querySelector('[data-testid="cron-webhook-requests"]')
        .textContent,
    ).toContain(t('cron.webhook.waitingMany', { count: 2 }));

    // The secret stays hidden until the user asks for it.
    const secretText = () =>
      document.querySelector('[data-testid="cron-webhook-secret"]').textContent;
    expect(secretText()).not.toContain('vbw_');
    buttonByText(t('cron.webhook.showSecret')).click();
    await waitForCondition(() => secretText() === 'vbw_first');

    buttonByText(t('cron.webhook.rotate')).click();
    flushSync();
    confirmDialog(t('cron.webhook.rotateConfirmLabel'));
    await waitForCondition(() => secretText() === 'vbw_second');
    expect(rpcMock).toHaveBeenCalledWith('cron.rotate_webhook_secret', {
      id: 'job-hook',
    });
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: t('cron.webhook.rotated') }),
    );
    expect(updateCronJobMock).not.toHaveBeenCalled();
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
    // Like the App's navigator, the handle saves pending edits before a step.
    const coordinator = createAutosaveCoordinator();
    const navigation = gatedNavigation(coordinator);
    mountedComponent = mount(AutosaveContextHost, {
      target: document.body,
      props: {
        component: CronView,
        coordinator,
        componentProps: { onToast: toastMock, navigation },
      },
    });
    flushSync();
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
    expect(navigation.place).toEqual(['schedules', 'job-two']);
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

// A standalone navigation handle whose steps pass an autosave gate first, as
// the App's navigator does.
function gatedNavigation(coordinator, initialPlace = []) {
  const inner = createStandaloneNavigation(initialPlace);
  const gated =
    (move) =>
    async (...args) =>
      (await coordinator.flushPending()) ? move(...args) : false;
  return {
    active: true,
    get place() {
      return inner.place;
    },
    get extra() {
      return inner.extra;
    },
    get origin() {
      return inner.origin;
    },
    get revision() {
      return inner.revision;
    },
    navigate: gated(inner.navigate),
    replace: inner.replace,
    up: gated(inner.up),
  };
}

// Clicks a button in the open ConfirmDialog by its label.
function confirmDialog(label) {
  const footer = document.body.querySelector('.modal-footer');
  expect(footer, 'confirm dialog not open').toBeTruthy();
  const button = Array.from(footer.querySelectorAll('button')).find((item) =>
    item.textContent.trim().startsWith(label),
  );
  expect(button, `confirm button not found: ${label}`).toBeTruthy();
  button.click();
}

function createAgentListRpcMock(agents = defaultAgents(), channels = []) {
  return async (method) => {
    if (method === 'agent.list') {
      return { agents };
    }
    if (method === 'channel.list') {
      return { channels };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

// Opens the dropdown with this id and picks the option with this label.
function chooseOption(id, label) {
  document.getElementById(id).click();
  flushSync();
  const option = [...document.querySelectorAll('[role="option"]')].find(
    (item) => item.textContent.trim().startsWith(label),
  );
  expect(option, `option not found: ${label}`).toBeTruthy();
  option.click();
  flushSync();
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
    working_project_id: null,
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
