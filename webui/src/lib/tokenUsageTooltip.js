// Composes the context usage breakdown: the Context headline
// ("tokens / contextWindow"), the Session's cache hit rate and token totals,
// and a "Last turn" block that splits the input into its cache shares.
// `contextUsageCardModel` returns label/value rows for the Chat context
// ring's card; `formatTokenUsageTooltip` renders the same model as text with
// line breaks and middot-indented sub-lines (Swarm Activity's quick tooltip).
//
// The cache lines matter for spotting prompt-cache breaks: canonical
// `input_tokens` already contains the cached tokens, so the sub-lines are
// shares of the input, never additions on top. Whether a Provider reported a
// figure or vBot estimated it is not shown here; Statistics diagnostics keep
// that distinction.
import { activeLocaleTag, t } from './i18n.js';

export function formatTokenUsageTooltip(
  contextUsage,
  usage,
  sessionUsage,
  contextWindow,
) {
  const { summary, sections } = contextUsageCardModel(
    contextUsage,
    usage,
    sessionUsage,
    contextWindow,
  );
  const blocks = [
    summary ? [summary] : [],
    ...sections.map((section) => [
      ...(section.title ? [section.title] : []),
      ...section.rows.map(
        (row) => `${row.sub ? '  · ' : ''}${row.label}: ${row.value}`,
      ),
    ]),
  ].filter((block) => block.length > 0);
  return blocks.length > 0
    ? blocks.map((block) => block.join('\n')).join('\n\n')
    : undefined;
}

// Context fill share at which the window counts as nearly exhausted.
const CONTEXT_CRITICAL_RATIO = 0.9;
// Without an automatic Compaction point, the ring warns from this share.
const CONTEXT_LIMIT_WARNING_RATIO = 0.7;
// With one, it warns this many percentage points before the trigger.
const COMPACTION_WARNING_LEAD = 0.1;
// Absorbs binary rounding, so 0.8 - 0.1 still warns from exactly 70%.
const RATIO_TOLERANCE = 1e-9;

/**
 * Context-window share at which automatic Compaction triggers under the
 * Session's effective Compaction Policy, or null when automatic Compaction is
 * off, the Policy is unknown, or the trigger lies beyond the window. A
 * `context_ratio` trigger's optional token cap applies when it comes first.
 */
export function automaticCompactionRatio(compactionPolicy, contextWindow) {
  const trigger = compactionPolicy?.trigger;
  if (
    compactionPolicy?.enabled !== true ||
    !trigger ||
    !Number.isFinite(contextWindow) ||
    contextWindow <= 0
  ) {
    return null;
  }
  const tokenRatio = (tokens) =>
    Number.isFinite(tokens) && tokens > 0 ? tokens / contextWindow : null;
  let ratio = null;
  if (trigger.type === 'context_ratio') {
    const threshold = Number.isFinite(trigger.threshold)
      ? trigger.threshold
      : null;
    const cap = tokenRatio(trigger.tokens);
    ratio =
      threshold !== null && cap !== null
        ? Math.min(threshold, cap)
        : (threshold ?? cap);
  } else if (trigger.type === 'input_tokens') {
    ratio = tokenRatio(trigger.tokens);
  }
  return ratio !== null && ratio > 0 && ratio < 1 ? ratio : null;
}

/**
 * Warning level of the context ring and card for a fill share (tokens of
 * Current Context Usage / context window). Red near the end of the window;
 * amber shortly before automatic Compaction triggers, or, when no automatic
 * Compaction applies, as the window fills. `message` is the card's line.
 */
export function contextLimitWarning(
  fillRatio,
  contextWindow,
  compactionPolicy,
) {
  if (fillRatio === null || !Number.isFinite(fillRatio)) {
    return { level: 'normal', message: '' };
  }
  if (fillRatio >= CONTEXT_CRITICAL_RATIO) {
    return {
      level: 'critical',
      message: t('chat.contextCard.atLimit'),
    };
  }
  const compactionRatio = automaticCompactionRatio(
    compactionPolicy,
    contextWindow,
  );
  if (compactionRatio === null) {
    return fillRatio >= CONTEXT_LIMIT_WARNING_RATIO
      ? {
          level: 'high',
          message: t('chat.contextCard.nearContextLimit'),
        }
      : { level: 'normal', message: '' };
  }
  if (fillRatio >= compactionRatio) {
    return {
      level: 'high',
      message: t('chat.contextCard.compactionThresholdReached'),
    };
  }
  if (
    fillRatio >=
    compactionRatio - COMPACTION_WARNING_LEAD - RATIO_TOLERANCE
  ) {
    return {
      level: 'high',
      message: t('chat.contextCard.nearLimit'),
    };
  }
  return { level: 'normal', message: '' };
}

