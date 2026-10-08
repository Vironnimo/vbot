// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { t } from '../../lib/i18n.js';
import {
  flushSync,
  mount,
  unmount,
  rpcMock,
  StatisticsView,
  makeReport,
  makeUsageReport,
  routedRpc,
  openLimitsTab,
  waitForCondition,
  waitForOverview,
  buttonNamed,
  setupStatisticsViewSuite,
} from './StatisticsView.support.js';

describe('StatisticsView', () => {
  const suite = setupStatisticsViewSuite();

  it('lazily loads provider usage and its history when the Limits sub-view opens', async () => {
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
        (button) => button.textContent.trim() === t('common.refresh'),
      ),
    ).toBe(false);
    expect(document.querySelector('.stats-view__generated')).toBeNull();
    // The hourly history (owned by LimitHistory) loads beside the live cards.
    await waitForCondition(() =>
      document.querySelector('.limit-history > .empty-state'),
    );
    expect(rpcMock).toHaveBeenCalledWith(
      'provider.usage_history',
      expect.anything(),
    );
  });

  it('shows reset times and money amounts with their currency', async () => {
    const usage = makeUsageReport({
      providers: [
        {
          connection: 'ollama-cloud:api-key',
          account: 'default',
          display_name: 'Ollama Cloud',
          plan: null,
          credits: { enabled: true, balance: 4.5, unit: 'USD' },
          windows: [
            {
              label: 'Month',
              used_percent: 25,
              reset_at: '2099-11-01T00:00:00+00:00',
              window_seconds: null,
              used_units: 5,
              remaining_units: 15.25,
              total_units: 20,
              unit: 'USD',
              unlimited: null,
            },
          ],
          error: null,
        },
      ],
    });
    rpcMock.mockImplementation(routedRpc(usage));

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    openLimitsTab();
    await waitForCondition(() =>
      document.body.textContent.includes('Ollama Cloud'),
    );

    const fill = document.querySelector('.stats-limit-window__fill');
    expect(fill.getAttribute('style')).toContain('25%');
    expect(document.querySelector('.stats-limit-window__reset')).not.toBeNull();
    const units = document.querySelector('.stats-limit-window__units');
    expect(units.textContent).toContain('$15.25');
    expect(units.textContent).toContain('$20.00');
    expect(
      document.querySelector('.stats-limit-card__credits').textContent,
    ).toContain('$4.50');
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

    buttonNamed('statistics.subview.overview').click();
    flushSync();
    await vi.advanceTimersByTimeAsync(20_000);

    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'provider.usage'),
    ).toHaveLength(2);
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
    ].find((button) => button.textContent.trim() === t('common.retry'));
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
        ).toContain(t('statistics.subview.limits'));
      }
    },
  );

  it('never overlaps provider usage requests, across tab changes and after teardown', async () => {
    vi.useFakeTimers();
    const pending = [];
    const route = routedRpc(makeUsageReport());
    rpcMock.mockImplementation((method, params) =>
      method === 'provider.usage'
        ? new Promise((resolve) => pending.push(resolve))
        : route(method, params),
    );
    const usageCalls = () =>
      rpcMock.mock.calls.filter(([method]) => method === 'provider.usage')
        .length;
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    openLimitsTab();
    await waitForCondition(() => pending.length === 1);

    // Polling and reopening Limits wait for the pending request.
    buttonNamed('statistics.subview.overview').click();
    flushSync();
    openLimitsTab();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(usageCalls()).toBe(1);

    // Once it resolves, polling resumes on its regular interval.
    pending[0](makeUsageReport());
    await waitForCondition(() => document.querySelector('.stats-limit-card'));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(usageCalls()).toBe(2);

    // A request that finishes after teardown neither renders nor polls.
    await unmount(suite.mountedComponent);
    suite.mountedComponent = null;
    pending[1](makeUsageReport());
    await vi.advanceTimersByTimeAsync(30_000);
    expect(usageCalls()).toBe(2);
    expect(document.querySelector('.stats-limit-card')).toBeNull();
  });
});
