import { t } from '$lib/i18n.js';

const TOOL_DETAIL_HIDDEN_KEYS = ['artifacts', 'description'];
const TOOL_ARGUMENT_HIDDEN_KEYS = {
  edit: ['newString', 'new_string', 'oldString', 'old_string'],
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

export const toolNameHasHiddenArguments = (toolName) =>
  Boolean(TOOL_ARGUMENT_HIDDEN_KEYS[toolName]);

export const compactToolValue = (
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
      );

  if (!hasMeaningfulToolDetail(processed)) {
    return t('chat.toolNoData', '—');
  }

  if (typeof processed === 'string') {
    return processed;
  }

  if (typeof processed === 'number' || typeof processed === 'boolean') {
    return String(processed);
  }

  if (isPlainObject(processed)) {
    return formatPlainObjectInner(processed);
  }

  try {
    return JSON.stringify(processed);
  } catch {
    return String(processed);
  }
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

function sanitizeToolDetailNode(value, additionalHiddenKeys = null) {
  const parsedValue = parseJsonValue(value);

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
  const sanitizedValue = sanitizeToolDetailNode(value);

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

function formatPlainObjectInner(value) {
  return Object.entries(value)
    .filter(([, entryValue]) => hasMeaningfulToolDetail(entryValue))
    .map(([key, entryValue]) => `${key}: ${formatToolFieldValue(entryValue)}`)
    .join('\n');
}

function formatToolFieldValue(value) {
  if (typeof value === 'string') {
    return value;
  }

  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }

  if (value === null) {
    return 'null';
  }

  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
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

function isPlainObject(value) {
  return Object.prototype.toString.call(value) === '[object Object]';
}
