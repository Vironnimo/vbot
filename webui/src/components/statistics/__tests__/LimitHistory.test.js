// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';
import { rpcBackedApiMock } from '../../__tests__/apiMock.js';

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

function runActivityReport() {
  return {
    generated_at: '2026-07-25T12:00:00+00:00',
    window: {
      since: '2026-07-25T10:00:00+00:00',
      until: '2026-07-25T11:00:00+00:00',
    },
    total_runs: 0,
    truncated: false,
    runs: [],
  };
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

  it('renders the hourly trace and loads overlapping Run activity', async () => {
    rpcMock.mockImplementation((method) =>
      Promise.resolve(
        method === 'provider.usage_history'
          ? historyReport()
          : runActivityReport(),
      ),
    );

    mountedComponent = mount(LimitHistory, { target: document.body });
    await waitForCondition(() => document.querySelector('.limit-trace__line'));
    await waitForCondition(() =>
      document.querySelector('.limit-activity .empty-state'),
    );

    expect(document.body.textContent).toContain('+20 pp');
    expect(document.querySelectorAll('.limit-trace__line')).toHaveLength(1);
    expect(rpcMock).toHaveBeenCalledWith('statistics.run_activity', {
      since: '2026-07-25T10:00:00+00:00',
      until: '2026-07-25T11:00:00+00:00',
    });
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
