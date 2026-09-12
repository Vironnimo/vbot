const { mount, flushSync, unmount } = await import('svelte');
// @vitest-environment jsdom
import { afterEach, beforeEach, vi } from 'vitest';

import { init, t } from '../../lib/i18n.js';
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

async function waitForOverview() {
  await waitForCondition(() => document.querySelector('.stats-health__track'));
}

function buttonNamed(key) {
  return [...document.querySelectorAll('button')].find(
    (button) =>
      button.getAttribute('aria-label') === t(key) ||
      button.textContent.trim() === t(key),
  );
}

function cardValue(key) {
  return [...document.querySelectorAll('.stats-card')]
    .find((card) =>
      card
        .querySelector('.stats-card__label')
        .textContent.trim()
        .startsWith(t(key)),
    )
    ?.querySelector('.stats-card__value')
    .textContent.trim();
}

function setupStatisticsViewSuite() {
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
  makeUsageReport,
  makeUsageHistoryReport,
  makeRunActivityReport,
  routedRpc,
  openLimitsTab,
  waitForCondition,
  waitForOverview,
  buttonNamed,
  cardValue,
  setupStatisticsViewSuite,
};

export { flushSync, mount, unmount };
