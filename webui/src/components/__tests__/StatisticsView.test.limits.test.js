// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  unmount,
  rpcMock,
  StatisticsView,
  makeReport,
  makeUsageReport,
  makeUsageHistoryReport,
  makeRunActivityReport,
  routedRpc,
  openLimitsTab,
  waitForCondition,
  waitForOverview,
  setupStatisticsViewSuite,
} from './StatisticsView.support.js';

describe('StatisticsView', () => {
  const suite = setupStatisticsViewSuite();

  it('lazily loads provider usage when the Limits sub-view opens', async () => {
    rpcMock.mockImplementation(routedRpc(makeUsageReport()));

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    // provider.usage is not fetched until the Limits tab is opened.
    expect(rpcMock).not.toHaveBeenCalledWith('provider.usage');

    openLimitsTab();
    await waitForCondition(() => document.body.textContent.includes('OpenAI'));

    expect(rpcMock).toHaveBeenCalledWith('provider.usage');
    expect(document.body.textContent).toContain('Plus');
    expect(document.body.textContent).toContain('5h');
    expect(document.querySelector('.stats-limit-window__reset')).toBeTruthy();
    // The error snapshot renders its message cleanly rather than crashing.
    expect(document.body.textContent).toContain('GitHub Copilot');
    expect(document.body.textContent).toContain('HTTP 401');
    expect(
      [...document.querySelectorAll('.stats-view button')].some(
        (button) => button.textContent.trim() === 'Refresh',
      ),
    ).toBe(false);
    expect(document.querySelector('.stats-view__generated')).toBeNull();
  });

  it('shows Ollama Cloud quota percentages and labels request counts as observed', async () => {
    const ollamaUsage = makeUsageReport({
      providers: [
        {
          connection: 'ollama-cloud:api-key',
          account: 'default',
          display_name: 'Ollama Cloud',
          plan: null,
          credits: null,
          windows: [
            {
              label: '5h',
              used_percent: 1.9,
              reset_at: null,
              window_seconds: 18000,
              used_units: 9,
              remaining_units: null,
              total_units: null,
              unit: 'requests',
              unlimited: null,
            },
            {
              label: 'Week',
              used_percent: 0.7,
              reset_at: null,
              window_seconds: 604800,
              used_units: 14,
              remaining_units: null,
              total_units: null,
              unit: 'requests',
              unlimited: null,
            },
          ],
          error: null,
        },
      ],
    });
    rpcMock.mockImplementation(routedRpc(ollamaUsage));

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    openLimitsTab();
    await waitForCondition(() =>
      document.body.textContent.includes('Ollama Cloud'),
    );

    const fills = document.querySelectorAll('.stats-limit-window__fill');
    expect(fills).toHaveLength(2);
    expect(fills[0].getAttribute('style')).toContain('1.9%');
    expect(fills[1].getAttribute('style')).toContain('0.7%');
    const units = document.querySelectorAll('.stats-limit-window__units');
    expect(units[0].textContent).toContain('9');
    expect(units[1].textContent).toContain('14');
    expect(document.querySelector('.stats-limit-window__reset')).toBeNull();
  });

  it('renders hourly limit history and correlated vBot Runs', async () => {
    rpcMock.mockImplementation((method) => {
      if (method === 'provider.usage') {
        return Promise.resolve(makeUsageReport());
      }
      if (method === 'provider.usage_history') {
        return Promise.resolve(makeUsageHistoryReport());
      }
      if (method === 'statistics.run_activity') {
        return Promise.resolve(makeRunActivityReport());
      }
      return Promise.resolve(makeReport());
    });

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    openLimitsTab();
    await waitForCondition(() => document.querySelector('.limit-trace__line'));
    await waitForCondition(() => document.querySelector('.limit-run__tokens'));

    expect(document.body.textContent).toContain('+28.5 pp');
    expect(document.querySelector('.limit-run__tokens').textContent).toContain(
      '120',
    );
    expect(
      rpcMock.mock.calls.some(
        ([method, params]) =>
          method === 'statistics.run_activity' &&
          params.since === '2026-06-16T10:00:00+00:00' &&
          params.until === '2026-06-16T11:00:00+00:00',
      ),
    ).toBe(true);
  });

  it('clears hourly limit history only after confirmation', async () => {
    rpcMock.mockImplementation((method) => {
      if (method === 'provider.usage') {
        return Promise.resolve(makeUsageReport());
      }
      if (method === 'provider.usage_history') {
        return Promise.resolve(makeUsageHistoryReport());
      }
      if (method === 'statistics.run_activity') {
        return Promise.resolve(makeRunActivityReport());
      }
      if (method === 'provider.usage_history.clear') {
        return Promise.resolve({ deleted_samples: 2, deleted_files: 1 });
      }
      return Promise.resolve(makeReport());
    });

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    openLimitsTab();
    await waitForCondition(() => document.querySelector('.limit-trace__line'));

    const deleteButton = [...document.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === 'Delete history',
    );
    deleteButton.click();
    flushSync();
    expect(document.querySelector('[role="dialog"]')).toBeTruthy();

    const confirmButton = [...document.querySelectorAll('button')]
      .filter((button) => button.textContent.trim() === 'Delete history')
      .at(-1);
    confirmButton.click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some(
        ([method]) => method === 'provider.usage_history.clear',
      ),
    );

    expect(
      rpcMock.mock.calls.some(
        ([method]) => method === 'provider.usage_history.clear',
      ),
    ).toBe(true);
    await waitForCondition(() =>
      document.querySelector('.limit-history > .empty-state'),
    );
  });

  it('refreshes provider usage every ten seconds only while Limits is visible', async () => {
    vi.useFakeTimers();
    rpcMock.mockImplementation(routedRpc(makeUsageReport()));

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    openLimitsTab();
    await waitForCondition(
      () =>
        rpcMock.mock.calls.filter(([method]) => method === 'provider.usage')
          .length === 1,
    );

    await vi.advanceTimersByTimeAsync(10_000);
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'provider.usage'),
    ).toHaveLength(2);

    const overviewTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Overview',
    );
    overviewTab.click();
    flushSync();
    await vi.advanceTimersByTimeAsync(20_000);

    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'provider.usage'),
    ).toHaveLength(2);
  });

  it('does not overlap provider usage requests', async () => {
    vi.useFakeTimers();
    let resolveFirstUsage;
    let usageCalls = 0;
    rpcMock.mockImplementation((method) => {
      if (method !== 'provider.usage') {
        return Promise.resolve(makeReport());
      }
      usageCalls += 1;
      if (usageCalls === 1) {
        return new Promise((resolve) => {
          resolveFirstUsage = resolve;
        });
      }
      return Promise.resolve(makeUsageReport());
    });

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    openLimitsTab();
    await waitForCondition(() => usageCalls === 1);

    await vi.advanceTimersByTimeAsync(20_000);
    expect(usageCalls).toBe(1);

    resolveFirstUsage(makeUsageReport());
    await waitForCondition(() => document.body.textContent.includes('OpenAI'));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(usageCalls).toBe(2);
  });

  it('pauses provider usage while the page is hidden and refreshes on return', async () => {
    vi.useFakeTimers();
    let visibility = 'visible';
    const visibilitySpy = vi
      .spyOn(document, 'visibilityState', 'get')
      .mockImplementation(() => visibility);
    rpcMock.mockImplementation(routedRpc(makeUsageReport()));

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    openLimitsTab();
    await waitForCondition(
      () =>
        rpcMock.mock.calls.filter(([method]) => method === 'provider.usage')
          .length === 1,
    );

    visibility = 'hidden';
    document.dispatchEvent(new Event('visibilitychange'));
    flushSync();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'provider.usage'),
    ).toHaveLength(1);

    visibility = 'visible';
    document.dispatchEvent(new Event('visibilitychange'));
    await waitForCondition(
      () =>
        rpcMock.mock.calls.filter(([method]) => method === 'provider.usage')
          .length === 2,
    );
    visibilitySpy.mockRestore();
  });

  it('keeps a contextual Retry after a Limits RPC failure', async () => {
    let usageCalls = 0;
    rpcMock.mockImplementation((method) => {
      if (method !== 'provider.usage') {
        return Promise.resolve(makeReport());
      }
      usageCalls += 1;
      return usageCalls === 1
        ? Promise.reject(new Error('limits unavailable'))
        : Promise.resolve(makeUsageReport());
    });

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    openLimitsTab();
    await waitForCondition(() =>
      document.body.textContent.includes('limits unavailable'),
    );

    const retryButton = [
      ...document.querySelectorAll('.stats-panel button'),
    ].find((button) => button.textContent.trim() === 'Retry');
    retryButton.click();
    await waitForCondition(() => document.body.textContent.includes('OpenAI'));

    expect(usageCalls).toBe(2);
    expect(document.body.textContent).not.toContain('limits unavailable');
  });

  it('shows the limits empty state when no providers are connected', async () => {
    rpcMock.mockImplementation(
      routedRpc({ generated_at: '2026-06-16T12:00:00+00:00', providers: [] }),
    );

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    openLimitsTab();
    await waitForCondition(() =>
      document.querySelector('.stats-panel .empty-state'),
    );

    expect(document.querySelector('.stats-panel .empty-state')).toBeTruthy();
  });

  it.each(['failed', 'pending'])(
    'opens independent Limits when the local report is %s',
    async (state) => {
      let finishReport;
      const route = routedRpc(makeUsageReport());
      rpcMock.mockImplementation((method, params) => {
        if (method === 'statistics.report') {
          return state === 'failed'
            ? Promise.reject(new Error('local report unavailable'))
            : new Promise((resolve) => {
                finishReport = resolve;
              });
        }
        return route(method, params);
      });
      suite.mountedComponent = mount(StatisticsView, { target: document.body });
      await waitForCondition(() => document.querySelector('[role="tab"]'));
      openLimitsTab();
      await waitForCondition(() => document.querySelector('.stats-limit-card'));
      expect(document.body.textContent).toContain('OpenAI');
      expect(document.body.textContent).not.toContain(
        'local report unavailable',
      );
      if (finishReport) {
        finishReport(makeReport());
        await waitForCondition(() =>
          document.querySelector('.stats-limit-card'),
        );
        expect(
          document.querySelector('[role="tab"][aria-selected="true"]')
            .textContent,
        ).toContain('Limits');
      }
    },
  );

  it('keeps one pending Limits request across tab changes and stops after teardown', async () => {
    vi.useFakeTimers();
    let finishUsage;
    const route = routedRpc(makeUsageReport());
    rpcMock.mockImplementation((method, params) =>
      method === 'provider.usage'
        ? new Promise((resolve) => {
            finishUsage = resolve;
          })
        : route(method, params),
    );
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    openLimitsTab();
    await waitForCondition(() => finishUsage);
    const overview = [...document.querySelectorAll('[role="tab"]')].find(
      (tab) => tab.textContent.trim() === 'Overview',
    );
    overview.click();
    flushSync();
    openLimitsTab();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'provider.usage'),
    ).toHaveLength(1);
    await unmount(suite.mountedComponent);
    suite.mountedComponent = null;
    finishUsage(makeUsageReport());
    await vi.advanceTimersByTimeAsync(30_000);
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'provider.usage'),
    ).toHaveLength(1);
    expect(document.querySelector('.stats-limit-card')).toBeNull();
  });
});
