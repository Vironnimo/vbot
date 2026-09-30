import { getAttachmentUrl } from '$lib/api.js';
import { t } from '$lib/i18n.js';
import { isPlainObject } from '$lib/values.js';

const TOOL_DETAIL_HIDDEN_KEYS = ['artifacts', 'description'];
const TOOL_ARGUMENT_HIDDEN_KEYS = {
  edit: ['edits', 'new_string', 'old_string'],
  write: ['content'],
};
const TOOL_ERROR_DETAIL_KEYS = [
  'error',
  'message',
  'code',
  'details',
  'status',
  'type',
];

// Use server-issued file URLs or stored attachment identities, never raw Tool paths.
export function toolDetailImages(
  value,
  { preferPayload = false, tool = null } = {},
) {
  if (!preferPayload) {
    const images = toolDisplay(tool)?.images;
    if (!Array.isArray(images)) return [];
    const seen = new Set();
    return images.flatMap((image) => {
      if (
        typeof image?.url !== 'string' ||
        !/^\/api\/files\/[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(image.url) ||
        seen.has(image.url)
      )
        return [];
      seen.add(image.url);
      return [
        {
          src: image.url,
          filename:
            typeof image.filename === 'string' && image.filename
              ? image.filename
              : t('chat.attachment.preview'),
        },
      ];
    });
  }
  const result = parseJsonValue(value);
  const candidates = Array.isArray(result?.artifacts)
    ? result.artifacts.filter((item) => item?.kind === 'read_media')
    : [];
  if (!Array.isArray(candidates)) return [];
  const seen = new Set();
  return candidates.flatMap((item) => {
    const id = item?.attachment_id;
    if (
      typeof id !== 'string' ||
      !/^[a-z0-9][a-z0-9_-]{0,127}$/.test(id) ||
      typeof item?.media_type !== 'string' ||
      !item.media_type.startsWith('image/') ||
      seen.has(id)
    )
      return [];
    seen.add(id);
    return [
      {
        src: getAttachmentUrl(id),
        filename:
          typeof item.filename === 'string' && item.filename
            ? item.filename
            : t('chat.attachment.preview'),
      },
    ];
  });
}

// `raw` shows arguments with the keys the Tool's display hides, as sent;
// `literal` shows a text as it is, even one that reads as JSON.
export const toolDetailPresentation = (
  value,
  {
    preferPayload = false,
    raw = false,
    literal = false,
    toolName = '',
    tool = null,
  } = {},
) => {
  const processed = literal
    ? value
    : preferPayload
      ? preferredToolResultValue(value, toolName, tool)
      : sanitizeToolDetailNode(
          value,
          raw
            ? null
            : tool
              ? hiddenArgumentKeysForTool(tool, toolName)
              : hiddenArgumentKeysForTool(toolName),
          true,
        );

  if (!hasMeaningfulToolDetail(processed)) {
    const emptyText = t('chat.toolNoData');
    return { copyText: emptyText, fields: [], kind: 'empty', text: emptyText };
  }

  if (isPlainObject(processed)) {
    const fields = Object.entries(processed).map(([key, entryValue]) => ({
      key,
      kind: toolDetailValueKind(entryValue),
      text: formatReadableToolValue(entryValue),
    }));
    const copyText = fields
      .map(({ key, text }) => `${key}: ${indentContinuationLines(text)}`)
      .join('\n');
    return { copyText, fields, kind: 'fields', text: copyText };
  }

  const text = formatReadableToolValue(processed);
  return {
    copyText: text,
    fields: [],
    kind: toolDetailValueKind(processed),
    text,
  };
};

function hiddenArgumentKeysForTool(toolOrName, fallbackName = '') {
  const toolName =
    typeof toolOrName === 'string'
      ? toolOrName
      : fallbackName || toolNameForRunTool(toolOrName);
  const keys = [...(TOOL_ARGUMENT_HIDDEN_KEYS[toolName] ?? [])];

  if (typeof toolOrName !== 'string') {
    for (const key of toolDisplay(toolOrName)?.hidden_argument_keys ?? []) {
      if (typeof key === 'string' && key && !keys.includes(key)) {
        keys.push(key);
      }
    }
  }

  return keys.length > 0 ? keys : null;
}

function toolDisplay(tool) {
  const display =
    tool?.display ??
    tool?.toolCall?.display ??
    tool?.startedEvent?.payload?.display;
  return isPlainObject(display) ? display : null;
}

function toolNameForRunTool(tool) {
  return tool.name || tool.toolCall?.name || t('chat.toolPendingName');
}

function sanitizeToolDetailNode(
  value,
  additionalHiddenKeys = null,
  parseSerializedValue = false,
) {
  const parsedValue = parseSerializedValue ? parseJsonValue(value) : value;

  if (Array.isArray(parsedValue)) {
    return parsedValue
      .map((entry) => sanitizeToolDetailNode(entry, additionalHiddenKeys))
      .filter((entry) => entry !== undefined);
  }

  if (!isPlainObject(parsedValue)) {
    return parsedValue;
  }

  return Object.fromEntries(
    Object.entries(parsedValue).flatMap(([key, entryValue]) => {
      if (
        TOOL_DETAIL_HIDDEN_KEYS.includes(key) ||
        additionalHiddenKeys?.includes(key) ||
        entryValue === undefined
      ) {
        return [];
      }
      return [[key, sanitizeToolDetailNode(entryValue, additionalHiddenKeys)]];
    }),
  );
}

function preferredToolResultValue(value, toolName = '', tool = null) {
  const sanitizedValue = sanitizeToolDetailNode(value, null, true);

  if (!isPlainObject(sanitizedValue)) {
    return sanitizedValue;
  }

  const errorValue = preferredToolErrorValue(sanitizedValue);
  if (errorValue !== null) {
    return errorValue;
  }

  if (
    isSuccessfulToolResult(sanitizedValue) &&
    isPlainObject(sanitizedValue.data)
  ) {
    if (toolName === 'bash') {
      return preferredBashResultValue(sanitizedValue.data, tool);
    }

    if (
      ['read', 'glob', 'grep'].includes(toolName) &&
      hasMeaningfulToolDetail(sanitizedValue.data.content)
    ) {
      return sanitizeToolDetailNode(sanitizedValue.data.content);
    }

    if (hasOnlyContentField(sanitizedValue.data)) {
      return sanitizeToolDetailNode(sanitizedValue.data.content);
    }
  }

  if (hasMeaningfulToolDetail(sanitizedValue.data)) {
    return sanitizeToolDetailNode(sanitizedValue.data);
  }

  if (hasMeaningfulToolDetail(sanitizedValue.result)) {
    return sanitizeToolDetailNode(sanitizedValue.result);
  }

  return sanitizedValue;
}

function preferredToolErrorValue(value) {
  if (!isPlainObject(value)) {
    return null;
  }

  if (hasMeaningfulToolDetail(value.error)) {
    const errorValue = sanitizeToolDetailNode(value.error);
    if (isPlainObject(errorValue)) {
      return errorValue;
    }

    const errorDetails = TOOL_ERROR_DETAIL_KEYS.reduce((details, key) => {
      const detailValue =
        key === 'error' ? errorValue : sanitizeToolDetailNode(value[key]);
      if (hasMeaningfulToolDetail(detailValue)) {
        details[key] = detailValue;
      }
      return details;
    }, {});

    return Object.keys(errorDetails).length > 1 ? errorDetails : errorValue;
  }

  if (
    value.ok === false ||
    value.success === false ||
    ['error', 'failed'].includes(value.status)
  ) {
    const errorDetails = TOOL_ERROR_DETAIL_KEYS.reduce((details, key) => {
      const detailValue = sanitizeToolDetailNode(value[key]);
      if (hasMeaningfulToolDetail(detailValue)) {
        details[key] = detailValue;
      }
      return details;
    }, {});

    return Object.keys(errorDetails).length > 0 ? errorDetails : value;
  }

  return null;
}

function preferredBashResultValue(data, tool) {
  const hasStreamedOutput = Boolean(tool?.stdout || tool?.stderr);
  if (!hasStreamedOutput && hasMeaningfulToolDetail(data.output)) {
    return sanitizeToolDetailNode(data.output);
  }

  const { output, ...summary } = data;
  if (hasMeaningfulToolDetail(summary)) {
    return sanitizeToolDetailNode(summary);
  }

  return sanitizeToolDetailNode(output);
}

function hasMeaningfulToolDetail(value) {
  if (value === undefined || value === null || value === '') {
    return false;
  }
  if (Array.isArray(value)) {
    return value.length > 0;
  }
  if (isPlainObject(value)) {
    return Object.keys(value).length > 0;
  }
  return true;
}

function isSuccessfulToolResult(value) {
  return (
    isPlainObject(value) &&
    !preferredToolErrorValue(value) &&
    (value.ok === true ||
      value.success === true ||
      ['success', 'completed'].includes(value.status) ||
      (value.ok !== false && value.success !== false && !value.status))
  );
}

function hasOnlyContentField(value) {
  return (
    isPlainObject(value) &&
    Object.keys(value).length === 1 &&
    hasMeaningfulToolDetail(value.content)
  );
}

function formatReadableToolValue(value) {
  if (typeof value === 'string') {
    return value;
  }

  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }

  if (value === null) {
    return 'null';
  }

  if (Array.isArray(value)) {
    if (value.length === 0) {
      return '[]';
    }
    return value
      .map((entry) => {
        const formatted = formatReadableToolValue(entry);
        return `- ${indentContinuationLines(formatted)}`;
      })
      .join('\n');
  }

  if (isPlainObject(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      return '{}';
    }
    return entries
      .map(([key, entryValue]) => {
        const formatted = formatReadableToolValue(entryValue);
        return `${key}: ${indentContinuationLines(formatted)}`;
      })
      .join('\n');
  }

  return String(value);
}

