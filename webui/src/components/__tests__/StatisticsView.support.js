const { mount, flushSync, unmount } = await import('svelte');
// @vitest-environment jsdom
import { afterEach, beforeEach, vi } from 'vitest';

import { init, t } from '../../lib/i18n.js';
import { setApplicationTimeZone } from '../../lib/dateTimePrefs.svelte.js';
import { rpcBackedApiMock } from './apiMock.support.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

// The report cache is a SvelteMap; the client build keeps it reactive.
vi.mock('svelte/reactivity', async () => {
  return import('../../../node_modules/svelte/src/reactivity/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: StatisticsView } = await import('../StatisticsView.svelte');
const { clearStatisticsReports } =
  await import('../../lib/statisticsReports.svelte.js');

// Spec-shaped `statistics.report` sections. Each tab requests only its own
// sections; makeReport() assembles an answer from the requested ones.

function makeTotals(overrides = {}) {
  return {
    calls: 40,
    failed_calls: 2,
    input_tokens: 1_200_000,
    output_tokens: 80_000,
    estimated_input_tokens: 2_000,
    estimated_output_tokens: 500,
    reasoning_tokens: 12_000,
    cache_read_tokens: 600_000,
    cache_write_tokens: 10_000,
    cache_input_tokens: 1_000_000,
    cache_calls: 30,
    cost_usd: 12.5,
    reported_cost_usd: 4.5,
    estimated_cost_usd: 8,
    reported_calls: 10,
    estimated_calls: 28,
    unpriced_calls: 2,
    retrospective_calls: 3,
    unreported_calls: 1,
    uncached_cost_usd: 2,
    uncached_calls: 4,
    estimated_token_calls: 1,
    ...overrides,
  };
}

function makeRunRow(overrides = {}) {
  return {
    run_id: 'run-1',
    agent_id: 'main',
    session_id: 'session-1',
    session_title: 'Plan the release',
    started_at: '2026-06-12T09:00:00+00:00',
    origin: 'user',
    status: 'completed',
    duration_ms: 95_000,
    model_steps: 12,
    tool_calls: 7,
    input_tokens: 90_000,
    output_tokens: 4_000,
    cost_usd: 0.42,
    primary_model: 'openrouter/anthropic/claude-sonnet-4',
    models: ['openrouter/anthropic/claude-sonnet-4'],
    ...overrides,
  };
}

function makeSeries() {
  return [
    {
      date: '2026-06-12',
      cost_usd: 5,
      reported_cost_usd: 2,
      estimated_cost_usd: 3,
      input_tokens: 500_000,
      output_tokens: 30_000,
      runs: 6,
      failed_runs: 1,
    },
    {
      date: '2026-06-13',
      cost_usd: 7.5,
      reported_cost_usd: 2.5,
      estimated_cost_usd: 5,
      input_tokens: 700_000,
      output_tokens: 50_000,
      runs: 4,
      failed_runs: 0,
    },
  ];
}

function makeOverviewSection(overrides = {}) {
  return {
    totals: makeTotals(),
    previous: makeTotals({ cost_usd: 10, input_tokens: 1_000_000 }),
    runs: {
      total: 10,
      completed: 8,
      failed: 1,
      cancelled: 1,
      interrupted: 0,
      running: 0,
    },
    previous_runs: {
      total: 8,
      completed: 6,
      failed: 2,
      cancelled: 0,
      interrupted: 0,
      running: 0,
    },
    user_runs: {
      runs: 7,
      duration_p50_ms: 42_000,
      duration_p90_ms: 180_000,
      cost_p50_usd: 0.31,
      cost_p90_usd: 1.2,
    },
    previous_user_runs: {
      runs: 5,
      duration_p50_ms: 40_000,
      duration_p90_ms: 150_000,
      cost_p50_usd: 0.3,
      cost_p90_usd: 1,
    },
    series: makeSeries(),
    by_origin: [
      { origin: 'user', runs: 7, cost_usd: 9 },
      { origin: 'automation', runs: 3, cost_usd: 3.5 },
    ],
    top_agents: [
      {
        agent_id: 'main',
        runs: 6,
        calls: 30,
        input_tokens: 900_000,
        output_tokens: 60_000,
        cost_usd: 9,
      },
      {
        agent_id: 'writer@docs',
        runs: 4,
        calls: 10,
        input_tokens: 300_000,
        output_tokens: 20_000,
        cost_usd: 3.5,
      },
    ],
    top_models: [
      {
        model: 'openrouter/anthropic/claude-sonnet-4',
        calls: 30,
        cache_read_tokens: 500_000,
        cache_input_tokens: 800_000,
        cache_calls: 25,
        cost_usd: 10,
      },
    ],
    insights: [
      {
        id: 'uncached_value',
        severity: 'warn',
        values: { share: 0.25, cost_usd: 2, top_model: 'local/qwen' },
      },
      {
        id: 'tool_failure',
        severity: 'info',
        values: { tool: 'web_fetch', rate: 0.4, calls: 10 },
      },
      { id: 'unknown_insight', severity: 'warn', values: {} },
    ],
    ...overrides,
  };
}

function breakdownRow(key, overrides = {}) {
  return { key, ...makeTotals(), ...overrides };
}

function makeUsageSection(overrides = {}) {
  return {
    totals: makeTotals(),
    breakdowns: {
      agent: [
        breakdownRow('main', { cost_usd: 9 }),
        breakdownRow('writer@docs', { cost_usd: 3.5 }),
      ],
      model: [
        breakdownRow('openrouter/anthropic/claude-sonnet-4', { cost_usd: 10 }),
        breakdownRow('model-breakdown-sentinel', { cost_usd: 2.5 }),
      ],
      provider: [breakdownRow('openrouter', { cost_usd: 12.5 })],
      project: [
        breakdownRow('', { cost_usd: 9 }),
        breakdownRow('docs', { cost_usd: 3.5 }),
      ],
      origin: [breakdownRow('user', { cost_usd: 12.5 })],
      kind: [breakdownRow('chat', { cost_usd: 12.5 })],
    },
    series: makeSeries(),
    top_runs: [makeRunRow()],
    top_sessions: [
      {
        agent_id: 'main',
        session_id: 'session-1',
        session_title: 'Plan the release',
        runs: 3,
        calls: 12,
        input_tokens: 400_000,
        output_tokens: 20_000,
        cost_usd: 4,
      },
    ],
    recent_calls: [
      {
        timestamp: '2026-06-13T09:30:00+00:00',
        model: 'openrouter/anthropic/claude-sonnet-4',
        kind: 'chat',
        status: 'completed',
        origin: 'user',
        agent_id: 'main',
        session_id: 'session-1',
        session_title: 'Plan the release',
        input_tokens: 12_000,
        output_tokens: 800,
        estimated_tokens: 0,
        retrospective: false,
        cost: {
          source: 'catalog',
          amount_usd: 0.0123,
          pricing: { rates: { input: 3, output: 15 }, source: 'models.dev' },
        },
      },
    ],
    ...overrides,
  };
}

function makeRunsSection(overrides = {}) {
  return {
    totals: {
      total: 10,
      completed: 8,
      failed: 1,
      cancelled: 1,
      interrupted: 0,
      running: 1,
    },
    cancelled: { cost_usd: 0.8, wait_p50_ms: 30_000 },
    by_origin: [
      {
        origin: 'user',
        runs: 7,
        completed: 6,
        failed: 1,
        cancelled: 0,
        duration_p50_ms: 42_000,
        duration_p90_ms: 180_000,
        cost_usd: 9,
        cost_p50_usd: 0.31,
        avg_tool_calls: 4.2,
        avg_model_steps: 9.5,
      },
    ],
    duration_buckets: [
      { upper_ms: 10_000, by_origin: { user: 2 } },
      { upper_ms: 60_000, by_origin: { user: 3, automation: 1 } },
      { upper_ms: null, by_origin: { user: 2 } },
    ],
    agents: [
      {
        agent_id: 'main',
        runs: 7,
        completed: 6,
        failed: 1,
        cost_usd: 9,
        cost_p50_usd: 0.31,
        avg_tool_calls: 4.2,
        tool_ms: 64_000,
        changed_files: 3,
        lines_added: 40,
        lines_removed: 12,
        duration_p50_ms: 42_000,
        duration_p90_ms: 180_000,
      },
    ],
    longest: [makeRunRow({ run_id: 'run-long', session_title: 'Longest run' })],
    costliest: [
      makeRunRow({ run_id: 'run-cost', session_title: 'Costliest run' }),
    ],
    most_steps: [
      makeRunRow({ run_id: 'run-steps', session_title: 'Busiest run' }),
    ],
    errors: {
      total: 3,
      failed_attempts: 2,
      by_kind: [{ key: 'ProviderError', count: 2 }],
      by_provider: [{ key: 'openrouter', count: 3 }],
      by_model: [{ key: 'model-error-sentinel', count: 3 }],
      by_agent: [{ key: 'main', count: 3 }],
      by_hour: [{ hour: 9, count: 3 }],
    },
    ...overrides,
  };
}

function makeToolsSection(overrides = {}) {
  return {
    totals: {
      calls: 20,
      tools: 3,
      accepted: 15,
      rejected: 4,
      unknown: 1,
      tool_ms: 64_000,
    },
    tools: [
      {
        name: 'web_fetch',
        calls: 10,
        rejected: 4,
        rejection_rate: 0.4,
        top_codes: [{ code: 'invalid_arguments', count: 4 }],
        p50_ms: 800,
        p95_ms: 4_000,
        total_ms: 30_000,
        time_share: 0.47,
      },
    ],
    rejection_codes: [
      { code: 'invalid_arguments', count: 4, tools: ['web_fetch'] },
    ],
    by_agent: [{ agent_id: 'main', calls: 20, rejected: 4, tool_ms: 64_000 }],
    ...overrides,
  };
}

function makeSkillsSection(overrides = {}) {
  return {
    total_skills: 2,
    used_skills: 1,
    offered_unactivated_skills: 1,
    skills_without_offer_data: 0,
    skills: [
      {
        name: 'release-notes',
        origins: ['global'],
        offered_sessions: 4,
        activated_sessions: 2,
        activated_offered_sessions: 2,
        usage_rate: 0.5,
        last_activated: '2026-06-12T10:00:00+00:00',
      },
      {
        name: 'unused-skill-sentinel',
        origins: ['agent:main'],
        offered_sessions: 3,
        activated_sessions: 0,
        activated_offered_sessions: 0,
        usage_rate: 0,
        last_activated: null,
      },
    ],
    ...overrides,
  };
}

function makeExtensionActivity(overrides = {}) {
  return {
    sessions: 2,
    runs: 3,
    run_status: { completed: 2, failed: 1, cancelled: 0, interrupted: 0 },
    measured_input_tokens: 20_000,
    measured_output_tokens: 2_000,
    estimated_input_tokens: 0,
    estimated_output_tokens: 0,
    tool_calls: 5,
    costs: {
      reported_usd: 0.2,
      estimated_usd: 0.1,
      reported_calls: 2,
      estimated_calls: 1,
      unpriced_calls: 0,
    },
    ...overrides,
  };
}

function makeExtensionsSection(overrides = {}) {
  return {
    extensions: [
      {
        name: 'swarm',
        total_groups: 1,
        groups_truncated: false,
        activity: makeExtensionActivity(),
        groups: [
          {
            group_id: 'group-abcdef',
            title: 'Walross research',
            started_at: '2026-06-12T08:00:00+00:00',
            activity: makeExtensionActivity(),
            participants: [
              {
                participant_id: 'p1',
                name: 'Scout',
                session_id: 'swarm-session-1',
                model: 'prov/a',
                activity: makeExtensionActivity({ runs: 2 }),
              },
            ],
          },
        ],
      },
    ],
    ...overrides,
  };
}

function makeDiagnosticsSection(overrides = {}) {
  return {
    compactions: {
      total_compactions: 4,
      sessions_with_compactions: 2,
      average_per_compacted_session: 2,
      context: {
        observations: 4,
        reduction_ratio: 0.6,
        average_after_tokens: 40_000,
        p95_after_tokens: 60_000,
        average_duration_ms: 12_000,
        average_steps_between: 18,
        non_shrinking: 0,
        rapid_recompactions: 1,
      },
      by_strategy: [
        {
          strategy: 'summary_tail',
          compactions: 4,
          average_before_tokens: 100_000,
          average_after_tokens: 40_000,
          reduction_ratio: 0.6,
        },
      ],
      recent: [
        {
          agent_id: 'main',
          session_id: 'session-1',
          session_title: 'Plan the release',
          timestamp: '2026-06-13T08:00:00+00:00',
          strategy: 'summary_tail',
          before_tokens: 100_000,
          after_tokens: 40_000,
          duration_ms: 12_000,
          steps_since_previous: 18,
        },
      ],
    },
    cache: {
      lowest_hit_rate_sessions: [
        {
          agent_id: 'main',
          session_id: 'session-1',
          session_title: 'Plan the release',
          cache_turns: 4,
          input_tokens: 100_000,
          cache_read_tokens: 10_000,
          hit_rate: 0.1,
          last_activity: '2026-06-13T09:00:00+00:00',
        },
      ],
      suspected_breaks: {
        suspected_turns: 1,
        evaluated_turns: 9,
        incidents: [],
      },
    },
    data_quality: [
      {
        model: 'local/qwen',
        calls: 10,
        unreported_calls: 1,
        estimated_token_calls: 2,
        uncached_calls: 4,
        uncached_cost_usd: 2,
        unpriced_calls: 0,
        retrospective_calls: 0,
      },
    ],
    failed_attempts: {
      total: 2,
      hours: [
        {
          hour_start: '2026-06-13T09:00:00+00:00',
          failed: 2,
          calls: 5,
          models: [{ key: 'local/qwen', count: 2 }],
          agents: ['main'],
        },
      ],
    },
    runaway_runs: [
      makeRunRow({ run_id: 'run-runaway', session_title: 'Runaway sentinel' }),
    ],
    open_runs: 1,
    roles: {
      chat_messages_by_role: { user: 5, assistant: 6 },
      session_records_by_role: { user: 5, assistant: 6, tool: 4 },
    },
    ...overrides,
  };
}

const SECTION_BUILDERS = {
  overview: makeOverviewSection,
  usage: makeUsageSection,
  runs: makeRunsSection,
  tools: makeToolsSection,
  skills: makeSkillsSection,
  extensions: makeExtensionsSection,
  diagnostics: makeDiagnosticsSection,
};

/** A report answering `sections`, with `overrides` replacing whole
 *  sections or top-level fields. */
function makeReport(sections = Object.keys(SECTION_BUILDERS), overrides = {}) {
  const report = {
    generated_at: '2026-06-13T10:00:00+00:00',
    window: { since: null, until: '2026-06-13T10:00:00+00:00' },
    timezone: 'UTC',
  };
  for (const section of sections) {
    report[section] = SECTION_BUILDERS[section]();
  }
  return { ...report, ...overrides };
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

/**
 * An RPC stand-in: `statistics.report` answers the requested sections
 * (`reportFor(params)` may replace the answer), Limits RPCs answer from
 * `usageReport`.
 */
function routedRpc(usageReport = makeUsageReport(), reportFor = null) {
  return (method, params) => {
    if (method === 'statistics.report') {
      return Promise.resolve(
        reportFor ? reportFor(params) : makeReport(params?.sections),
      );
    }
    if (method === 'provider.usage') return Promise.resolve(usageReport);
    if (method === 'provider.usage_history') {
      return Promise.resolve({
        generated_at: usageReport.generated_at,
        samples: [],
      });
    }
    return Promise.resolve({});
  };
}

/** The `statistics.report` calls so far, as their params. */
function reportCalls() {
  return rpcMock.mock.calls
    .filter(([method]) => method === 'statistics.report')
    .map(([, params]) => params);
}

function openLimitsTab() {
  buttonNamed('statistics.subview.limits').click();
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

async function waitForOverview() {
  await waitForCondition(() =>
    document.querySelector(
      '.stats-view__panel .stats-tiles .stats-tile__value',
    ),
  );
}

function buttonNamed(key) {
  return [...document.querySelectorAll('button')].find(
    (button) =>
      button.getAttribute('aria-label') === t(key) ||
      button.textContent.trim() === t(key),
  );
}

/** The text of the KPI tile labelled `key`: `{ value, detail }`. */
function tileText(key) {
  const tile = [...document.querySelectorAll('.stats-tile')].find((node) =>
    node
      .querySelector('.stats-tile__label')
      ?.textContent.trim()
      .startsWith(t(key)),
  );
  return tile
    ? {
        value: tile.querySelector('.stats-tile__value')?.textContent.trim(),
        detail: tile.querySelector('.stats-tile__detail')?.textContent.trim(),
        tile,
      }
    : null;
}

function setupStatisticsViewSuite() {
  let mountedComponent;
  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    setApplicationTimeZone('UTC');
    clearStatisticsReports();
    try {
      localStorage.clear();
    } catch {
      // jsdom always provides storage; nothing to clear otherwise.
    }
    mountedComponent = null;
    rpcMock.mockReset();
  });
  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    vi.useRealTimers();
    vi.restoreAllMocks();
    document.body.innerHTML = '';
  });
  return {
    get mountedComponent() {
      return mountedComponent;
    },
    set mountedComponent(value) {
      mountedComponent = value;
    },
  };
}

export {
  rpcMock,
  StatisticsView,
  makeReport,
  makeRunRow,
  makeUsageReport,
  routedRpc,
  reportCalls,
  openLimitsTab,
  waitForCondition,
  waitForOverview,
  buttonNamed,
  tileText,
  setupStatisticsViewSuite,
};

export { flushSync, mount, unmount };
