// Composes the chat-header token badge hover text: a "Last turn" block that
// splits the input into its cache shares, plus a whole-session block with the
// session cache hit rate. Rendered by the shared quick tooltip (lib/tooltip.js),
// which keeps line breaks, so structure is expressed with line breaks and
// middot-indented sub-lines.
//
// The cache lines matter for spotting prompt-cache breaks: canonical
// `input_tokens` already contains the cached tokens, so the sub-lines are
// shares of the input ("davon"), never additions on top.
import { activeLocaleTag, t } from './i18n.js';

export function formatTokenUsageTooltip(contextUsage, usage, sessionUsage) {
  const numberFormat = new Intl.NumberFormat(activeLocaleTag());
  const format = (value) => numberFormat.format(value);

  const sections = [
    contextUsageLines(contextUsage, format),
    usage ? lastTurnLines(usage, format) : [],
    sessionUsageLines(sessionUsage, format),
  ].filter((section) => section.length > 0);
  return sections.length > 0
    ? sections.map((section) => section.join('\n')).join('\n\n')
    : undefined;
}

function contextUsageLines(contextUsage, format) {
  const tokens = finiteOrNull(contextUsage?.tokens);
  if (tokens === null) {
    return [];
  }
  const tokenText = `${contextUsage.estimated === true ? '~' : ''}${format(tokens)}`;
  const lines = [
    t('chat.tokenTooltipContext', 'Current context: {tokens} tok', {
      tokens: tokenText,
    }),
  ];
  const providerInput = finiteOrNull(contextUsage.provider_input_tokens);
  const providerOutput = finiteOrNull(contextUsage.provider_output_tokens);
  const estimatedDelta = finiteOrNull(contextUsage.estimated_delta_tokens);
  if (providerInput !== null) {
    lines.push(
      t('chat.tokenTooltipContextInput', '  · provider input: {tokens}', {
        tokens: format(providerInput),
      }),
    );
  }
  if (providerOutput !== null) {
    lines.push(
      t('chat.tokenTooltipContextOutput', '  · provider output: {tokens}', {
        tokens: format(providerOutput),
      }),
    );
  }
  if (estimatedDelta !== null) {
    lines.push(
      t(
        'chat.tokenTooltipContextDelta',
        '  · estimated newer messages: {tokens}',
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
