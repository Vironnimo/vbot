// Statistics presentation owns labels, tooltips and insights. Its focused
// internal units own report queries, numeric formatting and chart projection;
// this public import remains stable for the view and report components.
import { parseAgentAddress } from './agentAddress.js';
import { t, tOr } from './i18n.js';
import {
  finite,
  toFiniteNumber,
  numberFormat,
  formatPercent,
  formatInteger,
  formatTokensExact,
  formatCost,
  formatCostExact,
  formatDateTime,
  formatDurationMs,
} from './statisticsView/formatting.js';

export * from './statisticsView/query.js';
export * from './statisticsView/charts.js';
export * from './statisticsView/activity.js';
export {
  formatInteger,
  formatDecimal,
  formatTokens,
  formatTokensExact,
  formatCost,
  formatCostExact,
  formatPercent,
  formatShare,
  formatDurationMs,
  formatDateTime,
  formatShortDateTime,
  formatHourLabel,
  cacheHitRate,
  totalTokens,
  costPerMillionTokens,
  shareOf,
} from './statisticsView/formatting.js';

const EM_DASH = '\u2014';

// ---------------------------------------------------------------------------
// Changes against the previous period

/**
 * The change of a value against the previous period of equal length, or null
 * when either value is unknown. `kind: 'relative'` is the percent change of
 * the value, `kind: 'points'` the difference of two ratios in percentage
 * points. `judge: 'lowerIsBetter'` marks a rise as bad and a fall as good;
 * without it the change stays neutral (more cost is not a verdict).
 */
export function periodChange(
  current,
  previous,
  { kind = 'relative', judge = null, locale = 'en' } = {},
) {
  const now = finite(current);
  const before = finite(previous);
  if (now === null || before === null) return null;
  let text;
  let direction;
  if (kind === 'points') {
    const points = Math.round((now - before) * 1000) / 10;
    direction = points > 0 ? 'up' : points < 0 ? 'down' : 'flat';
    text = t('statistics.change.points', {
      value: numberFormat(locale, {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
      }).format(Math.abs(points)),
    });
  } else if (before === 0) {
    if (now === 0) {
      direction = 'flat';
      text = formatPercent(0, locale);
    } else {
      direction = now > 0 ? 'up' : 'down';
      text = t('statistics.change.new');
    }
  } else {
    const ratio = (now - before) / Math.abs(before);
    const rounded = Math.round(ratio * 1000) / 1000;
    direction = rounded > 0 ? 'up' : rounded < 0 ? 'down' : 'flat';
    // From double on, a multiple reads better than a large percentage.
    const multiple = before > 0 ? now / before : 0;
    text =
      multiple >= 2
        ? t('statistics.change.times', {
            value: numberFormat(locale, {
              maximumFractionDigits: multiple < 10 ? 1 : 0,
            }).format(multiple),
          })
        : formatPercent(Math.abs(rounded), locale);
  }
  let tone = 'neutral';
  if (judge === 'lowerIsBetter' && direction !== 'flat') {
    tone = direction === 'up' ? 'bad' : 'good';
  }
  return { direction, text, tone };
}

/**
 * A KPI tile's change marker: `periodChange` with the previous value,
 * formatted by `formatPrevious`, as its tooltip; null like `periodChange`.
 */
export function tileChange(current, previous, formatPrevious, options = {}) {
  const result = periodChange(current, previous, options);
  if (!result) return null;
  return {
    ...result,
    tooltip: t('statistics.change.previous', {
      value: formatPrevious(previous),
    }),
  };
}

// ---------------------------------------------------------------------------
// Names and labels

/** Who started a Run or Model call (`user`, `subagent`, ...), translated. */
export function originLabel(origin) {
  return tOr(`statistics.origin.${origin}`, String(origin || EM_DASH));
}

/** Run status, translated; unknown statuses stay raw. */
export function runStatusLabel(status) {
  return tOr(`statistics.status.${status}`, String(status || EM_DASH));
}

/** The Badge variant of a Run status. */
export function runStatusVariant(status) {
  switch (status) {
    case 'completed':
      return 'success';
    case 'failed':
      return 'error';
    case 'interrupted':
      return 'warn';
    case 'running':
      return 'info';
    default:
      return 'neutral';
  }
}

