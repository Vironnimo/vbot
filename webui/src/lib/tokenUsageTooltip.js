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
    rows.push(
      row(
        t('chat.contextCard.providerInput', 'Provider input'),
        format(providerInput),
      ),
    );
  }
  if (providerOutput !== null) {
    rows.push(
      row(
        t('chat.contextCard.providerOutput', 'Provider output'),
        format(providerOutput),
      ),
    );
  }
  if (estimatedDelta !== null) {
    rows.push(
      row(
        t('chat.contextCard.requestChanges', 'Estimated request changes'),
        format(estimatedDelta),
        true,
      ),
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
      t('chat.contextCard.input', 'Input'),
      `${inputEstimated ? '~' : ''}${format(input)}`,
    ),
  ];
  if (cacheRead !== null) {
    rows.push(
      row(
        t('chat.contextCard.cacheRead', 'Read from cache'),
        cacheShareValue(cacheRead, input, format),
        true,
      ),
    );
  }
  if (cacheWrite !== null) {
    rows.push(
      row(
        t('chat.contextCard.cacheWrite', 'Written to cache'),
        format(cacheWrite),
        true,
      ),
    );
  }
  if (cacheRead !== null || cacheWrite !== null) {
    rows.push(
      row(
        t('chat.contextCard.uncached', 'Uncached'),
        format(Math.max(0, input - (cacheRead ?? 0) - (cacheWrite ?? 0))),
        true,
      ),
    );
  }
  rows.push(
    row(
      t('chat.contextCard.output', 'Output'),
      `${outputEstimated ? '~' : ''}${format(output)}`,
    ),
  );
  if (reasoning !== null) {
    rows.push(
      row(
        t('chat.contextCard.reasoning', 'Reasoning'),
        format(reasoning),
        true,
      ),
    );
  }
  const notes = [];
  if (inputEstimated && outputEstimated) {
    notes.push(
      t(
        'chat.tokenTooltipEstimated',
        'Estimated (provider sent no usage data)',
      ),
    );
  } else if (inputEstimated) {
    notes.push(
      t(
        'chat.tokenTooltipInputEstimated',
        'Input estimated (provider omitted input usage)',
      ),
    );
  } else if (outputEstimated) {
    notes.push(
      t(
        'chat.tokenTooltipOutputEstimated',
        'Output estimated (provider omitted output usage)',
      ),
    );
  }
  return {
    id: 'last-turn',
    title: t('chat.tokenTooltipLastTurn', 'Last turn'),
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

  const rows = [row(t('chat.contextCard.input', 'Input'), format(input))];
  if (cacheTurns > 0) {
    rows.push(
      row(
        t('chat.contextCard.cacheRead', 'Read from cache'),
        cacheShareValue(cacheRead, input, format),
        true,
      ),
    );
  }
  rows.push(row(t('chat.contextCard.output', 'Output'), format(output)));
  if (reasoningTurns > 0) {
    rows.push(
      row(
        t('chat.contextCard.reasoningTurns', 'Reasoning ({turns} turns)', {
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
        t('chat.contextCard.avgCacheRead', 'Avg cache read per turn'),
        format(Math.round(cacheRead / cacheTurns)),
      ),
    );
  }
  const notes = [];
  if (estimatedTurns > 0) {
    notes.push(
      t(
        'chat.tokenTooltipSessionEstimatedTurns',
        'Turns with estimated token fields: {count}; those fields are excluded',
        { count: format(estimatedTurns) },
      ),
    );
  }
  return {
    id: 'session',
    title: t('chat.contextCard.session', 'Session'),
    meta: t('chat.contextCard.measuredTurns', '{turns} measured turns', {
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
      t('chat.tokenTooltipContextSummary', '{tokens} / {context}', {
        tokens: tokenText,
        context: format(contextWindow),
      }),
    );
  } else {
    lines.push(
      t('chat.tokenTooltipContextSummaryNoWindow', '{tokens}', {
        tokens: tokenText,
      }),
    );
  }
  const providerInput = finiteOrNull(contextUsage.provider_input_tokens);
  const providerOutput = finiteOrNull(contextUsage.provider_output_tokens);
  if (providerInput !== null && providerOutput !== null) {
    lines.push(
      t('chat.tokenTooltipContextInOut', '(in {input}, out {output})', {
        input: format(providerInput),
        output: format(providerOutput),
      }),
    );
  } else if (providerInput !== null) {
    lines.push(
      t('chat.tokenTooltipContextInOnly', '(in {input})', {
        input: format(providerInput),
      }),
    );
  } else if (providerOutput !== null) {
    lines.push(
      t('chat.tokenTooltipContextOutOnly', '(out {output})', {
        output: format(providerOutput),
      }),
    );
  }
  const estimatedDelta = finiteOrNull(contextUsage.estimated_delta_tokens);
  if (estimatedDelta !== null) {
    lines.push(
      t(
        'chat.tokenTooltipContextDelta',
        '  · estimated request changes: {tokens}',
        { tokens: format(estimatedDelta) },
      ),
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
    t('chat.tokenTooltipLastTurn', 'Last turn'),
    t('chat.tokenTooltipInput', 'Input: {tokens} tok', {
      tokens: `${inputEstimated ? '~' : ''}${format(input)}`,
    }),
  ];
  if (cacheRead !== null) {
    lines.push(cacheReadShareLine(cacheRead, input, format));
  }
  if (cacheWrite !== null) {
    lines.push(
      t('chat.tokenTooltipCacheWrite', '  · newly written to cache: {tokens}', {
        tokens: format(cacheWrite),
      }),
    );
  }
  if (cacheRead !== null || cacheWrite !== null) {
    const uncached = Math.max(0, input - (cacheRead ?? 0) - (cacheWrite ?? 0));
    lines.push(
      t('chat.tokenTooltipUncached', '  · uncached: {tokens}', {
        tokens: format(uncached),
      }),
    );
  }
  lines.push(
    t('chat.tokenTooltipOutput', 'Output: {tokens} tok', {
      tokens: `${outputEstimated ? '~' : ''}${format(output)}`,
    }),
  );
  if (reasoning !== null) {
    lines.push(
      t(
        'chat.tokenTooltipReasoning',
        '  · reasoning (included in output): {tokens}',
        { tokens: format(reasoning) },
      ),
    );
  }
  if (inputEstimated && outputEstimated) {
    lines.push(
      t(
        'chat.tokenTooltipEstimated',
        'Estimated (provider sent no usage data)',
      ),
    );
  } else if (inputEstimated) {
    lines.push(
      t(
        'chat.tokenTooltipInputEstimated',
        'Input estimated (provider omitted input usage)',
      ),
    );
  } else if (outputEstimated) {
    lines.push(
      t(
        'chat.tokenTooltipOutputEstimated',
        'Output estimated (provider omitted output usage)',
      ),
    );
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
    t('chat.tokenTooltipSession', 'Session ({turns} fully measured turns)', {
      turns: format(measuredTurns),
    }),
    t('chat.tokenTooltipInput', 'Input: {tokens} tok', {
      tokens: format(input),
    }),
  ];
  if (cacheTurns > 0) {
    lines.push(cacheReadShareLine(cacheRead, input, format));
  }
  lines.push(
    t('chat.tokenTooltipOutput', 'Output: {tokens} tok', {
      tokens: format(output),
    }),
  );
  if (reasoningTurns > 0) {
    lines.push(
      t(
        'chat.tokenTooltipSessionReasoning',
        '  · reasoning: {tokens} tok ({turns} reporting turns; included in output)',
        {
          tokens: format(reasoning),
          turns: format(reasoningTurns),
        },
      ),
    );
  }
  if (cacheTurns > 0) {
    lines.push(
      t(
        'chat.tokenTooltipSessionAvgCacheRead',
        'Avg cache read per turn: {tokens} tok',
        { tokens: format(Math.round(cacheRead / cacheTurns)) },
      ),
    );
  }
  if (estimatedTurns > 0) {
    lines.push(
      t(
        'chat.tokenTooltipSessionEstimatedTurns',
        'Turns with estimated token fields: {count}; those fields are excluded',
        { count: format(estimatedTurns) },
      ),
    );
  }
  return lines;
}

function usageFieldIsEstimated(usage, tokenField) {
  const estimationField = `${tokenField}_estimated`;
  if (Object.hasOwn(usage, estimationField)) {
    return usage[estimationField] === true;
  }
  if (
    Object.hasOwn(usage, 'input_tokens_estimated') ||
    Object.hasOwn(usage, 'output_tokens_estimated')
  ) {
    return false;
  }
  return usage.estimated === true;
}

function cacheReadShareLine(cacheRead, input, format) {
  if (input > 0) {
    return t(
      'chat.tokenTooltipCacheReadPct',
      '  · read from cache: {tokens} ({percent}%)',
      {
        tokens: format(cacheRead),
        percent: Math.round((cacheRead / input) * 100),
      },
    );
  }
  return t('chat.tokenTooltipCacheRead', '  · read from cache: {tokens}', {
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
