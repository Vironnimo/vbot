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
} from './toolFacts.js';
import { t } from '$lib/i18n.js';
import { formatDurationMs, elapsedSinceTimestamp } from './time.js';
import { trimmedString } from './values.js';
import { isPlainObject } from '$lib/values.js';
import { toolNameHasHiddenArguments } from '$lib/chatToolDetails.js';
import { subAgentToolLabel } from './subagents.js';

const TOOL_DISPLAY_ARGS = {
  read: ['path'],
  write: ['path'],
  edit: ['path'],
  bash: ['command'],
  glob: ['pattern'],
  grep: ['pattern', 'path'],
  subagent: ['action', 'id', 'agent_id', 'content'],
  web_fetch: ['url'],
  web_search: ['query'],
  process: ['action', 'process_id'],
  cron: ['action', 'id', 'agent_id', 'schedule_type'],
  channel_send: ['channel_id', 'message'],
  skill: ['name'],
};

const TOOL_NO_SUMMARY_NAMES = new Set(['status']);

export const DEFAULT_TOOL_PRIMARY_MAX_CHARACTERS = 64;

const TOOL_PATH_SEGMENT_LIMIT = 3;

const SUBAGENT_TOOL_NAMES = new Set(['subagent']);

export const toolArgumentForEvent = (event) => {
  return toolArgumentSummary(toolRowFromEvent(event));
};

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
    const duration = formatDurationMs(
      toolDurationMs(tool),
      'chat.toolDurationSeconds',
    );
    return [t('chat.toolCancelled', 'cancelled'), duration]
      .filter(Boolean)
      .join(' · ');
  }
  if (toolStatus(tool) === 'running') {
    if (isToolPreparing(tool)) {
      return '';
    }
    return formatDurationMs(
      elapsedSinceTimestamp(toolStartedTimestamp(tool), nowMs),
      'chat.toolDurationSeconds',
    );
  }
  const duration = formatDurationMs(
    toolDurationMs(tool),
    'chat.toolDurationSeconds',
  );
  if (toolStatus(tool) === 'partial') {
    return [t('chat.toolPartial', 'partial'), duration]
      .filter(Boolean)
      .join(' · ');
  }
  return duration;
};

export const toolArgumentSummary = (tool) => {
  return toolRowPresentation(tool)
    .primary.map((part) => part.text)
    .filter(Boolean)
    .join(' · ');
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
  const tooltipMode = ['always', 'none', 'truncated'].includes(part.tooltip)
    ? part.tooltip
    : 'truncated';
  const showTooltip =
    tooltipMode === 'always' ||
    (tooltipMode === 'truncated' && text !== fullText);
  return {
    kind,
    text,
    fullText,
    truncate,
    copyable: part.copyable === true,
    tooltipText: showTooltip ? fullText : '',
  };
}

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
      text: t('chat.toolFact.lines', 'lines {start}-{end}', {
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
    !['edits', 'failures', 'files', 'matches', 'results'].includes(fact.unit)
  ) {
    return null;
  }
  const renderedCount = `${fact.value}${fact.at_least === true ? '+' : ''}`;
  const singular = fact.value === 1 && fact.at_least !== true;
  const unitKeys = {
    edits: ['chat.toolFact.edit', 'chat.toolFact.edits', 'edit', 'edits'],
    failures: [
      'chat.toolFact.failure',
      'chat.toolFact.failures',
      'failed',
      'failed',
    ],
    files: ['chat.toolFact.file', 'chat.toolFact.files', 'file', 'files'],
    matches: [
      'chat.toolFact.match',
      'chat.toolFact.matches',
      'match',
      'matches',
    ],
    results: [
      'chat.toolFact.result',
      'chat.toolFact.results',
      'result',
      'results',
    ],
  };
  const [singularKey, pluralKey, singularWord, pluralWord] =
    unitKeys[fact.unit];
  const label = singular
    ? t(singularKey, `{count} ${singularWord}`, { count: renderedCount })
    : t(pluralKey, `{count} ${pluralWord}`, { count: renderedCount });
  return { kind: 'count', text: label, variant: 'neutral' };
}

export function compactToolPath(value) {
  const normalized = trimmedString(value).replaceAll('\\', '/');
  if (!normalized) {
    return '';
  }
  const absolute =
    normalized.startsWith('/') || /^[A-Za-z]:\//.test(normalized);
  const segments = normalized.split('/').filter(Boolean);
  if (!absolute && segments.length <= TOOL_PATH_SEGMENT_LIMIT) {
    return normalized;
  }
  if (segments.length <= TOOL_PATH_SEGMENT_LIMIT) {
    return normalized;
  }
  return `…/${segments.slice(-TOOL_PATH_SEGMENT_LIMIT).join('/')}`;
}

export function truncateSemanticValue(
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

  const displayArgs = TOOL_DISPLAY_ARGS[toolName];
  if (displayArgs) {
    for (const key of displayArgs) {
      const value = args[key];
      if (typeof value === 'string' && value.trim() !== '') {
        return value;
      }
    }
  }

  if (toolNameHasHiddenArguments(toolName)) {
    return '';
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