// Model request kinds and outcomes with a catalog label; any other value
// renders the generic label.
const MODEL_CALL_KINDS = new Set([
  'chat',
  'compaction',
  'speech_to_text',
  'text_to_speech',
  'image_understanding',
  'image_generation',
  'video_generation',
  'music_generation',
  'text_embedding',
  'decision',
  'live_voice',
  'live_voice_backend',
  'session_title',
  'group_title',
  'extension_sampling',
  'agent_description',
]);

const MODEL_CALL_STATUSES = new Set([
  'started',
  'completed',
  'failed',
  'cancelled',
  'interrupted',
]);

export function modelCallKindLabel(kind) {
  return MODEL_CALL_KINDS.has(kind)
    ? t(`statistics.kind.${kind}`)
    : t('statistics.kind.other');
}

/** An id in words: `tool_iterations_exceeded` -> `Tool iterations exceeded`. */
function humanizeId(id) {
  const words = String(id ?? '')
    .replace(/[_-]+/g, ' ')
    .trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : EM_DASH;
}

/** A recorded Run error kind (`rate_limit`, `auth_error`, ...), translated;
 *  a kind without a label reads as its id in words. */
export function errorKindLabel(kind) {
  return tOr(`statistics.errorKind.${kind}`, humanizeId(kind));
}

/** An error kind's name with its stored id as a secondary row. */
export function errorKindTooltip(kind) {
  return {
    title: errorKindLabel(kind),
    rows: [
      {
        label: t('statistics.errors.kindId'),
        value: String(kind ?? ''),
        mono: true,
      },
    ],
  };
}

/** Why failed Model calls failed: a recorded failure class, `unknown` for a
 *  failed call recorded without one, or the call status for a cancelled or
 *  interrupted call. */
export function failureClassLabel(key) {
  if (key === 'cancelled' || key === 'interrupted') {
    return modelCallStatusLabel(key);
  }
  return tOr(`statistics.failureClass.${key}`, humanizeId(key));
}

/** A failure class's name and explanation with its stored id. */
export function failureClassTooltip(key) {
  const text = tOr(`statistics.failureClassHint.${key}`, '');
  return {
    title: failureClassLabel(key),
    ...(text ? { text } : {}),
    rows: [
      {
        label: t('statistics.failures.classId'),
        value: String(key ?? ''),
        mono: true,
      },
    ],
  };
}

export function modelCallStatusLabel(status) {
  return MODEL_CALL_STATUSES.has(status)
    ? t(`statistics.requestStatus.${status}`)
    : t('statistics.requestStatus.unknown');
}

// Report rows attribute Extension-owned Sessions (for example Swarm
// participants) to `extension:<name>`; Agent ids never contain `:`.
const EXTENSION_ACTOR_PREFIX = 'extension:';

/**
 * Display parts of a statistics Agent key: a Project Agent (`agent@project`)
 * shows its bare name with a Project badge, an Extension key
 * (`extension:<name>`) its Extension name with an Extension badge, and an
 * identity Agent its id.
 */
export function agentDisplay(agentId) {
  if (
    typeof agentId === 'string' &&
    agentId.startsWith(EXTENSION_ACTOR_PREFIX) &&
    agentId.length > EXTENSION_ACTOR_PREFIX.length
  ) {
    return {
      name: agentId.slice(EXTENSION_ACTOR_PREFIX.length),
      projectId: null,
      extension: true,
    };
  }
  const { agentId: bareId, projectId } = parseAgentAddress(agentId);
  return { name: bareId, projectId, extension: false };
}

/** Text an Agent cell is found by in a table filter. */
export function agentFilterText(agentId) {
  const display = agentDisplay(agentId);
  return [display.name, display.projectId].filter(Boolean).join(' ');
}

/** Short, stable suffix identifying an untitled Extension group. */
export function shortGroupId(groupId) {
  const value = typeof groupId === 'string' ? groupId : '';
  return value.slice(-6);
}

export const USAGE_DIMENSIONS = Object.freeze([
  'agent',
  'model',
  'provider',
  'project',
  'origin',
  'kind',
]);

