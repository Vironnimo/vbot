// Composes the context usage breakdown: a compact context summary
// ("tokens / contextWindow" plus provider in/out), a "Last turn" block that
// splits the input into its cache shares, and a whole-session block with the
// session cache hit rate. `formatTokenUsageTooltip` renders it as text with
// line breaks and middot-indented sub-lines (Swarm Activity's quick tooltip);
// `contextUsageCardModel` returns the same figures as label/value rows for
// the Chat context ring's card.
//
// The cache lines matter for spotting prompt-cache breaks: canonical
// `input_tokens` already contains the cached tokens, so the sub-lines are
// shares of the input ("davon"), never additions on top.
import { activeLocaleTag, t } from './i18n.js';

export function formatTokenUsageTooltip(
  contextUsage,
  usage,
  sessionUsage,
  contextWindow,
) {
  const numberFormat = new Intl.NumberFormat(activeLocaleTag());
  const format = (value) => numberFormat.format(value);

  const sections = [
    contextUsageLines(contextUsage, contextWindow, format),
    usage ? lastTurnLines(usage, format) : [],
    sessionUsageLines(sessionUsage, format),
  ].filter((section) => section.length > 0);
  return sections.length > 0
    ? sections.map((section) => section.join('\n')).join('\n\n')
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
 * "tokens / contextWindow" headline (null without a context measurement);
 * `sections` hold label/value rows (`sub` rows are shares of the row above,
 * never additions) plus optional notes, so the card lays figures out in a
 * right-aligned column instead of preformatted text. It reports the same
 * figures as `formatTokenUsageTooltip`.
 */
export function contextUsageCardModel(
  contextUsage,
  usage,
  sessionUsage,
  contextWindow,
) {
  const numberFormat = new Intl.NumberFormat(activeLocaleTag());
  const format = (value) => numberFormat.format(value);
  const [summary = null] = contextUsageLines(
    contextUsage,
    contextWindow,
    format,
  );
  const sections = [
    contextCardSection(contextUsage, format),
    usage ? lastTurnCardSection(usage, format) : null,
    sessionCardSection(sessionUsage, format),
  ].filter((section) => section && section.rows.length > 0);
  return { summary, sections };
}

function row(label, value, sub = false) {
  return { label, value, sub };
}

function contextCardSection(contextUsage, format) {
  if (finiteOrNull(contextUsage?.tokens) === null) {
    return null;
  }
  const rows = [];
  const providerInput = finiteOrNull(contextUsage.provider_input_tokens);
  const providerOutput = finiteOrNull(contextUsage.provider_output_tokens);
  const estimatedDelta = finiteOrNull(contextUsage.estimated_delta_tokens);
  if (providerInput !== null) {
    rows.push(row(t('chat.contextCard.providerInput'), format(providerInput)));
  }
  if (providerOutput !== null) {
    rows.push(
      row(t('chat.contextCard.providerOutput'), format(providerOutput)),
    );
  }
  if (estimatedDelta !== null) {
    rows.push(
      row(t('chat.contextCard.requestChanges'), format(estimatedDelta), true),
    );
  }
  return { id: 'context', title: '', meta: '', rows, notes: [] };
}

function cacheShareValue(cacheRead, input, format) {
  return input > 0
    ? `${format(cacheRead)} (${Math.round((cacheRead / input) * 100)}%)`
    : format(cacheRead);
}

function lastTurnCardSection(usage, format) {
  const input = nonNegative(usage.input_tokens);
  const output = nonNegative(usage.output_tokens);
  const inputEstimated = usageFieldIsEstimated(usage, 'input_tokens');
  const outputEstimated = usageFieldIsEstimated(usage, 'output_tokens');
  const cacheRead = finiteOrNull(usage.cache_read_tokens);
  const cacheWrite = finiteOrNull(usage.cache_write_tokens);
  const reasoning = nonNegativeOrNull(usage.reasoning_tokens);

  const rows = [
    row(
      t('chat.contextCard.input'),
      `${inputEstimated ? '~' : ''}${format(input)}`,
    ),
  ];
  if (cacheRead !== null) {
    rows.push(
      row(
        t('chat.contextCard.cacheRead'),
        cacheShareValue(cacheRead, input, format),
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
  rows.push(
    row(
      t('chat.contextCard.output'),
      `${outputEstimated ? '~' : ''}${format(output)}`,
    ),
  );
  if (reasoning !== null) {
    rows.push(row(t('chat.contextCard.reasoning'), format(reasoning), true));
  }
  const notes = [];
  if (inputEstimated && outputEstimated) {
    notes.push(t('chat.tokenTooltipEstimated'));
  } else if (inputEstimated) {
    notes.push(t('chat.tokenTooltipInputEstimated'));
  } else if (outputEstimated) {
    notes.push(t('chat.tokenTooltipOutputEstimated'));
  }
  return {
    id: 'last-turn',
    title: t('chat.tokenTooltipLastTurn'),
    meta: '',
    rows,
    notes,
  };
}

function sessionCardSection(sessionUsage, format) {
  const measuredTurns = nonNegative(sessionUsage?.measured_turns);
  const input = nonNegative(sessionUsage?.input_tokens);
  const output = nonNegative(sessionUsage?.output_tokens);
  if (measuredTurns <= 0 && input <= 0 && output <= 0) {
    return null;
  }
  const cacheRead = nonNegative(sessionUsage.cache_read_tokens);
  const cacheTurns = nonNegative(sessionUsage.cache_turns);
  const estimatedTurns = nonNegative(sessionUsage.estimated_turns);
  const reasoningTurns = nonNegative(sessionUsage.reasoning_turns);
  const reasoning = nonNegative(sessionUsage.reasoning_tokens);

  const rows = [row(t('chat.contextCard.input'), format(input))];
  if (cacheTurns > 0) {
    rows.push(
      row(
        t('chat.contextCard.cacheRead'),
        cacheShareValue(cacheRead, input, format),
        true,
      ),
    );
  }
  rows.push(row(t('chat.contextCard.output'), format(output)));
  if (reasoningTurns > 0) {
    rows.push(
      row(
        t('chat.contextCard.reasoningTurns', {
          turns: format(reasoningTurns),
        }),
        format(reasoning),
        true,
      ),
    );
  }
  if (cacheTurns > 0) {
    rows.push(
      row(
        t('chat.contextCard.avgCacheRead'),
        format(Math.round(cacheRead / cacheTurns)),
      ),
    );
  }
  const notes = [];
  if (estimatedTurns > 0) {
    notes.push(
      t('chat.tokenTooltipSessionEstimatedTurns', {
        count: format(estimatedTurns),
      }),
    );
  }
  return {
    id: 'session',
    title: t('chat.contextCard.session'),
    meta: t('chat.contextCard.measuredTurns', {
      turns: format(measuredTurns),
    }),
    rows,
    notes,
  };
}

function contextUsageLines(contextUsage, contextWindow, format) {
  const tokens = finiteOrNull(contextUsage?.tokens);
  if (tokens === null) {
    return [];
  }
  const tokenText = `${contextUsage.estimated === true ? '~' : ''}${format(tokens)}`;
  const lines = [];
  if (Number.isFinite(contextWindow) && contextWindow > 0) {
    lines.push(
      t('chat.tokenTooltipContextSummary', {
        tokens: tokenText,
        context: format(contextWindow),
      }),
    );
  } else {
    lines.push(
      t('chat.tokenTooltipContextSummaryNoWindow', {
        tokens: tokenText,
      }),
    );
  }
  const providerInput = finiteOrNull(contextUsage.provider_input_tokens);
  const providerOutput = finiteOrNull(contextUsage.provider_output_tokens);
  if (providerInput !== null && providerOutput !== null) {
    lines.push(
      t('chat.tokenTooltipContextInOut', {
        input: format(providerInput),
        output: format(providerOutput),
      }),
    );
  } else if (providerInput !== null) {
    lines.push(
      t('chat.tokenTooltipContextInOnly', {
        input: format(providerInput),
      }),
    );
  } else if (providerOutput !== null) {
    lines.push(
      t('chat.tokenTooltipContextOutOnly', {
        output: format(providerOutput),
      }),
    );
  }
  const estimatedDelta = finiteOrNull(contextUsage.estimated_delta_tokens);
  if (estimatedDelta !== null) {
    lines.push(
      t('chat.tokenTooltipContextDelta', { tokens: format(estimatedDelta) }),
    );
  }
  return lines;
}

function lastTurnLines(usage, format) {
  const input = nonNegative(usage.input_tokens);
  const output = nonNegative(usage.output_tokens);
  const inputEstimated = usageFieldIsEstimated(usage, 'input_tokens');
  const outputEstimated = usageFieldIsEstimated(usage, 'output_tokens');
  const cacheRead = finiteOrNull(usage.cache_read_tokens);
  const cacheWrite = finiteOrNull(usage.cache_write_tokens);
  const reasoning = nonNegativeOrNull(usage.reasoning_tokens);

  const lines = [
    t('chat.tokenTooltipLastTurn'),
    t('chat.tokenTooltipInput', {
      tokens: `${inputEstimated ? '~' : ''}${format(input)}`,
    }),
  ];
  if (cacheRead !== null) {
    lines.push(cacheReadShareLine(cacheRead, input, format));
  }
  if (cacheWrite !== null) {
    lines.push(
      t('chat.tokenTooltipCacheWrite', {
        tokens: format(cacheWrite),
      }),
    );
  }
  if (cacheRead !== null || cacheWrite !== null) {
    const uncached = Math.max(0, input - (cacheRead ?? 0) - (cacheWrite ?? 0));
    lines.push(
      t('chat.tokenTooltipUncached', {
        tokens: format(uncached),
      }),
    );
  }
  lines.push(
    t('chat.tokenTooltipOutput', {
      tokens: `${outputEstimated ? '~' : ''}${format(output)}`,
    }),
  );
  if (reasoning !== null) {
    lines.push(t('chat.tokenTooltipReasoning', { tokens: format(reasoning) }));
  }
  if (inputEstimated && outputEstimated) {
    lines.push(t('chat.tokenTooltipEstimated'));
  } else if (inputEstimated) {
    lines.push(t('chat.tokenTooltipInputEstimated'));
  } else if (outputEstimated) {
    lines.push(t('chat.tokenTooltipOutputEstimated'));
  }
  return lines;
}

function sessionUsageLines(sessionUsage, format) {
  const measuredTurns = nonNegative(sessionUsage?.measured_turns);
  const input = nonNegative(sessionUsage?.input_tokens);
  const output = nonNegative(sessionUsage?.output_tokens);
  if (measuredTurns <= 0 && input <= 0 && output <= 0) {
    return [];
  }
  const cacheRead = nonNegative(sessionUsage.cache_read_tokens);
  // Turns that reported cache fields at all — a session on a provider without
  // cache reporting must not render as a 0% hit rate.
  const cacheTurns = nonNegative(sessionUsage.cache_turns);
  const estimatedTurns = nonNegative(sessionUsage.estimated_turns);
  const reasoningTurns = nonNegative(sessionUsage.reasoning_turns);
  const reasoning = nonNegative(sessionUsage.reasoning_tokens);

  const lines = [
    t('chat.tokenTooltipSession', {
      turns: format(measuredTurns),
    }),
    t('chat.tokenTooltipInput', {
      tokens: format(input),
    }),
  ];
  if (cacheTurns > 0) {
    lines.push(cacheReadShareLine(cacheRead, input, format));
  }
  lines.push(
    t('chat.tokenTooltipOutput', {
      tokens: format(output),
    }),
  );
  if (reasoningTurns > 0) {
    lines.push(
      t('chat.tokenTooltipSessionReasoning', {
        tokens: format(reasoning),
        turns: format(reasoningTurns),
      }),
    );
  }
  if (cacheTurns > 0) {
    lines.push(
      t('chat.tokenTooltipSessionAvgCacheRead', {
        tokens: format(Math.round(cacheRead / cacheTurns)),
      }),
    );
  }
  if (estimatedTurns > 0) {
    lines.push(
      t('chat.tokenTooltipSessionEstimatedTurns', {
        count: format(estimatedTurns),
      }),
    );
  }
  return lines;
}

function usageFieldIsEstimated(usage, tokenField) {
  return usage[`${tokenField}_estimated`] === true;
}

function cacheReadShareLine(cacheRead, input, format) {
  if (input > 0) {
    return t('chat.tokenTooltipCacheReadPct', {
      tokens: format(cacheRead),
      percent: Math.round((cacheRead / input) * 100),
    });
  }
  return t('chat.tokenTooltipCacheRead', {
    tokens: format(cacheRead),
  });
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
