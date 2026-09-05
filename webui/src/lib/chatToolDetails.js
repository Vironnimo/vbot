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

// Media URLs come only from stored attachment identities, never Tool paths or URLs.
export function toolDetailImages(
  value,
  { preferPayload = false, tool = null } = {},
) {
  const result = parseJsonValue(value);
  const candidates = preferPayload
    ? Array.isArray(result?.artifacts)
      ? result.artifacts.filter((item) => item?.kind === 'read_media')
      : []
    : (toolDisplay(tool)?.images ?? []);
  if (!Array.isArray(candidates)) return [];
  const seen = new Set();
  return candidates.flatMap((item) => {
    const id = item?.attachment_id;
    if (
      typeof id !== 'string' ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
        id,
      ) ||
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
            : t('chat.attachment.preview', 'Preview attachment'),
      },
    ];
  });
}

export const toolNameHasHiddenArguments = (toolName) =>
  Boolean(TOOL_ARGUMENT_HIDDEN_KEYS[toolName]);

export const compactToolValue = (
  value,
  { preferPayload = false, toolName = '', tool = null } = {},
) => toolDetailPresentation(value, { preferPayload, toolName, tool }).copyText;

export const toolDetailPresentation = (
  value,
  { preferPayload = false, toolName = '', tool = null } = {},
) => {
  const processed = preferPayload
    ? preferredToolResultValue(value, toolName, tool)
    : sanitizeToolDetailNode(
        value,
        tool
          ? hiddenArgumentKeysForTool(tool, toolName)
          : hiddenArgumentKeysForTool(toolName),
        true,
      );

  if (!hasMeaningfulToolDetail(processed)) {
    const emptyText = t('chat.toolNoData', '—');
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
  return tool.name || tool.toolCall?.name || t('chat.toolPendingName', 'tool');
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