const USAGE_DIMENSION_LABELS = Object.freeze({
  agent: () => t('statistics.dimension.agent'),
  model: () => t('statistics.dimension.model'),
  provider: () => t('statistics.dimension.provider'),
  project: () => t('statistics.dimension.project'),
  origin: () => t('statistics.dimension.origin'),
  kind: () => t('statistics.dimension.kind'),
});

/** The name of a Costs & tokens breakdown dimension. */
export function usageDimensionLabel(dimension) {
  return (USAGE_DIMENSION_LABELS[dimension] ?? USAGE_DIMENSION_LABELS.agent)();
}

/** A breakdown row's display text in its dimension (Agent rows render their
 *  own cell). */
export function usageRowLabel(dimension, key) {
  switch (dimension) {
    case 'project':
      return key ? key : t('statistics.usage.identityUsage');
    case 'origin':
      return originLabel(key);
    case 'kind':
      return modelCallKindLabel(key);
    case 'agent':
      return key ? agentFilterText(key) : t('statistics.cost.withoutSession');
    default:
      return key || EM_DASH;
  }
}

// ---------------------------------------------------------------------------
// Tooltips. Each builder returns what the shared `use:tooltip` action accepts
// (lib/tooltip.js): '' for nothing, a string, or `{ title, text, rows }`.
// Definitions live in InfoHints on labels and column headers; these carry
// the facts about one value.

/**
 * An Agent cell: a Project Agent names its Project and full address, an
 * Extension explains its Sessions, and a plain Agent name shows in full only
 * while it is truncated.
 */
export function agentTooltip(agentId) {
  const display = agentDisplay(agentId);
  if (display.extension) {
    return {
      title: display.name,
      text: t('statistics.agent.extensionBadgeTitle'),
    };
  }
  if (display.projectId) {
    return {
      title: display.name,
      rows: [
        { label: t('statistics.agent.project'), value: display.projectId },
        {
          label: t('statistics.agent.address'),
          value: agentId,
          mono: true,
        },
      ],
    };
  }
  return { text: display.name, whenTruncated: true };
}

/**
 * A Session cell names the Session by its title and keeps the id as a
 * secondary row; an untitled Session shows its id, in full only while
 * truncated.
 */
export function sessionTooltip(row) {
  const id = typeof row?.session_id === 'string' ? row.session_id : '';
  if (row?.session_title) {
    return {
      title: row.session_title,
      rows: [{ label: t('statistics.sessionId'), value: id, mono: true }],
    };
  }
  return { text: id, mono: true, selectable: true, whenTruncated: true };
}

/** A compact token value's exact count. */
export function tokenTooltip(value, locale = 'en') {
  return formatTokensExact(value, locale);
}

/**
 * The parts of a cost: the Provider-reported and catalog-estimated amounts
 * with the calls they cover, the calls without a price, and the estimates
 * priced with today's catalog.
 */
export function costTooltip(totals, locale = 'en') {
  if (!totals) return '';
  const rows = [];
  const callCount = (count) =>
    t('statistics.cost.callCount', { count: formatInteger(count, locale) });
  if (toFiniteNumber(totals.reported_calls) > 0) {
    rows.push({
      label: t('statistics.cost.reported'),
      value: `${formatCostExact(totals.reported_cost_usd, locale)} · ${callCount(totals.reported_calls)}`,
    });
  }
  if (toFiniteNumber(totals.estimated_calls) > 0) {
    rows.push({
      label: t('statistics.cost.estimated'),
      value: `${formatCostExact(totals.estimated_cost_usd, locale)} · ${callCount(totals.estimated_calls)}`,
    });
  }
  if (toFiniteNumber(totals.unpriced_calls) > 0) {
    rows.push({
      label: t('statistics.cost.unpriced'),
      value: callCount(totals.unpriced_calls),
      tone: 'warning',
    });
  }
  if (toFiniteNumber(totals.retrospective_calls) > 0) {
    rows.push({
      label: t('statistics.cost.retrospective'),
      value: callCount(totals.retrospective_calls),
    });
  }
  if (rows.length === 0) return '';
  return { title: formatCostExact(totals.cost_usd, locale), rows };
}

/**
 * Output speed in tokens per second, rounded like the composer footer: one
 * decimal below 10, else whole; a dash without a measured call.
 */
