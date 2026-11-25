// Context ring and hover card. Cache reads are a share of canonical input.
import { activeLocaleTag, t } from './i18n.js';
import {
  cacheHitRate,
  formatPercent,
  formatTokens,
  formatTokensExact,
} from './statisticsView/formatting.js';

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

/** One row per metric keeps the three scopes comparable in the hover card. */
export function contextUsageCardModel(
  contextUsage,
  { session, run, iteration },
  contextWindow,
) {
  const locale = activeLocaleTag();
  const format = (value) => formatTokensExact(value, locale);
  const scopes = [session, run, iteration];
  const summary = contextSummary(contextUsage, contextWindow, format);
  if (
    !scopes.some((scope) => scope?.input_tokens > 0 || scope?.output_tokens > 0)
  ) {
    return { summary, rows: [] };
  }

  function metric(id, value) {
    return {
      id,
      label: t(`chat.contextCard.${id}`),
      session: value(session),
      run: value(run),
      iteration: value(iteration),
    };
  }
  const tokenRow = (id, key) =>
    metric(id, (scope) => {
      if (!scope) return format(null);
      const count = scope[key] ?? 0;
      return count >= 1_000_000 ? formatTokens(count, locale) : format(count);
    });
  const rows = [
    metric('cacheHit', (scope) => formatPercent(cacheHitRate(scope), locale)),
    tokenRow('input', 'input_tokens'),
    tokenRow('output', 'output_tokens'),
  ];
  if (scopes.some((scope) => scope?.reasoning_tokens > 0)) {
    rows.push(tokenRow('reasoning', 'reasoning_tokens'));
  }
  return { summary, rows };
}

function contextSummary(contextUsage, contextWindow, format) {
  const tokens = contextUsage?.tokens;
  if (!Number.isFinite(tokens)) {
    return null;
  }
  return Number.isFinite(contextWindow) && contextWindow > 0
    ? t('chat.contextCard.summary', {
        tokens: format(tokens),
        context: format(contextWindow),
      })
    : format(tokens);
}
