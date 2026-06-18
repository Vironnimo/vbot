// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
}));

const { default: StatisticsView } = await import('../StatisticsView.svelte');

function makeReport(overrides = {}) {
  return {
    generated_at: '2026-06-13T10:00:00+00:00',
    window: { since: null, until: null },
    overview: {
      total_agents: 2,
      total_sessions: 3,
      total_runs: 4,
      open_run_groups: 1,
      total_messages: 20,
      messages_by_role: {
        system: 1,
        user: 5,
        assistant: 6,
        tool: 4,
        note: 1,
        error: 1,
        compaction_checkpoint: 0,
        run_summary: 2,
      },
      last_activity: '2026-06-13T09:00:00+00:00',
      run_status: { completed: 3, failed: 1, cancelled: 0 },
      average_run_duration_ms: 1500,
      median_run_duration_ms: 1200,
      runs_with_tool_calls: 2,
      total_tool_calls: 7,
      agents: [
        {
          agent_id: 'main',
          sessions: 2,
          runs: 3,
          messages: 15,
          errors: 1,
          last_activity: '2026-06-13T09:00:00+00:00',
        },
      ],
      daily_trend: [
        { date: '2026-06-12', runs: 2, errors: 1 },
        { date: '2026-06-13', runs: 2, errors: 0 },
      ],
    },
    usage: {
      totals: {
        assistant_messages: 6,
        measured_turns: 5,
        estimated_turns: 1,
        measured_input_tokens: 1000,
        measured_output_tokens: 200,
        estimated_input_tokens: 30,
        estimated_output_tokens: 5,
        cache_read_tokens: 50,
        cache_write_tokens: 10,
      },
      providers: [
        {
          provider: 'openrouter',
          runs: 3,
          assistant_messages: 5,
          measured_input_tokens: 1000,
          measured_output_tokens: 200,
          estimated_input_tokens: 0,
          estimated_output_tokens: 0,
          estimated_turns: 0,
          errors: 1,
          total_tokens: 1200,
        },
      ],
      models: [
        {
          provider: 'openrouter',
          model: 'openrouter/anthropic/claude-sonnet-4',
          runs: 3,
          assistant_messages: 5,
          measured_input_tokens: 1000,
          measured_output_tokens: 200,
          estimated_input_tokens: 30,
          estimated_output_tokens: 5,
          estimated_turns: 1,
          errors: 1,
          total_tokens: 1235,
          average_run_duration_ms: 1500,
        },
      ],
      daily: [
        {
          date: '2026-06-13',
          runs: 2,
          errors: 0,
          measured_input_tokens: 1000,
          measured_output_tokens: 200,
          estimated_input_tokens: 30,
          estimated_output_tokens: 5,
        },
      ],
    },
    runs: {
      total_runs: 4,
      open_run_groups: 1,
      status: { completed: 3, failed: 1, cancelled: 0 },
      cancel_rate: 0,
      failure_rate: 0.25,
      duration: {
        count: 4,
        average_ms: 1500,
        p50_ms: 1200,
        p90_ms: 2000,
        p95_ms: 2200,
      },
      runs_with_tool_calls: 2,
      total_tool_calls: 7,
      average_tool_calls_per_run: 1.75,
      derived_fallback_runs: 1,
      runs_per_agent: [{ agent_id: 'main', runs: 3 }],
      top_sessions_by_runs: [],
      runs_per_day: [{ date: '2026-06-13', count: 2 }],
      longest_runs: [
        {
          agent_id: 'main',
          session_id: 's1',
          run_id: 'r1',
          status: 'completed',
          duration_ms: 2200,
          started_at: '2026-06-13T08:00:00+00:00',
          completed_at: '2026-06-13T08:00:02+00:00',
          models: ['openrouter/anthropic/claude-sonnet-4'],
        },
      ],
    },
    errors: {
      total_errors: 1,
      by_kind: [{ key: 'rate_limit', count: 1 }],
      by_provider: [{ key: 'openrouter', count: 1 }],
      by_model: [{ key: 'openrouter/anthropic/claude-sonnet-4', count: 1 }],
      by_agent: [{ key: 'main', count: 1 }],
      by_hour: Array.from({ length: 24 }, (_, hour) => ({
        hour,
        count: hour === 9 ? 1 : 0,
      })),
      daily: [{ date: '2026-06-12', count: 1 }],
    },
    tools: {
      total_calls: 7,
      tools: [
        {
          name: 'read',
          calls: 5,
          successes: 4,
          failures: 1,
          success_rate: 0.8,
          error_rate: 0.2,
          average_duration_ms: 40,
          p95_duration_ms: 120,
          top_error_code: 'not_found',
          error_codes: [{ key: 'not_found', count: 1 }],
        },
      ],
      by_agent: [{ key: 'main', count: 7 }],
      top_sessions: [{ agent_id: 'main', session_id: 's1', calls: 7 }],
    },
    ...overrides,
  };
}

