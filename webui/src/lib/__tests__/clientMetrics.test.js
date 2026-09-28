import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { rpc } from '../api/transport.js';
import {
  INVALIDATION_WAVE_MS,
  bucketIndex,
  noteExtensionPageInvalidation,
  noteInvalidation,
  startClientMetrics,
  trackRpc,
} from '../clientMetrics.js';

class FakeObserver {
  static supportedEntryTypes = ['longtask'];
  static instances = [];

  constructor(callback) {
    this.callback = callback;
    this.disconnected = false;
    FakeObserver.instances.push(this);
  }

  observe(options) {
    this.type = options.type;
  }

  disconnect() {
    this.disconnected = true;
  }

  emit(...durations) {
    this.callback({
      getEntries: () => durations.map((duration) => ({ duration })),
    });
  }
}

function okFetch() {
  return vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ ok: true, result: {} }),
  }));
}

describe('client metrics', () => {
  let clock;
  let reports;
  let stop;

  beforeEach(() => {
    vi.useFakeTimers();
    clock = 1000;
    reports = [];
    FakeObserver.instances = [];
    stop = startClientMetrics({
      report: async (payload) => reports.push(payload),
      intervalMs: 60_000,
      now: () => clock,
      Observer: FakeObserver,
    });
  });

  afterEach(() => {
    stop();
    vi.useRealTimers();
  });

  async function flush() {
    await vi.advanceTimersByTimeAsync(60_000);
  }

  it('sorts durations into the server histogram buckets', () => {
    // Indexes from core.performance._metrics.bucket_index.
    const cases = [
      [0.005, 0],
      [0.01, 1],
      [1, 49],
      [2.5, 58],
      [16.6, 78],
      [50, 90],
      [1234.5, 124],
      [3_600_000, 208],
    ];
    expect(cases.map(([ms]) => bucketIndex(ms))).toEqual(
      cases.map(([, index]) => index),
    );
  });

  it('reports RPC timing, first-wave causes, Long Tasks and page invalidations once per interval', async () => {
    noteInvalidation('extensions');
    clock += 100;
    const inWave = trackRpc('extensions.pages');
    clock += 40;
    inWave(true);
    clock += INVALIDATION_WAVE_MS;
    const afterWave = trackRpc('extensions.pages');
    clock += 10;
    afterWave(false);
    FakeObserver.instances[0].emit(80, 120);
    noteExtensionPageInvalidation('change');

    await flush();
    await flush();

    expect(FakeObserver.instances.map((observer) => observer.type)).toEqual([
      'longtask',
    ]);
    expect(reports).toEqual([
      {
        histograms: {
          'webui.rpc': {
            count: 2,
            sum_ms: 50,
            min_ms: 10,
            max_ms: 40,
            buckets: { [bucketIndex(10)]: 1, [bucketIndex(40)]: 1 },
          },
          'webui.long_task': {
            count: 2,
            sum_ms: 200,
            min_ms: 80,
            max_ms: 120,
            buckets: { [bucketIndex(80)]: 1, [bucketIndex(120)]: 1 },
          },
        },
        counters: {
          'webui.invalidations.extensions': 1,
          'webui.invalidation_rpcs.extensions.extensions.pages': 1,
          'webui.rpc_errors': 1,
          'webui.extension_page.invalidations.change': 1,
        },
      },
    ]);
  });

  it('measures RPCs through the transport, except untracked ones', async () => {
    const fetch = okFetch();

    await rpc('settings.get', {}, { fetch });
    await rpc('performance.client_report', {}, { fetch, untracked: true });
    await flush();

    expect(fetch).toHaveBeenCalledTimes(2);
    expect(reports).toHaveLength(1);
    expect(reports[0].histograms['webui.rpc'].count).toBe(1);
  });

  it('stops observing and reporting when stopped', async () => {
    stop();
    noteInvalidation('agents');
    trackRpc('agent.list')(true);
    await flush();

    expect(FakeObserver.instances[0].disconnected).toBe(true);
    expect(reports).toEqual([]);
  });
});
