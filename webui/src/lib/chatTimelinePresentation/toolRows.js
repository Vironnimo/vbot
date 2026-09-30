import {
  toolCallFromEvent,
  toolNameForEvent,
  hasToolResultError,
  hasToolResultPartial,
} from './messages.js';
import {
  toolDisplayFromEvent,
  toolStatus,
  isToolPreparing,
  toolDurationMs,
  toolStartedTimestamp,
  toolArguments,
  streamingPreviewArguments,
  toolNameForRunTool,
  toolDisplay,
  executionStateTitle,
} from './toolFacts.js';
import { t } from '$lib/i18n.js';
import {
  formatDurationMs,
  elapsedSinceTimestamp,
  executionDetailRows,
} from './time.js';
import { trimmedString } from './values.js';
import { isPlainObject } from '$lib/values.js';
import { subAgentToolLabel } from './subagents.js';

// Row summaries for calls without a server display payload. `write`, `edit`,
// `glob` and `grep` are retired Tools that older Sessions still contain.
const TOOL_DISPLAY_ARGS = {
  read: ['path'],
  write: ['path'],
  edit: ['path'],
  bash: ['command'],
  search_files: [
    'pattern',
    'path',
    'glob',
    'args',
    'patterns',
    'paths',
    'action',
  ],
  glob: ['pattern'],
  grep: ['pattern', 'path'],
  subagent: ['action', 'id', 'agent_id', 'content'],
  web_fetch: ['url'],
  web_search: ['query'],
  process: ['action', 'process_id'],
  cron: ['name', 'id', 'target', 'schedule'],
  channel_send: ['channel_id', 'message'],
  skill: ['name'],
};

const TOOL_NO_SUMMARY_NAMES = new Set(['status']);

const DEFAULT_TOOL_PRIMARY_MAX_CHARACTERS = 64;

const TOOL_PATH_SEGMENT_LIMIT = 3;

const SUBAGENT_TOOL_NAMES = new Set(['subagent']);

export const toolRowFromEvent = (event) => {
  const resultEvent = event?.type === 'tool_call_result' ? event : null;
  return {
    name: toolNameForEvent(event),
    toolCall: toolCallFromEvent(event),
    display: toolDisplayFromEvent(event),
    startedEvent: resultEvent ? null : event,
    resultEvent,
    result: resultEvent?.payload?.result,
    timing: resultEvent?.payload?.timing ?? null,
    status: resultEvent
      ? hasToolResultError(event)
        ? 'failed'
        : hasToolResultPartial(event)
          ? 'partial'
          : 'success'
      : 'running',
  };
};

export const toolStatusLabel = (tool, nowMs = Date.now()) => {
  if (toolStatus(tool) === 'cancelled') {
    const duration = formatDurationMs(toolDurationMs(tool));
    return [t('chat.toolCancelled'), duration].filter(Boolean).join(' · ');
  }
  if (toolStatus(tool) === 'running') {
    if (isToolPreparing(tool)) {
      return '';
    }
    return formatDurationMs(
      elapsedSinceTimestamp(toolStartedTimestamp(tool), nowMs),
    );
  }
  const duration = formatDurationMs(toolDurationMs(tool));
  if (toolStatus(tool) === 'partial') {
    return [t('chat.toolPartial'), duration].filter(Boolean).join(' · ');
  }
  return duration;
};

/**
 * Tooltip details behind a Tool row's status dot and time: the state in
 * words, when the call started and finished, and how long it ran.
 */
export const toolStatusDetails = (tool, nowMs = Date.now()) => {
  if (isToolPreparing(tool)) {
    return {
      title: t('chat.toolState.preparing'),
      text: t('chat.toolState.preparingHint'),
    };
  }
  const status = toolStatus(tool);
  const running = status === 'running';
  const startedAt = toolStartedTimestamp(tool);
  return {
    title: executionStateTitle(status),
    text: status === 'partial' ? t('chat.toolState.partialHint') : '',
    rows: executionDetailRows({
      startedAt,
      finishedAt:
        tool?.timing?.completed_at ?? tool?.resultEvent?.timestamp ?? '',
      durationMs: running
        ? elapsedSinceTimestamp(startedAt, nowMs)
        : toolDurationMs(tool),
      running,
      nowMs,
    }),
  };
};

export const toolRowPresentation = (tool) => {
  const display = toolDisplay(tool);
  const structuredPrimary = Array.isArray(display?.primary)
    ? display.primary.map(toolPrimaryPart).filter(Boolean).slice(0, 2)
    : [];
  const displaySummary = trimmedString(display?.summary);
  const legacySummary =
    displaySummary ||
    humanReadableToolLabel(
      toolNameForRunTool(tool),
      toolArguments(tool) ?? streamingPreviewArguments(tool),
    );
  const primary =
    structuredPrimary.length > 0
      ? structuredPrimary
      : legacySummary
        ? [
            toolPrimaryPart({
              kind: 'text',
              value: legacySummary,
              full_value: legacySummary,
              truncate: 'end',
              tooltip: 'truncated',
              max_characters: DEFAULT_TOOL_PRIMARY_MAX_CHARACTERS,
            }),
          ].filter(Boolean)
        : [];
  const facts = Array.isArray(display?.facts)
    ? display.facts.map(toolFactPresentation).filter(Boolean)
    : [];
  return { primary, facts };
};