function indentContinuationLines(value) {
  return String(value).replaceAll('\n', '\n  ');
}

function toolDetailValueKind(value) {
  if (value === null) {
    return 'null';
  }
  if (Array.isArray(value)) {
    return 'array';
  }
  if (isPlainObject(value)) {
    return 'object';
  }
  return typeof value;
}

function parseJsonValue(value) {
  if (typeof value !== 'string') {
    return value;
  }

  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

const FILE_CHANGE_KINDS = new Set([
  'created',
  'updated',
  'replaced',
  'deleted',
  'moved',
]);
const DIFF_LINE_KINDS = { '+': 'added', '-': 'removed', ' ': 'context' };

const NOTICE_LEVELS = new Set(['info', 'warning', 'error']);
const MEMORY_SCOPES = new Set(['agent', 'user']);
const MEMORY_CHANGE_OPS = new Set(['added', 'removed', 'replaced']);
const TEXT_LABELS = new Set(['command', 'output', 'query', 'results']);

// The user-facing detail blocks of a Tool whose display declares them
// (`display.details`), in the Tool's order, or null for a Tool without them.
// `file_changes` blocks carry the changed files' diffs with rows numbered
// from each hunk's start; `notice` blocks a leveled message; `text` blocks a
// labelled text, either given or read from the call's `args` or `result` at
// the block's source path; `results` blocks the items a call found, each with
// a title and optional meta, ISO time and text; `memory_changes` blocks the
// entries a call added, removed or replaced (with its previous text) in one
// Memory scope and the history revision that recorded them. Malformed blocks
// and entries, texts without a value and empty lists are dropped.
export function toolDetailBlocks(tool, { args, result } = {}) {
  const blocks = toolDisplay(tool)?.details;
  if (!Array.isArray(blocks)) return null;
  return blocks.flatMap((block) => {
    if (block?.type === 'text' && TEXT_LABELS.has(block.label)) {
      const text =
        typeof block.text === 'string'
          ? block.text
          : sourceText(block.source, { args, result });
      return text.trim() ? [{ type: 'text', label: block.label, text }] : [];
    }
    if (block?.type === 'results' && Array.isArray(block.items)) {
      const items = block.items.flatMap((item) =>
        isPlainObject(item) && typeof item.title === 'string' && item.title
          ? [
              {
                title: item.title,
                meta: typeof item.meta === 'string' ? item.meta : '',
                time: typeof item.time === 'string' ? item.time : '',
                text: typeof item.text === 'string' ? item.text : '',
              },
            ]
          : [],
      );
      return items.length > 0 ? [{ type: 'results', items }] : [];
    }
    if (
      block?.type === 'memory_changes' &&
      MEMORY_SCOPES.has(block.scope) &&
      Array.isArray(block.changes)
    ) {
      const changes = block.changes.flatMap((change) =>
        isPlainObject(change) &&
        MEMORY_CHANGE_OPS.has(change.op) &&
        typeof change.text === 'string' &&
        change.text &&
        (change.op !== 'replaced' ||
          (typeof change.previous === 'string' && change.previous))
          ? [
              {
                op: change.op,
                text: change.text,
                previous: change.op === 'replaced' ? change.previous : '',
              },
            ]
          : [],
      );
      return changes.length > 0
        ? [
            {
              type: 'memory_changes',
              scope: block.scope,
              revision: Number.isInteger(block.revision)
                ? block.revision
                : null,
              changes,
            },
          ]
        : [];
    }
    if (block?.type === 'file_changes' && Array.isArray(block.files)) {
      const files = fileChanges(block.files);
      return files.length > 0 ? [{ type: 'file_changes', files }] : [];
    }
    if (
      block?.type === 'notice' &&
      NOTICE_LEVELS.has(block.level) &&
      typeof block.text === 'string' &&
      block.text
    ) {
      return [
        {
          type: 'notice',
          level: block.level,
          text: block.text,
          subject: typeof block.subject === 'string' ? block.subject : '',
        },
      ];
    }
    return [];
  });
}

function sourceText(source, { args, result }) {
  if (!isPlainObject(source) || !Array.isArray(source.path)) return '';
  let value =
    source.from === 'arguments'
      ? parseJsonValue(args)
      : source.from === 'result'
        ? parseJsonValue(result)
        : undefined;
  for (const key of source.path) {
    if (!isPlainObject(value)) return '';
    value = value[key];
  }
  return typeof value === 'string' ? value : '';
}

function fileChanges(changes) {
  return changes.flatMap((change) => {
    if (
      !isPlainObject(change) ||
      typeof change.path !== 'string' ||
      !change.path ||
      !FILE_CHANGE_KINDS.has(change.change)
    )
      return [];
    const hunks = Array.isArray(change.hunks)
      ? change.hunks.filter(
          (hunk) =>
            isPlainObject(hunk) &&
            isLineNumber(hunk.old_start) &&
            isLineNumber(hunk.new_start) &&
            Array.isArray(hunk.lines) &&
            hunk.lines.every(
              (line) => typeof line === 'string' && line[0] in DIFF_LINE_KINDS,
            ),
        )
      : [];
    return [
      {
        path: change.path,
        change: change.change,
        destination:
          typeof change.destination === 'string' && change.destination
            ? change.destination
            : '',
        added: countValue(change.added),
        removed: countValue(change.removed),
        binary: change.binary === true,
        omittedLines: countValue(change.omitted_lines),
        rows: diffRows(hunks),
      },
    ];
  });
}

// One row per diff line; a `gap` row separates hunks. Removed lines carry
// their old line number, added and context lines their new one.
function diffRows(hunks) {
  const rows = [];
  hunks.forEach((hunk, hunkIndex) => {
    if (hunkIndex > 0) rows.push({ kind: 'gap', number: null, text: '' });
    let oldNumber = hunk.old_start;
    let newNumber = hunk.new_start;
    for (const line of hunk.lines) {
      const kind = DIFF_LINE_KINDS[line[0]];
      const text = line.slice(1);
      if (kind === 'removed') {
        rows.push({ kind, number: oldNumber, text });
        oldNumber += 1;
      } else {
        rows.push({ kind, number: newNumber, text });
        newNumber += 1;
        if (kind === 'context') oldNumber += 1;
      }
    }
  });
  return rows;
}

// The unified-diff text of the shown lines, for the Copy action.
export function fileChangesCopyText(changes) {
  return changes
    .map((change) => {
      const target = change.destination
        ? `${change.path} -> ${change.destination}`
        : change.path;
      const lines = change.rows.map((row) =>
        row.kind === 'gap'
          ? '...'
          : `${row.kind === 'added' ? '+' : row.kind === 'removed' ? '-' : ' '}${row.text}`,
      );
      return [target, ...lines].join('\n');
    })
    .join('\n\n');
}

function isLineNumber(value) {
  return Number.isInteger(value) && value >= 0;
}

function countValue(value) {
  return Number.isInteger(value) && value > 0 ? value : 0;
}