function makeUsageReport(overrides = {}) {
  return {
    generated_at: '2026-06-16T12:00:00+00:00',
    providers: [
      {
        connection: 'openai:subscription',
        display_name: 'OpenAI',
        plan: 'Plus',
        windows: [
          {
            label: '5h',
            used_percent: 42.5,
            reset_at: '2099-06-16T15:00:00+00:00',
          },
          {
            label: 'Week',
            used_percent: 88,
            reset_at: '2099-06-20T00:00:00+00:00',
          },
        ],
        error: null,
      },
      {
        connection: 'github-copilot:oauth',
        display_name: 'GitHub Copilot',
        plan: null,
        windows: [],
        error: 'HTTP 401',
      },
    ],
    ...overrides,
  };
}

function routedRpc(usageReport) {
  return (method) =>
    method === 'provider.usage'
      ? Promise.resolve(usageReport)
      : Promise.resolve(makeReport());
}

function openLimitsTab() {
  const limitsTab = [...document.querySelectorAll('.stats-view__tab')].find(
    (button) => button.textContent.trim() === 'Limits',
  );
  limitsTab.click();
}

async function waitForCondition(predicate, attempts = 50) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    flushSync();
    if (predicate()) {
      return;
    }
    await Promise.resolve();
  }
  flushSync();
  if (!predicate()) {
    throw new Error('condition not met');
  }
}

describe('StatisticsView', () => {
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
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('loads the report on mount and renders the overview', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    expect(rpcMock).toHaveBeenCalledWith('statistics.report');
    expect(document.body.textContent).toContain('Run status');
    // total runs stat card value.
    expect(document.body.textContent).toContain('Open runs');
    expect(document.body.textContent).toContain('main');
  });

  it('switches to the usage sub-view and badges estimated tokens', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    const usageTab = [...document.querySelectorAll('.stats-view__tab')].find(
      (button) => button.textContent.trim() === 'Usage',
    );
    usageTab.click();
    flushSync();

    expect(document.body.textContent).toContain('Providers');
    expect(document.body.textContent).toContain(
      'openrouter/anthropic/claude-sonnet-4',
    );
    expect(document.body.textContent).toContain('~ estimated');
  });

  it('renders the runs & errors sub-view with derived fallback labelling', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    const runsTab = [...document.querySelectorAll('.stats-view__tab')].find(
      (button) => button.textContent.trim() === 'Runs & errors',
    );
    runsTab.click();
    flushSync();

    expect(document.body.textContent).toContain('Fallback runs (derived)');
    expect(document.body.textContent).toContain('Longest runs');
    expect(document.body.textContent).toContain('By hour of day');
  });

  it('renders the tools sub-view without exposing arguments', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    const toolsTab = [...document.querySelectorAll('.stats-view__tab')].find(
      (button) => button.textContent.trim() === 'Tools',
    );
    toolsTab.click();
    flushSync();

    expect(document.body.textContent).toContain('Per tool');
    expect(document.body.textContent).toContain('read');
    expect(document.body.textContent).toContain('not_found');
    expect(document.body.textContent).toContain(
      'Tool arguments are never collected.',
    );
  });

  it('lazily loads provider usage when the Limits sub-view opens', async () => {
    rpcMock.mockImplementation(routedRpc(makeUsageReport()));

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    // provider.usage is not fetched until the Limits tab is opened.
    expect(rpcMock).not.toHaveBeenCalledWith('provider.usage');

    openLimitsTab();
    await waitForCondition(() => document.body.textContent.includes('OpenAI'));

    expect(rpcMock).toHaveBeenCalledWith('provider.usage');
    expect(document.body.textContent).toContain('Plus');
    expect(document.body.textContent).toContain('5h');
    expect(document.body.textContent).toContain('Resets in');
    // The error snapshot renders its message cleanly rather than crashing.
    expect(document.body.textContent).toContain('GitHub Copilot');
    expect(document.body.textContent).toContain('HTTP 401');
  });

  it('shows the limits empty state when no providers are connected', async () => {
    rpcMock.mockImplementation(
      routedRpc({ generated_at: '2026-06-16T12:00:00+00:00', providers: [] }),
    );

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    openLimitsTab();
    await waitForCondition(() =>
      document.body.textContent.includes(
        'No subscription providers connected.',
      ),
    );

    expect(document.body.textContent).toContain(
      'No subscription providers connected.',
    );
  });

  it('shows an error message and retries on failure', async () => {
    rpcMock.mockRejectedValueOnce(new Error('boom'));

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() => document.body.textContent.includes('boom'));

    rpcMock.mockResolvedValueOnce(makeReport());
    const retryButton = [...document.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === 'Retry',
    );
    retryButton.click();
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    expect(rpcMock).toHaveBeenCalledTimes(2);
  });
});