/**
 * Structured model for the Chat context ring's card. `summary` is the
 * "tokens / contextWindow" headline (null without a Context projection);
 * `sections` hold label/value rows (`sub` rows are shares of the row above,
 * never additions): first the Session's cache hit rate and totals, then the
 * last turn. The card lays them out in a right-aligned column.
 */
export function contextUsageCardModel(
  contextUsage,
  usage,
  sessionUsage,
  contextWindow,
) {
  const numberFormat = new Intl.NumberFormat(activeLocaleTag());
  const format = (value) => numberFormat.format(value);
  const sections = [
    sessionSection(sessionUsage, format),
    usage ? lastTurnSection(usage, format) : null,
  ].filter((section) => section && section.rows.length > 0);
  return {
    summary: contextSummary(contextUsage, contextWindow, format),
    sections,
  };
}

function row(label, value, sub = false) {
  return { label, value, sub };
}

function percent(part, whole) {
  return `${Math.round((part / whole) * 100)}%`;
}

function contextSummary(contextUsage, contextWindow, format) {
  const tokens = finiteOrNull(contextUsage?.tokens);
  if (tokens === null) {
    return null;
  }
  return Number.isFinite(contextWindow) && contextWindow > 0
    ? t('chat.contextCard.summary', {
        tokens: format(tokens),
        context: format(contextWindow),
      })
    : format(tokens);
}

function sessionSection(sessionUsage, format) {
  const input = nonNegative(sessionUsage?.input_tokens);
  const output = nonNegative(sessionUsage?.output_tokens);
  if (input <= 0 && output <= 0) {
    return null;
  }
  const rows = [];
  // Only turns that report caching form the hit rate, so a Provider without
  // cache reporting never reads as a 0% hit rate.
  const cacheInput = nonNegative(sessionUsage.cache_input_tokens);
  if (nonNegative(sessionUsage.cache_turns) > 0 && cacheInput > 0) {
    rows.push(
      row(
        t('chat.contextCard.cacheHitRate'),
        percent(nonNegative(sessionUsage.cache_read_tokens), cacheInput),
      ),
    );
  }
  rows.push(row(t('chat.contextCard.totalInput'), format(input)));
  rows.push(row(t('chat.contextCard.totalOutput'), format(output)));
  if (nonNegative(sessionUsage.reasoning_turns) > 0) {
    rows.push(
      row(
        t('chat.contextCard.reasoning'),
        format(nonNegative(sessionUsage.reasoning_tokens)),
        true,
      ),
    );
  }
  return { id: 'session', title: '', rows };
}

function lastTurnSection(usage, format) {
  const input = nonNegative(usage.input_tokens);
  const output = nonNegative(usage.output_tokens);
  const cacheRead = nonNegativeOrNull(usage.cache_read_tokens);
  const cacheWrite = nonNegativeOrNull(usage.cache_write_tokens);
  const reasoning = nonNegativeOrNull(usage.reasoning_tokens);

  const rows = [row(t('chat.contextCard.input'), format(input))];
  if (cacheRead !== null) {
    rows.push(
      row(
        t('chat.contextCard.cacheRead'),
        input > 0
          ? `${format(cacheRead)} (${percent(cacheRead, input)})`
          : format(cacheRead),
        true,
      ),
    );
  }
  if (cacheWrite !== null) {
    rows.push(row(t('chat.contextCard.cacheWrite'), format(cacheWrite), true));
  }
  if (cacheRead !== null || cacheWrite !== null) {
    rows.push(
      row(
        t('chat.contextCard.uncached'),
        format(Math.max(0, input - (cacheRead ?? 0) - (cacheWrite ?? 0))),
        true,
      ),
    );
  }
  rows.push(row(t('chat.contextCard.output'), format(output)));
  if (reasoning !== null) {
    rows.push(row(t('chat.contextCard.reasoning'), format(reasoning), true));
  }
  return { id: 'last-turn', title: t('chat.contextCard.lastTurn'), rows };
}

function finiteOrNull(value) {
  return Number.isFinite(value) ? value : null;
}

function nonNegativeOrNull(value) {
  return Number.isFinite(value) && value >= 0 ? value : null;
}

function nonNegative(value) {
  return Number.isFinite(value) && value > 0 ? value : 0;
}