export function formatOutputSpeed(rate, locale = 'en') {
  const value = finite(rate);
  if (value === null) return EM_DASH;
  const digits = value < 10 ? 1 : 0;
  return t('statistics.usage.speedValue', {
    rate: numberFormat(locale, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value),
  });
}

export function outputSpeedBasisLabel(basis) {
  if (basis === 'answer') return t('statistics.usage.speedAnswer');
  if (basis === 'output') return t('statistics.usage.speedOutput');
  if (basis === 'provider') return t('statistics.usage.speedProvider');
  return t('statistics.usage.speedUnknown');
}

export function formatMeasuredOutputSpeed(measurement, locale = 'en') {
  if (
    !['answer', 'output', 'provider'].includes(measurement?.basis) ||
    finite(measurement?.output_tokens_per_second) === null
  )
    return EM_DASH;
  const value = formatOutputSpeed(measurement.output_tokens_per_second, locale);
  return measurement.basis === 'provider'
    ? value
    : t('statistics.usage.approximateSpeed', { value });
}

/** The evidence for one measurement basis, never a mixture of rates. */
export function outputSpeedTooltip(measurement, locale = 'en') {
  const calls = toFiniteNumber(measurement?.calls);
  if (calls <= 0) return '';
  const rows = [
    {
      label: t('statistics.usage.speedMeasured'),
      value: t('statistics.usage.speedMeasuredValue', {
        tokens: formatInteger(measurement.tokens, locale),
        duration: formatDurationMs(measurement.window_ms),
      }),
    },
    {
      label: t('statistics.usage.measuredCalls'),
      value: formatInteger(calls, locale),
    },
  ];
  if (toFiniteNumber(measurement.estimated_calls) > 0) {
    rows.push({
      label: t('statistics.usage.estimatedSpeedCalls'),
      value: formatInteger(measurement.estimated_calls, locale),
    });
  }
  return {
    title: outputSpeedBasisLabel(measurement.basis),
    text:
      measurement.basis === 'provider'
        ? t('statistics.usage.providerSpeedHint')
        : measurement.basis === 'answer'
          ? t('statistics.usage.answerSpeedHint')
          : measurement.basis === 'output'
            ? t('statistics.usage.outputSpeedHint')
            : t('statistics.usage.unknownSpeedHint'),
    rows,
  };
}

/** Distribution and coverage of observed first-output latency. */
export function firstTokenTooltip(row, locale = 'en') {
  const calls = toFiniteNumber(row?.first_token_calls);
  if (calls <= 0) return '';
  return {
    rows: [
      {
        label: t('statistics.usage.firstTokenMedian'),
        value: formatDurationMs(row.first_token_p50_ms),
      },
      {
        label: t('statistics.usage.firstTokenP95'),
        value: formatDurationMs(row.first_token_p95_ms),
      },
      {
        label: t('statistics.usage.firstTokenAverage'),
        value: formatDurationMs(row.avg_first_token_ms),
      },
      {
        label: t('statistics.usage.measuredCalls'),
        value: formatInteger(calls, locale),
      },
    ],
  };
}

const COST_REASONS = Object.freeze({
  missing_usage: () => t('statistics.cost.reason.usage'),
  missing_price: () => t('statistics.cost.reason.price'),
  unsupported_tier: () => t('statistics.cost.reason.tier'),
  invalid_cache: () => t('statistics.cost.reason.cache'),
  invalid_reasoning: () => t('statistics.cost.reason.reasoning'),
  missing_reasoning_usage: () => t('statistics.cost.reason.reasoningUsage'),
  missing_bucket_price: () => t('statistics.cost.reason.bucket'),
});

const PRICE_RATES = Object.freeze([
  ['input', () => t('statistics.cost.rate.input')],
  ['output', () => t('statistics.cost.rate.output')],
  ['cache_read', () => t('statistics.cost.rate.cacheRead')],
  ['cache_write', () => t('statistics.cost.rate.cacheWrite')],
  ['reasoning', () => t('statistics.cost.rate.reasoning')],
]);

/** How one Model call's cost was determined: its price source, the reason
 *  it has none, and the catalog rates used. */
