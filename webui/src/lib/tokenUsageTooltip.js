// Composes the chat-header token badge hover text: a "Last turn" block that
// splits the input into its cache shares, plus a whole-session block with the
// session cache hit rate. Native `title` tooltips render plain text only, so
// structure is expressed with line breaks and middot-indented sub-lines.
//
// The cache lines matter for spotting prompt-cache breaks: canonical
// `input_tokens` already contains the cached tokens, so the sub-lines are
// shares of the input ("davon"), never additions on top.
import { activeLocaleTag, t } from './i18n.js';

export function formatTokenUsageTooltip(usage, sessionUsage) {
  const numberFormat = new Intl.NumberFormat(activeLocaleTag());
  const format = (value) => numberFormat.format(value);

  const lines = usage ? lastTurnLines(usage, format) : [];
  const sessionLines = sessionUsageLines(sessionUsage, format);
  if (sessionLines.length > 0) {
    if (lines.length > 0) {
      lines.push('');
    }
    lines.push(...sessionLines);
  }
  return lines.length > 0 ? lines.join('\n') : undefined;
}

function lastTurnLines(usage, format) {
  const input = nonNegative(usage.input_tokens);
  const output = nonNegative(usage.output_tokens);
  const cacheRead = finiteOrNull(usage.cache_read_tokens);
  const cacheWrite = finiteOrNull(usage.cache_write_tokens);

  const lines = [
    t('chat.tokenTooltipLastTurn', 'Last turn'),
    t('chat.tokenTooltipInput', 'Input: {tokens} tok', {
      tokens: format(input),
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
      tokens: format(output),
    }),
  );
  if (usage.estimated === true) {
    lines.push(
      t(
        'chat.tokenTooltipEstimated',
        'Estimated (provider sent no usage data)',
      ),
    );
  }
  return lines;
}

function sessionUsageLines(sessionUsage, format) {
  const measuredTurns = nonNegative(sessionUsage?.measured_turns);
  if (measuredTurns <= 0) {
    return [];
  }
  const input = nonNegative(sessionUsage.input_tokens);
  const output = nonNegative(sessionUsage.output_tokens);
  const cacheRead = nonNegative(sessionUsage.cache_read_tokens);
  // Turns that reported cache fields at all — a session on a provider without
  // cache reporting must not render as a 0% hit rate.
  const cacheTurns = nonNegative(sessionUsage.cache_turns);
  const estimatedTurns = nonNegative(sessionUsage.estimated_turns);

  const lines = [
    t('chat.tokenTooltipSession', 'Session ({turns} measured turns)', {
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
        '{count} estimated turns excluded',
        { count: format(estimatedTurns) },
      ),
    );
  }
  return lines;
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

function nonNegative(value) {
  return Number.isFinite(value) && value > 0 ? value : 0;
}