// Value kinds shown in the mono face inside tooltips and cards; a quoted
// query is prose (a web search), an unquoted one a pattern.
const CODE_LIKE_VALUE_KINDS = new Set(['command', 'identifier', 'path', 'url']);

const COPY_LABELS = {
  command: () => [t('chat.copyCommand'), t('chat.commandCopied')],
  path: () => [t('chat.copyPath'), t('chat.pathCopied')],
  url: () => [t('chat.copyUrl'), t('chat.urlCopied')],
};

function isCodeLike(kind, quoted = false) {
  return CODE_LIKE_VALUE_KINDS.has(kind) || (kind === 'query' && !quoted);
}

/**
 * One primary argument of a Tool row. `reveal` (null when the value is never
 * revealed) describes the complete value shown on hover and focus: `value`,
 * an optional leading `title` (the description a Bash command stands for),
 * `mono` for code-like values, `whenTruncated` when it shows only while the
 * row clips the visible text, and `copy` labels when a card offers Copy.
 */
function toolPrimaryPart(part) {
  if (!isPlainObject(part)) {
    return null;
  }
  const fullText = trimmedString(part.full_value) || trimmedString(part.value);
  if (!fullText) {
    return null;
  }
  const kind = trimmedString(part.kind) || 'text';
  const truncate = ['start', 'end', 'middle', 'never'].includes(part.truncate)
    ? part.truncate
    : 'end';
  const maxCharacters =
    Number.isInteger(part.max_characters) && part.max_characters > 0
      ? part.max_characters
      : DEFAULT_TOOL_PRIMARY_MAX_CHARACTERS;
  const sourceValue =
    kind === 'path' ? compactToolPath(trimmedString(part.value)) : fullText;
  const text = truncateSemanticValue(sourceValue, truncate, maxCharacters);
  return {
    kind,
    text,
    truncate,
    reveal: toolPrimaryReveal(part, kind, text, fullText),
  };
}

function toolPrimaryReveal(part, kind, text, fullText) {
  const tooltipMode = ['always', 'none', 'truncated'].includes(part.tooltip)
    ? part.tooltip
    : 'truncated';
  const detail = trimmedString(part.detail);
  if (tooltipMode === 'none' && !detail) {
    return null;
  }
  const valueKind = detail ? trimmedString(part.detail_kind) || 'text' : kind;
  const copyLabels = COPY_LABELS[valueKind]?.() ?? [
    t('chat.copyToolValue'),
    t('chat.toolValueCopied'),
  ];
  return {
    title: detail ? fullText : '',
    value: detail || fullText,
    mono: isCodeLike(valueKind, !detail && part.quote === true),
    whenTruncated: !detail && tooltipMode === 'truncated' && text === fullText,
    copy:
      part.copyable === true
        ? { label: copyLabels[0], copiedLabel: copyLabels[1] }
        : null,
  };
}

// Singular and plural label of each counted tool-fact unit.
const COUNT_FACT_LABELS = {
  edits: [
    (count) => t('chat.toolFact.edit', { count }),
    (count) => t('chat.toolFact.edits', { count }),
  ],
  failures: [
    (count) => t('chat.toolFact.failure', { count }),
    (count) => t('chat.toolFact.failures', { count }),
  ],
  files: [
    (count) => t('chat.toolFact.file', { count }),
    (count) => t('chat.toolFact.files', { count }),
  ],
  matches: [
    (count) => t('chat.toolFact.match', { count }),
    (count) => t('chat.toolFact.matches', { count }),
  ],
  results: [
    (count) => t('chat.toolFact.result', { count }),
    (count) => t('chat.toolFact.results', { count }),
  ],
};

function toolFactPresentation(fact) {
  if (
    isPlainObject(fact) &&
    fact.kind === 'line_range' &&
    Number.isInteger(fact.start) &&
    fact.start >= 1 &&
    Number.isInteger(fact.end) &&
    fact.end >= fact.start
  ) {
    return {
      kind: 'line_range',
      text: t('chat.toolFact.lines', {
        start: fact.start,
        end: fact.end,
      }),
      variant: 'neutral',
    };
  }
  if (
    isPlainObject(fact) &&
    fact.kind === 'line_change' &&
    Number.isInteger(fact.value) &&
    fact.value >= 0 &&
    ['added', 'removed'].includes(fact.change)
  ) {
    return {
      kind: 'line_change',
      text: `${fact.change === 'added' ? '+' : '-'}${fact.value}`,
      variant: fact.change,
    };
  }
  if (
    !isPlainObject(fact) ||
    fact.kind !== 'count' ||
    !Number.isInteger(fact.value) ||
    fact.value < 0 ||
    !Object.hasOwn(COUNT_FACT_LABELS, fact.unit)
  ) {
    return null;
  }
  const count = `${fact.value}${fact.at_least === true ? '+' : ''}`;
  const singular = fact.value === 1 && fact.at_least !== true;
  const [singularLabel, pluralLabel] = COUNT_FACT_LABELS[fact.unit];
  const label = singular ? singularLabel(count) : pluralLabel(count);
  return { kind: 'count', text: label, variant: 'neutral' };
}