export function callCostTooltip(call, locale = 'en') {
  const cost = call?.cost ?? {};
  const rows = [];
  let title;
  if (cost.source === 'provider') {
    title = t('statistics.cost.providerSource');
  } else if (cost.source === 'catalog') {
    title = call?.retrospective
      ? t('statistics.cost.currentCatalog')
      : t('statistics.cost.savedCatalog');
    const rates = cost.pricing?.rates ?? {};
    for (const [key, label] of PRICE_RATES) {
      if (finite(rates[key]) !== null) {
        rows.push({
          label: label(),
          value: t('statistics.cost.perMillion', {
            price: formatCostExact(rates[key], locale),
          }),
        });
      }
    }
    if (Array.isArray(rates.tiers) && rates.tiers.length > 0) {
      rows.push({
        label: t('statistics.cost.tiers'),
        value: formatInteger(rates.tiers.length, locale),
      });
    }
    if (cost.pricing?.source) {
      rows.push({
        label: t('statistics.cost.source'),
        value: String(cost.pricing.source),
      });
    }
  } else {
    title = t('statistics.cost.unknown');
    const reason = Object.hasOwn(COST_REASONS, cost.reason)
      ? COST_REASONS[cost.reason]()
      : '';
    if (reason)
      rows.push({ label: t('statistics.cost.reason'), value: reason });
  }
  if (call?.estimated_tokens === true) {
    rows.push({
      label: t('statistics.col.tokens'),
      value: t('statistics.tokens.estimatedPart'),
    });
  }
  return { title, rows };
}

/** A Tool's most frequent rejection codes. */
export function toolRejectionTooltip(tool, locale = 'en') {
  const codes = Array.isArray(tool?.top_codes) ? tool.top_codes : [];
  if (codes.length === 0) return '';
  return {
    title: t('statistics.tools.rejectionsOf', {
      count: formatInteger(tool.rejected, locale),
      calls: formatInteger(tool.calls, locale),
    }),
    rows: codes.map((entry) => ({
      label: String(entry.code),
      value: formatInteger(entry.count, locale),
    })),
  };
}

// ---------------------------------------------------------------------------
// Skills

// Origin scopes that carry a detail after a colon (`agent:<id>`,
// `project:<name>`). `bundled` / `global` are bare scope tokens.
const SCOPED_ORIGIN_PREFIXES = Object.freeze(['agent', 'project']);

/**
 * Split a Skill origin (`bundled`, `global`, `agent:<id>`,
 * `project:<name>`) into `{ scope, detail }`; only the first colon
 * separates, and bare or unknown tokens have a null detail.
 */
export function parseOrigin(origin) {
  const raw = typeof origin === 'string' ? origin.trim() : '';
  if (!raw) return { scope: '', detail: null };
  const separatorIndex = raw.indexOf(':');
  if (separatorIndex === -1) return { scope: raw, detail: null };
  const scope = raw.slice(0, separatorIndex);
  const detail = raw.slice(separatorIndex + 1);
  if (!SCOPED_ORIGIN_PREFIXES.includes(scope) || detail.length === 0) {
    return { scope: raw, detail: null };
  }
  return { scope, detail };
}

/** A Skill origin, translated. */
export function skillOriginLabel(origin) {
  const { scope, detail } = parseOrigin(origin);
  if (detail !== null) {
    return t(`statistics.skills.scopedOrigin.${scope}`, { detail });
  }
  return tOr(`statistics.skills.origin.${scope}`, scope);
}

/** The evidence behind a Skill's offer conversion. */
export function skillConversionTooltip(skill, locale = 'en') {
  const offered = toFiniteNumber(skill?.offered_sessions);
  if (offered === 0) return t('statistics.skills.noOfferDataRowTitle');
  const converted = toFiniteNumber(skill?.activated_offered_sessions);
  const outside = toFiniteNumber(skill?.activated_sessions) - converted;
  return {
    text: t('statistics.skills.conversionDetail', {
      activated: formatInteger(converted, locale),
      offered: formatInteger(offered, locale),
    }),
    rows:
      outside > 0
        ? [
            {
              label: t('statistics.skills.activatedWithoutOffer'),
              value: formatInteger(outside, locale),
            },
          ]
        : [],
  };
}

/** Skills offered in Sessions that never activated them: candidates to
 *  improve or remove. */
export function skillCandidates(skills) {
  return (Array.isArray(skills) ? skills : []).filter(
    (skill) =>
      toFiniteNumber(skill?.offered_sessions) > 0 &&
      toFiniteNumber(skill?.activated_offered_sessions) === 0,
  );
}

