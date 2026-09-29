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
    expect(
      document.querySelectorAll('.cron-detail-scroll .s-section'),
    ).toHaveLength(3);
    expect(document.querySelector('.cron-technical-details')).toBeTruthy();
    // The Enabled switch states active or paused; no duplicate chip.
    expect(document.querySelector('.detail-btns .chip')).toBeNull();
    // An expression the planner cannot express stays editable as Custom.
    expect(inputById('cron-job-expression').value).toBe('*/30 * * * *');
    expect(
      document.querySelectorAll(
        '.cron-detail-scroll .s-group label.s-row-label',
      ).length,
    ).toBe(7);
  });

  it('shows the job named by the place and records job and new-job steps in it', async () => {
    listCronJobsMock.mockResolvedValue({
      jobs: [
        cronJob({ id: 'job-one' }),
        cronJob({ id: 'job-two', prompt: 'Second prompt' }),
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
    expect(navigation.place).toEqual(['job-one']);
    expect(inputById('cron-job-prompt').value).toBe('Default cron prompt');

    buttonByAriaLabel('Create schedule').click();
    flushSync();
    expect(navigation.place).toEqual(['new']);
    expect(inputById('cron-job-prompt').value).toBe('');

    // Cancel goes up to the job the new job's form was opened from.
    buttonByText(t('common.cancel')).click();
    flushSync();
    expect(navigation.place).toEqual(['job-one']);

    // The empty place (the view's start page) names the first job.
    navigation.navigate([]);
    flushSync();
    expect(navigation.place).toEqual(['job-one']);
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
    expect(navigation.place).toEqual(['job-one']);
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
    // The saved job replaces the new job's entry.
    expect(navigation.place).toEqual(['job-created']);

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
    expect(navigation.place).toEqual(['job-two']);
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
