// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../../lib/i18n.js';
import { rpcBackedApiMock } from '../../__tests__/apiMock.support.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: LimitHistory } = await import('../LimitHistory.svelte');

function historyReport() {
  const snapshot = (usedPercent) => ({
    connection: 'openai:subscription',
    account: 'default',
    display_name: 'OpenAI',
    plan: 'Plus',
    credits: null,
    windows: [
      {
        label: '5h',
        used_percent: usedPercent,
        reset_at: '2026-07-25T15:00:00+00:00',
        window_seconds: 18000,
      },
    ],
    error: null,
  });
  return {
    generated_at: '2026-07-25T12:00:00+00:00',
    samples: [
      {
        sampled_at: '2026-07-25T10:00:00+00:00',
        providers: [snapshot(10)],
      },
      {
        sampled_at: '2026-07-25T11:00:00+00:00',
        providers: [snapshot(30)],
      },
    ],
  };
}

function runActivityReport(runs = []) {
  return {
    generated_at: '2026-07-25T12:00:00+00:00',
    window: {
      since: '2026-07-25T10:00:00+00:00',
      until: '2026-07-25T11:00:00+00:00',
    },
    total_runs: runs.length,
    truncated: false,
    runs,
  };
}

function routeHistory(runs) {
  return (method) => {
    if (method === 'provider.usage_history.clear') {
      return Promise.resolve({ deleted_samples: 2 });
    }
    return Promise.resolve(
      method === 'provider.usage_history'
        ? historyReport()
        : runActivityReport(runs),
    );
  };
}

function clearCalls() {
  return rpcMock.mock.calls.filter(
    ([method]) => method === 'provider.usage_history.clear',
  ).length;
}

function buttonsLabelled(label) {
  return [...document.querySelectorAll('button')].filter(
    (button) => button.textContent.trim() === label,
  );
}

async function waitForCondition(predicate, attempts = 50) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    flushSync();
    if (predicate()) {
      return;
    }
    await Promise.resolve();
  }
  throw new Error('condition not met');
}

describe('LimitHistory', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    rpcMock.mockReset();
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
    }
    document.body.innerHTML = '';
  });

  it('renders the hourly trace and the vBot Runs overlapping its interval', async () => {
    rpcMock.mockImplementation(
      routeHistory([
        {
          agent_id: 'main',
          session_id: 's1',
          session_title: 'Investigate limits',
          run_id: 'r1',
          status: 'completed',
          started_at: '2026-07-25T10:15:00+00:00',
          completed_at: '2026-07-25T10:16:00+00:00',
          duration_ms: 60000,
          models: ['openai/gpt-5'],
          tool_calls: 2,
          measured_input_tokens: 100,
          measured_output_tokens: 20,
          estimated_input_tokens: 5,
          estimated_output_tokens: 2,
        },
      ]),
    );

    mountedComponent = mount(LimitHistory, { target: document.body });
    await waitForCondition(() => document.querySelector('.limit-run__tokens'));

    expect(document.body.textContent).toContain('+20 pp');
    expect(document.querySelectorAll('.limit-trace__line')).toHaveLength(1);
    expect(rpcMock).toHaveBeenCalledWith('statistics.run_activity', {
      since: '2026-07-25T10:00:00+00:00',
      until: '2026-07-25T11:00:00+00:00',
    });
    expect(document.querySelector('.limit-run__tokens').textContent).toContain(
      '127',
    );
    expect(document.querySelector('.limit-run__head').textContent).toContain(
      'Completed',
    );

    // Every snapshot is a focusable slot; the trace keeps one Tab stop, on
    // the latest snapshot, and arrow keys move it between snapshots.
    const slots = () => [...document.querySelectorAll('.limit-trace__slot')];
    expect(slots()).toHaveLength(2);
    expect(slots().map((slot) => slot.tabIndex)).toEqual([-1, 0]);
    expect(slots()[1].getAttribute('aria-label')).toMatch(/: 30% used$/);
    slots()[1].focus();
    slots()[1].dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Home', bubbles: true }),
    );
    flushSync();
    expect(document.activeElement).toBe(slots()[0]);
    expect(slots().map((slot) => slot.tabIndex)).toEqual([0, -1]);
  });

  it('lists ten overlapping Runs until all are asked for', async () => {
    rpcMock.mockImplementation(
      routeHistory(
        Array.from({ length: 12 }, (_, index) => ({
          agent_id: 'main',
          session_id: `s${index}`,
          session_title: `Run ${index}`,
          run_id: `r${index}`,
          status: 'completed',
          started_at: '2026-07-25T10:15:00+00:00',
          duration_ms: 1000,
          models: [],
          tool_calls: 0,
        })),
      ),
    );

    mountedComponent = mount(LimitHistory, { target: document.body });
    await waitForCondition(() => document.querySelector('.limit-runs'));
    const runs = () => document.querySelectorAll('.limit-runs > li');
    expect(runs()).toHaveLength(10);

    const toggle = document.querySelector('.limit-runs__toggle');
    expect(toggle.textContent.trim()).toBe(
      t('statistics.limits.showAllRuns', { count: '12' }),
    );
    toggle.click();
    flushSync();
    expect(runs()).toHaveLength(12);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
  });

  it('deletes the history only after confirmation', async () => {
    rpcMock.mockImplementation(routeHistory([]));

    mountedComponent = mount(LimitHistory, { target: document.body });
    await waitForCondition(() =>
      document.querySelector('.limit-activity .empty-state'),
    );

    buttonsLabelled(t('statistics.limits.deleteHistory'))[0].click();
    flushSync();
    expect(document.querySelector('[role="dialog"]')).toBeTruthy();
    expect(clearCalls()).toBe(0);

    buttonsLabelled(t('statistics.limits.deleteHistoryConfirm')).at(-1).click();
    await waitForCondition(() =>
      document.querySelector('.limit-history > .empty-state'),
    );
    expect(clearCalls()).toBe(1);
    expect(document.querySelectorAll('.limit-trace')).toHaveLength(0);
  });

  it('shows a baseline empty state before the first snapshot exists', async () => {
    rpcMock.mockResolvedValue({
      generated_at: '2026-07-25T12:00:00+00:00',
      samples: [],
    });

    mountedComponent = mount(LimitHistory, { target: document.body });
    await waitForCondition(() =>
      document.querySelector('.limit-history > .empty-state'),
    );

    expect(document.querySelectorAll('.limit-trace')).toHaveLength(0);
  });
});