function compactToolPath(value) {
  const normalized = trimmedString(value).replaceAll('\\', '/');
  if (!normalized) {
    return '';
  }
  const segments = normalized.split('/').filter(Boolean);
  if (segments.length <= TOOL_PATH_SEGMENT_LIMIT) {
    return normalized;
  }
  return `…/${segments.slice(-TOOL_PATH_SEGMENT_LIMIT).join('/')}`;
}

function truncateSemanticValue(
  value,
  mode,
  maxCharacters = DEFAULT_TOOL_PRIMARY_MAX_CHARACTERS,
) {
  const text = typeof value === 'string' ? value : '';
  if (
    mode === 'never' ||
    !Number.isInteger(maxCharacters) ||
    maxCharacters < 1 ||
    text.length <= maxCharacters
  ) {
    return text;
  }
  if (maxCharacters === 1) {
    return '…';
  }
  if (mode === 'start') {
    return `…${text.slice(-(maxCharacters - 1))}`;
  }
  if (mode === 'middle') {
    const available = maxCharacters - 1;
    const prefixLength = Math.ceil(available / 2);
    const suffixLength = Math.floor(available / 2);
    return `${text.slice(0, prefixLength)}…${text.slice(-suffixLength)}`;
  }
  return `${text.slice(0, maxCharacters - 1)}…`;
}

function humanReadableToolLabel(toolName, argumentsValue) {
  let args = argumentsValue;
  if (typeof args === 'string') {
    try {
      args = JSON.parse(args);
    } catch {
      return args;
    }
  }

  if (!args || typeof args !== 'object' || Array.isArray(args)) {
    return typeof argumentsValue === 'string' ? argumentsValue.trim() : '';
  }

  if (TOOL_NO_SUMMARY_NAMES.has(toolName) || Object.keys(args).length === 0) {
    return '';
  }

  if (toolName === 'search_files') {
    const fields = ['pattern', 'path', 'glob']
      .map((key) =>
        Array.isArray(args[key])
          ? args[key].filter((value) => trimmedString(value)).join(', ')
          : trimmedString(args[key]),
      )
      .filter(Boolean);
    if (Array.isArray(args.args)) {
      fields.push(
        args.args.filter((value) => typeof value === 'string').join(' '),
      );
    }
    if (fields.some(Boolean)) {
      return fields.filter(Boolean).join(' · ');
    }
    return (
      ['patterns', 'paths']
        .map((key) =>
          Array.isArray(args[key]) ? args[key].filter(Boolean).join(', ') : '',
        )
        .filter(Boolean)
        .join(' · ') ||
      args.action ||
      ''
    );
  }

  if (toolName === 'glob') {
    return searchToolLabel(args, false) ?? '';
  }

  if (toolName === 'grep') {
    return searchToolLabel(args, true) ?? '';
  }

  if (SUBAGENT_TOOL_NAMES.has(toolName)) {
    return subAgentToolLabel(toolName, args) ?? '';
  }

  if (toolName === 'process') {
    const action = trimmedString(args.action);
    if (action) {
      return [action, trimmedString(args.process_id)]
        .filter(Boolean)
        .join(' · ');
    }
    const legacyOperation = isPlainObject(args.request)
      ? trimmedString(args.request.operation)
      : '';
    if (legacyOperation) {
      return legacyOperation;
    }
  }

  if (toolName === 'cron') {
    const action = trimmedString(args.action);
    if (action) {
      const detail = ['name', 'id', 'target', 'schedule']
        .map((key) => trimmedString(args[key]))
        .find(Boolean);
      return [action, detail].filter(Boolean).join(' · ');
    }
  }

  const displayArgs = TOOL_DISPLAY_ARGS[toolName];
  if (displayArgs) {
    for (const key of displayArgs) {
      const value = args[key];
      if (typeof value === 'string' && value.trim() !== '') {
        return value;
      }
    }
  }

  return '';
}

function searchToolLabel(args, includePath) {
  const pattern = trimmedString(args.pattern);
  if (!pattern) {
    return null;
  }

  const path = includePath ? trimmedString(args.path) : '';
  return path ? `${pattern} · ${path}` : pattern;
}