// ---------------------------------------------------------------------------
// Compactions

// Stored Compaction strategy ids and the Compaction mode names Settings uses.
const COMPACTION_STRATEGIES = Object.freeze({
  summary_tail: () => ({
    label: t('compaction.strategy.summaryTail'),
    description: t('compaction.strategy.summaryTailDescription'),
  }),
  continuation: () => ({
    label: t('compaction.strategy.continuation'),
    description: t('compaction.strategy.continuationDescription'),
  }),
});

function compactionStrategy(strategy) {
  return Object.hasOwn(COMPACTION_STRATEGIES, strategy)
    ? COMPACTION_STRATEGIES[strategy]()
    : null;
}

/** The Compaction mode name for a stored strategy id; unknown ids stay raw. */
export function compactionStrategyLabel(strategy) {
  return compactionStrategy(strategy)?.label ?? String(strategy ?? '');
}

/** What a Compaction mode does, with its stored id as a secondary row. */
export function compactionStrategyTooltip(strategy) {
  const known = compactionStrategy(strategy);
  if (!known) return '';
  return {
    title: known.label,
    text: known.description,
    rows: [
      {
        label: t('statistics.compactions.strategyId'),
        value: strategy,
        mono: true,
      },
    ],
  };
}

// ---------------------------------------------------------------------------
// Insights

const INSIGHT_TABS = Object.freeze({
  uncached_value: 'usage',
  cancelled_cost: 'runs',
  top_runs_share: 'runs',
  failed_attempt_burst: 'diagnostics',
  runaway_runs: 'diagnostics',
  tool_failure: 'tools',
});

function insightText(id, values, locale) {
  switch (id) {
    case 'uncached_value':
      return values.top_model
        ? t('statistics.insight.uncachedValueModel', {
            share: formatPercent(values.share, locale),
            cost: formatCost(values.cost_usd, locale),
            model: values.top_model,
          })
        : t('statistics.insight.uncachedValue', {
            share: formatPercent(values.share, locale),
            cost: formatCost(values.cost_usd, locale),
          });
    case 'cancelled_cost':
      return t('statistics.insight.cancelledCost', {
        runs: formatInteger(values.runs, locale),
        cost: formatCost(values.cost_usd, locale),
        share: formatPercent(values.share, locale),
      });
    case 'top_runs_share':
      return t('statistics.insight.topRunsShare', {
        runs: formatInteger(values.runs, locale),
        share: formatPercent(values.share, locale),
      });
    case 'failed_attempt_burst':
      return values.model
        ? t('statistics.insight.failedAttemptBurstModel', {
            failed: formatInteger(values.failed, locale),
            time: formatDateTime(values.hour_start, locale),
            model: values.model,
          })
        : t('statistics.insight.failedAttemptBurst', {
            failed: formatInteger(values.failed, locale),
            time: formatDateTime(values.hour_start, locale),
          });
    case 'runaway_runs':
      return t('statistics.insight.runawayRuns', {
        runs: formatInteger(values.runs, locale),
      });
    case 'tool_failure':
      return t('statistics.insight.toolFailure', {
        tool: String(values.tool ?? EM_DASH),
        rate: formatPercent(values.rate, locale),
        calls: formatInteger(values.calls, locale),
      });
    default:
      return '';
  }
}

/**
 * The Overview's "Worth a look" lines: one plain-language sentence per
 * insight the report raised, with the tab that explains it. Unknown insight
 * ids are skipped.
 */
export function insightLines(insights, locale = 'en') {
  return (Array.isArray(insights) ? insights : []).flatMap((insight) => {
    if (!Object.hasOwn(INSIGHT_TABS, insight?.id)) return [];
    const text = insightText(insight.id, insight.values ?? {}, locale);
    return text
      ? [
          {
            id: insight.id,
            severity: insight.severity === 'warn' ? 'warn' : 'info',
            text,
            tab: INSIGHT_TABS[insight.id],
          },
        ]
      : [];
  });
}

/** The insight with `id`, or null. */
export function findInsight(insights, id) {
  return (
    (Array.isArray(insights) ? insights : []).find(
      (insight) => insight?.id === id,
    ) ?? null
  );
}
