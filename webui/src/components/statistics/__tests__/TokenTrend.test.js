// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: TokenTrend } = await import('../TokenTrend.svelte');

function usageReport(daily, totals) {
  return {
    generated_at: '2026-09-23T12:00:00+00:00',
    window: {},
    usage: { daily, totals },
  };
}

const OLD_USAGE = {
  measured_input_tokens: 900,
  measured_output_tokens: 100,
  estimated_input_tokens: 0,
  estimated_output_tokens: 0,
};

describe('TokenTrend', () => {
  let target;
  let component;

  beforeEach(() => {
    init('en');
    target = document.createElement('div');
    document.body.append(target);
  });

  afterEach(() => {
    if (component) unmount(component);
    component = null;
    target.remove();
  });

  function render(report, granularity = 'day') {
    component = mount(TokenTrend, {
      target,
      props: { report, granularity, reportRange: 'all' },
    });
    flushSync();
  }

  it('offers the month period when all recorded usage predates the day window', () => {
    render(usageReport([{ date: '2026-08-17', ...OLD_USAGE }], OLD_USAGE));

    expect(target.querySelector('.empty-state')).not.toBeNull();
    const action = target.querySelector('.empty-state__actions button');
    expect(action).not.toBeNull();

    action.click();
    flushSync();

    expect(target.querySelector('.empty-state')).toBeNull();
    expect(target.querySelector('.stats-token-chart')).not.toBeNull();
  });

  it('keeps the plain empty state when nothing was ever recorded', () => {
    render(usageReport([], {}));

    expect(target.querySelector('.empty-state')).not.toBeNull();
    expect(target.querySelector('.empty-state__actions')).toBeNull();
  });
});
