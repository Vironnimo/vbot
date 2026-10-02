// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { t } from '../../lib/i18n.js';
import { setApplicationTimeZone } from '../../lib/dateTimePrefs.svelte.js';
import { createStandaloneNavigation } from '../../lib/navigation.svelte.js';
import {
  flushSync,
  mount,
  unmount,
  rpcMock,
  StatisticsView,
  makeReport,
  routedRpc,
  reportCalls,
  waitForCondition,
  waitForOverview,
  buttonNamed,
  tileText,
  setupStatisticsViewSuite,
} from './StatisticsView.support.js';

const NOW = Date.parse('2026-06-13T10:00:00Z');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function panel() {
  return document.querySelector('.stats-view__panel');
}

function overviewCalls() {
  return reportCalls().filter((params) => params.sections.includes('overview'));
}

describe('StatisticsView', () => {
  const suite = setupStatisticsViewSuite();

  it('shows the header, tabs and range at once and loads the Overview section in the Settings time zone', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW);
    setApplicationTimeZone('Europe/Berlin');
    const overview = deferred();
    rpcMock.mockImplementation(
      routedRpc(undefined, (params) =>
        params.sections.includes('overview')
          ? overview.promise
          : makeReport(params.sections),
      ),
    );

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    flushSync();

    expect(document.querySelector('.stats-view .view-header')).toBeTruthy();
    expect(document.querySelector('.stats-view [role="tablist"]')).toBeTruthy();
    expect(
      buttonNamed('statistics.range.30d').getAttribute('aria-pressed'),
    ).toBe('true');
    expect(panel().querySelector('.stats-skeleton')).toBeTruthy();
    expect(panel().getAttribute('aria-busy')).toBe('true');
    // Local midnight in Berlin, 30 calendar days including today.
    expect(reportCalls()).toEqual([
      {
        since: '2026-05-14T22:00:00.000Z',
        timezone: 'Europe/Berlin',
        sections: ['overview'],
      },
    ]);

    overview.resolve(makeReport(['overview']));
    await waitForOverview();

    expect(panel().querySelector('.stats-skeleton')).toBeNull();
    const cost = tileText('statistics.overview.cost');
    expect(cost.value).toBe('$12.50');
    expect(cost.detail).toBe('$4.50 reported · $8.00 estimated');
    // A cost change is reported, never judged.
    const costChange = cost.tile.querySelector('.stats-change');
    expect(costChange.textContent).toContain('25.0%');
    expect(costChange.classList).toContain('stats-change--up');
    expect(costChange.classList).toContain('stats-change--neutral');
    // A falling failure rate is good news.
    const runs = tileText('statistics.overview.runs');
    expect(runs.detail).toBe('80.0% completed · 1 failed');
    const failureChange = runs.tile.querySelector(
      '.stats-tile__detail-row .stats-change',
    );
    expect(failureChange.textContent).toContain('15.0 pts');
    expect(failureChange.classList).toContain('stats-change--down');
    expect(failureChange.classList).toContain('stats-change--good');
    expect(document.body.textContent).not.toContain(
      t('statistics.change.noComparison'),
    );
    // Where the cost comes from, the largest share first.
    expect(
      [...panel().querySelectorAll('.stats-bars__label')].map((label) =>
        label.textContent.trim(),
      ),
    ).toEqual([t('statistics.origin.user'), t('statistics.origin.automation')]);
  });

  it('requests only the sections of the tab it shows', async () => {
    rpcMock.mockImplementation(routedRpc());
    const navigation = createStandaloneNavigation(['overview']);
    suite.mountedComponent = mount(StatisticsView, {
      target: document.body,
      props: { navigation },
    });
    await waitForOverview();

    const expectations = [
      ['usage', ['usage'], 'Plan the release'],
      ['runs', ['runs'], 'model-error-sentinel'],
      ['tools', ['tools', 'skills'], 'unused-skill-sentinel'],
      ['diagnostics', ['diagnostics'], 'Runaway sentinel'],
    ];
    for (const [tab, sections, sentinel] of expectations) {
      buttonNamed(`statistics.subview.${tab}`).click();
      await waitForCondition(() => panel().textContent.includes(sentinel));
      expect(navigation.place).toEqual([tab]);
      expect(reportCalls().at(-1).sections).toEqual(sections);
    }
    // Diagnostics opens with every block collapsed.
    expect(panel().querySelectorAll('details').length).toBeGreaterThan(0);
    expect(panel().querySelectorAll('details[open]')).toHaveLength(0);

    const before = reportCalls().length;
    buttonNamed('statistics.subview.limits').click();
    await waitForCondition(() => document.querySelector('.stats-limits'));
    expect(reportCalls()).toHaveLength(before);
    expect(document.querySelector('.stats-toolbar')).toBeNull();
  });

  it('compares the Runs tab with the previous period and names error kinds', async () => {
    // Only a report with a start has a previous period.
    rpcMock.mockImplementation(
      routedRpc(undefined, (params) =>
        params.since
          ? makeReport(params.sections)
          : makeReport(params.sections, {
              runs: { ...makeReport(['runs']).runs, previous: null },
            }),
      ),
    );
    const navigation = createStandaloneNavigation(['runs']);
    suite.mountedComponent = mount(StatisticsView, {
      target: document.body,
      props: { navigation },
    });
    await waitForCondition(() => tileText('statistics.runs.failed'));

    const marker = (key, row = '.stats-tile__value-row') =>
      tileText(key).tile.querySelector(`${row} .stats-change`);
    expect(marker('statistics.overview.runs').textContent).toContain('25.0%');
    expect(marker('statistics.runs.completed').textContent).toContain(
      '5.0 pts',
    );
    expect(marker('statistics.runs.cancelled').textContent).toContain('new');
    // Counts are not judged; a falling failure rate is good news.
    const failed = marker('statistics.runs.failed');
    expect(failed.classList).toContain('stats-change--down');
    expect(failed.classList).toContain('stats-change--neutral');
    const failureRate = marker(
      'statistics.runs.failed',
      '.stats-tile__detail-row',
    );
    expect(failureRate.textContent).toContain('15.0 pts');
    expect(failureRate.classList).toContain('stats-change--good');
    expect(marker('statistics.runs.yourRuns').textContent).toContain('5.0%');
    expect(panel().textContent).not.toContain(
      t('statistics.change.noComparison'),
    );
    // Run cost covers only requests inside Runs; error kinds read as names.
    expect(panel().textContent).toContain(t('statistics.col.runCost'));
    expect(panel().textContent).toContain(t('statistics.errorKind.rate_limit'));

    buttonNamed('statistics.range.all').click();
    await waitForCondition(() =>
      panel().textContent.includes(t('statistics.change.noComparison')),
    );
    expect(panel().querySelector('.stats-tiles .stats-change')).toBeNull();
  });

  it('shows a revisited tab from its cache while one request revalidates it', async () => {
    const answers = [];
    rpcMock.mockImplementation(
      routedRpc(undefined, (params) => {
        if (!params.sections.includes('overview')) {
          return makeReport(params.sections);
        }
        const answer = deferred();
        answers.push(answer);
        return answer.promise;
      }),
    );
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    flushSync();
    answers[0].resolve(makeReport(['overview']));
    await waitForOverview();

    buttonNamed('statistics.subview.usage').click();
    await waitForCondition(() =>
      panel().textContent.includes('Plan the release'),
    );
    buttonNamed('statistics.subview.overview').click();
    flushSync();

    // The cached numbers show at once; the toolbar says they are updating.
    expect(tileText('statistics.overview.cost').value).toBe('$12.50');
    expect(
      document.querySelector('.stats-toolbar__meta').textContent,
    ).toContain(t('statistics.updating'));
    expect(overviewCalls()).toHaveLength(2);

    // Leaving and returning while that request is pending starts no other.
    buttonNamed('statistics.subview.usage').click();
    flushSync();
    buttonNamed('statistics.subview.overview').click();
    flushSync();
    expect(overviewCalls()).toHaveLength(2);

    const updated = makeReport(['overview']);
    updated.overview.totals.cost_usd = 20;
    answers[1].resolve(updated);
    await waitForCondition(
      () => tileText('statistics.overview.cost').value === '$20.00',
    );
    expect(
      document.querySelector('.stats-toolbar__meta').textContent,
    ).not.toContain(t('statistics.updating'));
  });

  it('remembers the chosen range across visits', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW);
    rpcMock.mockImplementation(routedRpc());
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();

    buttonNamed('statistics.range.7d').click();
    flushSync();
    expect(overviewCalls().at(-1)).toEqual({
      since: '2026-06-07T00:00:00.000Z',
      timezone: 'UTC',
      sections: ['overview'],
    });
    expect(localStorage.getItem('vbot.statistics.range')).toBe('7d');

    await unmount(suite.mountedComponent);
    suite.mountedComponent = null;
    document.body.innerHTML = '';
    rpcMock.mockClear();

    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    flushSync();
    expect(
      buttonNamed('statistics.range.7d').getAttribute('aria-pressed'),
    ).toBe('true');
    expect(overviewCalls()[0].since).toBe('2026-06-07T00:00:00.000Z');

    buttonNamed('statistics.range.all').click();
    flushSync();
    expect(overviewCalls().at(-1)).toEqual({
      timezone: 'UTC',
      sections: ['overview'],
    });
  });

  it('keeps the shown numbers under an error banner and retries on request', async () => {
    let fail = true;
    rpcMock.mockImplementation(
      routedRpc(undefined, (params) =>
        fail && params.sections.includes('overview')
          ? Promise.reject(new Error('report-error-sentinel'))
          : makeReport(params.sections),
      ),
    );
    suite.mountedComponent = mount(StatisticsView, { target: document.body });

    // A first answer that fails: the error, no placeholder.
    await waitForCondition(() => panel().querySelector('.banner--error'));
    expect(panel().textContent).toContain('report-error-sentinel');
    expect(panel().querySelector('.stats-skeleton')).toBeNull();

    fail = false;
    buttonNamed('common.retry').click();
    await waitForOverview();
    expect(panel().querySelector('.banner--error')).toBeNull();

    // A failed refresh keeps the last numbers below the banner.
    fail = true;
    buttonNamed('common.refresh').click();
    await waitForCondition(() => panel().querySelector('.banner--error'));
    expect(tileText('statistics.overview.cost').value).toBe('$12.50');

    fail = false;
    buttonNamed('common.retry').click();
    await waitForCondition(() => !panel().querySelector('.banner--error'));
    expect(overviewCalls()).toHaveLength(4);
  });

  it('switches the Costs & tokens breakdown and opens it from the Overview', async () => {
    rpcMock.mockImplementation(routedRpc());
    const navigation = createStandaloneNavigation(['overview']);
    suite.mountedComponent = mount(StatisticsView, {
      target: document.body,
      props: { navigation },
    });
    await waitForOverview();

    buttonNamed('statistics.overview.allModels').click();
    await waitForCondition(() =>
      document.querySelector('#statistics-usage-breakdown'),
    );
    const breakdown = () =>
      document.querySelector('#statistics-usage-breakdown');
    expect(navigation.place).toEqual(['usage']);
    expect(
      document
        .querySelector('#statistics-usage-dimension-tab-model')
        .getAttribute('aria-selected'),
    ).toBe('true');
    expect(breakdown().textContent).toContain('model-breakdown-sentinel');

    // Calls made outside any Session are their own Agent row.
    document.querySelector('#statistics-usage-dimension-tab-agent').click();
    flushSync();
    expect(breakdown().textContent).toContain(
      t('statistics.cost.withoutSession'),
    );

    document.querySelector('#statistics-usage-dimension-tab-project').click();
    flushSync();
    expect(breakdown().textContent).not.toContain('model-breakdown-sentinel');
    expect(breakdown().textContent).toContain(
      t('statistics.usage.identityUsage'),
    );
    expect(breakdown().textContent).toContain('docs');
    // One request served every dimension.
    expect(
      reportCalls().filter((params) => params.sections.includes('usage')),
    ).toHaveLength(1);
  });

  it('lists the insights it knows, links each to its tab and flags an uncertain cost', async () => {
    rpcMock.mockImplementation(routedRpc());
    const navigation = createStandaloneNavigation(['overview']);
    suite.mountedComponent = mount(StatisticsView, {
      target: document.body,
      props: { navigation },
    });
    await waitForOverview();

    const items = [...document.querySelectorAll('.stats-insights__item')];
    expect(items).toHaveLength(2);
    expect(items[1].textContent).toContain(
      'web_fetch was rejected in 40.0% of 10 calls.',
    );
    expect(items[0].classList).toContain('stats-insights__item--warn');
    const warning = tileText('statistics.overview.cost').tile.querySelector(
      '.stats-tile__warning',
    );
    expect(warning.getAttribute('aria-label')).toContain('25.0%');

    items[1].querySelector('button').click();
    await waitForCondition(() => panel().textContent.includes('web_fetch'));
    expect(navigation.place).toEqual(['tools']);
  });

  it('offers the Extensions tab only while the range has Extension activity', async () => {
    rpcMock.mockImplementation(
      routedRpc(undefined, (params) =>
        params.sections.includes('extensions') && params.since
          ? makeReport(['extensions'], { extensions: { extensions: [] } })
          : makeReport(params.sections),
      ),
    );
    suite.mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForOverview();
    await waitForCondition(() =>
      reportCalls().some((params) => params.sections.includes('extensions')),
    );
    flushSync();
    expect(buttonNamed('statistics.subview.extensions')).toBeUndefined();

    buttonNamed('statistics.range.all').click();
    await waitForCondition(() =>
      Boolean(buttonNamed('statistics.subview.extensions')),
    );
    buttonNamed('statistics.subview.extensions').click();
    await waitForCondition(() =>
      panel().textContent.includes('Walross research'),
    );
    expect(tileText('statistics.extensions.groups').value).toBe('1');
    expect(tileText('statistics.extensions.runs').detail).toBe(
      t('statistics.extensions.unfinishedRuns', { count: '1' }),
    );
    expect(tileText('statistics.extensions.runs').value).toBe('3');
  });

  it('opens the tab a place names and corrects retired and empty places', async () => {
    rpcMock.mockImplementation(routedRpc());
    const navigation = createStandaloneNavigation(['compactions']);
    suite.mountedComponent = mount(StatisticsView, {
      target: document.body,
      props: { navigation },
    });
    await waitForCondition(() =>
      panel().textContent.includes(t('statistics.diagnostics.intro')),
    );
    expect(navigation.place).toEqual(['diagnostics']);
    expect(reportCalls()[0].sections).toEqual(['diagnostics']);

    navigation.navigate([]);
    await waitForOverview();
    expect(navigation.place).toEqual(['overview']);
  });
});
