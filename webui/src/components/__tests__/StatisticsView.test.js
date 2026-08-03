// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

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
      total_chat_messages: 11,
      chat_messages_by_role: {
        user: 5,
        assistant: 6,
      },
      total_session_records: 20,
      session_records_by_role: {
        system: 1,
        user: 5,
        assistant: 6,
        tool: 4,
        note: 1,
        error: 1,
        compaction_checkpoint: 0,
        run_summary: 2,
        agent_takeover: 0,
      },
      last_activity: '2026-06-13T09:00:00+00:00',
      run_status: { completed: 3, failed: 1, cancelled: 0, interrupted: 0 },
      average_run_duration_ms: 1500,
      median_run_duration_ms: 1200,
      runs_with_tool_calls: 2,
      total_tool_calls: 7,
      agents: [
        {
          agent_id: 'main',
          sessions: 2,
          runs: 3,
          chat_messages: 9,
          session_records: 15,
          errors: 1,
          last_activity: '2026-06-13T09:00:00+00:00',
        },
      ],
      daily_trend: [
        {
          date: '2026-06-12',
          runs: 2,
          completed: 1,
          failed: 1,
          cancelled: 0,
        },
        {
          date: '2026-06-13',
          runs: 2,
          completed: 2,
          failed: 0,
          cancelled: 0,
        },
      ],
    },
    usage: {
      totals: {
        assistant_messages: 6,
        measured_turns: 5,
        estimated_turns: 1,
        measured_input_tokens: 1000,
        measured_output_tokens: 200,
        reasoning_tokens: 120,
        reasoning_turns: 4,
        estimated_input_tokens: 30,
        estimated_output_tokens: 5,
        cache_read_tokens: 50,
        cache_write_tokens: 10,
        cache_turns: 4,
        cache_input_tokens: 500,
      },
      providers: [
        {
          provider: 'openrouter',
          runs: 3,
          assistant_messages: 5,
          measured_input_tokens: 1000,
          measured_output_tokens: 200,
          reasoning_tokens: 120,
          reasoning_turns: 4,
          estimated_input_tokens: 0,
          estimated_output_tokens: 0,
          estimated_turns: 0,
          errors: 1,
          cache_read_tokens: 50,
          cache_write_tokens: 10,
          cache_turns: 4,
          cache_input_tokens: 500,
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
          reasoning_tokens: 120,
          reasoning_turns: 4,
          estimated_input_tokens: 30,
          estimated_output_tokens: 5,
          estimated_turns: 1,
          errors: 1,
          cache_read_tokens: 50,
          cache_write_tokens: 10,
          cache_turns: 4,
          cache_input_tokens: 500,
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
          reasoning_tokens: 120,
          reasoning_turns: 4,
          estimated_input_tokens: 30,
          estimated_output_tokens: 5,
          cache_read_tokens: 50,
          cache_write_tokens: 10,
          cache_input_tokens: 500,
        },
      ],
      cache: {
        lowest_hit_rate_sessions: [
          {
            agent_id: 'main',
            session_id: 's1',
            cache_turns: 3,
            input_tokens: 500,
            cache_read_tokens: 50,
            cache_write_tokens: 10,
            hit_rate: 0.1,
            last_activity: '2026-06-13T09:00:00+00:00',
          },
        ],
        suspected_breaks: {
          evaluated_turns: 6,
          suspected_turns: 1,
          incidents: [
            {
              agent_id: 'main',
              session_id: 's1',
              timestamp: '2026-06-13T08:30:00+00:00',
              model: 'openrouter/anthropic/claude-sonnet-4',
              previous_input_tokens: 9000,
              cache_read_tokens: 100,
            },
          ],
        },
      },
    },
    runs: {
      total_runs: 4,
      open_run_groups: 1,
      status: { completed: 3, failed: 1, cancelled: 0, interrupted: 0 },
      cancel_rate: 0,
      failure_rate: 0.25,
      interruption_rate: 0,
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
      agent_messages: 6,
      model_steps: 10,
      average_agent_messages_per_run: 1.5,
      average_model_steps_per_run: 2.5,
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
    compactions: {
      total_compactions: 5,
      sessions_with_compactions: 2,
      average_per_compacted_session: 2.5,
      p50_per_compacted_session: 2,
      p95_per_compacted_session: 3,
      max_per_session: 3,
      by_strategy: [
        { strategy: 'summary_tail', compactions: 4 },
        { strategy: 'continuation', compactions: 1 },
      ],
      reclaim: {
        observations: 4,
        total_tokens: 220000,
        average_tokens: 55000,
        p50_tokens: 50000,
        p95_tokens: 70000,
      },
      top_sessions: [
        {
          agent_id: 'main',
          session_id: 'compacted-session',
          compactions: 3,
          estimated_reclaimed_tokens: 150000,
          last_compaction: '2026-06-13T08:45:00+00:00',
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
    skills: {
      total_skills: 3,
      used_skills: 1,
      never_used_skills: 2,
      offered_unactivated_skills: 1,
      skills_without_offer_data: 1,
      skills: [
        {
          name: 'deploy',
          origins: ['bundled'],
          offered_sessions: 10,
          activated_sessions: 4,
          activated_offered_sessions: 4,
          usage_rate: 0.4,
          first_offered: '2026-06-10T08:00:00+00:00',
          last_offered: '2026-06-13T09:00:00+00:00',
          first_activated: '2026-06-11T08:00:00+00:00',
          last_activated: '2026-06-13T08:30:00+00:00',
          by_agent: [
            { key: 'main', count: 3 },
            { key: 'builder@vbot', count: 1 },
          ],
        },
        {
          name: 'lonely-skill',
          origins: ['global', 'project:vBot'],
          offered_sessions: 8,
          activated_sessions: 0,
          activated_offered_sessions: 0,
          usage_rate: 0,
          first_offered: '2026-06-09T08:00:00+00:00',
          last_offered: '2026-06-12T09:00:00+00:00',
          first_activated: null,
          last_activated: null,
          by_agent: [],
        },
        {
          name: 'fresh-skill',
          origins: ['agent:assistant'],
          offered_sessions: 0,
          activated_sessions: 0,
          activated_offered_sessions: 0,
          usage_rate: null,
          first_offered: null,
          last_offered: null,
          first_activated: null,
          last_activated: null,
          by_agent: [],
        },
      ],
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
        account: 'default',
        display_name: 'OpenAI',
        plan: 'Plus',
        credits: { enabled: true, balance: 25 },
        windows: [
          {
            label: '5h',
            used_percent: 42.5,
            reset_at: '2099-06-16T15:00:00+00:00',
            window_seconds: 18000,
          },
          {
            label: 'Week',
            used_percent: 88,
            reset_at: '2099-06-20T00:00:00+00:00',
            window_seconds: 604800,
          },
        ],
        error: null,
      },
      {
        connection: 'github-copilot:oauth',
        account: 'default',
        display_name: 'GitHub Copilot',
        plan: null,
        credits: null,
        windows: [],
        error: 'HTTP 401',
      },
    ],
    ...overrides,
  };
}

function makeUsageHistoryReport() {
  const provider = (usedPercent) => ({
    connection: 'openai:subscription',
    account: 'default',
    display_name: 'OpenAI',
    plan: 'Plus',
    credits: { enabled: true, balance: 25 },
    windows: [
      {
        label: '5h',
        used_percent: usedPercent,
        reset_at: '2026-06-16T15:00:00+00:00',
        window_seconds: 18000,
        used_units: null,
        remaining_units: null,
        total_units: null,
        unit: null,
        unlimited: null,
      },
    ],
    error: null,
  });
  return {
    generated_at: '2026-06-16T12:00:00+00:00',
    samples: [
      {
        sampled_at: '2026-06-16T10:00:00+00:00',
        providers: [provider(20)],
      },
      {
        sampled_at: '2026-06-16T11:00:00+00:00',
        providers: [provider(48.5)],
      },
    ],
  };
}

function makeRunActivityReport() {
  return {
    generated_at: '2026-06-16T12:00:00+00:00',
    window: {
      since: '2026-06-16T10:00:00+00:00',
      until: '2026-06-16T11:00:00+00:00',
    },
    total_runs: 1,
    truncated: false,
    runs: [
      {
        agent_id: 'main',
        session_id: 's1',
        session_title: 'Investigate limits',
        run_id: 'r1',
        status: 'completed',
        started_at: '2026-06-16T10:15:00+00:00',
        completed_at: '2026-06-16T10:16:00+00:00',
        duration_ms: 60000,
        models: ['openai/gpt-5'],
        tool_calls: 2,
        measured_input_tokens: 100,
        measured_output_tokens: 20,
        estimated_input_tokens: 5,
        estimated_output_tokens: 2,
      },
    ],
  };
}

function routedRpc(usageReport) {
  return (method) =>
    method === 'provider.usage'
      ? Promise.resolve(usageReport)
      : method === 'provider.usage_history'
        ? Promise.resolve({
            generated_at: usageReport.generated_at,
            samples: [],
          })
        : Promise.resolve(makeReport());
}

function openLimitsTab() {
  const limitsTab = [...document.querySelectorAll('.tab-list__tab')].find(
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
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  it('loads the report on mount and renders the overview', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    expect(rpcMock).toHaveBeenCalledWith('statistics.report');
    expect(document.body.textContent).toContain(
      'subscription limits keep one automatic local snapshot per hour',
    );
    expect(document.body.textContent).toContain('Run health');
    expect(document.body.textContent).toContain('75.0%');
    expect(document.body.textContent).toContain('Activity & reliability');
    expect(document.body.textContent).toContain('Last 30 days');
    expect(document.querySelectorAll('.stats-activity__col')).toHaveLength(30);
    expect(document.querySelectorAll('.stats-health__segment')).toHaveLength(4);
    expect(document.querySelector('.stats-donut')).toBeNull();
    expect(
      document.querySelector('.stats-activity__legend').textContent,
    ).not.toContain('Errors');
    const chatMessagesCard = [...document.querySelectorAll('.stats-card')].find(
      (card) => card.textContent.includes('Chat messages'),
    );
    expect(chatMessagesCard.textContent).toContain('11');
    expect(document.body.textContent).toContain(
      'Visible chat messages by role',
    );
    expect(document.body.textContent).toContain('Model steps');
    expect(document.body.textContent).toContain('Stored Session records');
    expect(document.body.textContent).toContain('System reminder');
    expect(document.body.textContent).toContain('Run summary');
    // "Open run groups" lives on the Runs & errors tab now, not the overview.
    expect(document.body.textContent).not.toContain('Open run');
    expect(document.body.textContent).toContain('main');
    expect(document.querySelector('.stats-view.view-frame')).toBeTruthy();
    expect(document.querySelector('.stats-view .view-header')).toBeTruthy();
    expect(
      document.querySelector(
        '.stats-view .view-toolbar--tabs [role="tablist"]',
      ),
    ).toBeTruthy();
    expect(
      document.querySelector('.stats-view .view-toolbar__actions'),
    ).toBeTruthy();
  });

  it('switches the calendar-correct activity window with the granularity', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Activity & reliability'),
    );

    const weekButton = [
      ...document.querySelectorAll('.stats-toggle__option'),
    ].find((button) => button.textContent.trim() === 'Week');
    weekButton.click();
    flushSync();

    expect(document.body.textContent).toContain('Last 16 weeks');
    expect(document.querySelectorAll('.stats-activity__col')).toHaveLength(16);
  });

  it('shows a period-specific empty state when all Runs are older than the selected window', async () => {
    const report = makeReport();
    report.overview.daily_trend = [
      {
        date: '2026-04-01',
        runs: 4,
        completed: 3,
        failed: 1,
        cancelled: 0,
      },
    ];
    rpcMock.mockResolvedValue(report);

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('No Runs in this period.'),
    );

    expect(document.querySelector('.stats-activity')).toBeNull();
    expect(document.body.textContent).toContain('Last 30 days');
  });

  it('renders unavailable outcome shares instead of a misleading zero percent with no Runs', async () => {
    const report = makeReport();
    report.overview.total_runs = 0;
    report.overview.run_status = {
      completed: 0,
      failed: 0,
      cancelled: 0,
      interrupted: 0,
    };
    report.overview.daily_trend = [];
    rpcMock.mockResolvedValue(report);

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('No activity recorded yet.'),
    );

    expect(
      document.querySelector('.stats-health__hero strong').textContent,
    ).toBe('—');
    expect(
      [...document.querySelectorAll('.stats-health__share')].map((share) =>
        share.textContent.trim(),
      ),
    ).toEqual(['—', '—', '—', '—']);
  });

  it('switches to the usage sub-view and badges estimated tokens', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    const usageTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Usage',
    );
    usageTab.click();
    flushSync();

    expect(document.body.textContent).toContain('Providers');
    expect(document.body.textContent).toContain(
      'openrouter/anthropic/claude-sonnet-4',
    );
    expect(document.body.textContent).toContain('~ estimated');
    expect(document.body.textContent).toContain('Reasoning (output subset)');
    expect(document.body.textContent).toContain(
      'Reasoning tokens are provider-reported subsets of measured output',
    );
    const reasoningCard = [...document.querySelectorAll('.stats-card')].find(
      (card) => card.textContent.includes('Reasoning (output subset)'),
    );
    expect(reasoningCard.querySelector('.stats-card__value').textContent).toBe(
      '120',
    );
    expect(document.body.textContent).toContain(
      'A fallback Run can appear in multiple rows',
    );
  });

  it('renders cache hit rate, worst sessions and suspected breaks in the usage sub-view', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    const usageTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Usage',
    );
    usageTab.click();
    flushSync();

    const text = document.body.textContent;
    expect(text).toContain('Cache hit rate');
    // totals: 50 read of 500 cache-reporting input → 10.0%
    expect(text).toContain('10.0%');
    expect(text).toContain('Sessions with lowest cache hit rate');
    expect(text).toContain('Suspected cache breaks (derived)');
    expect(text).toContain(
      '1 suspected breaks across 6 evaluated continuation turns.',
    );
    // The incident table shows the collapsed turn's expectation vs. reality.
    expect(text).toContain('9,000');
    expect(text).toContain('Prev. input');
  });

  it('renders the runs & errors sub-view with derived fallback labelling', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    const runsTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Runs & errors',
    );
    runsTab.click();
    flushSync();

    expect(document.body.textContent).toContain('Fallback runs (derived)');
    expect(document.body.textContent).toContain('Open run groups');
    expect(document.body.textContent).toContain('Longest runs');
    expect(document.body.textContent).toContain('Avg Agent messages / Run');
    expect(document.body.textContent).toContain('Avg Model steps / Run');
    expect(document.body.textContent).toContain('By UTC hour');
    expect(document.body.textContent).toContain(
      'These are persisted Run errors',
    );
  });

  it('renders checkpoint-derived compaction statistics', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    const compactionsTab = [
      ...document.querySelectorAll('.tab-list__tab'),
    ].find((button) => button.textContent.trim() === 'Compactions');
    compactionsTab.click();
    flushSync();

    const text = document.body.textContent;
    expect(text).toContain('Compacted Sessions');
    expect(text).toContain('Estimated context reclaimed');
    expect(text).toContain('220,000');
    expect(text).toContain('By Strategy');
    expect(text).toContain('summary_tail');
    expect(text).toContain('Most compacted Sessions');
    expect(text).toContain('compacted-session');
    expect(text).toContain('150,000');
    expect(text).toContain('Fork-copied history is excluded');
  });

  it('renders the tools sub-view without exposing arguments', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    const toolsTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Tools',
    );
    toolsTab.click();
    flushSync();

    expect(document.body.textContent).toContain('Per tool');
    expect(document.body.textContent).toContain('read');
    expect(document.body.textContent).toContain('not_found');
    expect(document.body.textContent).toContain('Accepted');
    expect(document.body.textContent).toContain('Rejected');
    expect(document.body.textContent).toContain('Top rejection');
    expect(document.body.textContent).toContain(
      'Statistics never reads or includes Tool arguments',
    );
    expect(document.body.textContent).toContain(
      'including safe validation and guardrail rejections',
    );
  });

  it('renders the skills sub-view with per-skill rows, origins, and rates', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    const skillsTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Skills',
    );
    skillsTab.click();
    flushSync();

    const text = document.body.textContent;
    expect(text).toContain('Per skill');
    // Summary cards distinguish evidence-backed candidates from new Skills
    // whose catalogs have not been observed yet.
    expect(text).toContain('No offer conversion');
    expect(text).toContain('No offer data');
    // Per-skill rows.
    expect(text).toContain('deploy');
    expect(text).toContain('lonely-skill');
    // Origins render as short localized labels (agent:<id>/project:<name>).
    expect(text).toContain('bundled');
    expect(text).toContain('project: vBot');
    expect(text).toContain('agent: assistant');
    // usage_rate: 0.4 → 40%; null (offered == 0) → em dash, never NaN.
    expect(text).toContain('40%');
    expect(text).not.toContain('NaN');
    // Panel-wide activations-per-agent rollup shows the project agent.
    expect(text).toContain('Activations per agent');
    expect(text).toContain('builder');
  });

  it('highlights only offered zero-activation skills as candidates', async () => {
    rpcMock.mockResolvedValue(makeReport());

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    const skillsTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Skills',
    );
    skillsTab.click();
    flushSync();

    // lonely-skill has observed opportunities and no activation, so it is the
    // only candidate. fresh-skill has no offer evidence and stays neutral.
    const candidateRows = document.querySelectorAll(
      '.stats-skill-row--candidate',
    );
    expect(candidateRows.length).toBe(1);
    expect(candidateRows[0].textContent).toContain('lonely-skill');
    expect(document.body.textContent).toContain('No offer conversion');
    expect(document.body.textContent).toContain('No offer data');
    // The activated skill (deploy) is not highlighted.
    const rows = [...document.querySelectorAll('.stats-table tbody tr')].filter(
      (row) => row.textContent.includes('deploy'),
    );
    expect(
      rows.some((row) => row.classList.contains('stats-skill-row--candidate')),
    ).toBe(false);
  });

  it('shows the skills empty state instead of crashing when the inventory is empty', async () => {
    rpcMock.mockResolvedValue(
      makeReport({
        skills: {
          total_skills: 0,
          used_skills: 0,
          never_used_skills: 0,
          offered_unactivated_skills: 0,
          skills_without_offer_data: 0,
          skills: [],
        },
      }),
    );

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

    const skillsTab = [...document.querySelectorAll('.tab-list__tab')].find(
      (button) => button.textContent.trim() === 'Skills',
    );
    skillsTab.click();
    flushSync();

    expect(document.body.textContent).toContain(
      'No skills in the current inventory.',
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
    expect(
      [...document.querySelectorAll('.stats-view button')].some(
        (button) => button.textContent.trim() === 'Refresh',
      ),
    ).toBe(false);
    expect(document.querySelector('.stats-view__generated')).toBeNull();
    expect(document.body.textContent).toContain('updated every 10 seconds');
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

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );
    openLimitsTab();
    await waitForCondition(() =>
      document.body.textContent.includes('Largest observed changes'),
    );
    await waitForCondition(() =>
      document.body.textContent.includes('Investigate limits'),
    );

    expect(document.body.textContent).toContain('Subscription flight recorder');
    expect(document.body.textContent).toContain('+28.5 pp');
    expect(document.body.textContent).toContain('Measured tokens');
    expect(document.body.textContent).toContain('120');
    expect(document.body.textContent).toContain(
      'Parallel use outside vBot may also change the Subscription.',
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

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );
    openLimitsTab();
    await waitForCondition(() =>
      document.body.textContent.includes('Largest observed changes'),
    );

    const deleteButton = [...document.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === 'Delete history',
    );
    deleteButton.click();
    flushSync();
    expect(document.body.textContent).toContain('Delete limit history?');

    const confirmButton = [...document.querySelectorAll('button')]
      .filter((button) => button.textContent.trim() === 'Delete history')
      .at(-1);
    confirmButton.click();
    await waitForCondition(() =>
      document.body.textContent.includes('2 historical snapshots deleted.'),
    );

    expect(
      rpcMock.mock.calls.some(
        ([method]) => method === 'provider.usage_history.clear',
      ),
    ).toBe(true);
    expect(document.body.textContent).toContain('The flight recorder is ready');
  });

  it('refreshes provider usage every ten seconds only while Limits is visible', async () => {
    vi.useFakeTimers();
    rpcMock.mockImplementation(routedRpc(makeUsageReport()));

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );

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

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );
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

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );
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

    mountedComponent = mount(StatisticsView, { target: document.body });
    await waitForCondition(() =>
      document.body.textContent.includes('Per agent'),
    );
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
